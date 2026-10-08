"""git worktree：每个会话一个独立的工作目录和分支（I09）。

    C:\\fwr\\wt\\<6位id>\\        ← worktree（D02：短路径，避开 ESP-IDF 在 Windows 上的 260 字符限制）
    分支 fwr/<id>               ← 从创建会话时工程的 HEAD 拉出来

参照 grok 的 x.ai/git/worktree/create · apply · remove（xai-grok-workspace/src/worktree/mod.rs），
有两处不同：
- grok 默认把主工作目录里未提交的改动一起拷进 worktree。这里原来只从 HEAD 开始（Deviation #26）；
  真机实测（2026-10-05）里用户在编辑器里看到的代码 agent 看不到，改为**默认带上、可以取消**（carry_dirty）。
  带上的改动在 checkpoint #0 里，不算 agent 的改动；收尾时用"应用到工程文件夹"把 agent 的改动套回去。
- grok 会在后台把被忽略的产物目录（target、node_modules）拷过去；这里只拷平台适配器列出的
  "没有它就编不对"的文件（ESP-IDF：没被 git 跟踪的 sdkconfig、.firmwright\\、managed_components\\），
  build 目录不拷（CMake 记了绝对路径，拷过去也不能用）。
工程可以是仓库的子目录：worktree 建的是整个仓库，会话的 cwd 是 worktree 里对应的子目录。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import stat
import tempfile
from pathlib import Path

from pydantic import BaseModel

from . import git
from .links import find_links, unlink

DEFAULT_ROOT = Path(r"C:\fwr\wt")


class RepoInfo(BaseModel):
    project: str
    is_git: bool
    repo_root: str | None = None
    subdir: str = ""  # 工程相对仓库根目录的路径（"" 表示就是根目录），用 / 分隔
    head: str | None = None  # None：还没有任何提交
    branch: str | None = None  # None：detached HEAD
    dirty: list[str] = []  # 工程目录下未提交的改动（git status --porcelain 的路径，最多 50 个）
    dirty_count: int = 0  # 未提交改动的总数


class WorktreeInfo(BaseModel):
    path: str  # worktree 根目录
    cwd: str  # 会话的工作目录 = path / subdir
    branch: str
    base_commit: str
    target_branch: str | None
    repo_root: str
    subdir: str
    seeded: list[str] = []  # 从主工程拷过来的未跟踪文件
    carried: list[str] = []  # 带进 worktree 的未提交改动（相对仓库根目录）


class NotGitRepo(Exception):
    pass


class NoCommits(Exception):
    pass


class EmptyProject(Exception):
    """初始化 git 时文件夹里除了 .gitignore 什么都没有（2026-10-04 真机实测：工程还没拷进来就建了会话）。"""


async def inspect(project: Path) -> RepoInfo:
    project = project.resolve()
    info = RepoInfo(project=str(project), is_git=False)
    if git.git_exe() is None:
        return info
    res = await git.run(project, "rev-parse", "--show-toplevel")
    if not res.ok:
        return info
    root = Path(res.stdout.strip()).resolve()
    info.is_git = True
    info.repo_root = str(root)
    rel = os.path.relpath(project, root)
    info.subdir = "" if rel == "." else rel.replace("\\", "/")
    head = await git.run(project, "rev-parse", "--verify", "-q", "HEAD")
    info.head = head.stdout.strip() if head.ok and head.stdout.strip() else None
    br = await git.run(project, "symbolic-ref", "-q", "--short", "HEAD")
    info.branch = br.stdout.strip() if br.ok and br.stdout.strip() else None
    if info.head:
        st = await git.run(project, "status", "--porcelain", "--untracked-files=normal", "--", ".")
        lines = [line[3:] for line in st.stdout.splitlines() if line.strip()]
        info.dirty, info.dirty_count = lines[:50], len(lines)
    return info


async def init_repo(project: Path, gitignore: str = "", *, new_project: bool = False) -> RepoInfo:
    """用户在界面上确认后才调用：git init（如果还不是仓库），写默认的 .gitignore（如果没有），做第一个提交。

    已经是仓库但还没有提交时，提交范围是**整个仓库**（界面上会把仓库根目录告诉用户）。
    """
    project = project.resolve()
    info = await inspect(project)
    if not info.is_git:
        await git.out(project, "init")
        info = await inspect(project)
    root = Path(info.repo_root or project)
    gi = root / ".gitignore"
    if gitignore and not gi.exists():
        gi.write_text(gitignore, "utf-8")
    if info.head is None:
        await git.out(root, "add", "-A", timeout=900)
        # 不用 --allow-empty：空提交会让 worktree 是空的，agent 只能绕到工程目录外面干活（2026-10-04 事故）
        staged = [f for f in (await git.out(root, "ls-files")).splitlines() if f and f != ".gitignore"]
        # new_project：用户在新建会话页明确选了"在这个空文件夹里新建工程"，空的起点是故意的（2026-10-05）；
        # 否则空文件夹多半是工程还没拷进来，拒绝
        if not staged and not new_project:
            raise EmptyProject(str(root))
        env = None if await _has_identity(root) else git.BOT_IDENTITY
        await git.out(root, "commit", "--allow-empty", "-m", "Initial commit (created by Firmwright before starting a session)",
                      env=env, timeout=600)
    return await inspect(project)


def scan_folder(folder: Path, *, skip: tuple[str, ...] = (".git", "build", "managed_components", "node_modules"),
                limit: int = 200_000) -> dict:
    """新建会话前告诉用户"初始化 git 会提交多少文件"：数文件数和大小，跳过产物目录；超过 limit 就停。"""
    files = size = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for f in filenames:
            files += 1
            with contextlib.suppress(OSError):
                size += os.path.getsize(os.path.join(dirpath, f))
        if files >= limit:
            truncated = True
            break
    return {"files": files, "bytes": size, "truncated": truncated}


async def _has_identity(root: Path) -> bool:
    name = await git.run(root, "config", "user.name")
    mail = await git.run(root, "config", "user.email")
    return name.ok and bool(name.stdout.strip()) and mail.ok and bool(mail.stdout.strip())


async def list_branches(repo: Path) -> list[str]:
    res = await git.run(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return [b for b in res.stdout.splitlines() if b and not b.startswith("fwr/")]


async def create(project: Path, session_id: str, *, root: Path = DEFAULT_ROOT,
                 seeds: list[str] | None = None, carry_dirty: bool = False) -> WorktreeInfo:
    info = await inspect(project)
    if not info.is_git:
        raise NotGitRepo(str(project))
    if info.head is None:
        raise NoCommits(info.repo_root or str(project))
    repo = Path(info.repo_root or project)
    path = root / session_id
    if path.exists():
        raise FileExistsError(f"The worktree directory already exists: {path}")
    root.mkdir(parents=True, exist_ok=True)
    branch = f"fwr/{session_id}"
    await git.out(repo, "worktree", "add", "-b", branch, str(path), info.head, timeout=600)
    cwd = path / info.subdir if info.subdir else path
    carried = await _carry(repo, info.subdir, path, info.head) if carry_dirty and info.dirty_count else []
    seeded = _seed(project.resolve(), cwd, seeds or [])
    return WorktreeInfo(path=str(path), cwd=str(cwd), branch=branch, base_commit=info.head,
                        target_branch=info.branch, repo_root=str(repo), subdir=info.subdir, seeded=seeded,
                        carried=carried)


async def _carry(repo: Path, subdir: str, wt: Path, head: str) -> list[str]:
    """把主工作目录里工程范围内未提交的改动（含未跟踪、未被忽略的文件）套到新 worktree 上。

    在临时 index 上 `add -A` 得到"用户此刻看到的样子"的树，再把 HEAD → 这棵树的补丁 apply 到 worktree。
    用户自己的 index 和工作目录都不动；git apply 不带 --index，只改 worktree 里的文件（换行符按用户配置转换）。
    """
    tmp = Path(tempfile.mkdtemp(prefix="fwr-carry-"))
    env = {"GIT_INDEX_FILE": str(tmp / "index")}
    try:
        await git.out(repo, "read-tree", head, env=env)
        await git.out(repo, "add", "-A", "--", subdir or ".", env=env, timeout=600)
        tree = await git.out(repo, "write-tree", env=env)
        names = [n for n in (await git.out(repo, "diff", "--name-only", "--no-renames", head, tree)).splitlines() if n]
        if names:
            patch = await git.raw(repo, "diff", "--binary", "--no-renames", "--no-color", head, tree)
            await git.raw(wt, "apply", "--binary", "--whitespace=nowarn", "-", stdin=patch, timeout=600)
        return names
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _seed(src: Path, dst: Path, names: list[str]) -> list[str]:
    """把主工程里存在、但 worktree 里没有的文件拷过去（不覆盖 git 检出的文件）。"""
    copied: list[str] = []
    for name in names:
        s = src / name
        if s.is_file():
            d = dst / name
            if not d.exists():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
                copied.append(name)
        elif s.is_dir():
            n = 0
            for f in s.rglob("*"):
                if not f.is_file():
                    continue
                d = dst / f.relative_to(src)
                if not d.exists():
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, d)
                    n += 1
            if n:
                copied.append(f"{name}/ ({n} files)")
    return copied


async def remove(repo: Path, path: Path, branch: str | None) -> list[str]:
    """删掉 worktree 和会话分支。返回没能删干净的东西（文件被占用等），由调用方告诉用户。"""
    problems: list[str] = []
    if path.exists():
        # 先删掉 worktree 里所有目录链接（只删链接本身）：删目录时不能经过链接删到外面去（2026-10-04 事故）
        for link in await asyncio.to_thread(find_links, path):
            try:
                unlink(link)
            except OSError as e:
                problems.append(f"A directory link could not be removed, so the worktree was kept: {link} ({e})")
        if problems:
            return problems
        res = await git.run(repo, "worktree", "remove", "--force", str(path), timeout=300)
        if not res.ok and path.exists():
            # Windows 上 build 目录里的文件可能还被别的进程占着；先尽量删，再让 git 忘掉这个 worktree
            await asyncio.to_thread(shutil.rmtree, path, onexc=_force_remove)
            if path.exists():
                problems.append(f"The directory could not be fully removed (files may be in use): {path}")
    await git.run(repo, "worktree", "prune")
    if branch:
        res = await git.run(repo, "branch", "-D", branch)
        if not res.ok and "not found" not in res.stderr:
            problems.append(f"Branch {branch} could not be deleted: {res.stderr.strip()[-200:]}")
    return problems


def _force_remove(func, p, _exc) -> None:
    try:
        os.chmod(p, stat.S_IWRITE)  # 只读文件（git 的 pack 文件）先去掉只读
        func(p)
    except OSError:
        pass
