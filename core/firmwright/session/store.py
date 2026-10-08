"""会话存储（D04）：每个会话 3 个 JSONL——对话历史、界面事件、执行轨迹。
参照 grok storage/jsonl：只追加；history 在压缩时整体重写（原子替换）。

恢复时要容忍"写到一半被杀"的文件：Windows 上进程中途被强制结束，文件末尾可能是一串 NUL
或半行 JSON。坏行跳过并记日志，对话历史再做一次"愈合"（见 heal_history）。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from ..model.types import Message, TextBlock, ToolResultBlock

_msg = TypeAdapter(Message)
log = logging.getLogger("firmwright.store")


class SessionMeta(BaseModel):
    id: str
    title: str
    project_root: str
    cwd: str  # 会话的工作目录：worktree 里对应工程的目录；直接在工程里工作时等于 project_root
    branch: str | None = None  # fwr/<id>
    # ---- W5 工作区（I09 / D06）
    isolation: str = "in_place"  # worktree | in_place（W5 之前的会话、非 git 工程、用户选择不隔离）
    worktree: str | None = None  # worktree 根目录 C:\fwr\wt\<id>
    repo_root: str | None = None
    base_commit: str | None = None  # 创建会话时工程的 HEAD
    target_branch: str | None = None  # 创建会话时工程所在的分支（合并的默认目标）
    carried: list[str] = []  # 创建时带进 worktree 的未提交改动（2026-10-05）
    state: str = "active"  # active | merged | applied | discarded
    ended_at: str | None = None
    merged_commit: str | None = None
    board_id: str | None = None
    model_id: str
    effort: str | None = None  # 思考程度（2026-10-05）
    permission_mode: str = "default"
    parent_id: str | None = None
    created_at: str
    updated_at: str | None = None


def project_key(root: Path) -> str:
    """sessions\\<project-key>\\：参照 grok 按 cwd 分目录。把路径变成一个安全的目录名。"""
    s = str(root.resolve()).replace(":", "").replace("\\", "-").replace("/", "-").strip("-")
    return s[-80:]


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读 JSONL，返回 (行, 坏行数)。坏行（NUL、半行）跳过。"""
    if not path.exists():
        return [], 0
    rows: list[dict[str, Any]] = []
    bad = 0
    for line in path.read_text("utf-8", errors="replace").splitlines():
        if "\x00" in line:
            bad += 1  # 算作损坏：调用方会把文件重写干净
            line = line.replace("\x00", "")
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            bad += 1
    if bad:
        log.warning("%s has %d corrupted lines, skipped (the process was most likely killed mid-write)", path, bad)
    return rows, bad


def heal_history(msgs: list[Message]) -> tuple[list[Message], int]:
    """保证每个工具调用后面都有结果（OpenAI 协议要求，否则下一次请求 400）。

    任务被中途打断（核心崩溃、被杀）时，最后一条 assistant 消息的工具调用可能没有结果，
    这里补上"未执行"。返回 (修好的历史, 补了几条)。
    """
    out: list[Message] = []
    fixed = 0
    for i, m in enumerate(msgs):
        out.append(m)
        calls = m.tool_calls() if m.role == "assistant" else []
        if not calls:
            continue
        nxt = msgs[i + 1] if i + 1 < len(msgs) else None
        have = {b.call_id for b in nxt.content if isinstance(b, ToolResultBlock)} if nxt and nxt.role == "tool" else set()
        missing = [c for c in calls if c.id not in have]
        if not missing:
            continue
        filler = [ToolResultBlock(call_id=c.id, name=c.name, is_error=True,
                                  content=[TextBlock(text="Not executed: the session was interrupted (the core process exited unexpectedly)")]) for c in missing]
        fixed += len(filler)
        if nxt is not None and nxt.role == "tool":
            nxt.content.extend(filler)
        else:
            out.append(Message(role="tool", content=[*filler]))
    return out, fixed


class SessionStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.history_path = root / "history.jsonl"
        self.ui_path = root / "ui-events.jsonl"
        self.trace_path = root / "trace.jsonl"
        self.meta_path = root / "meta.json"
        self.recovered: dict[str, int] = {}  # 恢复时的修补统计：坏行数、补了几个工具结果

    # ---- meta
    def save_meta(self, meta: SessionMeta) -> None:
        _atomic_write(self.meta_path, meta.model_dump_json(indent=2))

    def load_meta(self) -> SessionMeta | None:
        if not self.meta_path.exists():
            return None
        return SessionMeta.model_validate_json(self.meta_path.read_text("utf-8"))

    # ---- history
    def append_message(self, msg: Message) -> None:
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(msg.model_dump_json() + "\n")

    def load_history(self) -> list[Message]:
        rows, bad = read_jsonl(self.history_path)
        msgs: list[Message] = []
        for r in rows:
            try:
                msgs.append(_msg.validate_python(r))
            except ValidationError:
                bad += 1
        msgs, fixed = heal_history(msgs)
        if bad or fixed:
            self.recovered = {"bad_lines": bad, "filled_results": fixed}
            self.rewrite_history(msgs)  # 修好后整体重写，避免下次再读到坏行
        return msgs

    def rewrite_history(self, msgs: list[Message]) -> None:
        _atomic_write(self.history_path, "".join(m.model_dump_json() + "\n" for m in msgs))

    # ---- ui events
    def append_ui(self, update: dict[str, Any]) -> None:
        with self.ui_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(update, ensure_ascii=False, default=str) + "\n")

    def load_ui(self, *, compact: bool = False) -> list[dict[str, Any]]:
        """compact=True：合并流式片段后写回文件（会话不在运行时才可以，避免和追加写冲突）。"""
        rows, bad = read_jsonl(self.ui_path)
        merged = coalesce_ui(rows)
        if bad or (compact and len(merged) < len(rows)):
            # 去掉坏行 / 合并片段后重写；之后追加的内容才不会接在半行后面
            out = merged if compact else rows
            _atomic_write(self.ui_path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in out))
        return merged


# 流式片段：连续的同类片段可以拼成一条
_CHUNKS = {"agent_message_chunk", "agent_thought_chunk"}
# 状态类更新：连续的只有最后一条有意义
_LATEST = {"_fwr/prebuild", "_fwr/context", "_fwr/status", "_fwr/goal"}


def coalesce_ui(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """回放前合并界面事件。演示会话 1.3 万条事件里 97% 是一两个字的流式片段，逐条回放要 26 秒。
    合并后界面折叠出的时间线不变（store.ts 的 reduceUpdate 对拼接后的片段结果相同）。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        kind = r.get("sessionUpdate")
        last = out[-1] if out else None
        if last is not None and last.get("sessionUpdate") == kind:
            if kind in _CHUNKS:
                a, b = last.get("content") or {}, r.get("content") or {}
                if a.get("type") == "text" and b.get("type") == "text":
                    out[-1] = {**last, "content": {"type": "text", "text": a.get("text", "") + b.get("text", "")}}
                    continue
            elif kind in _LATEST and (kind != "_fwr/prebuild" or last.get("status") == "running"):
                out[-1] = r
                continue
        out.append(r)
    return out


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, "utf-8")
    os.replace(tmp, path)
