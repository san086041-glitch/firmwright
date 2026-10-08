"""设备事件与相关数据（方案 §3.2）。第 4 层（事件 / 中断模型）的核心数据。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

EventKind = Literal[
    "boot", "panic", "wdt_reset", "brownout", "assert", "abort",
    "stack_overflow", "stack_smash", "reboot_loop", "download_mode",
    "marker", "disconnect", "reconnect",
]
# 启动失败的分类参照 esparagus 的 crash_context：panic / 看门狗 / abort / 反复重启 / 栈破坏 / 掉电 / 卡在下载模式

Severity = Literal["info", "warn", "critical"]

SEVERITY: dict[str, Severity] = {
    "boot": "info", "marker": "info", "reconnect": "info", "disconnect": "warn",
    "download_mode": "warn", "wdt_reset": "critical", "brownout": "critical", "panic": "critical",
    "assert": "critical", "abort": "critical", "stack_overflow": "critical", "stack_smash": "critical",
    "reboot_loop": "critical",
}


class LogRef(BaseModel):
    """指向串口日志的字节范围，需要时再读原文（context.log_digest）。"""

    board_id: str
    file: str
    start: int
    end: int

    def short(self) -> str:
        return f"{self.board_id}@{self.start}-{self.end}"


class Frame(BaseModel):
    pc: str
    sp: str | None = None
    function: str | None = None
    file: str | None = None
    line: int | None = None
    internal: bool = False  # D21：ESP-IDF / FreeRTOS / ROM 内部的帧，界面默认折叠

    def render(self) -> str:
        loc = f"{self.file}:{self.line}" if self.file else "??"
        return f"{self.pc} {self.function or '??'} at {loc}"


class Backtrace(BaseModel):
    frames: list[Frame] = Field(default_factory=list)
    raw: str = ""  # 原始的 Backtrace: 行或寄存器
    decoded: bool = False
    corrupted: bool = False  # Backtrace 末尾带 |<-CORRUPTED

    def user_frames(self) -> list[Frame]:
        return [f for f in self.frames if not f.internal]


class DeviceEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    board_id: str
    kind: EventKind
    severity: Severity = "info"
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    summary: str  # 一行人话，模型和界面共用
    detail: dict = Field(default_factory=dict)  # 例：panic 的异常名、寄存器、marker 的命中行
    backtrace: Backtrace | None = None
    log_ref: LogRef | None = None

    @classmethod
    def make(cls, board_id: str, kind: EventKind, summary: str, **kw) -> DeviceEvent:
        return cls(board_id=board_id, kind=kind, severity=SEVERITY.get(kind, "info"), summary=summary, **kw)

    def reminder_text(self, raw: str | None = None) -> str:
        """注入 agent 循环时的文字（ReminderBlock）。只给摘要 + log_ref，原文按需 read_log。
        raw 不为 None 时是 context.log_digest 关掉的对照组：只给事件类型和串口原文，
        不给一行摘要、不给预先解析 / 解码的调用栈——相当于通用 agent 自己读串口日志。"""
        if raw is not None:
            return "\n".join([f"[device event] {self.kind} · {self.severity} · board {self.board_id}",
                              "Raw serial output:", raw.rstrip() or "(empty)"])
        lines = [f"[device event] {self.kind} · {self.severity} · board {self.board_id}", self.summary]
        if self.backtrace and self.backtrace.frames:
            label = "Backtrace (decoded)" if self.backtrace.decoded else "Backtrace (not decoded)"
            lines.append(label + ":")
            lines += [f"  {f.render()}" for f in self.backtrace.frames[:12]]
        elif self.backtrace and self.backtrace.raw:
            lines.append(f"Raw: {self.backtrace.raw[:300]}")
        if self.log_ref:
            lines.append(f"log_ref={self.log_ref.short()} (use read_log for the raw lines)")
        if self.kind in ("panic", "abort", "assert", "stack_overflow", "stack_smash"):
            lines.append(f"Use diagnose_crash(event_id=\"{self.id}\") for fault evidence and the full backtrace.")
        return "\n".join(lines)


class BoardStateChange(BaseModel):
    board_id: str
    state: str
    port: str | None = None
    owner_session: str | None = None
