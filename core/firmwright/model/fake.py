"""假模型后端：按脚本回放模型输出，让主循环可以做确定性测试（方案 §11 测试）。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from .types import (
    CancelToken,
    ModelCaps,
    ModelEvent,
    ModelRequest,
    Stop,
    TextDelta,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallDone,
    Usage,
)

# 一步脚本：要么是事件列表，要么是"看了请求再决定"的函数
Step = list[ModelEvent] | Callable[[ModelRequest], list[ModelEvent]]


def say(text: str) -> list[ModelEvent]:
    return [TextDelta(text=text), Usage(input_tokens=10, output_tokens=5), Stop(reason="end_turn")]


def call(name: str, args: dict[str, Any] | None = None, *, id: str | None = None, text: str = "") -> list[ModelEvent]:
    return calls([(name, args or {})], ids=[id] if id else None, text=text)


def calls(
    items: list[tuple[str, dict[str, Any]]], *, ids: list[str] | None = None, text: str = ""
) -> list[ModelEvent]:
    evs: list[ModelEvent] = []
    if text:
        evs.append(TextDelta(text=text))
    for i, (name, args) in enumerate(items):
        cid = (ids[i] if ids else None) or f"call_{name}_{i}"
        raw = json.dumps(args, ensure_ascii=False)
        evs.append(ToolCallDelta(index=i, id=cid, name=name, arguments_delta=raw))
    for i, (name, args) in enumerate(items):
        cid = (ids[i] if ids else None) or f"call_{name}_{i}"
        evs.append(ToolCallDone(call=ToolCallBlock(id=cid, name=name, arguments=args)))
    evs += [Usage(input_tokens=10, output_tokens=5), Stop(reason="tool_calls")]
    return evs


class ScriptedBackend:
    def __init__(self, steps: list[Step], caps: ModelCaps | None = None) -> None:
        self.steps = list(steps)
        self.caps = caps or ModelCaps()
        self.requests: list[ModelRequest] = []

    async def stream(self, req: ModelRequest, *, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        self.requests.append(req.model_copy(deep=True))
        if not self.steps:
            for ev in say("(script exhausted)"):
                yield ev
            return
        step = self.steps.pop(0)
        events = step(req) if callable(step) else step
        for ev in events:
            if cancel.cancelled:
                yield Stop(reason="cancelled")
                return
            yield ev
