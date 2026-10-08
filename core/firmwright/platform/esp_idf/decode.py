"""崩溃诊断的两步（照搬 embedded-debugger-mcp 的 diagnose_fault → unwind_exception）：

1. fault_evidence：从寄存器转储里取异常原因（Xtensa EXCCAUSE / RISC-V mcause）、出错地址，给出解释；
2. unwind：用 ELF + addr2line 把调用栈地址映射到函数和源码行。

D21：ESP-IDF / FreeRTOS / ROM 内部的帧标记为 internal，界面默认折叠，突出用户代码。
RISC-V（C / H / P 系列）不打印 Backtrace 行：有"Stack memory:"转储时用 gdb + esp_idf_panic_decoder 回溯（unwind_riscv，W7），
没有时退回只解码 MEPC 和 RA 两帧。Xtensa（ESP32 / S2 / S3）解 Backtrace 行；落在 ROM 里的地址用 esp-rom-elfs 再解一次。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ...device.events import Backtrace, DeviceEvent, Frame
from ...osal import run_process
from ..base import FaultEvidence

XTENSA_EXC = {
    "IllegalInstruction": "Executed an illegal instruction: a bad function pointer, a corrupted stack that broke the return address, or a jump into data",
    "LoadProhibited": "Read from an invalid address: most often a NULL or dangling pointer dereference (see EXCVADDR)",
    "StoreProhibited": "Wrote to an invalid address: NULL / dangling pointer or out-of-bounds write (see EXCVADDR)",
    "InstrFetchProhibited": "Fetched an instruction from an invalid address: called an invalid function pointer, or the return address was corrupted",
    "LoadStoreAlignment": "Unaligned memory access: a pointer of the wrong type accessed an unaligned address",
    "IntegerDivideByZero": "Integer division by zero",
    "Unhandled debug exception": "Debug exception: usually a watchpoint (e.g. stack overflow detection) or a breakpoint",
    "Double exception": "An exception occurred while handling an exception, usually a stack overflow",
    "Cache disabled but cached memory region accessed": "Code or constants in flash were accessed while the flash cache was disabled (e.g. during a flash write): put the ISR in IRAM",
    "Interrupt wdt timeout on CPU0": "Interrupt watchdog timeout: an ISR ran too long or interrupts were disabled too long",
    "Interrupt wdt timeout on CPU1": "Interrupt watchdog timeout: an ISR ran too long or interrupts were disabled too long",
}
RISCV_EXC = {
    "Instruction access fault": "Instruction access fault: called an invalid function pointer",
    "Illegal instruction": "Illegal instruction: bad function pointer or corrupted stack",
    "Breakpoint": "Breakpoint / stack overflow protection triggered",
    "Load access fault": "Read from an invalid address: NULL or dangling pointer (see MTVAL)",
    "Store access fault": "Wrote to an invalid address: NULL / dangling pointer or out-of-bounds write (see MTVAL)",
    "Load address misaligned": "Misaligned load",
    "Store address misaligned": "Misaligned store",
    "Stack protection fault": "Stack protection fault: stack overflow",
}


def fault_evidence(ev: DeviceEvent) -> FaultEvidence:
    regs: dict[str, str] = ev.detail.get("registers", {})
    exc = ev.detail.get("exception")
    arch = "riscv" if ("MEPC" in regs or "MCAUSE" in regs) else ("xtensa" if "EXCCAUSE" in regs or "PS" in regs else "unknown")
    explain = ""
    if exc:
        table = RISCV_EXC if arch == "riscv" else XTENSA_EXC
        explain = next((v for k, v in table.items() if exc.startswith(k)), "")
    if ev.kind == "abort":
        explain = "The code called abort(): typically ESP_ERROR_CHECK() got an error code, an assert failed, or a malloc-failure check fired"
    elif ev.kind == "assert":
        explain = f"Assertion failed: {ev.detail.get('what')}"
    elif ev.kind == "stack_overflow":
        explain = f"Task {ev.detail.get('task')} ran out of stack: increase the stack size in xTaskCreate, or avoid large local arrays"
    elif ev.kind == "stack_smash":
        explain = "The stack canary was overwritten: a local array was written out of bounds"
    addr = regs.get("EXCVADDR") or regs.get("MTVAL")
    if addr and exc and ("Load" in exc or "Store" in exc) and int(addr, 16) < 0x1000:
        explain += f". The faulting address {addr} is near 0, which almost certainly means a NULL pointer (or NULL plus a struct member offset)"
    return FaultEvidence(exception=exc, explanation=explain, registers=regs, fault_address=addr,
                         core=ev.detail.get("core"), arch=arch)


A2L_LINE = re.compile(r"^(?P<pc>0x[0-9a-fA-F]+): (?P<func>.+?) at (?P<loc>.+)$")


def is_internal(file: str | None, func: str | None, idf_path: str | None, project_root: Path | None) -> bool:
    if not file or file.startswith("??"):
        return True  # ROM 或没有调试信息
    f = file.replace("\\", "/").lower()
    if project_root and f.startswith(str(project_root).replace("\\", "/").lower() + "/"):
        return "/managed_components/" in f or "/build/" in f
    if idf_path and f.startswith(idf_path.replace("\\", "/").lower()):
        return True
    return any(s in f for s in ("/esp-idf/components/", "/freertos/", "/newlib/", "/esp_system/", "/esp_rom/",
                                "/xtensa/", "/riscv/", "/managed_components/"))


async def addr2line(tool: str, elf: Path, pcs: list[str], env: dict[str, str]) -> list[tuple[str, str | None, str | None, int | None]]:
    """返回 [(pc, 函数, 文件, 行)]；inline 展开的帧会多出几行。"""
    if not pcs:
        return []
    res = await run_process([tool, "-pfiaC", "-e", str(elf), *pcs], env=env, timeout=30)
    out = []
    for line in res.stdout.splitlines():
        line = line.strip()
        inlined = line.startswith("(inlined by)")
        if inlined:
            m = re.match(r"\(inlined by\) (?P<func>.+?) at (?P<loc>.+)$", line)
            pc = out[-1][0] if out else "?"
        else:
            m = A2L_LINE.match(line)
            pc = m.group("pc") if m else "?"
        if not m:
            continue
        func = m.group("func")
        loc = m.group("loc")
        file, _, ln = loc.rpartition(":")
        ln = ln.split(" ")[0]
        out.append((pc, None if func == "??" else func, None if file in ("??", "") else file,
                    int(ln) if ln.isdigit() and ln != "0" else None))
    return out


GDB_FRAME = re.compile(r"^#(?P<n>\d+)\s+(?:(?P<pc>0x[0-9a-fA-F]+) in )?(?P<func>[^\s(]+) \(.*?\)"
                       r"(?: at (?P<file>.+):(?P<line>\d+))?\s*$")


async def unwind_riscv(ev: DeviceEvent, elf: Path, *, toolprefix: str, env: dict[str, str], python: str,
                       idf_path: str | None, project_root: Path | None) -> Backtrace | None:
    """RISC-V（P4）的完整调用栈：和 idf.py monitor 一样，让 gdb 连接 esp_idf_panic_decoder 模拟的 GDB 服务器，
    服务器用崩溃输出里的寄存器和栈内存回答 gdb 的读请求，gdb 按 ELF 里的调试信息回溯（bt）。
    任何一步失败都返回 None，调用方退回 MEPC / RA 两帧。"""
    import os
    import tempfile

    dump = ev.detail.get("panic_dump")
    gdb = shutil.which(toolprefix + "gdb", path=env.get("PATH") or env.get("Path"))
    if not dump or not gdb:
        return None
    fd, tmp = tempfile.mkstemp(suffix=".panic.txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(dump + "\n")
        # 和 esp_idf_panic_decoder.PanicOutputDecoder 一样：-ex 里的 Windows 路径要把反斜杠写成两个
        py, tf = python.replace("\\", "\\\\"), tmp.replace("\\", "\\\\")
        res = await run_process([gdb, "--batch", "-n", str(elf),
                                 "-ex", f'target remote | "{py}" -m esp_idf_panic_decoder "{tf}"', "-ex", "bt"],
                                env=env, timeout=60)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    frames: list[Frame] = []
    for line in res.output.splitlines():
        m = GDB_FRAME.match(line.strip())
        if not m:
            continue
        pc = m.group("pc") or (ev.detail.get("registers", {}).get("MEPC") if m.group("n") == "0" else None) or "?"
        file = m.group("file")
        func = m.group("func")
        frames.append(Frame(pc=pc, function=None if func == "??" else func, file=file,
                            line=int(m.group("line")) if m.group("line") else None,
                            internal=is_internal(file, func, idf_path, project_root)))
    if not frames:
        return None
    return Backtrace(raw="gdb bt (esp_idf_panic_decoder)", frames=frames, decoded=True,
                     corrupted="Backtrace stopped" in res.output and len(frames) < 2)


def find_addr2line(prefix: str, env: dict[str, str]) -> str | None:
    return shutil.which(prefix + "addr2line", path=env.get("PATH") or env.get("Path"))


async def unwind(ev: DeviceEvent, elf: Path, *, toolprefix: str, env: dict[str, str],
                 idf_path: str | None, project_root: Path | None, rom_elf: Path | None = None) -> Backtrace:
    bt = ev.backtrace.model_copy(deep=True) if ev.backtrace else Backtrace()
    if not bt.frames:
        return bt
    tool = find_addr2line(toolprefix, env)
    if not tool:
        return bt
    # IDF 打印 Backtrace 时已经用 esp_cpu_process_stack_pc() 把返回地址换算到 call 指令上，直接解码即可
    pcs = [f.pc for f in bt.frames]
    rows = await addr2line(tool, elf, pcs, env)
    by_pc: dict[str, list[tuple]] = {}
    for row in rows:
        by_pc.setdefault(row[0].lower(), []).append(row)
    # 应用 ELF 里解不出来的地址（落在 ROM 里），用芯片的 ROM ELF 再解一次（2026-10-06）
    if rom_elf is not None and rom_elf.is_file():
        unknown = [pc for pc in pcs if all(r[1] is None for r in by_pc.get(pc.lower(), [(None, None)]))]
        if unknown:
            for row in await addr2line(tool, rom_elf, unknown, env):
                if row[1]:  # ROM 的源码不在本机：只留函数名，文件置空（帧算作内部帧，界面折叠）
                    by_pc[row[0].lower()] = [(row[0], f"{row[1]} (ROM)", None, None)]
    frames: list[Frame] = []
    for orig, q in zip(bt.frames, pcs, strict=True):
        hits = by_pc.get(q.lower()) or by_pc.get(hex(int(q, 16)).lower()) or []
        if not hits:
            frames.append(orig.model_copy(update={"internal": True}))
            continue
        for _pc, func, file, line in hits:
            frames.append(Frame(pc=orig.pc, sp=orig.sp, function=func, file=file, line=line,
                                internal=is_internal(file, func, idf_path, project_root)))
    bt.frames = frames
    bt.decoded = True
    return bt
