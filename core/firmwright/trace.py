"""执行轨迹（I16）：从第一天起记录，给之后的评测和对照实验用。只追加，永不删除。

每行一条 JSON：{"ts", "session", "turn", "step", "kind", ...}。
kind 取值：turn_start / model_request / model_response / tool_call / tool_result /
permission / inject / interrupt / loop_guard / turn_end / error / device_event ...
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Trace:
    def __init__(self, path: Path | None, session_id: str) -> None:
        self.path = path
        self.session_id = session_id
        self.turn = 0
        self.step = 0
        self._lock = threading.Lock()
        self.memory: list[dict[str, Any]] = []  # path 为 None 时只记在内存（测试用）
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, **data: Any) -> dict[str, Any]:
        row = {"ts": now_iso(), "session": self.session_id, "turn": self.turn, "step": self.step, "kind": kind}
        row.update(data)
        line = json.dumps(row, ensure_ascii=False, default=str)
        with self._lock:
            if self.path:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            else:
                self.memory.append(row)
        return row


def read_trace(path: Path) -> list[dict[str, Any]]:
    """读 trace.jsonl；进程被中途结束留下的坏行（NUL、半行）跳过。"""
    rows = []
    for line in path.read_text("utf-8", errors="replace").splitlines():
        line = line.strip().strip("\x00")
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
