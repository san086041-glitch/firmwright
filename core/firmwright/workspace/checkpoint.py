"""checkpoint 时间线（D05，§6.4）。参照 grok RewindCheckpoint（xai-grok-workspace/src/session/checkpoint.rs）：
每轮一个点。和 grok 的不同：

- **存法**：每轮结束时把工作目录做成一个 git 提交，挂在影子引用 refs/fwr/ckpt/<会话>/<序号> 上。
  用单独的 index 文件（GIT_INDEX_FILE），不碰用户的 index，也不移动任何分支。没有变化的轮次不新建提交。
- **固件**（新提）：这一轮烧过固件的点记下固件哈希，并把烧录用的文件存档（flasher_args.json + 各个 bin），
  回退时可以把"当时板子上的固件"重新烧回去，而不是用现在的代码重新编译。
- **回退**：只回退文件，不截断对话（Deviation #27）。grok 的 /rewind 截断对话但不动文件；这里反过来，
  因为对话里有设备事件和诊断过程，删掉以后模型会重复踩坑；回退后注入一条 reminder 告诉模型代码变了。
  回退前先把当前状态记一个点，所以回退本身也能撤销。
- 被平台适配器排除的目录（ESP-IDF 的 build\\ 等）不进 checkpoint，回退时也不动它们。
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from . import git
from .links import find_links, skip_names, unlink

log = logging.getLogger("firmwright.checkpoint")

KEEP_FIRMWARE = 20  # 固件存档最多保留几份（每份约 1–2 MB）


class FirmwareRecord(BaseModel):
    seq: int
    turn: int
    sha256: str | None = None
    scope: str = "app"
    chip: str | None = None
    board_id: str | None = None
    port: str | None = None
    at: str
    archive: str | None = None  # 存档目录；None = 存档失败或已被清理
    source: Literal["agent", "restore"] = "agent"


class CheckpointEntry(BaseModel):
    seq: int  # 时间线上的序号（0 = 会话开始时的状态）
    kind: Literal["base", "turn", "manual", "restore"]
    turn: int
    commit: str
    tree: str
    ref: str
    changed: bool = False  # 和上一个点相比文件有没有变化
    files: list[str] = []  # 变化的文件（最多 30 个）
    added: int = 0
    deleted: int = 0
    prompt: str = ""  # 这一轮用户输入的开头
    stop: str | None = None
    firmware: FirmwareRecord | None = None  # 这一轮最后一次烧录（或回退时重烧的固件）
    restored_to: int | None = None
    at: str


class Checkpointer:
    def __init__(self, store_dir: Path, worktree: Path, session_id: str, *, excludes: list[str] | None = None,
                 platform: Any = None) -> None:
        self.dir = store_dir
        self.wt = worktree  # git 工作目录的根（worktree 根或用户的仓库根）
        self.sid = session_id
        self.excludes = excludes or []
        self.platform = platform
        self.path = store_dir / "checkpoints.jsonl"
        self.index = store_dir / "ckpt.index"
        self.fw_dir = store_dir / "firmware"
        self._lock = asyncio.Lock()
        self.unlinked: list[str] = []  # 最近一次回退前删掉的目录链接（相对 worktree 根）
        self.entries: list[CheckpointEntry] = self._load()
        self._fw_seq = max((e.firmware.seq for e in self.entries if e.firmware), default=0)

    def _load(self) -> list[CheckpointEntry]:
        from ..session.store import read_jsonl

        rows, _bad = read_jsonl(self.path)
        out = []
        for r in rows:
            try:
                out.append(CheckpointEntry.model_validate(r))
            except Exception:
                continue
        return out

    def _append(self, e: CheckpointEntry) -> None:
        self.entries.append(e)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(e.model_dump_json() + "\n")

    def _env(self) -> dict[str, str]:
        return {"GIT_INDEX_FILE": str(self.index), **git.BOT_IDENTITY}

    def _pathspec(self, links: list[Path] | None = None) -> list[str]:
        spec = ["--", ".", *[f":(exclude,glob){p}" for p in self.excludes]]
        # 目录链接（联接 / 符号链接）不进 checkpoint：快照不能经过它们把工作目录外面的东西存进来
        for p in links or []:
            spec.append(f":(exclude,literal){p.relative_to(self.wt).as_posix()}")
        return spec

    def _links(self) -> list[Path]:
        return find_links(self.wt, skip=skip_names(self.excludes))

    async def _tree_now(self) -> str:
        env = self._env()
        if not self.index.exists():
            await git.out(self.wt, "read-tree", "HEAD", env=env)
        links = await asyncio.to_thread(self._links)
        if links:
            # index 里可能已经有链接下面的文件（旧快照经过链接存进来的）：先从 index 去掉，不动工作目录
            for p in links:
                await git.run(self.wt, "rm", "-r", "-q", "--cached", "--ignore-unmatch", "--",
                              p.relative_to(self.wt).as_posix(), env=env)
        await git.out(self.wt, "add", "-A", *self._pathspec(links), env=env, timeout=300)
        return await git.out(self.wt, "write-tree", env=env)

    # ------------------------------------------------------------------ 记录

    async def snapshot(self, *, kind: str = "turn", turn: int = 0, prompt: str = "", stop: str | None = None,
                       firmware: FirmwareRecord | None = None, restored_to: int | None = None,
                       force_entry: bool = True) -> CheckpointEntry | None:
        async with self._lock:
            return await self._snapshot(kind=kind, turn=turn, prompt=prompt, stop=stop, firmware=firmware,
                                        restored_to=restored_to, force_entry=force_entry)

    async def _snapshot(self, *, kind, turn, prompt, stop, firmware, restored_to, force_entry,
                        tree: str | None = None, commit: str | None = None) -> CheckpointEntry | None:
        tree = tree or await self._tree_now()
        prev = self.entries[-1] if self.entries else None
        if prev and prev.tree == tree and commit is None:
            if not force_entry:
                return None
            commit = prev.commit
        elif commit is None:
            parent = prev.commit if prev else await git.out(self.wt, "rev-parse", "HEAD")
            msg = f"fwr checkpoint {self.sid} #{len(self.entries)} ({kind}, turn {turn})"
            commit = await git.out(self.wt, "commit-tree", tree, "-p", parent, "-m", msg, env=self._env())
        seq = len(self.entries)
        ref = f"refs/fwr/ckpt/{self.sid}/{seq}"
        await git.out(self.wt, "update-ref", ref, commit)
        files, added, deleted = ([], 0, 0)
        if prev and prev.tree != tree:
            files, added, deleted = await self._numstat(prev.commit, commit)
        e = CheckpointEntry(seq=seq, kind=kind, turn=turn, commit=commit, tree=tree, ref=ref,
                            changed=bool(prev and prev.tree != tree), files=files[:30], added=added,
                            deleted=deleted, prompt=prompt[:120], stop=stop, firmware=firmware,
                            restored_to=restored_to, at=_now())
        self._append(e)
        return e

    async def _numstat(self, a: str, b: str) -> tuple[list[str], int, int]:
        out = await git.out(self.wt, "diff", "--numstat", "--no-renames", a, b)
        files, add, dele = [], 0, 0
        for line in out.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            files.append(parts[2])
            add += int(parts[0]) if parts[0].isdigit() else 0
            dele += int(parts[1]) if parts[1].isdigit() else 0
        return files, add, dele

    async def ensure_base(self) -> CheckpointEntry:
        """会话开始时的状态（序号 0）。"""
        if self.entries:
            return self.entries[0]
        e = await self.snapshot(kind="base", turn=0)
        assert e is not None
        return e

    # ------------------------------------------------------------------ 固件

    def record_flash(self, *, turn: int, cwd: Path, sha256: str | None, scope: str, chip: str | None,
                     board_id: str | None, port: str | None, source: str = "agent",
                     archive_from: Path | None = None) -> FirmwareRecord:
        """烧录成功后立刻调用：存档烧录用的文件（之后的编译会覆盖 build 目录）。"""
        self._fw_seq += 1
        rec = FirmwareRecord(seq=self._fw_seq, turn=turn, sha256=sha256, scope=scope, chip=chip,
                             board_id=board_id, port=port, at=_now(), source=source)  # type: ignore[arg-type]
        if archive_from is not None:
            rec.archive = str(archive_from)  # 用已有的存档重烧：直接指向它
        elif self.platform is not None and hasattr(self.platform, "archive_image"):
            dest = self.fw_dir / f"{rec.seq:04d}"
            try:
                if self.platform.archive_image(cwd, dest):
                    rec.archive = str(dest)
            except Exception as e:  # 存档失败不影响烧录结果
                log.warning("Archiving the firmware failed: %s", e)
            self._prune_firmware()
        return rec

    def _prune_firmware(self) -> None:
        if not self.fw_dir.is_dir():
            return
        dirs = sorted(p for p in self.fw_dir.iterdir() if p.is_dir())
        for p in dirs[:-KEEP_FIRMWARE]:
            shutil.rmtree(p, ignore_errors=True)

    def firmware_at(self, seq: int) -> FirmwareRecord | None:
        """序号 seq 这个点上，板子上跑的是哪份固件：往前找最近一次烧录。"""
        for e in reversed(self.entries[: seq + 1]):
            if e.firmware:
                fw = e.firmware
                if fw.archive and not Path(fw.archive).is_dir():
                    fw = fw.model_copy(update={"archive": None})
                return fw
        return None

    # ------------------------------------------------------------------ 回退

    async def restore(self, seq: int, *, turn: int) -> CheckpointEntry:
        """把工作目录回退到序号 seq 的状态。回退前没记录的改动先记一个 manual 点。"""
        async with self._lock:
            if not 0 <= seq < len(self.entries):
                raise ValueError(f"No checkpoint #{seq}")
            target = self.entries[seq]
            # 先删掉工作目录里的目录链接（只删链接本身）：回退改文件时就不会经过链接改到外面（2026-10-04 事故）
            self.unlinked = []
            for p in await asyncio.to_thread(self._links):
                unlink(p)
                self.unlinked.append(p.relative_to(self.wt).as_posix())
                log.warning("Removed a directory link before restore: %s", p)
            cur = await self._tree_now()
            last = self.entries[-1]
            if cur != last.tree:
                await self._snapshot(kind="manual", turn=turn, prompt="unrecorded changes before restore", stop=None, firmware=None,
                                     restored_to=None, force_entry=True, tree=cur)
            if cur != target.tree:
                # 两棵树的 read-tree：index 现在等于 cur，-u 把工作目录从 cur 改成 target（多出来的文件会被删掉）
                await git.out(self.wt, "read-tree", "-m", "-u", cur, target.tree, env=self._env(), timeout=300)
            return await self._snapshot(kind="restore", turn=turn, prompt=f"restore to #{seq}", stop=None,
                                        firmware=None, restored_to=seq, force_entry=True, tree=target.tree,
                                        commit=target.commit)  # type: ignore[return-value]

    async def preview(self, seq: int) -> dict[str, Any]:
        """回退前给用户看：回到 seq 会删掉 / 新建 / 改动哪些文件，以及会先删掉哪些目录链接。"""
        async with self._lock:
            if not 0 <= seq < len(self.entries):
                raise ValueError(f"No checkpoint #{seq}")
            links = [p.relative_to(self.wt).as_posix() for p in await asyncio.to_thread(self._links)]
            cur = await self._tree_now()
            out = await git.out(self.wt, "diff", "--name-status", "--no-renames", cur, self.entries[seq].tree)
        groups: dict[str, list[str]] = {"D": [], "A": [], "M": []}
        for line in out.splitlines():
            st, _, path = line.partition("\t")
            groups.setdefault(st[:1], []).append(path)
        return {"delete": len(groups["D"]), "add": len(groups["A"]), "modify": len(groups["M"]),
                "deleteFiles": groups["D"][:20], "links": links}

    def attach_firmware(self, seq: int, fw: FirmwareRecord) -> None:
        """回退后重烧了固件：记到回退那个点上（重写 jsonl）。"""
        e = self.entries[seq]
        e.firmware = fw
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(x.model_dump_json() + "\n" for x in self.entries), "utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ 收尾

    async def diff_base(self, *, patch: bool = True, max_bytes: int = 2_000_000) -> dict[str, Any]:
        """从会话开始（序号 0）到现在的完整改动：给"合并前展示完整 diff"和"导出补丁"用。"""
        base = await self.ensure_base()
        cur = await self.snapshot(kind="manual", turn=0, prompt="state before finishing", force_entry=False)
        head = cur or self.entries[-1]
        files, added, deleted = ([], 0, 0)
        text = ""
        if head.tree != base.tree:
            files, added, deleted = await self._numstat(base.commit, head.commit)
            if patch:
                text = await self.patch(base.commit, head.commit)
        truncated = len(text.encode("utf-8")) > max_bytes
        return {"base": base.commit, "head": head.commit, "files": files, "added": added, "deleted": deleted,
                "patch": text[:max_bytes] if truncated else text, "truncated": truncated}

    async def patch_bytes(self, a: str, b: str, *, exclude: list[str] | None = None) -> bytes:
        """git diff --binary 的原始字节（保留 CRLF，能直接 git apply）。"""
        args = ["diff", "--binary", "--no-renames", "--no-color", a, b]
        if exclude:
            args += ["--", ".", *[f":(exclude,literal){p}" for p in exclude]]
        return await git.raw(self.wt, *args)

    async def patch(self, a: str, b: str) -> str:
        return (await self.patch_bytes(a, b)).decode("utf-8", "replace")

    async def delete_refs(self) -> None:
        refs = await git.out(self.wt, "for-each-ref", "--format=%(refname)", f"refs/fwr/ckpt/{self.sid}/")
        if refs:
            cmds = "".join(f"delete {r}\n" for r in refs.splitlines())
            await git.out(self.wt, "update-ref", "--stdin", stdin=cmds.encode())
        self.index.unlink(missing_ok=True)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def dump(entries: list[CheckpointEntry]) -> list[dict]:
    return [json.loads(e.model_dump_json()) for e in entries]
