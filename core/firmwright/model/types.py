"""统一消息格式（方案 §4.1，D07）。

核心内部只认这里的类型；每种 API 协议（OpenAI 兼容、以后的 Anthropic Messages 等）
在各自的后端里做双向转换。对应 grok 的 xai-grok-sampling-types。
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, Field

# ---------------------------------------------------------------- 内容块


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    media_type: str  # image/png ...
    data: str  # base64
    alt: str = ""  # 模型不支持视觉时用来替换的文字说明（I13）


class ToolCallBlock(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str | None = None  # 模型给的 JSON 解析失败时保留原文


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    call_id: str
    name: str
    content: list[TextBlock | ImageBlock]
    is_error: bool = False


class ReasoningBlock(BaseModel):
    type: Literal["reasoning"] = "reasoning"
    text: str


class ReminderBlock(BaseModel):
    """grok 的 system-reminder：项目规则、硬件事件、计划模式提示都通过它注入。"""

    type: Literal["reminder"] = "reminder"
    source: str  # "rules" | "device_event" | "interjection" | "loop_guard" | ...
    text: str
    event_id: str | None = None  # 设备事件的 id，用来去重（已经由 await_marker 交给模型的不再注入）


Block = Annotated[
    TextBlock | ImageBlock | ToolCallBlock | ToolResultBlock | ReasoningBlock | ReminderBlock,
    Field(discriminator="type"),
]


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: list[Block]

    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    def tool_calls(self) -> list[ToolCallBlock]:
        return [b for b in self.content if isinstance(b, ToolCallBlock)]


# ---------------------------------------------------------------- 请求


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


class ModelCaps(BaseModel):
    context_window: int = 128_000
    vision: bool = False  # I13：不支持视觉时，图片块替换为文字说明
    parallel_tool_calls: bool = True
    reasoning: bool = False
    echo_reasoning: bool = False  # 把 reasoning_content 回传（DeepSeek 思考模式 + 工具调用需要）
    # 2026-10-06 评测第二轮：按 context_window 强制报"上下文超长"（模拟真实的小窗口模型；API 实际窗口更大时用）
    enforce_window: bool = False


class ModelRequest(BaseModel):
    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    effort: str | None = None  # 思考程度：None = 不指定（用服务商默认）；off / low / medium / high / on


# ---------------------------------------------------------------- 流式事件


class TextDelta(BaseModel):
    kind: Literal["text_delta"] = "text_delta"
    text: str


class ReasoningDelta(BaseModel):
    kind: Literal["reasoning_delta"] = "reasoning_delta"
    text: str


class ToolCallDelta(BaseModel):
    kind: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


class ToolCallDone(BaseModel):
    kind: Literal["tool_call_done"] = "tool_call_done"
    call: ToolCallBlock


class Usage(BaseModel):
    kind: Literal["usage"] = "usage"
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0


class Stop(BaseModel):
    kind: Literal["stop"] = "stop"
    reason: str  # "end_turn" | "tool_calls" | "max_tokens" | "cancelled" | ...


class ModelError(BaseModel):
    kind: Literal["error"] = "error"
    message: str
    retryable: bool = False
    status: int | None = None
    retry_after: float | None = None  # 服务端的 Retry-After（秒）


ModelEvent = TextDelta | ReasoningDelta | ToolCallDelta | ToolCallDone | Usage | Stop | ModelError


# ---------------------------------------------------------------- 取消


class CancelToken:
    """可等待的取消标记。会话取消、工具被事件打断都通过它传递。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self.reason: str = ""

    def cancel(self, reason: str = "cancelled") -> None:
        if not self._event.is_set():
            self.reason = reason
            self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()


class ModelBackend(Protocol):
    """I07：每种 API 协议一个实现，对应 grok 的 ApiBackend 枚举。"""

    caps: ModelCaps

    def stream(self, req: ModelRequest, *, cancel: CancelToken) -> Any:  # AsyncIterator[ModelEvent]
        ...
