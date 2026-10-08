"""会话内的子 agent（I12，§6.3）。参照 grok（docs/user-guide/16-subagents.md、xai-grok-agent/src/config.rs 的 builtin_subagents）：

- spawn_subagent 工具：父 agent 把一件事交给子 agent，子 agent 有**自己的上下文**，做完把结论交回来，
  父 agent 的上下文里只多一条结果（grok："delegate work without consuming its own context"）。
- 内置三种类型（grok 同名）：
    general   全部工具（除了再派子 agent），权限模式跟父会话一样
    explore   只读：读文件、搜索、看工程 / 设备状态、读串口日志，不能改文件、不能跑 shell
    plan      只读，最后输出一份结构化的实现计划
  goal 模式另外用到两个内部类型：planner（写目标计划）和 verifier（独立验证，见 goal.py）。
- 和父会话共享工作目录（grok 默认 isolation=none）和绑定的板子：设备访问照样经过设备管理器的独占锁（CLAUDE.md：
  子 agent 访问设备也经过设备管理器，与所属会话共享设备归属）。
- 审批和人工操作请求转给父会话的界面，标题前面标出是哪个子 agent。
- 深度只有一层：子 agent 不能再派子 agent（grok 的 max_depth 默认也很小）。
- 默认前台运行：父 agent 等它结束。同一步里派多个子 agent 时，它们并发执行。
- **后台运行**（2026-10-05，`background=true`，参照 grok 默认后台运行）：工具立刻返回 id，子 agent 在后台跑。
  结果的送达和硬件事件走同一套规则（I04）：父会话正在执行 → 报告作为 system-reminder 注入下一步；
  父会话空闲 → 不自动开新一轮，报告排队等下一轮，界面上提示并给"让 agent 继续"按钮，由用户决定。
  父 agent 可以用 check_subagents 看进度或等待（等待能被硬件事件打断），stop_subagent 停掉。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ..model.types import ReminderBlock, ToolCallBlock
from ..tools.base import Tool, ToolCaps, ToolContext, ToolResult

if TYPE_CHECKING:
    from .agent import Session

SubagentType = Literal["general", "explore", "plan", "planner", "verifier"]

# 只读类型能用的工具（plan 模式本来就只给只读工具，这里再收窄一层，不让它碰 memory 写入、MCP 等）
READ_TOOLS = {"read_file", "list_dir", "grep", "project_status", "size", "read_log", "diagnose_crash", "skill",
              "memory_search", "memory_get", "search_tool"}
# explore 另外可以等设备输出（只读）：后台盯串口做稳定性观察是后台子 agent 的典型用法
EXPLORE_TOOLS = READ_TOOLS | {"await_marker"}
# 验证者：只读 + 编译 / 烧录 / 复位 / 等设备输出。不能改代码（§6.3："验证者不能修改代码"）
VERIFIER_TOOLS = READ_TOOLS | {"build", "flash", "reset", "await_marker"}

ROLE_PROMPT: dict[str, str] = {
    "general": """
## Your role: sub-agent (general)
A parent agent sent you to do one specific task. You have your own separate context. Do only this task; don't widen it.
When done, report in one concise message: what you did, which files you changed, what you verified on the device (say
what you did not verify), and what is still unresolved. The parent agent sees only your final report.""",
    "explore": """
## Your role: sub-agent (explore, read-only investigation)
You can only read: read files, search, inspect project and device state, read serial logs, and wait for device output
with await_marker. You cannot change files, build, flash or reset.
Finish with a concise conclusion: what you found, where (file:line), the evidence, and what is still uncertain. The
parent agent sees only your final conclusion.""",
    "plan": """
## Your role: sub-agent (plan, read-only planning)
First investigate the project read-only (code, sdkconfig, facts.toml, device state), then output an actionable
implementation plan: the goal, which files to change and how, how to verify each step on the device (build, flash,
expected serial output), and the risks. Do not modify any file.""",
}


class SpawnArgs(BaseModel):
    prompt: str = Field(description="Complete task brief for the sub-agent: what to do, relevant files and background, "
                                    "and what counts as done. The sub-agent cannot see your conversation, so be explicit")
    description: str = Field(description="Short task label (3–6 words), shown in the UI")
    subagent_type: Literal["general", "explore", "plan"] = Field(
        "general", description="general: can edit code, build and flash; explore: read-only investigation (can also "
                               "wait for device output); plan: read-only, outputs an implementation plan")
    background: bool = Field(False, description="true = run in the background: returns an id at once and you keep "
                                                "working; the report arrives later as a system-reminder (or use "
                                                "check_subagents). For long work you don't need to wait for, e.g. "
                                                "watching the device's serial output for several minutes")


ChildFactory = Callable[["Session", str, str], "Session"]


class SpawnSubagent(Tool):
    name = "spawn_subagent"
    description = (
        "Hand a self-contained task to a sub-agent. It has its own context and returns only its conclusion. Good for: "
        "broad read-only investigation (explore), planning before acting (plan), a well-bounded change (general). "
        "Several sub-agents spawned in the same step run concurrently. With background=true it runs while you keep "
        "working (at most 4 at a time). A sub-agent shares your working directory and board; it cannot spawn "
        "sub-agents itself. Don't edit the same files as a background general sub-agent.")
    Args = SpawnArgs
    caps = ToolCaps(risk="safe")  # 派出去本身没有风险；子 agent 的每个工具调用照常过权限检查

    def __init__(self, factory: ChildFactory) -> None:
        self.factory = factory

    def permission_subject(self, args: SpawnArgs) -> str:
        return args.subagent_type

    async def run(self, ctx: ToolContext, args: SpawnArgs) -> ToolResult:
        parent = ctx.extra.get("session")
        if parent is None:
            return ToolResult.error("This session cannot spawn sub-agents")
        if args.background:
            bg = background_of(parent, self.factory)
            try:
                run = bg.start(args.subagent_type, args.description, args.prompt)
            except RuntimeError as e:
                return ToolResult.error(str(e))
            return ToolResult.text(
                f"Started background sub-agent {run.id} ({args.subagent_type} · {args.description}). Keep working; its "
                "report will arrive as a system-reminder when it finishes. Use check_subagents to see its progress or "
                "wait for it, stop_subagent to stop it.", subagent={"id": run.id, "background": True})
        res = await run_child(self.factory, parent, args.subagent_type, args.description, args.prompt, ctx)
        return ToolResult.text(res["report"], is_error=res["stop"] not in ("end_turn",), subagent=res["meta"])


# ---------------------------------------------------------------- 后台子 agent


@dataclass
class BackgroundRun:
    id: str
    kind: str
    description: str
    child: Session
    started: float = field(default_factory=time.monotonic)
    task: asyncio.Task | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    result: dict[str, Any] | None = None
    delivered: bool = False  # 报告已经交给父 agent（工具结果或 reminder）
    waiters: int = 0  # 正在 check_subagents 里等它的调用：有人等，报告就由等的那一方交出去

    @property
    def running(self) -> bool:
        return self.result is None

    def progress(self) -> str:
        """一行进度：状态、用时、步数、最近一个工具调用。"""
        secs = int(time.monotonic() - self.started)
        if self.result is not None:
            return (f"{self.id} ({self.kind} · {self.description}): finished ({self.result['stop']}) after {secs} s, "
                    f"{self.result['meta']['steps']} steps")
        last = next((b.name for m in reversed(self.child.history) for b in reversed(m.content)
                     if isinstance(b, ToolCallBlock)), None)
        return (f"{self.id} ({self.kind} · {self.description}): running for {secs} s, step {self.child.trace.step}"
                + (f", last tool {last}" if last else ""))


class BackgroundRuns:
    """一个父会话的后台子 agent。"""

    MAX_RUNNING = 4

    def __init__(self, parent: Session, factory: ChildFactory) -> None:
        self.parent = parent
        self.factory = factory
        self.runs: dict[str, BackgroundRun] = {}

    def active(self) -> list[BackgroundRun]:
        return [r for r in self.runs.values() if r.running]

    def start(self, kind: str, description: str, prompt: str) -> BackgroundRun:
        if len(self.active()) >= self.MAX_RUNNING:
            raise RuntimeError(f"{self.MAX_RUNNING} background sub-agents are already running; wait for one "
                               "(check_subagents) or stop one (stop_subagent) first")
        child = self.factory(self.parent, kind, description)
        run = BackgroundRun(id=child.id, kind=kind, description=description, child=child)
        self.runs[run.id] = run
        run.task = asyncio.create_task(self._run(run, prompt))
        return run

    async def _run(self, run: BackgroundRun, prompt: str) -> None:
        try:
            res = await run_child(self.factory, self.parent, run.kind, run.description, prompt, child=run.child,
                                  background=True)
        except Exception as e:  # 子 agent 自己出错不能拖垮父会话
            res = {"report": f"[sub-agent · {run.kind} · {run.description}] failed: {type(e).__name__}: {e}",
                   "stop": "error", "text": "", "meta": {"id": run.id, "steps": run.child.trace.step}}
        run.result = res
        run.done.set()
        if run.delivered or run.waiters:  # 父 agent 正在 check_subagents 里等它：报告由那边交出去
            return
        run.delivered = True
        parent = self.parent
        parent.inject(ReminderBlock(source="subagent", text=(
            f"Background sub-agent {run.id} finished. Its report:\n\n{res['report']}")))
        parent.trace.record("subagent_report", id=run.id, route="inject" if parent.running else "queued")
        if not parent.running:
            # 父会话空闲：不自动开新一轮（和空闲时的硬件事件一样，I04）。界面提示，由用户决定要不要继续
            await parent.emit({"sessionUpdate": "_fwr/subagent", "subagentId": run.id, "kind": run.kind,
                               "description": run.description, "status": "done", "background": True,
                               "awaitingParent": True, "stop": res["stop"], "steps": res["meta"].get("steps"),
                               "usage": res["meta"].get("usage"), "files": res["meta"].get("files"),
                               "text": res.get("text", "")[:4000]})

    async def wait(self, ids: list[str], timeout: float, cancel) -> None:
        """等其中任何一个结束（或超时 / 被打断）。等待期间结束的，报告由调用方交出去（见 _run）。"""
        targets = [r for r in self.runs.values() if r.id in ids and r.running]
        if not targets:
            return
        for r in targets:
            r.waiters += 1
        waits = [asyncio.ensure_future(r.done.wait()) for r in targets]
        stop = asyncio.ensure_future(cancel.wait())
        try:
            await asyncio.wait([*waits, stop], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in [*waits, stop]:
                w.cancel()
            for r in targets:
                r.waiters -= 1

    def stop(self, run_id: str, reason: str) -> bool:
        run = self.runs.get(run_id)
        if run is None or not run.running:
            return False
        run.child.cancel(reason)
        return True

    def stop_all(self, reason: str) -> None:
        for r in self.active():
            r.child.cancel(reason)


def background_of(parent: Session, factory: ChildFactory) -> BackgroundRuns:
    if parent.background is None:
        parent.background = BackgroundRuns(parent, factory)
    return parent.background


class CheckArgs(BaseModel):
    id: str | None = Field(None, description="Sub-agent id; omit for all background sub-agents of this session")
    wait_s: float = Field(0, description="Wait up to this many seconds for it (or any of them) to finish; 0 = just "
                                          "report progress. A device event ends the wait early. Max 600")


class CheckSubagents(Tool):
    name = "check_subagents"
    description = ("Show the progress of background sub-agents, or wait for them. A finished sub-agent's report is "
                   "returned here (once) if it has not been delivered yet.")
    Args = CheckArgs
    caps = ToolCaps(read_only=True, risk="safe", interruptible=True)

    async def run(self, ctx: ToolContext, args: CheckArgs) -> ToolResult:
        parent = ctx.extra.get("session")
        bg: BackgroundRuns | None = getattr(parent, "background", None)
        if bg is None or not bg.runs:
            return ToolResult.text("No background sub-agents in this session.")
        runs = [bg.runs[args.id]] if args.id in bg.runs else list(bg.runs.values()) if args.id is None else []
        if not runs:
            return ToolResult.error(f"No background sub-agent {args.id!r}. Known: {', '.join(bg.runs)}")
        if args.wait_s > 0:
            await bg.wait([r.id for r in runs], min(args.wait_s, 600.0), ctx.cancel)
        lines = [r.progress() for r in runs]
        for r in runs:
            if r.result is not None and not r.delivered:
                r.delivered = True
                lines.append(f"\nReport from {r.id}:\n{r.result['report']}")
        return ToolResult.text("\n".join(lines))


class StopArgs(BaseModel):
    id: str = Field(description="Background sub-agent id")


class StopSubagent(Tool):
    name = "stop_subagent"
    description = "Stop a background sub-agent. Its partial report still arrives as usual."
    Args = StopArgs
    caps = ToolCaps(risk="safe")

    async def run(self, ctx: ToolContext, args: StopArgs) -> ToolResult:
        bg: BackgroundRuns | None = getattr(ctx.extra.get("session"), "background", None)
        if bg is None or not bg.stop(args.id, "stopped by the parent agent"):
            return ToolResult.error(f"No running background sub-agent {args.id!r}")
        return ToolResult.text(f"Stopping {args.id}.")


async def run_child(factory: ChildFactory, parent: Session, kind: str, description: str, prompt: str,
                    ctx: ToolContext | None = None,
                    on_done: Callable[[Session], Awaitable[None]] | None = None, *,
                    child: Session | None = None, background: bool = False) -> dict[str, Any]:
    """建一个子会话、跑一轮、收尾。spawn_subagent（前台 / 后台）和 goal 模式共用。"""
    child = child or factory(parent, kind, description)
    parent.trace.record("subagent_start", id=child.id, agent_type=kind, description=description, prompt=prompt[:2000],
                        background=background)
    await parent.emit({"sessionUpdate": "_fwr/subagent", "subagentId": child.id, "kind": kind,
                       "description": description, "status": "running", "background": background})
    watcher = None
    if ctx is not None:  # 父会话取消 → 子会话也取消
        async def watch() -> None:
            await ctx.cancel.wait()
            child.cancel(ctx.cancel.reason or "The parent session was cancelled")

        watcher = asyncio.create_task(watch())
    try:
        result = await child.prompt(prompt)
    finally:
        if watcher:
            watcher.cancel()
    changed = sorted({str(b.arguments.get("path")) for m in child.history for b in m.content
                      if isinstance(b, ToolCallBlock) and b.name in ("write_file", "edit_file")
                      and b.arguments.get("path")})
    meta = {"id": child.id, "kind": kind, "description": description, "stop": result.stop_reason,
            "steps": result.steps, "usage": result.usage, "files": changed}
    head = f"[sub-agent · {kind} · {description}] finished: {result.stop_reason}, {result.steps} steps"
    if changed:
        head += f", changed {', '.join(changed)}"
    report = f"{head}\n\n{result.text.strip() or '(the sub-agent gave no written conclusion)'}"
    if result.error:
        report += f"\n\nError: {result.error}"
    parent.trace.record("subagent_end", **{("agent_type" if k == "kind" else k): v for k, v in meta.items()})
    await parent.emit({"sessionUpdate": "_fwr/subagent", "subagentId": child.id, "kind": kind,
                       "description": description, "status": "done", "background": background, "stop": result.stop_reason,
                       "steps": result.steps, "usage": result.usage, "files": changed, "text": result.text[:4000]})
    if on_done:
        await on_done(child)
    return {"report": report, "stop": result.stop_reason, "text": result.text, "meta": meta, "child": child}
