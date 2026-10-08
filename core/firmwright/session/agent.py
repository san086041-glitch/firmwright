"""会话主循环（I01，§6.1）。设计参照 grok xai-grok-shell/src/session/acp_session_impl/turn.rs。

一轮（turn）= 用户一次输入到 agent 给出最终回复；一轮里有多步（step）= 多次调用模型。
每一步：
  1. 取出待注入的内容（用户插话、硬件事件……），作为 ReminderBlock 加入上下文   ← grok interjection 队列
  2. 上下文用量超过 80% 先压缩（W6，context/compaction.py）                  ← grok compaction.rs
  3. 流式调用模型                                                           ← grok sampler_turn.rs
  4. 逐个检查工具调用的权限                                                  ← grok prepare_tool_call
  5. 并发执行：同一路径写串行、session 锁串行、独占设备串行；等待类工具可被事件打断（D10）
  6. 防原地打转：连续 3 次相同调用先提醒，第 5 次强制结束本轮                    ← grok IdenticalToolCallRun
模型回复里没有工具调用、且没有待注入内容时，本轮结束。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

from ..config import Features
from ..context.compaction import (
    UsageTracker,
    build_compacted,
    is_context_overflow,
    last_user_text,
    should_compact,
    summarize,
    write_segment,
)
from ..model.types import (
    Block,
    CancelToken,
    ImageBlock,
    Message,
    ModelBackend,
    ModelError,
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
from ..permissions.engine import Decision, Mode, PermissionEngine
from ..permissions.ps_parse import analyze as ps_analyze
from ..services import Services
from ..tools.base import (
    HumanAction,
    HumanReply,
    Tool,
    ToolContext,
    ToolRegistry,
    ToolResult,
    validation_message,
)
from ..trace import Trace
from ..workspace.checkpoint import Checkpointer, FirmwareRecord
from .prompt import SYSTEM_PROMPT, load_project_rules
from .store import SessionStore

if TYPE_CHECKING:
    from .subagent import BackgroundRuns

INTERRUPT_REASON = "a new device event arrived"  # 可打断的工具（await_marker）被关键设备事件打断时的取消原因
USER_INTERRUPT = "stopped by the user"  # 用户点了"立即送达 / 停止这一步"（2026-10-05）
NO_USER_INTERRUPT = {"flash"}  # 烧录中途杀掉会留下烧了一半的固件：这一步做完再送达

Emit = Callable[[dict[str, Any]], Awaitable[None]]
PermissionReply = Literal["allow_once", "allow_always", "reject"]


class PermissionRequest(BaseModel):
    tool_call_id: str
    tool: str
    title: str
    reason: str
    risk: str
    subjects: list[str]
    arguments: dict[str, Any]


AskPermission = Callable[[PermissionRequest], Awaitable[PermissionReply]]
AskHuman = Callable[[HumanAction], Awaitable[HumanReply]]

TOOL_KIND = {  # ACP 的 ToolKind
    "read_file": "read", "list_dir": "search", "grep": "search", "write_file": "edit", "edit_file": "edit",
    "shell": "execute", "build": "execute", "flash": "execute", "set_target": "execute", "clean": "execute",
    "await_marker": "fetch", "read_log": "read", "diagnose_crash": "think", "reset": "execute",
    "project_status": "read", "size": "read", "ask_human": "other",
    "skill": "read", "memory_search": "search", "memory_get": "read", "remember": "edit",
    "search_tool": "search", "use_tool": "other",
    "check_subagents": "fetch", "stop_subagent": "other",
}

LOOP_WARN, LOOP_STOP = 3, 5


@dataclass
class TurnResult:
    stop_reason: str  # end_turn | cancelled | max_steps | loop_guard | model_error | readonly
    text: str
    steps: int
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None


class Session:
    def __init__(
        self,
        *,
        id: str,
        cwd: Path,
        backend: ModelBackend,
        model: str,
        registry: ToolRegistry,
        permissions: PermissionEngine | None = None,
        mode: Mode = "default",
        store: SessionStore | None = None,
        trace: Trace | None = None,
        services: Services | None = None,
        board_id: str | None = None,
        features: Features | None = None,
        emit: Emit | None = None,
        ask_permission: AskPermission | None = None,
        ask_human: AskHuman | None = None,
        max_steps: int = 60,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self.id = id
        self.cwd = cwd
        self.backend = backend
        self.model = model
        self.effort: str | None = None  # 思考程度（界面选择；None = 服务商默认）
        self.registry = registry
        self.permissions = permissions or PermissionEngine()
        self.mode: Mode = mode
        self.store = store
        self.trace = trace or Trace(store.trace_path if store else None, id)
        if store and store.trace_path.exists() and trace is None:
            # 恢复的会话：轮次接着上次往下数，trace 里的 turn 编号不重复
            from ..trace import read_trace

            self.trace.turn = max((r.get("turn", 0) for r in read_trace(store.trace_path)), default=0)
        self.services = services or Services()
        self.board_id = board_id
        self.features = features or Features()
        self._emit = emit
        self.ask_permission_cb = ask_permission
        self.ask_human_cb = ask_human
        self.max_steps = max_steps
        self.system_prompt = system_prompt
        self.history: list[Message] = store.load_history() if store else []
        self.status: Literal["idle", "running", "awaiting_approval", "awaiting_human", "error"] = "idle"
        self._pending: list[ReminderBlock] = []  # 待注入（interjection / device_event）
        self._running_tools: dict[str, tuple[str, CancelToken]] = {}  # 正在执行的工具：调用 id → (工具名, 取消令牌)
        self._interrupt = asyncio.Event()  # 有关键事件注入时置位，打断等待中的工具
        self._turn_cancel: CancelToken | None = None
        self._lock = asyncio.Lock()  # 同一时间只跑一轮
        self.last_usage: Usage | None = None
        self.delivered_events: set[str] = set()  # 已经通过工具结果交给模型的设备事件
        self.injected_events: set[str] = set()  # 已经通过 reminder 注入给模型的设备事件
        self.checkpoints: Checkpointer | None = None  # W5：每轮结束记一个点（D05）；不是 git 工程时为 None
        self.readonly: str | None = None  # 会话已合并 / 丢弃：说明文字；不再接受输入
        self._turn_firmware: FirmwareRecord | None = None  # 本轮最后一次烧录
        # ---- W6 上下文工程
        # 第一轮（和压缩之后）注入的内容：项目规则、skill 清单、记忆。运行时会换成完整的版本（context/assembler.py）
        self.preface: Callable[[str], list[ReminderBlock]] = self._default_preface
        self.state_fn: Callable[[], str] = lambda: ""  # 压缩时附上的"当前状态"（板子、固件、worktree）
        self.compaction_dir: Path | None = store.root / "compaction" if store else None
        self.usage = UsageTracker()
        # ---- W7 子 agent
        self.parent_id: str | None = None
        self.child_seq = 0
        self.background: BackgroundRuns | None = None  # 后台子 agent（2026-10-05）

    # ------------------------------------------------------------------ 外部接口

    @property
    def running(self) -> bool:
        return self.status in ("running", "awaiting_approval", "awaiting_human")

    def cancel(self, reason: str = "cancelled by the user") -> None:
        if self._turn_cancel:
            self._turn_cancel.cancel(reason)

    def interrupt_tools(self) -> list[str]:
        """停掉正在执行的工具（轮次不结束）：用户要马上送达插话，或者觉得这一步卡住了。返回被停掉的工具名。
        烧录不打断，这一步做完插话自然就送到了。"""
        stopped = []
        for name, cancel in list(self._running_tools.values()):
            if name not in NO_USER_INTERRUPT and not cancel.cancelled:
                cancel.cancel(USER_INTERRUPT)
                stopped.append(name)
        return stopped

    def inject(self, reminder: ReminderBlock, *, interrupt: bool = False) -> None:
        """事件路由把硬件事件交给正在运行的会话（I04）；用户插话也走这里。"""
        if not self.features.events_inject and reminder.source == "device_event":
            self.trace.record("inject_dropped", source=reminder.source, reason="events.inject=off")
            return
        if reminder.source == "checkpoint":
            # 两轮之间回退了好几次：只有最后一次的状态有意义，前面的说明已经过时（2026-10-05 真机：三条都交给了 agent）
            stale = [r for r in self._pending if r.source == "checkpoint"]
            if stale:
                self._pending = [r for r in self._pending if r.source != "checkpoint"]
                reminder = ReminderBlock(source="checkpoint", text=(
                    f"(The user restored checkpoints {len(stale) + 1} times since your last turn; only the final state "
                    "below matters.) " + reminder.text))
        self._pending.append(reminder)
        self.trace.record("inject", source=reminder.source, text=reminder.text[:2000], interrupt=interrupt)
        if interrupt and self.features.events_interrupt:
            self._interrupt.set()

    async def _set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status  # type: ignore[assignment]
            await self.emit({"sessionUpdate": "_fwr/status", "status": status})

    async def emit(self, update: dict[str, Any]) -> None:
        if self.store:
            self.store.append_ui(update)
        if self._emit:
            await self._emit(update)

    async def prompt(self, text: str, images: list[ImageBlock] | None = None) -> TurnResult:
        async with self._lock:
            return await self._run_turn(text, images or [])

    # ------------------------------------------------------------------ 一轮

    def _append(self, msg: Message) -> None:
        self.history.append(msg)
        if self.store:
            self.store.append_message(msg)

    async def _run_turn(self, text: str, images: list[ImageBlock]) -> TurnResult:
        if self.readonly:
            return TurnResult(stop_reason="readonly", text="", steps=0, error=self.readonly)
        if self.checkpoints and self.features.checkpoint_enabled:
            try:  # 第一轮之前记下起点（序号 0）；ACP 新建会话时已经记过，这里是脚本入口的兜底
                await self.checkpoints.ensure_base()
            except Exception as e:
                self.trace.record("error", where="checkpoint", message=str(e))
        self._turn_firmware = None
        self.trace.turn += 1
        self.trace.step = 0
        cancel = CancelToken()
        self._turn_cancel = cancel
        await self._set_status("running")
        t0 = time.monotonic()
        content: list[Block] = []
        if not self.history:
            content += self._safe_preface(text)
        content += [*images, TextBlock(text=text)]
        user_msg = Message(role="user", content=content)
        self._append(user_msg)
        self.trace.record("turn_start", text=text, images=len(images), mode=self.mode, model=self.model)
        await self.emit({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}})

        usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        guard_key: str | None = None
        guard_run = 0
        final_text = ""
        stop_reason = "end_turn"
        error: str | None = None
        step = 0
        try:
            for step in range(1, self.max_steps + 1):
                self.trace.step = step
                self._interrupt.clear()
                if injected := self._drain():
                    self._append(Message(role="user", content=[*injected]))
                    for r in injected:
                        await self.emit({"sessionUpdate": "_fwr/injected", "source": r.source, "text": r.text})

                tools = self.registry.specs(read_only_only=self.mode == "plan")
                window = self.backend.caps.context_window
                used = self.usage.current(self.history, self.system_prompt, tools)
                if self.features.context_compaction and should_compact(used, window):
                    await self._try_compact("auto", cancel, used=used)
                req = ModelRequest(model=self.model, system=self.system_prompt, messages=self.history, tools=tools,
                                   effort=self.effort)
                self.trace.record("model_request", messages=len(req.messages), tools=len(req.tools))
                blocks, stop, step_usage, err = await self._sample(req, cancel)
                for k in usage:
                    usage[k] += getattr(step_usage, k) if step_usage else 0
                self.usage.observe(step_usage, len(self.history))
                if step_usage and step_usage.input_tokens:
                    await self.emit({"sessionUpdate": "_fwr/context", "used": step_usage.input_tokens,
                                     "window": window})
                if err and not blocks and is_context_overflow(err) and self.features.context_compaction \
                        and await self._try_compact("overflow", cancel, used=used):
                    # 模型说上下文超长：压缩后重试这一步（只重试一次：压缩后的历史很短）
                    req = ModelRequest(model=self.model, system=self.system_prompt, messages=self.history, tools=tools,
                                       effort=self.effort)
                    blocks, stop, step_usage, err = await self._sample(req, cancel)
                    self.usage.observe(step_usage, len(self.history))
                if err:
                    error = err.message
                    stop_reason = "model_error"
                    self.trace.record("error", where="model", message=err.message, status=err.status)
                    if blocks:
                        self._append(Message(role="assistant", content=blocks))
                    break
                assistant = Message(role="assistant", content=blocks or [TextBlock(text="")])
                calls = assistant.tool_calls()
                self._append(assistant)
                final_text = assistant.text()
                self.trace.record(
                    "model_response",
                    stop=stop,
                    text=final_text[:4000],
                    tool_calls=[{"id": c.id, "name": c.name, "args": c.arguments} for c in calls],
                    usage=step_usage.model_dump() if step_usage else None,
                )
                if cancel.cancelled or stop == "cancelled":
                    stop_reason = "cancelled"
                    break
                if not calls:
                    if self._pending:  # 回复的同时设备出事了：继续一步，让模型看到事件
                        continue
                    stop_reason = "end_turn"
                    break

                results = await self._run_tools(calls, cancel)
                tool_msg = Message(role="tool", content=[*results])
                # 防原地打转（grok IdenticalToolCallRun）
                key = json.dumps([(c.name, c.arguments) for c in calls], sort_keys=True, ensure_ascii=False)
                guard_run = guard_run + 1 if key == guard_key else 1
                guard_key = key
                self._append(tool_msg)
                if guard_run >= LOOP_STOP:
                    stop_reason = "loop_guard"
                    self.trace.record("loop_guard", action="stop", run=guard_run, calls=key[:500])
                    break
                if guard_run == LOOP_WARN:
                    self.trace.record("loop_guard", action="warn", run=guard_run, calls=key[:500])
                    self._pending.append(ReminderBlock(
                        source="loop_guard",
                        text=f"You have called the same tool with identical arguments {guard_run} times in a row; the result will not change. "
                        "Try a different approach, or stop and tell the user where you are stuck. Two more repeats will end this turn.",
                    ))
                if cancel.cancelled:
                    stop_reason = "cancelled"
                    break
            else:
                stop_reason = "max_steps"
        except Exception as e:  # 保底：任何异常都要让会话回到可用状态
            stop_reason = "error"
            error = f"{type(e).__name__}: {e}"
            self.trace.record("error", where="turn", message=error)
        finally:
            self._turn_cancel = None
        self.trace.record("turn_end", stop=stop_reason, steps=step, usage=usage,
                          duration_ms=int((time.monotonic() - t0) * 1000), error=error)
        await self._checkpoint(text, stop_reason)
        await self.emit({"sessionUpdate": "_fwr/turn_end", "stopReason": stop_reason, "usage": usage, "error": error})
        await self._set_status("idle")
        return TurnResult(stop_reason=stop_reason, text=final_text, steps=step, usage=usage, error=error)

    async def _checkpoint(self, prompt: str, stop: str) -> None:
        """本轮结束：把工作目录记成一个 checkpoint（D05）。任何结局（包括取消、出错）都记，回退时才有落脚点。"""
        if not self.checkpoints or not self.features.checkpoint_enabled:
            return
        try:
            e = await self.checkpoints.snapshot(kind="turn", turn=self.trace.turn, prompt=prompt, stop=stop,
                                                firmware=self._turn_firmware)
        except Exception as ex:  # git 出问题不能影响对话本身
            self.trace.record("error", where="checkpoint", message=f"{type(ex).__name__}: {ex}")
            return
        if e:
            self.trace.record("checkpoint", seq=e.seq, commit=e.commit, changed=e.changed, files=len(e.files),
                              firmware=e.firmware.sha256 if e.firmware else None)
            await self.emit({"sessionUpdate": "_fwr/checkpoint", "entry": e.model_dump(mode="json")})

    async def _on_flashed(self, res: Any, board: Any) -> None:
        """flash 工具成功后回调：存档固件，记到本轮的 checkpoint 上。"""
        if not self.checkpoints:
            return
        self._turn_firmware = await asyncio.to_thread(
            self.checkpoints.record_flash, turn=self.trace.turn, cwd=self.cwd, sha256=res.image_sha256,
            scope=res.scope, chip=getattr(board, "chip", None), board_id=getattr(board, "id", None), port=res.port)
        self.trace.record("firmware_archived", seq=self._turn_firmware.seq, sha256=res.image_sha256,
                          archive=self._turn_firmware.archive)

    # ------------------------------------------------------------------ 上下文（W6）

    def _default_preface(self, first_prompt: str) -> list[ReminderBlock]:
        rules = load_project_rules(self.cwd)
        return [rules] if rules else []

    def _safe_preface(self, first_prompt: str) -> list[ReminderBlock]:
        try:
            return self.preface(first_prompt)
        except Exception as e:  # skill / 记忆读取出错不能影响对话
            self.trace.record("error", where="preface", message=f"{type(e).__name__}: {e}")
            return self._default_preface(first_prompt)

    async def _try_compact(self, reason: str, cancel: CancelToken, *, used: int) -> bool:
        try:
            await self._compact(reason=reason, cancel=cancel, used=used)
            return True
        except Exception as e:  # 压缩失败不中断本轮：照原样继续，超长时由模型报错
            self.trace.record("error", where="compaction", message=f"{type(e).__name__}: {e}")
            await self.emit({"sessionUpdate": "_fwr/compacted", "reason": reason, "ok": False, "error": str(e)})
            return False

    async def compact(self, user_context: str = "") -> dict[str, Any]:
        """手动压缩（界面上的按钮）。会话正在运行时不能压缩。"""
        if self.running:
            raise RuntimeError("The session is running; wait for it to finish or stop it first")
        async with self._lock:
            cancel = CancelToken()
            self._turn_cancel = cancel
            try:
                return await self._compact(reason="manual", cancel=cancel, user_context=user_context,
                                           used=self.usage.current(self.history, self.system_prompt,
                                                                   self.registry.specs()))
            finally:
                self._turn_cancel = None

    async def _compact(self, *, reason: str, cancel: CancelToken, used: int, user_context: str = "") -> dict[str, Any]:
        if len(self.history) < 2:
            return {"ok": False, "skipped": "The conversation is too short to compact"}
        t0 = time.monotonic()
        await self.emit({"sessionUpdate": "_fwr/compacting", "reason": reason, "used": used,
                         "window": self.backend.caps.context_window})
        seg = None
        if self.compaction_dir is not None:
            seg = write_segment(self.compaction_dir, self.history, title=f"Session {self.id}", turn=self.trace.turn)
        tools = self.registry.specs(read_only_only=self.mode == "plan")
        summary, s_usage = await summarize(self.backend, model=self.model, system=self.system_prompt,
                                           history=self.history, tools=tools,
                                           window=self.backend.caps.context_window, cancel=cancel,
                                           user_context=user_context)
        preface = self._safe_preface(last_user_text(self.history))
        try:
            state = self.state_fn()
        except Exception:
            state = ""
        new = build_compacted(summary=summary, segment_loc=str(self.compaction_dir) if seg else None,
                              preface=preface, state=state, last_user=last_user_text(self.history))
        before = len(self.history)
        self.history = new
        if self.store:
            self.store.rewrite_history(new)
        self.usage.reset()
        after = self.usage.current(self.history, self.system_prompt, tools)
        info = {"ok": True, "reason": reason, "before": used, "after": after, "messages": before,
                "segment": seg.name if seg else None, "summaryChars": len(summary),
                "durationMs": int((time.monotonic() - t0) * 1000),
                "usage": s_usage.model_dump() if s_usage else None}
        self.trace.record("compaction", **info)
        await self.emit({"sessionUpdate": "_fwr/compacted", **info})
        return info

    def _drain(self) -> list[ReminderBlock]:
        out, self._pending = self._pending, []
        out = [r for r in out if not (r.event_id and r.event_id in self.delivered_events)]
        self.injected_events.update(r.event_id for r in out if r.event_id)
        return out

    # ------------------------------------------------------------------ 调用模型

    async def _sample(
        self, req: ModelRequest, cancel: CancelToken
    ) -> tuple[list[Block], str, Usage | None, ModelError | None]:
        text_parts: list[str] = []
        reasoning: list[str] = []
        calls: list[ToolCallBlock] = []
        usage: Usage | None = None
        stop = "end_turn"
        announced: set[int] = set()
        async for ev in self.backend.stream(req, cancel=cancel):
            if isinstance(ev, TextDelta):
                text_parts.append(ev.text)
                await self.emit({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": ev.text}})
            elif isinstance(ev, ReasoningDelta):
                reasoning.append(ev.text)
                await self.emit({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": ev.text}})
            elif isinstance(ev, ToolCallDelta):
                if ev.index not in announced and ev.name:
                    announced.add(ev.index)
            elif isinstance(ev, ToolCallDone):
                calls.append(ev.call)
            elif isinstance(ev, Usage):
                usage = ev
                self.last_usage = ev
            elif isinstance(ev, Stop):
                stop = ev.reason
            elif isinstance(ev, ModelError):
                blocks = self._blocks(text_parts, reasoning, [])
                return blocks, "error", usage, ev
        caps = self.backend.caps
        if caps.enforce_window and usage is not None and usage.input_tokens > caps.context_window:
            # 强制窗口（评测）：真实的小窗口模型会在生成前拒绝这个请求；这里丢掉回复、按同样的错误处理
            # （压缩开着 → 压缩后重试；关着 → 本轮以 model_error 结束）
            self.trace.record("window_enforced", input_tokens=usage.input_tokens, window=caps.context_window)
            return [], "error", usage, ModelError(
                status=400, message=f"This model's maximum context length is {caps.context_window} tokens, but the "
                                    f"request has {usage.input_tokens} input tokens (enforced window)")
        return self._blocks(text_parts, reasoning, calls), stop, usage, None

    @staticmethod
    def _blocks(text: list[str], reasoning: list[str], calls: list[ToolCallBlock]) -> list[Block]:
        blocks: list[Block] = []
        if reasoning:
            blocks.append(ReasoningBlock(text="".join(reasoning)))
        if text:
            blocks.append(TextBlock(text="".join(text)))
        blocks += calls
        return blocks

    # ------------------------------------------------------------------ 执行工具

    def _title(self, tool: Tool | None, call: ToolCallBlock) -> str:
        a = call.arguments
        hint = a.get("description") or a.get("path") or a.get("command") or a.get("pattern") or ""
        hint = str(hint).replace("\n", " ")
        return f"{call.name} {hint[:80]}".strip()

    async def _run_tools(self, calls: list[ToolCallBlock], cancel: CancelToken) -> list[ToolResultBlock]:
        prepared: list[tuple[ToolCallBlock, Tool | None, Any, ToolResult | None]] = []
        # ---- 1. 解析参数 + 权限（逐个、按顺序：审批要一个一个问）
        for call in calls:
            tool = self.registry.get(call.name)
            await self.emit({
                "sessionUpdate": "tool_call", "toolCallId": call.id, "title": self._title(tool, call),
                "kind": TOOL_KIND.get(call.name, "other"), "status": "pending", "rawInput": call.arguments,
            })
            if tool is None:
                prepared.append((call, None, None, ToolResult.error(f"No tool named {call.name}")))
                continue
            if call.raw_arguments is not None:
                prepared.append((call, tool, None, ToolResult.error(
                    f"Tool arguments are not a valid JSON object: {call.raw_arguments[:500]}")))
                continue
            try:
                args = tool.parse(call.arguments)
            except ValidationError as e:
                prepared.append((call, tool, None, ToolResult.error(validation_message(e))))
                continue
            decision = await self._check_permission(call, tool, args)
            if decision.action != "allow":
                prepared.append((call, tool, args, ToolResult.error(f"Not executed: {decision.reason}")))
                continue
            prepared.append((call, tool, args, None))
            if cancel.cancelled:
                break

        # ---- 2. 并发执行，按锁串行
        locks: dict[str, asyncio.Lock] = {}

        def lock_for(key: str) -> asyncio.Lock:
            return locks.setdefault(key, asyncio.Lock())

        async def one(call: ToolCallBlock, tool: Tool, args: Any) -> ToolResult:
            keys: list[str] = []
            if tool.caps.lock == "session":
                keys.append("__session__")
            keys += tool.lock_keys(args, self._ctx(call, cancel))
            if tool.caps.device == "exclusive":
                keys.append(f"__device__:{self.board_id}")
            acquired: list[asyncio.Lock] = []
            try:
                for k in sorted(set(keys)):
                    lk = lock_for(k)
                    await lk.acquire()
                    acquired.append(lk)
                return await self._execute(call, tool, args, cancel)
            finally:
                for lk in reversed(acquired):
                    lk.release()

        tasks: dict[str, asyncio.Task[ToolResult]] = {}
        for call, tool, args, early in prepared:
            if early is None and tool is not None:
                tasks[call.id] = asyncio.create_task(one(call, tool, args))
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

        out: list[ToolResultBlock] = []
        for call, _tool, _args, early in prepared:
            if early is not None:
                res = early
            else:
                t = tasks[call.id]
                exc = t.exception() if t.done() and not t.cancelled() else None
                res = ToolResult.error(f"Internal tool error: {type(exc).__name__}: {exc}") if exc else t.result()
            out.append(ToolResultBlock(call_id=call.id, name=call.name, content=res.content, is_error=res.is_error))
            self.trace.record("tool_result", id=call.id, name=call.name, is_error=res.is_error,
                              text=res.text_content()[:4000], meta=_small(res.meta))
            await self.emit({
                "sessionUpdate": "tool_call_update", "toolCallId": call.id,
                "status": "failed" if res.is_error else "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": res.text_content()[:20000]}}],
                "rawOutput": res.meta,
            })
        # 未执行到的调用（取消）也要给结果，否则协议上 assistant 的 tool_calls 没有对应
        done_ids = {b.call_id for b in out}
        for call in calls:
            if call.id not in done_ids:
                out.append(ToolResultBlock(call_id=call.id, name=call.name,
                                           content=[TextBlock(text="Not executed: this turn was cancelled")], is_error=True))
        return out

    def _ctx(self, call: ToolCallBlock, cancel: CancelToken) -> ToolContext:
        async def progress(text: str) -> None:
            await self.emit({"sessionUpdate": "tool_call_update", "toolCallId": call.id, "status": "in_progress",
                             "content": [{"type": "content", "content": {"type": "text", "text": text}}]})

        async def human(action: HumanAction) -> HumanReply:
            if not self.ask_human_cb:
                return HumanReply(done=False, note="no UI attached; cannot ask for a manual step")
            await self._set_status("awaiting_human")
            self.trace.record("ask_human", title=action.title, instructions=action.instructions)
            try:
                reply = await self.ask_human_cb(action)
            finally:
                await self._set_status("running")
            self.trace.record("human_reply", done=reply.done, note=reply.note)
            return reply

        return ToolContext(session_id=self.id, cwd=self.cwd, cancel=cancel, trace=self.trace, call_id=call.id,
                           services=self.services, board_id=self.board_id, progress_cb=progress,
                           ask_human_cb=human, extra={"delivered_events": self.delivered_events,
                                                          "injected_events": self.injected_events,
                                                          "on_flashed": self._on_flashed, "session": self})

    async def _execute(self, call: ToolCallBlock, tool: Tool, args: Any, turn_cancel: CancelToken) -> ToolResult:
        """执行一个工具。interruptible 的工具在有关键事件注入时被打断（D10）。"""
        cancel = CancelToken()
        watchers: list[asyncio.Task] = [asyncio.create_task(turn_cancel.wait())]
        if tool.caps.interruptible and self.features.events_interrupt:
            watchers.append(asyncio.create_task(self._interrupt.wait()))

        async def watch() -> None:
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
            reason = "this turn was cancelled" if watchers[0] in done else INTERRUPT_REASON
            cancel.cancel(reason)

        watcher = asyncio.create_task(watch())
        self._running_tools[call.id] = (tool.name, cancel)
        ctx = self._ctx(call, cancel)
        await self.emit({"sessionUpdate": "tool_call_update", "toolCallId": call.id, "status": "in_progress"})
        self.trace.record("tool_call", id=call.id, name=call.name, args=call.arguments)
        t0 = time.monotonic()
        try:
            res = await tool.run(ctx, args)
        finally:
            self._running_tools.pop(call.id, None)
            watcher.cancel()
            for w in watchers:
                w.cancel()
        if cancel.cancelled and cancel.reason == USER_INTERRUPT:
            self.trace.record("user_interrupt", id=call.id, name=call.name)
            note = ("\n(The user stopped this step before it finished. If they sent a note, it is in the next "
                    "system-reminder; follow it rather than retrying the same thing.)")
            res = ToolResult(content=[*res.content, TextBlock(text=note)], is_error=True, meta=res.meta)
        elif cancel.cancelled and cancel.reason == INTERRUPT_REASON:
            self.trace.record("interrupt", id=call.id, name=call.name)
            note = "\n(wait interrupted: a new device event arrived, see the next system-reminder)"
            res = ToolResult(content=[*res.content, TextBlock(text=note)], is_error=res.is_error, meta=res.meta)
        res.meta.setdefault("duration_ms", int((time.monotonic() - t0) * 1000))
        return res

    async def _check_permission(self, call: ToolCallBlock, tool: Tool, args: Any) -> Decision:
        ps = await ps_analyze(args.command) if tool.name == "shell" else None
        decision = self.permissions.evaluate(tool, args, mode=self.mode, cwd=self.cwd, ps=ps)
        reply: str | None = None
        if decision.action == "ask":
            if self.ask_permission_cb is None:
                decision = Decision("deny", f"Needs user approval, but no UI is attached ({decision.reason})", decision.risk)
            else:
                await self._set_status("awaiting_approval")
                req = PermissionRequest(
                    tool_call_id=call.id, tool=tool.name, title=self._title(tool, call), reason=decision.reason,
                    risk=decision.risk, subjects=decision.details.get("subjects", []), arguments=call.arguments,
                )
                try:
                    reply = await self.ask_permission_cb(req)
                finally:
                    await self._set_status("running")
                if reply == "allow_always" and "outside_write" in decision.details:
                    # 工作目录以外的写：只把那些目录加进本会话的可写区域（建链接的不加，每次都问）
                    if self.permissions.paths is not None:
                        for p in decision.details["outside_write"]:
                            if "$" not in p:
                                self.permissions.paths.add_session_write_root(p if Path(p).is_dir() else Path(p).parent)
                elif reply == "allow_always" and "unanalyzable" in decision.details:
                    # 分析不了的命令：只放行完全相同的命令（不按命令名前缀放行，前缀没有意义）
                    self.permissions.session_exact.add(decision.details["unanalyzable"])
                elif reply == "allow_always" and "outside" in decision.details:
                    # 工作目录以外的读取：只把那个目录加进本会话的可读区域，不按工具整体放行
                    if self.permissions.paths is not None:
                        for p in decision.details["outside"]:
                            self.permissions.paths.add_session_root(p if Path(p).is_dir() else Path(p).parent)
                elif reply == "allow_always":
                    for rule in session_rules(tool.name, req.subjects):
                        self.permissions.add_session_allow(rule)
                if reply in ("allow_once", "allow_always"):
                    decision = Decision("allow", f"Approved by the user ({reply})", decision.risk)
                else:
                    decision = Decision("deny", "The user denied this call", decision.risk)
        self.trace.record("permission", id=call.id, name=tool.name, action=decision.action, reason=decision.reason,
                          risk=decision.risk, reply=reply)
        return decision


def session_rules(tool: str, subjects: list[str]) -> list[str]:
    """用户点"本会话都允许"后加入的规则。shell 按"命令名 + 子命令"前缀放行，其他工具整体放行。"""
    if tool != "shell":
        return [tool]
    rules = []
    for s in subjects:
        words = s.split()
        if not words:
            continue
        prefix = words[0]
        if len(words) > 1 and not words[1].startswith("-"):
            prefix += " " + words[1]
        rules.append(f"shell({prefix}:*)")
    return rules


def _small(meta: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in meta.items():
        s = json.dumps(v, ensure_ascii=False, default=str)
        out[k] = v if len(s) < 4000 else s[:4000] + "…"
    return out
