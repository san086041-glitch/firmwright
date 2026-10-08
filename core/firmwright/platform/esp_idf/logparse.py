"""ESP-IDF 串口日志解析：逐行输入，产出 DeviceEvent（§3.2）。

启动失败的分类参照 esparagus 的 crash_context：panic / 看门狗 / abort / assert / 反复重启 /
栈溢出 / 栈破坏 / 掉电 / 卡在下载模式。S3（Xtensa）和 P4（RISC-V）的 panic 输出格式不同，
这里都按"块"收集：从 Guru Meditation / abort / assert 开始，到 Rebooting... / ELF file SHA256 /
下一次 ESP-ROM 启动 / 空闲超时为止，结束时产出一个事件。
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import cast

from ...device.events import Backtrace, DeviceEvent, EventKind, Frame, LogRef
from ...facts import Facts
from . import chips

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
RST = re.compile(r"rst:0x(?P<code>[0-9a-fA-F]+) \((?P<reason>[A-Z0-9_]+)\)(?:,boot:0x(?P<boot>[0-9a-fA-F]+) \((?P<mode>[^)]*)\))?")
GURU = re.compile(r"Guru Meditation Error: Core\s+(?P<core>\d+) panic'ed \((?P<exc>[^)]+)\)")
ABORT = re.compile(r"abort\(\) was called at PC (?P<pc>0x[0-9a-fA-F]+) on core (?P<core>\d+)")
ASSERT = re.compile(r"assert failed: (?P<what>.+)$")
STACK_OVF = re.compile(r"\*\*\*ERROR\*\*\* A stack overflow in task (?P<task>.+?) has been detected")
STACK_SMASH = re.compile(r"Stack smashing protect failure")
BROWNOUT = re.compile(r"Brownout detector was triggered")
TASK_WDT = re.compile(r"task_wdt: Task watchdog got triggered")
DOWNLOAD = re.compile(r"waiting for download")
BACKTRACE = re.compile(r"^Backtrace:\s*(?P<bt>.*)$")
BT_PAIR = re.compile(r"(0x[0-9a-fA-F]{8}):(0x[0-9a-fA-F]{8})")
REG = re.compile(r"\b([A-Z][A-Z0-9]{1,8})\s*:\s*(0x[0-9a-fA-F]{8})")
BLOCK_END = re.compile(r"Rebooting\.\.\.|ELF file SHA256|CPU halted|Entering gdb stub")

WDT_RESETS = ("WDT",)
BLOCK_IDLE_S = 1.5
LOOP_WINDOW_S = 20.0
LOOP_BOOTS = 3


@dataclass
class _Block:
    kind: str
    start: int
    end: int
    lines: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    last: float = 0.0


class EspLogParser:
    def __init__(self, board_id: str, log_file: str = "", facts: Facts | None = None,
                 clock=time.monotonic) -> None:
        self.board_id = board_id
        self.log_file = log_file
        self.clock = clock
        self.banner, self.passes, self.fails = (facts.compiled() if facts else (None, [], []))
        self._block: _Block | None = None
        self._boots: deque[float] = deque()
        self._loop_reported = False
        self.last_reset_reason: str | None = None
        self.chip: str | None = None  # 从 ESP-ROM: 行识别出的芯片型号

    def ref(self, start: int, end: int) -> LogRef:
        return LogRef(board_id=self.board_id, file=self.log_file, start=start, end=end)

    # ------------------------------------------------------------------

    def feed(self, line: str, start: int = 0, end: int = 0) -> list[DeviceEvent]:
        line = ANSI.sub("", line).rstrip("\r\n")
        now = self.clock()
        out: list[DeviceEvent] = []

        # ---- 正在收集一个崩溃块
        if self._block is not None:
            if chips.is_rom_line(line) or RST.search(line):
                out += self._close_block()
            else:
                self._block.lines.append(line)
                self._block.end = end
                self._block.last = now
                if BLOCK_END.search(line):
                    out += self._close_block()
                return out

        # ---- 崩溃块的开始
        if m := GURU.search(line):
            self._open("panic", start, end, line, now, exception=m.group("exc"), core=int(m.group("core")))
            return out
        if m := ABORT.search(line):
            self._open("abort", start, end, line, now, pc=m.group("pc"), core=int(m.group("core")))
            return out + self._markers(line, start, end)
        if m := ASSERT.search(line):
            self._open("assert", start, end, line, now, what=m.group("what").strip())
            return out
        if m := STACK_OVF.search(line):
            self._open("stack_overflow", start, end, line, now, task=m.group("task"))
            return out
        if STACK_SMASH.search(line):
            self._open("stack_smash", start, end, line, now)
            return out

        # ---- 单行事件
        if chip := chips.chip_from_boot_line(line):  # ESP-ROM:esp32c3-… / ESP32 初代的 "ets Jun  8 2016"
            self.chip = chip
        if m := RST.search(line):
            out += self._boot(m, start, end, now)
        elif BROWNOUT.search(line):
            out.append(DeviceEvent.make(self.board_id, "brownout", "Brownout detector triggered (insufficient supply or a current spike); the chip reset",
                                        log_ref=self.ref(start, end)))
        elif TASK_WDT.search(line):
            out.append(DeviceEvent.make(self.board_id, "wdt_reset",
                                        "Task watchdog triggered: a task held the CPU for too long without yielding (busy loop or blocking); see the tasks listed in the log",
                                        detail={"type": "task_wdt"}, log_ref=self.ref(start, end)))
        elif DOWNLOAD.search(line):
            out.append(DeviceEvent.make(self.board_id, "download_mode",
                                        "The chip is in download mode (waiting to be flashed) and will not run firmware. After flashing, press RESET or use the reset tool",
                                        log_ref=self.ref(start, end)))
        out += self._markers(line, start, end)
        return out

    def tick(self, now: float | None = None) -> list[DeviceEvent]:
        """空闲超时：崩溃块收集到一半时设备不再输出，也要把事件交出去。"""
        now = self.clock() if now is None else now
        if self._block and now - self._block.last > BLOCK_IDLE_S:
            return self._close_block()
        return []

    # ------------------------------------------------------------------

    def _open(self, kind: str, start: int, end: int, line: str, now: float, **detail) -> None:
        self._block = _Block(kind=kind, start=start, end=end, lines=[line], detail=detail, last=now)

    def _markers(self, line: str, start: int, end: int) -> list[DeviceEvent]:
        out = []
        if self.banner and self.banner.search(line):
            self._boots.clear()
            self._loop_reported = False
            out.append(DeviceEvent.make(self.board_id, "marker", f"Boot banner: {line.strip()[:200]}",
                                        detail={"marker": "boot_banner", "line": line}, log_ref=self.ref(start, end)))
        for rx in self.passes:
            if rx.search(line):
                out.append(DeviceEvent.make(self.board_id, "marker", f"Pass marker: {line.strip()[:200]}",
                                            detail={"marker": "pass", "line": line}, log_ref=self.ref(start, end)))
                break
        for rx in self.fails:
            if rx.search(line):
                ev = DeviceEvent.make(self.board_id, "marker", f"Fail marker: {line.strip()[:200]}",
                                      detail={"marker": "fail", "line": line}, log_ref=self.ref(start, end))
                ev.severity = "warn"
                out.append(ev)
                break
        return out

    def _boot(self, m: re.Match, start: int, end: int, now: float) -> list[DeviceEvent]:
        reason = m.group("reason")
        mode = m.group("mode") or ""
        self.last_reset_reason = reason
        out = [DeviceEvent.make(self.board_id, "boot", f"Chip booted, reset reason {reason}" + (f", boot mode {mode}" if mode else ""),
                                detail={"reset_reason": reason, "boot_mode": mode, "chip": self.chip},
                                log_ref=self.ref(start, end))]
        if any(w in reason for w in WDT_RESETS):
            out.append(DeviceEvent.make(self.board_id, "wdt_reset",
                                        f"Watchdog reset ({reason}): in the previous run, code hung or kept interrupts disabled too long",
                                        detail={"reset_reason": reason}, log_ref=self.ref(start, end)))
        elif "BROWN" in reason:
            out.append(DeviceEvent.make(self.board_id, "brownout", f"Brownout reset ({reason})",
                                        detail={"reset_reason": reason}, log_ref=self.ref(start, end)))
        if "DOWNLOAD" in mode.upper():
            out.append(DeviceEvent.make(self.board_id, "download_mode", "The chip booted into download mode (BOOT held down or a strapping pin at the wrong level)",
                                        log_ref=self.ref(start, end)))
            return out
        self._boots.append(now)
        while self._boots and now - self._boots[0] > LOOP_WINDOW_S:
            self._boots.popleft()
        if len(self._boots) >= LOOP_BOOTS and not self._loop_reported:
            self._loop_reported = True
            out.append(DeviceEvent.make(
                self.board_id, "reboot_loop",
                f"Reboot loop: booted {len(self._boots)} times within {LOOP_WINDOW_S:.0f} s without reaching the boot banner",
                detail={"boots": len(self._boots), "last_reset_reason": reason}, log_ref=self.ref(start, end)))
        return out

    def _close_block(self) -> list[DeviceEvent]:
        b = self._block
        self._block = None
        if b is None:
            return []
        text = "\n".join(b.lines)
        regs = dict(REG.findall(text))
        bt = None
        for line in b.lines:
            if m := BACKTRACE.match(line.strip()):
                raw = m.group("bt")
                bt = Backtrace(raw=line.strip(), corrupted="CORRUPTED" in raw,
                               frames=[Frame(pc=pc, sp=sp) for pc, sp in BT_PAIR.findall(raw)])
        detail = dict(b.detail)
        if bt is None and ("MEPC" in regs or "RA" in regs):
            # RISC-V（C / H / P 系列）不打印 Backtrace 行，而是打印寄存器 + "Stack memory:"。先放 MEPC / RA 两帧；
            # 解码时把寄存器转储和栈内存交给 gdb + esp_idf_panic_decoder 回溯完整调用栈（W7，decode.unwind_riscv）
            frames = [Frame(pc=regs[k]) for k in ("MEPC", "RA") if k in regs]
            bt = Backtrace(raw=f"MEPC={regs.get('MEPC')} RA={regs.get('RA')}", frames=frames)
            start = next((i for i, ln in enumerate(b.lines) if re.match(r"\s*Core\s+\d+ register dump:", ln)), None)
            if start is not None and any(ln.startswith("Stack memory:") for ln in b.lines):
                dump = [ln for ln in b.lines[start:] if not BLOCK_END.search(ln)]
                detail["panic_dump"] = "\n".join(dump)[:64_000]
        if regs:
            detail["registers"] = regs
        summary = self._summary(b.kind, detail, regs)
        ev = DeviceEvent.make(self.board_id, cast(EventKind, b.kind), summary, detail=detail, backtrace=bt,
                              log_ref=self.ref(b.start, b.end))
        return [ev]

    @staticmethod
    def _summary(kind: str, d: dict, regs: dict) -> str:
        if kind == "panic":
            exc = d.get("exception", "?")
            s = f"CPU exception {exc} (core {d.get('core')})"
            addr = regs.get("EXCVADDR") or regs.get("MTVAL")
            if addr:
                s += f", address {addr}"
                if int(addr, 16) < 0x1000:
                    s += " (near 0, most likely a NULL pointer)"
            pc = regs.get("PC") or regs.get("MEPC")
            if pc:
                s += f", PC={pc}"
            return s
        if kind == "abort":
            return (f"abort() was called (PC {d.get('pc')}, core {d.get('core')}); common causes: a failed "
                    f"ESP_ERROR_CHECK, an assert, an allocation failure")
        if kind == "assert":
            return f"Assertion failed: {d.get('what')}"
        if kind == "stack_overflow":
            return f"Stack overflow in task {d.get('task')}: increase its stack, or avoid large local arrays"
        if kind == "stack_smash":
            return "Stack smashing protection triggered: a local array was written out of bounds"
        return kind
