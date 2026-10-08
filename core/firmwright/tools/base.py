"""工具接口（方案 §4.2，D08）。参照 grok 的 Tool trait + ToolCapabilities，
增加 device / risk / interruptible 三个字段。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ValidationError

from ..model.types import CancelToken, ImageBlock, TextBlock, ToolSpec
from ..trace import Trace

if TYPE_CHECKING:
    from ..services import Services

Risk = Literal["safe", "normal", "dangerous", "forbidden"]


class ToolCaps(BaseModel):
    read_only: bool = False
    device: Literal["none", "shared", "exclusive"] = "none"  # 新提，服务于 I05
    lock: Literal["none", "path", "session"] = "none"  # grok：同一路径的写操作串行
    risk: Risk = "normal"  # 新提，D15
    interruptible: bool = False  # grok：等待类工具可被打断（D10）
    edits_files: bool = False  # 权限模式 accept_edits 用


class ToolResult(BaseModel):
    content: list[TextBlock | ImageBlock]
    is_error: bool = False
    meta: dict[str, Any] = {}  # 给界面看的结构化信息（diff、OpResult……），不发给模型

    @classmethod
    def text(cls, text: str, *, is_error: bool = False, **meta: Any) -> ToolResult:
        return cls(content=[TextBlock(text=text)], is_error=is_error, meta=meta)

    @classmethod
    def error(cls, text: str, **meta: Any) -> ToolResult:
        return cls.text(text, is_error=True, **meta)

    def text_content(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))


class ToolFailure(Exception):  # noqa: N818 —— 不是程序错误，是"这次调用的结果就是这条错误"
    """工具在前置检查里失败（没绑板子、没有平台适配器……）：抛出它，主循环把 result 当作工具结果交给模型。
    这样工具拿到的对象一定不是 None，不用每处写 `x, err = ...; if err: return err`。"""

    def __init__(self, result: ToolResult) -> None:
        super().__init__(result.text_content())
        self.result = result


class HumanAction(BaseModel):
    """人工操作卡片（D13）：请用户做一个物理操作。"""

    title: str
    instructions: str
    board_id: str | None = None
    kind: str = "generic"  # enter_download_mode / replug / wire / measure / generic


class HumanReply(BaseModel):
    done: bool
    note: str = ""


@dataclass
class ToolContext:
    session_id: str
    cwd: Path  # 即 worktree（I09）
    cancel: CancelToken
    trace: Trace
    call_id: str = ""
    services: Services | None = None
    board_id: str | None = None
    progress_cb: Callable[[str], Awaitable[None]] | None = None
    ask_human_cb: Callable[[HumanAction], Awaitable[HumanReply]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    async def progress(self, text: str) -> None:
        if self.progress_cb:
            await self.progress_cb(text)

    async def ask_human(self, action: HumanAction) -> HumanReply:
        if not self.ask_human_cb:
            return HumanReply(done=False, note="no UI is attached to ask for a manual step")
        return await self.ask_human_cb(action)

    def resolve(self, p: str) -> Path:
        path = Path(p)
        if not path.is_absolute():
            path = self.cwd / path
        return path.resolve()


class Tool:
    """所有工具的基类。子类声明 name / description / Args / caps，实现 run。"""

    name: ClassVar[str]
    description: ClassVar[str]
    Args: ClassVar[type[BaseModel]]
    caps: ClassVar[ToolCaps] = ToolCaps()

    def spec(self) -> ToolSpec:
        schema = self.Args.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return ToolSpec(name=self.name, description=self.description, parameters=schema)

    def parse(self, raw: dict[str, Any]) -> BaseModel:
        return self.Args.model_validate(raw)

    # ---- 调度和权限需要的信息（默认实现，子类按需覆盖）

    def lock_keys(self, args: Any, ctx: ToolContext) -> list[str]:
        """同一个 key 的调用串行执行。写文件工具返回规范化后的路径。"""
        return []

    def permission_subject(self, args: Any) -> str:
        """权限规则 Tool(pattern) 里 pattern 匹配的对象，例如路径或命令。"""
        return ""

    def risk_for(self, args: Any) -> Risk:
        """单次调用的风险等级，默认取 caps.risk；烧录这类工具会按参数细分。"""
        return self.caps.risk

    def risk_reason(self, args: Any) -> str:
        """dangerous 级调用在审批卡片上的理由；空串 = 用通用说明。"""
        return ""

    ask_reason: ClassVar[str] = ""  # 这个工具总要询问时，审批卡片上的理由

    async def run(self, ctx: ToolContext, args: Any) -> ToolResult:  # pragma: no cover
        raise NotImplementedError

    def __init_subclass__(cls, **kw: Any) -> None:
        """包一层子类的 run：前置检查抛出的 ToolFailure 变成普通的工具结果（调用方看到的契约不变）。"""
        super().__init_subclass__(**kw)
        run = cls.__dict__.get("run")
        if run is None:
            return

        async def wrapped(self: Tool, ctx: ToolContext, args: Any) -> ToolResult:
            try:
                return await run(self, ctx, args)
            except ToolFailure as f:
                return f.result

        wrapped.__doc__ = run.__doc__
        cls.run = wrapped  # type: ignore[method-assign]


def validation_message(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "(arguments)"
        parts.append(f"{loc}: {err['msg']}")
    return "Invalid arguments: " + "; ".join(parts)


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.add(t)

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def only(self, names: set[str]) -> ToolRegistry:
        """只保留这些工具的新注册表（子 agent / 验证者用）。"""
        return ToolRegistry([t for t in self._tools.values() if t.name in names])

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self, *, read_only_only: bool = False) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values() if t.caps.read_only or not read_only_only]

    def __iter__(self):
        return iter(self._tools.values())
