"""OpenAI 兼容后端（I07 第一版唯一实现）。DeepSeek / Qwen / GLM / Kimi 的兼容接口都走这里。

- 自己解析 SSE 流（httpx），不依赖厂商 SDK
- 重试参照 grok retry.rs：指数退避，上限 30 秒，遵守 Retry-After；只在还没产出任何增量时重试
- ReminderBlock 转成 <system-reminder> 文本，拼进 user 消息
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .types import (
    CancelToken,
    ImageBlock,
    ModelCaps,
    ModelError,
    ModelEvent,
    ModelRequest,
    ReasoningBlock,
    ReasoningDelta,
    ReminderBlock,
    Stop,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolCallDelta,
    ToolCallDone,
    ToolResultBlock,
    Usage,
)

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


def reminder_text(b: ReminderBlock) -> str:
    return f"<system-reminder source=\"{b.source}\">\n{b.text}\n</system-reminder>"


def _image_part(b: ImageBlock, vision: bool) -> dict[str, Any]:
    if vision:
        return {"type": "image_url", "image_url": {"url": f"data:{b.media_type};base64,{b.data}"}}
    return {"type": "text", "text": f"[image: {b.alt or b.media_type}; omitted because the current model has no vision]"}


def to_openai_messages(req: ModelRequest, caps: ModelCaps) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": req.system}]
    for m in req.messages:
        if m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant"}
            text = "".join(b.text for b in m.content if isinstance(b, TextBlock))
            msg["content"] = text or None
            calls = m.tool_calls()
            if calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": c.raw_arguments
                            if c.raw_arguments is not None
                            else json.dumps(c.arguments, ensure_ascii=False),
                        },
                    }
                    for c in calls
                ]
            if caps.echo_reasoning:
                reasoning = "".join(b.text for b in m.content if isinstance(b, ReasoningBlock))
                if reasoning:
                    msg["reasoning_content"] = reasoning
            out.append(msg)
        elif m.role == "tool":
            # 一条 tool 消息里可能有多个结果；OpenAI 协议要求每个结果一条 role=tool 消息。
            # 图片不能放进 tool 消息，挪到紧随其后的 user 消息里。
            images: list[ImageBlock] = []
            for b in m.content:
                if not isinstance(b, ToolResultBlock):
                    continue
                texts = []
                for c in b.content:
                    if isinstance(c, TextBlock):
                        texts.append(c.text)
                    elif caps.vision:
                        images.append(c)
                        texts.append(f"[image {c.alt or c.media_type}, see the next message]")
                    else:
                        texts.append(_image_part(c, False)["text"])
                body = "\n".join(t for t in texts if t)
                if b.is_error:
                    body = f"[tool error]\n{body}"
                out.append({"role": "tool", "tool_call_id": b.call_id, "content": body or "(no output)"})
            if images:
                out.append({"role": "user", "content": [_image_part(i, caps.vision) for i in images]})
        else:  # user
            parts: list[dict[str, Any]] = []
            for b in m.content:
                if isinstance(b, TextBlock):
                    parts.append({"type": "text", "text": b.text})
                elif isinstance(b, ReminderBlock):
                    parts.append({"type": "text", "text": reminder_text(b)})
                elif isinstance(b, ImageBlock):
                    parts.append(_image_part(b, caps.vision))
            if all(p["type"] == "text" for p in parts):
                out.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
            else:
                out.append({"role": "user", "content": parts})
    return out


def parse_arguments(raw: str) -> tuple[dict[str, Any], str | None]:
    """解析工具参数 JSON。失败时返回空字典并保留原文，交给工具层报错给模型。"""
    if not raw.strip():
        return {}, None
    try:
        val = json.loads(raw)
    except json.JSONDecodeError:
        return {}, raw
    if not isinstance(val, dict):
        return {}, raw
    return val, None


class OpenAICompatBackend:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        caps: ModelCaps | None = None,
        *,
        timeout: float = 120.0,
        max_retries: int = 4,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        effort_style: str = "reasoning_effort",
    ) -> None:
        self.effort_style = effort_style
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.caps = caps or ModelCaps()
        self.timeout = timeout
        self.max_retries = max_retries
        self.extra_body = extra_body or {}
        self._transport = transport

    def build_body(self, req: ModelRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": req.model,
            "messages": to_openai_messages(req, self.caps),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in req.tools
            ]
            if not self.caps.parallel_tool_calls:
                body["parallel_tool_calls"] = False
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.max_tokens is not None:
            body["max_tokens"] = req.max_tokens
        body.update(self.extra_body)
        if req.effort:
            body.update(effort_params(self.effort_style, req.effort))
        return body

    async def stream(self, req: ModelRequest, *, cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        body = self.build_body(req)
        attempt = 0
        while True:
            emitted = False
            retry_after: float | None = None
            try:
                async for ev in self._stream_once(body, cancel):
                    if isinstance(ev, ModelError):
                        if ev.retryable and not emitted and attempt < self.max_retries:
                            retry_after = ev.retry_after
                            raise _Retry(ev)
                        yield ev
                        return
                    emitted = emitted or ev.kind in ("text_delta", "reasoning_delta", "tool_call_delta")
                    yield ev
                return
            except _Retry:
                attempt += 1
                delay = retry_after if retry_after is not None else min(30.0, 1.0 * 2 ** (attempt - 1))
                delay = min(30.0, delay) * (0.8 + 0.4 * random.random())
                try:
                    await asyncio.wait_for(cancel.wait(), timeout=delay)
                    yield Stop(reason="cancelled")
                    return
                except TimeoutError:
                    continue

    async def _stream_once(self, body: dict[str, Any], cancel: CancelToken) -> AsyncIterator[ModelEvent]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "text/event-stream"}
        calls: dict[int, dict[str, Any]] = {}
        finish: str | None = None
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self._transport) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", json=body, headers=headers
                ) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")[:2000]
                        err = ModelError(
                            message=f"HTTP {resp.status_code}: {text}",
                            retryable=resp.status_code in RETRYABLE_STATUS,
                            status=resp.status_code,
                        )
                        ra = resp.headers.get("retry-after")
                        if ra:
                            try:
                                err.retry_after = float(ra)
                            except ValueError:
                                pass
                        yield err
                        return
                    async for line in resp.aiter_lines():
                        if cancel.cancelled:
                            yield Stop(reason="cancelled")
                            return
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            yield ModelError(message=json.dumps(chunk["error"], ensure_ascii=False))
                            return
                        usage = chunk.get("usage")
                        if usage:
                            details = usage.get("prompt_tokens_details") or {}
                            yield Usage(
                                input_tokens=usage.get("prompt_tokens", 0),
                                output_tokens=usage.get("completion_tokens", 0),
                                cached_tokens=details.get("cached_tokens", 0)
                                or usage.get("prompt_cache_hit_tokens", 0),
                            )
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("reasoning_content"):
                                yield ReasoningDelta(text=delta["reasoning_content"])
                            if delta.get("content"):
                                yield TextDelta(text=delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                slot = calls.setdefault(idx, {"id": None, "name": "", "args": ""})
                                fn = tc.get("function") or {}
                                if tc.get("id"):
                                    slot["id"] = tc["id"]
                                if fn.get("name"):
                                    slot["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["args"] += fn["arguments"]
                                yield ToolCallDelta(
                                    index=idx,
                                    id=tc.get("id"),
                                    name=fn.get("name"),
                                    arguments_delta=fn.get("arguments") or "",
                                )
                            if choice.get("finish_reason"):
                                finish = choice["finish_reason"]
        except httpx.TransportError as e:
            yield ModelError(message=f"Network error: {type(e).__name__}: {e}", retryable=True)
            return

        for idx in sorted(calls):
            slot = calls[idx]
            args, raw = parse_arguments(slot["args"])
            yield ToolCallDone(
                call=ToolCallBlock(
                    id=slot["id"] or f"call_{idx}_{random.randrange(1 << 30):x}",
                    name=slot["name"],
                    arguments=args,
                    raw_arguments=raw,
                )
            )
        reason = {"stop": "end_turn", "tool_calls": "tool_calls", "length": "max_tokens"}.get(
            finish or "stop", finish or "end_turn"
        )
        if calls and reason == "end_turn":
            reason = "tool_calls"
        yield Stop(reason=reason)


class _Retry(Exception):
    def __init__(self, err: ModelError) -> None:
        super().__init__(err.message)
        self.err = err



THINKING_BUDGET = {"low": 1024, "medium": 4096, "high": 16384}


def effort_params(style: str, effort: str) -> dict[str, Any]:
    """思考程度 → 各家的请求参数（2026-10-05）。"""
    if style in ("enable_thinking", "thinking_budget"):
        if effort == "off" and style == "enable_thinking":
            return {"enable_thinking": False}
        return {"enable_thinking": True, "thinking_budget": THINKING_BUDGET.get(effort, 4096)}
    if style == "thinking_type":
        return {"thinking": {"type": "disabled" if effort == "off" else "enabled"}}
    return {"reasoning_effort": effort} if effort in ("low", "medium", "high") else {}
