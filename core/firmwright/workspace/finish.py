"""会话收尾（D06，§5.3）：合并到分支 / 导出补丁 / 丢弃。参照 Codex 桌面版，由用户在界面上点选。

合并的做法（不碰用户工作目录里和本次合并无关的东西）：
1. 改动 = checkpoint 序号 0（会话开始）到现在的差异。从主工程拷进 worktree 的未跟踪文件
   （sdkconfig 等）不在用户的提交里，它们的改动不进合并，列在 skipped 里（导出的补丁里有）。
2. 在临时 index 上把改动套到会话的起点提交（base_commit）上，生成一个"会话提交"，作者是用户自己的 git 身份。
3. 把会话提交合并进目标分支：
   - 目标分支正在某个工作目录里检出（通常就是用户的主工程）：在那里 `git merge`，能快进就快进。
     有冲突就 `git merge --abort` 退回原样，把冲突的文件告诉用户。
   - 目标分支没有检出：用 `git merge-tree --write-tree` 在内存里合并，成功再更新分支引用。

另一种收尾"应用到工程文件夹"（apply_to_folder，2026-10-05）：不提交，把改动作为未提交改动套回用户的文件夹。
会话带着未提交改动开始时（worktree.carry_dirty）主要用这个。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import git
from .checkpoint import Checkpointer


class MergeError(Exception):
    def __init__(self, message: str, *, conflicts: list[str] | None = None, kind: str = "error") -> None:
        super().__init__(message)
        self.conflicts = conflicts or []
        self.kind = kind


async def _checked_out_at(repo: Path, branch: str) -> Path | None:
    out = await git.out(repo, "worktree", "list", "--porcelain")
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and path is not None:
            return path
    return None


async def _user_identity_ok(repo: Path) -> bool:
    name = await git.run(repo, "config", "user.name")
    mail = await git.run(repo, "config", "user.email")
    return bool(name.ok and name.stdout.strip() and mail.ok and mail.stdout.strip())


async def session_commit(ck: Checkpointer, *, base_commit: str, message: str) -> tuple[str | None, list[str], list[str]]:
    """生成会话提交。返回 (提交, 合并的文件, 跳过的文件)；没有可合并的改动时提交为 None。"""
    diff = await ck.diff_base(patch=False)
    if not diff["files"]:
        return None, [], []
    start = ck.entries[0].commit
    # 起点提交里没有、但 checkpoint 0 里有的文件 = 从主工程拷过来的未跟踪文件
    seeded = set((await git.out(ck.wt, "diff", "--name-only", "--no-renames", "--diff-filter=A",
                                base_commit, start)).splitlines())
    skipped = [f for f in diff["files"] if f in seeded]
    files = [f for f in diff["files"] if f not in seeded]
    if not files:
        return None, [], skipped
    patch = await ck.patch_bytes(start, diff["head"], exclude=sorted(seeded))
    env = {"GIT_INDEX_FILE": str(ck.dir / "merge.index")}
    try:
        await git.out(ck.wt, "read-tree", base_commit, env=env)
        try:
            await git.raw(ck.wt, "apply", "--cached", "--binary", "--whitespace=nowarn", "-", env=env, stdin=patch)
        except git.GitError as e:
            # 会话开始时带进来的未提交改动不在 base_commit 里；agent 又改了同一个文件，补丁就套不上提交
            raise MergeError("The session started from uncommitted changes in your project, and the agent edited some of "
                             "the same files, so its changes cannot be turned into a commit on top of the last commit. "
                             "Use \"Apply to project folder\" instead.",
                             conflicts=_patch_failures(str(e)), kind="carried") from e
        tree = await git.out(ck.wt, "write-tree", env=env)
    finally:
        (ck.dir / "merge.index").unlink(missing_ok=True)
    commit = await git.out(ck.wt, "commit-tree", tree, "-p", base_commit, "-m", message)
    return commit, files, skipped


async def merge(ck: Checkpointer, *, repo: Path, base_commit: str, target: str, message: str,
                session_path: Path | None = None) -> dict[str, Any]:
    if not await _user_identity_ok(repo):
        raise MergeError("git user.name / user.email are not set, so the merge commit would have no author. Set them in a terminal, then merge.",
                         kind="no_identity")
    if not await git.ok(repo, "show-ref", "--verify", "-q", f"refs/heads/{target}"):
        raise MergeError(f"Branch {target} does not exist", kind="no_branch")
    commit, files, skipped = await session_commit(ck, base_commit=base_commit, message=message)
    if commit is None:
        return {"ok": True, "empty": True, "files": [], "skipped": skipped, "commit": None}
    where = await _checked_out_at(repo, target)
    if where is not None and session_path is not None and where.resolve() == session_path.resolve():
        raise MergeError(f"Branch {target} is checked out in the session's own worktree; cannot merge into it", kind="bad_target")
    if where is not None:
        res = await git.run(where, "merge", "--no-edit", "-m", f"Merge Firmwright session: {message}", commit,
                            timeout=300)
        if not res.ok:
            unmerged = await git.run(where, "diff", "--name-only", "--diff-filter=U")
            conflicts = [f for f in unmerged.stdout.splitlines() if f]
            if conflicts:
                await git.run(where, "merge", "--abort")
                raise MergeError(f"Conflicts with changes on {target}; the merge was aborted and {where} is unchanged", conflicts=conflicts,
                                 kind="conflict")
            text = (res.stderr or res.stdout).strip()
            raise MergeError(f"git merge did not run (uncommitted changes in {where} would be overwritten?): {text[-600:]}",
                             kind="dirty")
        tip = await git.out(where, "rev-parse", "HEAD")
    else:
        old = await git.out(repo, "rev-parse", f"refs/heads/{target}")
        if old == base_commit:
            tip = commit  # 快进
        else:
            res = await git.run(repo, "merge-tree", "--write-tree", "--name-only", old, commit)
            if not res.ok:
                lines = res.stdout.splitlines()
                conflicts = [x for x in lines[1:] if x and not x.startswith(("Auto-merging", "CONFLICT"))]
                raise MergeError(f"Conflicts with changes on {target}", conflicts=conflicts, kind="conflict")
            tree = res.stdout.splitlines()[0].strip()
            tip = await git.out(repo, "commit-tree", tree, "-p", old, "-p", commit, "-m",
                                f"Merge Firmwright session: {message}")
        await git.out(repo, "update-ref", f"refs/heads/{target}", tip, old)
    return {"ok": True, "empty": False, "files": files, "skipped": skipped, "commit": tip, "sessionCommit": commit,
            "target": target, "checkedOutAt": str(where) if where else None}


def _patch_failures(stderr: str) -> list[str]:
    """从 git apply 的报错里取出套不上的文件名。"""
    files: list[str] = []
    for line in stderr.splitlines():
        m = re.match(r"error: patch failed: (.+):\d+$", line) or re.match(r"error: (.+?): (?!.*patch failed)", line)
        if m and m.group(1) != "patch failed" and m.group(1) not in files:
            files.append(m.group(1))
    return files[:30]


async def apply_to_folder(ck: Checkpointer, folder: Path) -> dict[str, Any]:
    """把会话的改动（checkpoint #0 → 现在）作为**未提交的改动**套到用户的工程文件夹（仓库根目录），不提交。

    会话带着未提交改动开始时用这个收尾：用户文件夹和 checkpoint #0 是同一个起点，补丁能直接套上。
    先 `git apply --check`，套不上就什么都不改，把冲突的文件告诉用户。
    """
    diff = await ck.diff_base(patch=False)
    if not diff["files"]:
        return {"ok": True, "empty": True, "files": []}
    patch = await ck.patch_bytes(diff["base"], diff["head"])
    args = ["apply", "--binary", "--whitespace=nowarn", "-"]
    try:
        await git.raw(folder, args[0], "--check", *args[1:], stdin=patch)
    except git.GitError as e:
        raise MergeError(f"Your project folder changed since the session started; nothing was applied ({folder} is unchanged)",
                         conflicts=_patch_failures(str(e)), kind="conflict") from e
    await git.raw(folder, *args, stdin=patch)
    return {"ok": True, "empty": False, "files": diff["files"], "folder": str(folder)}


async def export_patch(ck: Checkpointer, dest: Path) -> dict[str, Any]:
    diff = await ck.diff_base(patch=False)
    data = await ck.patch_bytes(diff["base"], diff["head"]) if diff["files"] else b""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"path": str(dest), "files": diff["files"], "bytes": len(data)}
