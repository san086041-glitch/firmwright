"""goal 模式：规划者 → 执行者 → 独立验证者的循环（I12，§6.3，设计层 6 规划与验证）。

参照 grok 的 /goal（xai-grok-shell/src/session/goal_planner.rs、goal_classifier.rs、templates/goal_*.md）：
- 规划者在目标开始时运行一次（只读），把目标写成计划：验收标准（可观察的结果，3–5 条）、验证步骤、不做的事。
  执行者和验证者都以它为准（grok："the single source of truth for what was supposed to happen"）。
- 执行者就是这个会话本身，一轮一轮地做；做完调用 goal_report(complete)，卡住调用 goal_report(blocked)。
- 执行者声称完成时，交给**独立的验证者**：另一个子会话，看不到执行者的对话，只拿到目标、计划和执行者的说法；
  grok 用的是"adversarial verification"——执行者自己说完成不算数。
- 验证没通过 → 把缺口交回执行者，下一轮继续；通过 → 完成；无法验证（例如需要用户动手）→ 暂停，等用户。

嵌入式的部分（新提）：
- 规划者被要求把能在设备上观察的标准写成串口上的期望行 / 失败行（和 facts.toml、await_marker 对得上）。
- 验证者**不能改代码**，只能读、编译、烧录、复位、等设备输出（§6.3）。
- **设备判据强制检查**（verify.device_oracle）：计划里有设备验证步骤时，验证者判"通过"必须在它自己的这次运行里
  有一次成功的 flash 和一次成功的 await_marker；否则不认这个"通过"，改判为未通过（trace 里记 oracle_override）。
  这是把"以设备上的真实运行结果为准"从提示词里的要求变成了程序检查。

进展判断和每轮记录（2026-10-05，对应 grok 的 stop detector / summarizer，但不再调一次模型）：
- 每轮结束由系统记一条 RoundNote：改了哪些文件、代码的树哈希、编译 / 烧录结果、设备上 await_marker 的结论、
  注入的崩溃数、验证结论和通过条数、执行者最后一段话。全部来自本轮的工具结果和 checkpoint，是测出来的事实。
- 下一轮给执行者的指令、给验证者的提示里附上这份进展记录（验证者被告知：结果是事实，执行者的话是说法）。
- 进展判断（verify.progress_judge）：一轮算"有进展"＝代码到了一个没出现过的状态，或者通过的验收标准比以前都多，
  或者设备上出现了没见过的表现。连续 STALL_ROUNDS 轮没有进展就暂停目标并写明原因，不把轮数耗完。
  "回到以前出现过的代码状态"不算进展——能抓住改了又改回去的循环。规则判断而不是再问模型：可解释、不花 token。

没有照搬的 grok 部分：strategist / 多个验证者并行 / token 预算的精细控制（Deviation）。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from ..model.types import Message, ReminderBlock, TextBlock, ToolCallBlock, ToolResultBlock
from ..tools.base import Tool, ToolCaps, ToolContext, ToolResult
from .subagent import ROLE_PROMPT, ChildFactory, run_child

STALL_ROUNDS = 2  # 连续这么多轮没有进展 → 暂停
CRASH_KINDS = {"panic", "abort", "assert", "stack_overflow", "stack_smash", "reboot_loop", "wdt_reset", "brownout"}

if TYPE_CHECKING:
    from .agent import Session

GoalStatus = Literal["planning", "working", "verifying", "done", "paused", "failed", "stopped"]

ROLE_PROMPT["planner"] = """
## Your role: goal planner (goal mode)
You run once when the goal starts and turn the user's goal into a plan. The implementer and the independent verifier
both treat it as the source of truth.
First investigate the project read-only (code, sdkconfig, .firmwright/facts.toml, device state). Do not modify any file.
Rules for the plan:
- Acceptance criteria describe observable outcomes, not how the code should be written (no file names, function names
  or structure). 3–5 criteria, each independently checkable.
- Embedded: anything observable on the device must be written as a device outcome: which serial line must appear
  (the expected line, as a regex, e.g. `TEST:led_period:PASS`) and which must not (failure lines, crashes, reboot
  loops). If the firmware has no such marker line yet, the implementation steps must tell the implementer to add
  self-test code that prints it.
- Verification steps are what the verifier will do, each tagged: [device] build → flash → await_marker(expect=...,
  fail=...); [static] read the code / it builds. If something cannot be verified on the device, say why.
- Don't widen the scope: anything the user did not ask for goes under "Out of scope". Never swap what the user asked for
  with an easier substitute.
Output only this block and nothing outside it:
<plan>
# Plan: <one-line summary of the goal>
## Acceptance criteria
1. …
## Verification steps
1. [device] …
## Out of scope
- …
## Implementation steps
- [ ] …
## Risks
- … (write "none" if there are none)
</plan>"""

ROLE_PROMPT["verifier"] = """
## Your role: independent verifier (goal mode)
The implementer claims the goal is done. Decide independently whether each acceptance criterion is really met. You
**cannot modify code**: you can only read code, build, flash, reset, wait for device output and read serial logs.
- The implementer's account is a claim, not evidence. Every criterion needs evidence you obtained yourself in this run
  (tool output).
- Device criteria: you must build → flash → await_marker yourself, see the expected line, and see no failure lines or
  crashes. "The code looks like it should work" is not a pass.
- No evidence, no pass. If the board is not connected or a human must act and you cannot, return unverifiable and say
  what is missing.
- Don't relax the bar because the implementer did a lot of work, and don't fail it because the approach differs from
  yours: judge only the outcomes in the acceptance criteria.
Output only this block:
<verdict>{"verdict": "pass or fail or unverifiable", "criteria": [{"id": 1, "pass": true, "evidence": "what you saw"}], "gaps": ["what is not met and what the implementer should do next"]}</verdict>"""

IMPLEMENTER_RULES = """Rules:
- Follow the plan; after changes, confirm on the device (build → flash → await_marker).
- When every acceptance criterion is met and you have seen the expected output on the device yourself, call
  goal_report(status="complete", summary=what you did, evidence=what you saw), then end this round.
- If you are stuck, need the user to decide or act, or the goal contradicts itself, call
  goal_report(status="blocked", summary=the reason).
- Don't claim completion before you are done: an independent verifier will rebuild, reflash and check every criterion."""


class RoundNote(BaseModel):
    """一轮的进展记录（系统从工具结果和 checkpoint 里测出来的，不是模型写的）。"""

    round: int
    files: list[str] = Field(default_factory=list)  # 本轮改动的文件
    tree: str | None = None  # 本轮结束时代码的树哈希（有 checkpoint 时）
    builds: int = 0
    build_ok: bool | None = None  # 最后一次编译
    flashes: int = 0
    flash_ok: bool | None = None  # 最后一次烧录
    device: Literal["pass", "fail", "crash", "timeout", "interrupted", "none"] = "none"  # 最后一次 await_marker
    device_line: str = ""  # 命中的期望行 / 失败行 / 崩溃摘要
    crashes: int = 0  # 本轮注入的崩溃类设备事件
    report: str | None = None  # 执行者的 goal_report：complete / blocked / None（没报告）
    verdict: str | None = None  # 验证者：pass / fail / unverifiable
    passed: int | None = None  # 验证者认可的验收标准条数
    note: str = ""  # 执行者本轮最后一段话（截断；是说法，不是证据）
    progress: bool = True
    why: str = ""  # 进展判断的理由

    def line(self) -> str:
        parts = [f"Round {self.round}:"]
        parts.append(f"changed {', '.join(self.files[:6])}" + ("…" if len(self.files) > 6 else "") if self.files
                     else "no code changes")
        if self.builds:
            parts.append(f"build {'ok' if self.build_ok else 'failed'}")
        if self.flashes:
            parts.append(f"flash {'ok' if self.flash_ok else 'failed'}")
        if self.device != "none":
            parts.append(f"device {self.device}" + (f" ({self.device_line[:80]})" if self.device_line else ""))
        if self.crashes:
            parts.append(f"{self.crashes} crash event{'s' if self.crashes > 1 else ''}")
        if self.verdict:
            parts.append(f"verifier {self.verdict}" + (f" {self.passed} criteria met" if self.passed is not None else ""))
        elif self.report is None:
            parts.append("no goal_report")
        if not self.progress:
            parts.append(f"NO PROGRESS: {self.why}")
        return " · ".join(parts)


class GoalState(BaseModel):
    objective: str
    status: GoalStatus = "planning"
    plan: str = ""
    criteria: list[str] = Field(default_factory=list)
    device_steps: bool = False  # 计划里有 [设备] 验证步骤
    round: int = 0
    max_rounds: int = 5
    verdicts: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[RoundNote] = Field(default_factory=list)  # 每轮的进展记录
    stalled: int = 0  # 连续没有进展的轮数
    report: dict[str, Any] | None = None  # 执行者最近一次 goal_report
    message: str = ""  # 结束 / 暂停的原因
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    ended_at: str | None = None
    usage: dict[str, int] = Field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0})


def parse_plan(text: str) -> tuple[str, list[str], bool]:
    m = re.search(r"<plan>(.*?)(?:</plan>|$)", text, re.S)
    plan = (m.group(1) if m else text).strip()
    sec = re.search(r"##\s*(?:Acceptance criteria|验收标准)\s*\n(.*?)(?=\n##\s|\Z)", plan, re.S | re.I)
    criteria = [re.sub(r"^\s*\d+[.、)]\s*", "", line).strip()
                for line in (sec.group(1).splitlines() if sec else []) if re.match(r"^\s*\d+[.、)]", line)]
    steps = re.search(r"##\s*(?:Verification steps|验证步骤)\s*\n(.*?)(?=\n##\s|\Z)", plan, re.S | re.I)
    device = bool(steps and re.search(r"\[(?:device|设备)\]|await_marker|flash", steps.group(1), re.I))
    return plan, criteria, device


def parse_verdict(text: str) -> dict[str, Any]:
    m = re.search(r"<verdict>(.*?)(?:</verdict>|$)", text, re.S)
    raw = (m.group(1) if m else text).strip()
    j = re.search(r"\{.*\}", raw, re.S)
    try:
        v = json.loads(j.group(0)) if j else {}
    except json.JSONDecodeError:
        v = {}
    verdict = str(v.get("verdict", "")).lower()
    if verdict not in ("pass", "fail", "unverifiable"):
        return {"verdict": "unverifiable", "criteria": [], "gaps": ["The verifier did not return a parseable verdict"], "raw": text[-2000:]}
    return {"verdict": verdict, "criteria": v.get("criteria") or [], "gaps": v.get("gaps") or []}


def _result_text(b: ToolResultBlock) -> str:
    return "".join(x.text for x in b.content if isinstance(x, TextBlock))


def collect_round(round_no: int, messages: list[Message], checkpoint: Any | None) -> RoundNote:
    """从这一轮新增的消息（和这一轮的 checkpoint）里测出进展记录。"""
    note = RoundNote(round=round_no)
    edited: list[str] = []
    calls: dict[str, ToolCallBlock] = {}
    last_text = ""
    for m in messages:
        for b in m.content:
            if isinstance(b, ToolCallBlock):
                calls[b.id] = b
            elif isinstance(b, ToolResultBlock):
                text = _result_text(b)
                if b.name in ("edit_file", "write_file") and not b.is_error:
                    call = calls.get(b.call_id)
                    path = str(call.arguments.get("path", "")) if call else ""
                    if path and path not in edited:
                        edited.append(path)
                elif b.name == "build":
                    note.builds += 1
                    note.build_ok = not b.is_error
                elif b.name == "flash":
                    note.flashes += 1
                    note.flash_ok = not b.is_error
                elif b.name == "await_marker":
                    first = text.strip().splitlines()[0] if text.strip() else ""
                    note.device = ("pass" if first.startswith("✓") else "fail" if first.startswith("✗ Failure")
                                   else "crash" if first.startswith("✗") else "timeout" if first.startswith("Timed out")
                                   else "interrupted" if first.startswith("Wait interrupted") else "none")
                    hit = re.search(r"(?:seen|line seen): (.+)", first)
                    note.device_line = (hit.group(1) if hit else first)[:200]
            elif isinstance(b, ReminderBlock) and b.source == "device_event":
                kind = re.match(r"\[device event\] (\w+)", b.text)
                if kind and kind.group(1) in CRASH_KINDS:
                    note.crashes += 1
            elif isinstance(b, TextBlock) and m.role == "assistant" and b.text.strip():
                last_text = b.text.strip()
    note.note = last_text[-600:]
    if checkpoint is not None:
        note.tree = checkpoint.tree
        note.files = list(checkpoint.files) if checkpoint.changed else []
    else:  # 不是 git 工程：只能按文件工具算（shell 改的看不到）
        note.files = edited
    return note


def _device_signature(n: RoundNote) -> tuple[str, str, bool] | None:
    if n.device == "none" and not n.crashes:
        return None
    line = re.sub(r"\(\d+\)|\d+(?:\.\d+)?\s*(?:ms|s)\b|0x[0-9a-fA-F]+", "#", n.device_line)  # 去掉时间戳 / 地址
    return n.device, line, n.crashes > 0


def device_evidence(child: Session) -> dict[str, bool]:
    """验证者自己这次运行里，有没有成功的 flash 和成功的 await_marker。"""
    ok = {"flash": False, "await_marker": False}
    names: dict[str, str] = {}
    for m in child.history:
        for b in m.content:
            if isinstance(b, ToolCallBlock):
                names[b.id] = b.name
            elif isinstance(b, ToolResultBlock) and b.name in ok and not b.is_error:
                ok[b.name] = True
    return ok


class GoalReportArgs(BaseModel):
    status: Literal["complete", "blocked"] = Field(description="complete: every acceptance criterion is met; blocked: stuck, the user is needed")
    summary: str = Field(description="What you did / where you are stuck")
    evidence: str = Field("", description="Evidence you saw on the device yourself (expected lines, tool output)")


class GoalReport(Tool):
    name = "goal_report"
    description = ("Goal mode only: report that the goal is done (an independent verifier will check it) or that "
                   "you are stuck and need the user.")
    Args = GoalReportArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def __init__(self, runner: GoalRunner) -> None:
        self.runner = runner

    async def run(self, ctx: ToolContext, args: GoalReportArgs) -> ToolResult:
        self.runner.state.report = args.model_dump()
        ctx.trace.record("goal_report", **args.model_dump())
        if args.status == "complete":
            return ToolResult.text("Recorded. After this round an independent verifier will check it. End this round now.")
        return ToolResult.text("Recorded as blocked; the goal will pause for the user. End this round now.")


class GoalRunner:
    def __init__(self, session: Session, factory: ChildFactory, objective: str, *, max_rounds: int = 5,
                 device_oracle: bool = True, progress_judge: bool = True, store_dir: Path | None = None) -> None:
        self.s = session
        self.factory = factory
        self.state = GoalState(objective=objective, max_rounds=max_rounds)
        self.device_oracle = device_oracle
        self.progress_judge = progress_judge
        self.dir = store_dir
        self.task: asyncio.Task | None = None
        self._stop = False
        # 进展判断用：出现过的代码状态、设备表现，以及到目前为止最多通过了几条验收标准
        self._trees: set[str] = set()
        self._device_seen: set[tuple[str, str, bool]] = set()
        self._best_passed = 0

    # ------------------------------------------------------------------ 状态

    async def _emit(self) -> None:
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "goal.json").write_text(self.state.model_dump_json(indent=2), "utf-8")
            if self.state.plan:
                (self.dir / "plan.md").write_text(self.state.plan, "utf-8")
        await self.s.emit({"sessionUpdate": "_fwr/goal", "goal": self.state.model_dump(mode="json")})

    def _add_usage(self, usage: dict[str, int] | None) -> None:
        for k in self.state.usage:
            self.state.usage[k] += (usage or {}).get(k, 0)

    async def _finish(self, status: GoalStatus, message: str) -> None:
        self.state.status = status
        self.state.message = message
        self.state.ended_at = datetime.now(UTC).isoformat()
        self.s.registry.remove("goal_report")
        self.s.trace.record("goal_end", status=status, message=message, rounds=self.state.round,
                            usage=self.state.usage)
        await self._emit()

    def start(self) -> asyncio.Task:
        self.task = asyncio.create_task(self.run())
        return self.task

    def stop(self) -> None:
        self._stop = True
        self.s.cancel("The user stopped the goal")

    # ------------------------------------------------------------------ 主流程

    async def run(self) -> GoalState:
        t0 = time.monotonic()
        self.s.trace.record("goal_start", objective=self.state.objective, max_rounds=self.state.max_rounds)
        try:
            await self._plan()
            if not self.state.criteria:
                return await self._end("failed", "The planner did not produce parseable acceptance criteria", t0)
            self.s.registry.add(GoalReport(self))
            feedback = ""
            while self.state.round < self.state.max_rounds:
                if self._stop:
                    return await self._end("stopped", "Stopped by the user", t0)
                self.state.round += 1
                self.state.status = "working"
                self.state.report = None
                await self._emit()
                start = len(self.s.history)
                r = await self.s.prompt(self._directive(feedback))
                self._add_usage(r.usage)
                if self._stop or r.stop_reason == "cancelled":
                    return await self._end("stopped", "Stopped by the user", t0)
                note = collect_round(self.state.round, self.s.history[start:], self._round_checkpoint())
                rep = self.state.report
                note.report = rep["status"] if rep else None
                if rep is None:
                    feedback = ("You ended the last round without calling goal_report. The goal is not finished: keep "
                                "going, and call goal_report when you are done.")
                    if stalled := await self._record(note):
                        return await self._end("paused", stalled, t0)
                    continue
                if rep["status"] == "blocked":
                    await self._record(note, judge=False)
                    return await self._end("paused", f"The implementer is blocked: {rep['summary']}", t0)
                verdict = await self._verify(rep)
                note.verdict = verdict["verdict"]
                note.passed = sum(1 for c in verdict.get("criteria", []) if c.get("pass"))
                if verdict["verdict"] == "pass":
                    await self._record(note, judge=False)
                    return await self._end("done", "The independent verifier confirmed every acceptance criterion", t0)
                if verdict["verdict"] == "unverifiable":
                    await self._record(note, judge=False)
                    return await self._end("paused", "Could not verify: " + "; ".join(verdict.get("gaps") or ["the verifier could not complete verification"]), t0)
                fails = [f"{c.get('id')}. {c.get('evidence', '')}" for c in verdict.get("criteria", []) if not c.get("pass")]
                feedback = ("The independent verifier says **not passed**.\nFailed criteria:\n" + ("\n".join(fails) or "(none listed)") +
                            "\nGaps reported by the verifier:\n" + "\n".join(f"- {g}" for g in verdict.get("gaps") or []) +
                            "\nKeep fixing, confirm on the device, then call goal_report again.")
                if stalled := await self._record(note):
                    return await self._end("paused", stalled, t0)
            return await self._end("failed", f"Used all {self.state.max_rounds} rounds without passing verification", t0)
        except Exception as e:
            return await self._end("failed", f"{type(e).__name__}: {e}", t0)

    async def _end(self, status: GoalStatus, message: str, t0: float) -> GoalState:
        await self._finish(status, message)
        self.s.trace.record("goal_duration", ms=int((time.monotonic() - t0) * 1000))
        return self.state

    async def _plan(self) -> None:
        self.state.status = "planning"
        await self._emit()
        prompt = f"The user's goal (verbatim):\n{self.state.objective}\n\nInvestigate the project, then write the plan."
        res = await run_child(self.factory, self.s, "planner", "write the goal plan", prompt)
        self._add_usage(res["meta"]["usage"])
        self.state.plan, self.state.criteria, self.state.device_steps = parse_plan(res["text"])
        self.s.trace.record("goal_plan", criteria=self.state.criteria, device_steps=self.state.device_steps)

    def _round_checkpoint(self) -> Any | None:
        """这一轮结束时记的 checkpoint（有 git 时）。"""
        ck = self.s.checkpoints
        if not ck or not ck.entries:
            return None
        last = ck.entries[-1]
        return last if last.turn == self.s.trace.turn else None

    def _progress_log(self) -> str:
        return "\n".join(n.line() for n in self.state.notes)

    async def _record(self, note: RoundNote, *, judge: bool = True) -> str | None:
        """记下这一轮；judge=True 时做进展判断。连续 STALL_ROUNDS 轮没有进展时返回暂停的理由。"""
        if judge and self.progress_judge:
            new_code = (note.tree not in self._trees) if note.tree else bool(note.files)
            better = note.passed is not None and note.passed > self._best_passed
            sig = _device_signature(note)
            new_device = sig is not None and sig not in self._device_seen
            note.progress = new_code or better or new_device
            if not note.progress:
                why = []
                if note.tree and note.tree in self._trees and note.files:
                    why.append("the code went back to a state from an earlier round")
                elif not note.files:
                    why.append("no code changes")
                else:
                    why.append("no new code state")
                why.append("no more criteria met than before" if note.passed is not None else "not verified")
                why.append("same device behavior as before" if sig else "no device check")
                note.why = ", ".join(why)
            self.state.stalled = 0 if note.progress else self.state.stalled + 1
        if note.tree:
            self._trees.add(note.tree)
        if (sig := _device_signature(note)) is not None:
            self._device_seen.add(sig)
        if note.passed is not None:
            self._best_passed = max(self._best_passed, note.passed)
        self.state.notes.append(note)
        self.s.trace.record("goal_round", **note.model_dump())
        await self._emit()
        if judge and self.progress_judge and self.state.stalled >= STALL_ROUNDS:
            self.s.trace.record("goal_stalled", rounds=self.state.stalled)
            return (f"No progress in the last {self.state.stalled} rounds ({note.why}). Paused instead of using up the "
                    "remaining rounds; look at the round log, then adjust the goal or give the agent a hint.")
        return None

    def _directive(self, feedback: str) -> str:
        if self.state.round == 1:
            return (f"[Goal mode] Goal: {self.state.objective}\n\nPlan (the acceptance criteria are hard requirements; an "
                    f"independent verifier will follow the verification steps):\n"
                    f"{self.state.plan}\n\n{IMPLEMENTER_RULES}")
        log = self._progress_log()
        warn = ("\nWarning: the last round made no measurable progress. Try a different approach instead of repeating "
                "the same change; if you cannot, call goal_report(status=\"blocked\") and say what you need."
                if self.state.stalled else "")
        return (f"[Goal mode · round {self.state.round} of {self.state.max_rounds}] {feedback}"
                + (f"\n\nProgress so far (measured by Firmwright):\n{log}" if log else "") + warn)

    async def _verify(self, rep: dict[str, Any]) -> dict[str, Any]:
        self.state.status = "verifying"
        await self._emit()
        log = self._progress_log()
        prompt = (f"Goal (user's words): {self.state.objective}\n\nPlan:\n{self.state.plan}\n\n"
                  f"The implementer's claim (a claim, not evidence):\n{rep.get('summary', '')}\n"
                  f"Evidence the implementer cites: {rep.get('evidence', '') or '(none)'}\n\n"
                  + (f"Earlier rounds (outcomes measured by Firmwright; still check everything yourself):\n{log}\n\n"
                     if log else "")
                  + "Verify every acceptance criterion independently.")
        res = await run_child(self.factory, self.s, "verifier", f"verify round {self.state.round}", prompt)
        self._add_usage(res["meta"]["usage"])
        verdict = parse_verdict(res["text"])
        ev = device_evidence(res["child"])
        verdict["device"] = ev
        if (verdict["verdict"] == "pass" and self.device_oracle and self.state.device_steps
                and not (ev["flash"] and ev["await_marker"])):
            # 设备判据：计划要求在设备上验证，验证者却没有自己烧录并观察到结果 → 不认这个"通过"
            self.s.trace.record("oracle_override", verdict="pass", device=ev)
            verdict["verdict"] = "fail"
            verdict["oracle_override"] = True
            verdict["gaps"] = [*verdict.get("gaps", []),
                               "The verifier passed it, but in this run it did not flash and observe the device output with await_marker "
                               "itself (device oracle not satisfied)"]
        verdict["round"] = self.state.round
        verdict["subagent"] = res["meta"]["id"]
        self.state.verdicts.append(verdict)
        self.s.trace.record("goal_verdict", **{k: v for k, v in verdict.items() if k != "raw"})
        await self._emit()
        return verdict
