"""给模型的硬件工具（§4.2b）。

set_target / build / flash / clean 的名称和粒度与 ESP-IDF 官方 MCP 保持一致，结果统一为 OpResult（D19）。
await_marker 的语义参照 flashprobe-mcp / esparagus --expect（D20）：等到期望行、失败行或崩溃就立刻返回，
超时也返回；不做无限期监听。默认参数来自工程的 facts.toml（D18）。
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..device.events import DeviceEvent, LogRef
from ..device.manager import Board, DeviceManager
from ..platform.base import FlashScope, PlatformAdapter
from .base import HumanAction, Tool, ToolCaps, ToolContext, ToolFailure, ToolResult

CRASH = {"panic", "abort", "assert", "stack_overflow", "stack_smash", "reboot_loop", "brownout", "wdt_reset"}


def _platform(ctx: ToolContext) -> PlatformAdapter:
    if not ctx.services or not ctx.services.platform:
        raise ToolFailure(ToolResult.error("No platform adapter (ESP-IDF) is configured for this session"))
    return ctx.services.platform


def _devices(ctx: ToolContext) -> DeviceManager:
    if not ctx.services or not ctx.services.devices:
        raise ToolFailure(ToolResult.error("The device manager is not running"))
    return ctx.services.devices


def _board(ctx: ToolContext) -> Board:
    """会话绑定的板子；没绑 / 找不到 / 掉线时抛 ToolFailure（工具基类把它变成工具结果）。"""
    devices = _devices(ctx)
    if not ctx.board_id:
        # 说清楚板子在哪：真机实测里 agent 只知道"没绑定"，给用户的指引是错的（让用户去找不存在的扫描按钮）
        boards = list(devices.boards.values())
        if not boards:
            hint = "No boards are connected. Use ask_human to ask the user to plug the board in with a data cable."
        else:
            hint = "Connected boards: " + "; ".join(
                f"{b.alias} ({b.state}, {'free' if not b.owner_session else 'bound to ' + devices.owner_text(b)})" for b in boards)
            hint += (". Ask the user to pick the board in this session's header (a board bound to another idle session "
                     "can be moved here from the same menu).")
        raise ToolFailure(ToolResult.error(f"No board is bound to this session. {hint}", error_class="no_board"))
    b = devices.boards.get(ctx.board_id)
    if b is None:
        raise ToolFailure(ToolResult.error(f"Board {ctx.board_id} not found"))
    if b.state == "disconnected":
        raise ToolFailure(ToolResult.error(
            f"Board {b.alias} is not connected. Use ask_human to ask the user to plug in the USB cable.",
            next_actions=[{"kind": "replug", "human": True}]))
    return b


def _facts(ctx: ToolContext):
    return ctx.services.facts if ctx.services else None


def _link_text(link: str) -> str:
    return {"usb_serial_jtag": "the chip's USB-Serial-JTAG port", "usb_otg": "the chip's native USB (USB-OTG) port",
            "uart_bridge": "a USB-UART bridge (UART0)"}.get(link, "an unknown kind of port")


class Empty(BaseModel):
    pass


# ---------------------------------------------------------------- 编译类


class SetTargetArgs(BaseModel):
    # 2026-10-06：ESP-IDF v5.5 正式支持的 8 款（预览芯片 esp32c5 / c61 / h21 / h4 不在范围内）
    chip: Literal["esp32", "esp32s2", "esp32s3", "esp32c2", "esp32c3", "esp32c6", "esp32h2", "esp32p4"] = Field(
        description="Target chip (Xtensa: esp32, esp32s2, esp32s3; RISC-V: esp32c2, esp32c3, esp32c6, esp32h2, esp32p4)")


class SetTarget(Tool):
    name = "set_target"
    description = ("Set the project's target chip (idf.py set-target). Regenerates sdkconfig and wipes the build "
                   "directory, so the next build is a full build.")
    Args = SetTargetArgs
    caps = ToolCaps(lock="session", risk="normal", edits_files=True)
    ask_reason = "set_target regenerates sdkconfig and wipes the build directory; the next build will be a full build"

    def permission_subject(self, args: SetTargetArgs) -> str:
        return args.chip

    async def run(self, ctx: ToolContext, args: SetTargetArgs) -> ToolResult:
        pa = _platform(ctx)
        res = await pa.set_target(ctx, args.chip)
        return ToolResult.text(res.for_model(), is_error=not res.ok, op=res.model_dump())


class Build(Tool):
    name = "build"
    description = ("Build the project (idf.py build). Returns a structured result: success, error class, compiler "
                   "diagnostics (file / line / message) and firmware size.")
    Args = Empty
    caps = ToolCaps(lock="session", risk="normal")
    default_allow = True  # §5.5 放行级

    async def run(self, ctx: ToolContext, args: Empty) -> ToolResult:
        pa = _platform(ctx)
        res = await pa.build(ctx)
        text = res.for_model()
        if res.ok and res.size and res.size.summary():
            text += f"\nFirmware size: {res.size.summary()}"
        return ToolResult.text(text, is_error=not res.ok, op=res.model_dump())


class CleanArgs(BaseModel):
    full: bool = Field(False, description="true = idf.py fullclean: delete the whole build directory (use when it is broken "
                                            "or belongs to another project)")


class Clean(Tool):
    name = "clean"
    description = "Remove build outputs (idf.py clean, keeps sdkconfig); full=true deletes the whole build directory."
    Args = CleanArgs
    caps = ToolCaps(lock="session", risk="normal")
    default_allow = True

    async def run(self, ctx: ToolContext, args: CleanArgs) -> ToolResult:
        pa = _platform(ctx)
        res = await pa.clean(ctx, args.full)
        return ToolResult.text(res.for_model(), is_error=not res.ok, op=res.model_dump())


class Size(Tool):
    name = "size"
    description = "Show firmware size and memory usage (Flash / IRAM / DRAM). Requires a build first."
    Args = Empty
    caps = ToolCaps(read_only=True, lock="session", risk="safe")

    async def run(self, ctx: ToolContext, args: Empty) -> ToolResult:
        pa = _platform(ctx)
        rep = await pa.size(ctx)
        if rep is None:
            return ToolResult.error("Could not read the size (not built yet?)")
        return ToolResult.text(f"Firmware size: {rep.summary()}\n{rep.model_dump_json(exclude_none=True)}",
                               size=rep.model_dump())


class ProjectStatus(Tool):
    name = "project_status"
    description = ("Show project status: target chip, IDF version, whether it has been built, whether the last "
                   "build succeeded, the bound board, and the board fact card (facts.toml).")
    Args = Empty
    caps = ToolCaps(read_only=True, risk="safe")

    async def run(self, ctx: ToolContext, args: Empty) -> ToolResult:
        pa = _platform(ctx)
        info = pa.detect(ctx.cwd)
        lines = []
        if info is None:
            lines.append("The working directory is not an ESP-IDF project (no CMakeLists.txt that includes project.cmake)")
        else:
            lines.append(f"Project {info.name} · target {info.target or 'not set'} · ESP-IDF {info.idf_version}")
            lines.append(f"Built: {'yes' if info.built else 'no'}; last build: "
                         f"{ {True: 'succeeded', False: 'failed', None: 'unknown'}[info.last_build_ok]}")
            if s := pa.chip_summary(info.target):
                lines.append(f"Target chip: {s}")
        if ctx.services and _devices(ctx) and ctx.board_id:
            b = _devices(ctx).boards.get(ctx.board_id)
            if b:
                lines.append(f"Board: {b.alias} ({b.id}) · {b.chip or 'unknown chip'} · {b.port} · {b.state} · "
                             f"connected via {_link_text(b.link)}")
                if b.chip and info and info.target and b.chip != info.target:
                    lines.append(f"Warning: the board is {b.chip} but the project targets {info.target} (use set_target)")
                if info and (warn := pa.console_check(ctx.cwd, b.link)):
                    lines.append(f"Console: {warn}")
        else:
            lines.append("Board: none bound to this session")
        facts = _facts(ctx)
        if facts:
            lines.append("facts.toml: " + facts.model_dump_json(exclude_none=True))
        else:
            lines.append("facts.toml: none (.firmwright/facts.toml does not exist; await_marker needs an explicit expect)")
        return ToolResult.text("\n".join(lines), project=info.model_dump() if info else None)


# ---------------------------------------------------------------- 设备类


class FlashArgs(BaseModel):
    scope: FlashScope = Field("app", description="app = app partition only (everyday); all = bootloader + partition table + "
                                                 "app; bootloader / partition_table = that part only; "
                                                 "erase_all = erase the whole chip, then write everything")


class Flash(Tool):
    name = "flash"
    description = (
        "Flash the built firmware to the board bound to this session. Writes only the app partition by default. "
        "After flashing, the chip is reset and serial capture restarts; then use await_marker to check the boot. "
        "On failure returns error_class and next_actions; steps that need a human are handed to the user."
    )
    Args = FlashArgs
    caps = ToolCaps(device="exclusive", lock="session", risk="normal")
    default_allow = True

    def permission_subject(self, args: FlashArgs) -> str:
        return args.scope

    def risk_for(self, args: FlashArgs):
        return "normal" if args.scope == "app" else "dangerous"

    def risk_reason(self, args: FlashArgs) -> str:
        return {"all": "Also writes the bootloader and partition table (needed the first time a new board is flashed)",
                "bootloader": "Rewrites the bootloader; a bad one leaves the board unbootable",
                "partition_table": "Rewrites the partition table; existing data may become unreachable",
                "erase_all": "Erases the whole chip (including settings in NVS), then writes everything"}.get(args.scope, "")

    async def run(self, ctx: ToolContext, args: FlashArgs) -> ToolResult:
        pa = _platform(ctx)
        board = _board(ctx)
        sec = pa.security_enabled(ctx.cwd)
        if sec:
            return ToolResult.error(
                f"Flashing refused: sdkconfig enables {', '.join(sec)}. A bootloader built with these options burns eFuses "
                "on first boot, which is irreversible. If this is really needed, the user must do it by hand.", error_class="forbidden_security")
        info = pa.detect(ctx.cwd)
        if info is None:
            # 真机实测第 5 条：工作目录是空的 / 不是工程时，原来要等 idf.py 报 "CMakeLists.txt not found"
            return ToolResult.error(
                f"The working directory {ctx.cwd} is not an ESP-IDF project, so there is nothing to flash. Do not build or "
                "flash in folders outside the working directory; tell the user the session's folder does not contain the "
                "project.", error_class="not_a_project")
        if board.chip and info.target and board.chip != info.target:
            return ToolResult.error(
                f"The project targets {info.target} but the board is {board.chip}. Change the target with "
                f"set_target and rebuild.",
                error_class="chip_mismatch")
        devices = _devices(ctx)
        for attempt in (1, 2):
            async with devices.exclusive(board.id) as b:
                await ctx.progress(f"Flashing {args.scope} → {b.port}")
                res = await pa.flash(ctx, b.port or "", args.scope)
            if res.ok:
                break
            human = next((a for a in res.next_actions if a.human), None)
            if attempt == 2 or human is None:
                break
            reply = await ctx.ask_human(HumanAction(title="Flashing needs your help", instructions=human.description,
                                                    board_id=board.id, kind=human.kind))
            if not reply.done:
                res.summary += (f" (the user did not complete the manual step: {reply.note})" if reply.note
                                else " (the user did not complete the manual step)")
                break
            await ctx.progress("The user finished the step; retrying the flash")
        if res.ok:
            # S3 原生 USB 在没人读的时候会丢掉输出：监听恢复后再复位一次，保证抓到完整的启动日志
            # 先记 mark 再复位：复位后马上就有启动输出，mark 放后面会把它们漏掉
            await asyncio.sleep(0.3)
            mark(ctx, board.id)
            if board.id in devices.monitors and (why := await devices.reset_after_flash(board.id)):
                ctx.trace.record("post_flash_reset_failed", error=why)
            if on_flashed := ctx.extra.get("on_flashed"):  # checkpoint 记下这次烧录并存档固件（D05）
                await on_flashed(res, board)
            if warn := pa.console_check(ctx.cwd, board.link):  # 连接口和控制台输出口对不上：先说清楚，免得空等日志
                return ToolResult.text(res.for_model() + f"\nNote: {warn}", op=res.model_dump())
        return ToolResult.text(res.for_model(), is_error=not res.ok, op=res.model_dump())


class ChipInfo(Tool):
    name = "chip_info"
    description = (
        "Identify the chip on the bound board with esptool (chip model and revision, features such as PSRAM, MAC, flash "
        "size). Firmwright holds the serial port for live capture, so never run esptool or idf.py monitor through "
        "the shell; use this tool. It resets the board (the running firmware restarts).")
    Args = Empty
    caps = ToolCaps(device="exclusive", lock="session", risk="normal")
    default_allow = True

    async def run(self, ctx: ToolContext, args: Empty) -> ToolResult:
        pa = _platform(ctx)
        board = _board(ctx)
        devices = _devices(ctx)
        async with devices.exclusive(board.id, state="busy") as b:
            await ctx.progress(f"Reading chip info from {b.port}")
            info = await pa.chip_info(ctx, b.port or "")
        if not info.get("ok"):
            return ToolResult.error(f"esptool could not read the chip: {info.get('summary')}",
                                    error_class=info.get("error_class"), next_actions=info.get("next_actions") or [])
        if info.get("chip"):
            devices.set_chip(devices.boards[board.id], info["chip"])
        if info.get("mac"):
            devices.set_mac(devices.boards[board.id], info["mac"])
        lines = [f"Chip: {info.get('description')}", f"Features: {info.get('features')}", f"MAC: {info.get('mac')}",
                 f"Flash size: {info.get('flash_size')}", f"Crystal: {info.get('crystal')}"]
        if info.get("summary_line"):
            lines.append(f"About this chip: {info['summary_line']}")
        proj = pa.detect(ctx.cwd)
        if proj and proj.target and info.get("chip") and proj.target != info["chip"]:
            lines.append(f"Warning: the project targets {proj.target}, not {info['chip']}; run set_target {info['chip']} "
                         "before building for this board.")
        return ToolResult.text("\n".join(lines), chip_info=info)


class ReadFlashArgs(BaseModel):
    size: str = Field("ALL", description='Bytes to read from offset 0, e.g. "0x400000"; "ALL" = the whole flash')


class ReadFlash(Tool):
    name = "read_flash"
    description = (
        "Back up the firmware currently on the board: read the flash (from offset 0) into a file in the session folder. "
        "Do this before the first flash of a board whose current firmware the user may want to keep. Takes 1–3 "
        "minutes for 16 MB and resets the board. Never use esptool through the shell for this.")
    Args = ReadFlashArgs
    caps = ToolCaps(device="exclusive", lock="session", risk="normal")
    default_allow = True

    async def run(self, ctx: ToolContext, args: ReadFlashArgs) -> ToolResult:
        pa = _platform(ctx)
        board = _board(ctx)
        s = ctx.extra.get("session")
        root = Path(s.store.root) if s is not None and getattr(s, "store", None) else ctx.cwd.parent
        dest = root / "firmware" / "backup" / f"{board.id}-{time.strftime('%Y%m%d-%H%M%S')}.bin"
        async with _devices(ctx).exclusive(board.id, state="busy") as b:
            await ctx.progress(f"Reading flash from {b.port} (this takes a while)")
            res = await pa.read_flash(ctx, b.port or "", dest, size=args.size)
        if not res.get("ok"):
            return ToolResult.error(f"Reading the flash failed: {res.get('summary')}", error_class=res.get("error_class"),
                                    next_actions=res.get("next_actions") or [])
        return ToolResult.text(
            f"Backed up {res['bytes'] / 1048576:.1f} MB of flash to {res['path']} (sha256 {res['sha256'][:16]}…). "
            "To restore it later: write it back at offset 0 (the user can do this with esptool write_flash 0 <file>).",
            backup=res)


def mark(ctx: ToolContext, board_id: str) -> None:
    """记下"从这里开始看"的位置：await_marker 默认只看最近一次烧录 / 复位之后的输出。"""
    _devices(ctx).marks[board_id] = time.monotonic()


class Reset(Tool):
    name = "reset"
    description = "Reset the chip through the serial RTS line (same as pressing RESET). Then use await_marker to read the boot output."
    Args = Empty
    caps = ToolCaps(device="exclusive", risk="normal")
    default_allow = True

    async def run(self, ctx: ToolContext, args: Empty) -> ToolResult:
        board = _board(ctx)
        mark(ctx, board.id)
        await _devices(ctx).reset(board.id)
        return ToolResult.text(f"Reset {board.alias}")


class AwaitMarkerArgs(BaseModel):
    expect: str | None = Field(None, description="Line to wait for (regex). Defaults to pass_marker from facts.toml, "
                                                    "else boot_banner")
    fail: list[str] | None = Field(None, description="Failure lines (regex). Defaults to fail_marker from facts.toml")
    timeout_s: float | None = Field(None, description="Maximum seconds to wait; defaults to boot_window_s from facts.toml (max 120)")
    since: Literal["mark", "now"] = Field("mark", description="mark = look from the last flash / reset (default); now = only new output "
                                                                "from this moment")


class AwaitMarker(Tool):
    name = "await_marker"
    description = (
        "Wait for the device's serial output: the expected line appears → success; a failure line or a crash → "
        "returns failure immediately with the event (crashes include the decoded backtrace); timeout → returns the "
        "last lines. Use it to verify how the firmware actually runs after flashing."
    )
    Args = AwaitMarkerArgs
    caps = ToolCaps(read_only=True, risk="safe", interruptible=True)

    async def run(self, ctx: ToolContext, args: AwaitMarkerArgs) -> ToolResult:
        board = _board(ctx)
        devices = _devices(ctx)
        facts = _facts(ctx)
        expect_s = args.expect or (facts.pass_marker[0] if facts and facts.pass_marker else None) or (
            facts.boot_banner if facts else None)
        if not expect_s:
            return ToolResult.error("No expect given, and the project has no pass_marker / boot_banner in facts.toml. "
                                     "Pass expect explicitly.")
        fail_s = args.fail if args.fail is not None else (facts.fail_marker if facts else [])
        try:
            expect = re.compile(expect_s)
            fails = [re.compile(f) for f in fail_s]
        except re.error as e:
            return ToolResult.error(f"Invalid regex: {e}")
        timeout = min(args.timeout_s or (facts.boot_window_s if facts else 8.0), 120.0)

        since = devices.marks.get(board.id, 0.0) if args.since == "mark" else time.monotonic()
        seen: list[str] = []
        hit: asyncio.Future[tuple[str, object]] = asyncio.get_running_loop().create_future()

        # 期望行出现之后还要再看一个稳定窗口：固件可能先打印自检 PASS、紧接着崩溃（W4 演示里真的遇到了）
        late: asyncio.Future[tuple[str, object]] = asyncio.get_running_loop().create_future()

        def settle(result: tuple[str, object]) -> None:
            fut = hit if not hit.done() else (late if hit.result()[0] == "pass" and not late.done() else None)
            if fut is not None:
                fut.set_result(result)

        def check(text: str) -> None:
            seen.append(text)
            if not hit.done() and expect.search(text):
                hit.set_result(("pass", text))
            elif any(f.search(text) for f in fails):
                settle(("fail", text))

        def on_dev(kind: str, payload) -> None:
            if kind == "event" and isinstance(payload, DeviceEvent) and payload.board_id == board.id:
                if payload.kind in CRASH:
                    settle(("crash", payload))

        mon = devices.monitors.get(board.id)
        if mon:
            for t, text in list(mon.lines):
                if t >= since:
                    check(text)
        # 只认 mark 之后的崩溃，避免拿到上一次运行的旧崩溃
        crash = devices.latest_crash(board.id)
        if crash and args.since == "mark" and _event_mono(crash) >= since:
            settle(("crash", crash))

        def on_line(bid: str, text: str, ref: LogRef) -> None:
            if bid == board.id:
                check(text)

        unlisten = devices.listen(on_dev)
        status, detail = "timeout", None
        try:
            with devices.watch_lines(on_line):
                cancel_wait = asyncio.ensure_future(ctx.cancel.wait())
                done, _ = await asyncio.wait({hit, cancel_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                cancel_wait.cancel()
                if hit in done:
                    status, detail = hit.result()
                    if status == "pass" and not late.done():
                        settle_s = facts.settle_s if facts else 2.0
                        cancel_wait = asyncio.ensure_future(ctx.cancel.wait())
                        await asyncio.wait({late, cancel_wait}, timeout=settle_s, return_when=asyncio.FIRST_COMPLETED)
                        cancel_wait.cancel()
                    if late.done():
                        pass_line = detail
                        status, detail = late.result()
                        seen.append(f"(note: after the expected line {pass_line!r} appeared, the device ran into trouble)")
                elif ctx.cancel.cancelled:
                    status = "interrupted"
                    await asyncio.sleep(0)  # 让同一时刻到达的事件也进来
                    if hit.done():
                        status, detail = hit.result()
        finally:
            unlisten()

        tail = "\n".join(seen[-20:]) or "(no output)"
        if status == "pass":
            return ToolResult.text(f"✓ Expected line seen: {detail}\n(waited through {len(seen)} lines of output)", status="pass",
                                   line=detail)
        if status == "fail":
            return ToolResult.error(f"✗ Failure line seen: {detail}\nRecent output:\n{tail}", status="fail", line=detail)
        if status == "crash":
            ev: DeviceEvent = detail  # type: ignore[assignment]
            ctx.extra.setdefault("delivered_events", set()).add(ev.id)
            if ctx.services and not ctx.services.log_digest:
                # 对照组（context.log_digest 关）：只给串口原文，不预先解码调用栈
                raw = devices.read_log(board.id, ev.log_ref, max_bytes=6000) if ev.log_ref else tail
                if ev.id in ctx.extra.get("injected_events", ()):
                    return ToolResult.error(f"✗ The device crashed. This is event {ev.id} from the device_event reminder above.",
                                            status="crash", event=ev.model_dump(mode="json"))
                return ToolResult.error(f"✗ Device problem\n{ev.reminder_text(raw)}", status="crash",
                                        event=ev.model_dump(mode="json"))
            ev = await _decoded(ctx, ev)
            if ev.id in ctx.extra.get("injected_events", ()):
                # 事件已经以 system-reminder 交给模型了：不重复摘要，只补上解码后的调用栈
                frames = "\n".join(f"  {f.render()}" for f in (ev.backtrace.frames if ev.backtrace else [])[:12])
                text = f"✗ The device crashed. This is event {ev.id} from the device_event reminder above."
                if ev.backtrace and ev.backtrace.decoded and frames:
                    text += f"\nDecoded backtrace:\n{frames}"
                return ToolResult.error(text, status="crash", event=ev.model_dump(mode="json"))
            return ToolResult.error(f"✗ Device problem\n{ev.reminder_text()}\nRecent output:\n{tail}", status="crash",
                                    event=ev.model_dump(mode="json"))
        if status == "interrupted":
            return ToolResult.error(f"Wait interrupted ({ctx.cancel.reason}). Expected line {expect_s!r} has not appeared yet."
                                    f"\nRecent output:\n{tail}",
                                    status="interrupted")
        hint = ""
        if not seen and ctx.services and ctx.services.platform:
            # 一行输出都没有：最常见的原因是控制台打到了另一个口（例如插的是 USB-Serial-JTAG 口，日志走 UART0）
            if warn := ctx.services.platform.console_check(ctx.cwd, board.link):
                hint = f"\nLikely cause: {warn}"
        return ToolResult.error(f"Timed out ({timeout:.0f}s): expected line {expect_s!r} did not appear.\nRecent output:\n{tail}"
                                + hint, status="timeout")


def _event_mono(ev: DeviceEvent) -> float:
    """事件时间（UTC）换算到 monotonic 时钟，用来和 mark 比较。"""
    from datetime import UTC, datetime

    age = (datetime.now(UTC) - ev.at).total_seconds()
    return time.monotonic() - age


async def _decoded(ctx: ToolContext, ev: DeviceEvent) -> DeviceEvent:
    pa = ctx.services.platform if ctx.services else None
    if not pa or not ev.backtrace or ev.backtrace.decoded:
        return ev
    info = pa.detect(ctx.cwd)
    if not info or not info.elf:
        return ev
    try:
        bt = await pa.unwind(ev, Path(info.elf), ctx.cwd)
    except Exception:
        return ev
    ev = ev.model_copy(update={"backtrace": bt})
    if _devices(ctx):
        _devices(ctx).events[ev.id] = ev
    return ev


class ReadLogArgs(BaseModel):
    log_ref: str | None = Field(None, description="A log_ref from an event (board@start-end); omit to read the latest log")
    grep: str | None = Field(None, description="Only lines matching this regex")
    tail: int = Field(100, description="Maximum number of lines to return")


class ReadLog(Tool):
    name = "read_log"
    description = "Read raw serial log lines: a range by an event's log_ref, or the latest log filtered by grep / tail."
    Args = ReadLogArgs
    caps = ToolCaps(read_only=True, risk="safe")

    async def run(self, ctx: ToolContext, args: ReadLogArgs) -> ToolResult:
        if not ctx.services or not _devices(ctx):
            return ToolResult.error("The device manager is not running")
        devices = _devices(ctx)
        ref = None
        board_id = ctx.board_id
        if args.log_ref:
            m = re.fullmatch(r"(?P<b>.+)@(?P<s>\d+)-(?P<e>\d+)", args.log_ref.strip())
            if not m:
                return ToolResult.error("log_ref must look like board@start-end")
            board_id = m.group("b")
            ev = next((e for e in devices.events.values() if e.log_ref and e.log_ref.short() == args.log_ref.strip()),
                      None)
            file = ev.log_ref.file if ev and ev.log_ref else str(devices._log_path(board_id))
            ref = LogRef(board_id=board_id, file=file, start=int(m.group("s")), end=int(m.group("e")))
        if not board_id:
            return ToolResult.error("No board is bound to this session")
        text = devices.read_log(board_id, ref, grep=args.grep, tail=max(1, min(args.tail, 1000)))
        lines = text.splitlines()
        if len(lines) > args.tail:
            text = "\n".join(lines[-args.tail:])
        return ToolResult.text(text or "(empty)")


class DiagnoseArgs(BaseModel):
    event_id: str | None = Field(None, description="Crash event id; omit to use the latest crash on this board")


class DiagnoseCrash(Tool):
    name = "diagnose_crash"
    description = (
        "Diagnose a crash: first collect fault evidence (exception type, faulting address, registers, explanation), "
        "then decode the backtrace to functions and source lines using the ELF. ESP-IDF / FreeRTOS internal frames "
        "are folded so your code stands out."
    )
    Args = DiagnoseArgs
    caps = ToolCaps(read_only=True, risk="safe")

    async def run(self, ctx: ToolContext, args: DiagnoseArgs) -> ToolResult:
        pa = _platform(ctx)
        if not _devices(ctx):
            return ToolResult.error("The device manager is not running")
        devices = _devices(ctx)
        ev = devices.event(args.event_id) if args.event_id else (
            devices.latest_crash(ctx.board_id) if ctx.board_id else None)
        if ev is None:
            return ToolResult.error("Crash event not found")
        evidence = await pa.fault_evidence(ev)
        ev = await _decoded(ctx, ev)
        lines = [f"Event {ev.id} · {ev.kind} · {ev.summary}"]
        if evidence.exception:
            lines.append(f"Exception: {evidence.exception} ({evidence.arch}, core {evidence.core})")
        if evidence.fault_address:
            lines.append(f"Faulting address: {evidence.fault_address}")
        if evidence.explanation:
            lines.append(f"Explanation: {evidence.explanation}")
        bt = ev.backtrace
        if bt and bt.frames:
            lines.append("Backtrace" + (" (decoded)" if bt.decoded else " (not decoded: ELF or addr2line not found)") + ":")
            folded = 0
            for f in bt.frames:
                if f.internal:
                    folded += 1
                    continue
                if folded:
                    lines.append(f"    … ({folded} ESP-IDF / FreeRTOS / ROM internal frames folded)")
                    folded = 0
                lines.append(f"  → {f.render()}")
            if folded:
                lines.append(f"    … ({folded} internal frames folded)")
            if bt.corrupted:
                lines.append("  Note: the backtrace ends with CORRUPTED; the stack may have been overwritten")
        if ev.log_ref:
            lines.append(f"log_ref={ev.log_ref.short()}")
        return ToolResult.text("\n".join(lines), event=ev.model_dump(mode="json"), evidence=evidence.model_dump())


class AskHumanArgs(BaseModel):
    title: str = Field(description="One-line title, e.g. \"Put the board into download mode\"")
    instructions: str = Field(description="Exactly what the user should do, step by step")
    kind: Literal["enter_download_mode", "replug", "wire", "measure", "observe", "generic"] = "generic"


class AskHuman(Tool):
    name = "ask_human"
    description = (
        "Ask the user to do something physical (press a button, replug USB, wire something, measure a voltage, check "
        "whether an LED is on, ...) and wait until they are done. Only use it when a human really has to act or look."
    )
    Args = AskHumanArgs
    caps = ToolCaps(read_only=True, risk="safe")

    async def run(self, ctx: ToolContext, args: AskHumanArgs) -> ToolResult:
        reply = await ctx.ask_human(HumanAction(title=args.title, instructions=args.instructions,
                                                board_id=ctx.board_id, kind=args.kind))
        if reply.done:
            return ToolResult.text("The user completed it" + (f". Note: {reply.note}" if reply.note else ""))
        return ToolResult.error("The user could not do it" + (f": {reply.note}" if reply.note else ""))


def hardware_tools() -> list[Tool]:
    return [ProjectStatus(), SetTarget(), Build(), Clean(), Size(), Flash(), Reset(), AwaitMarker(), ReadLog(),
            DiagnoseCrash(), AskHuman(), ChipInfo(), ReadFlash()]
