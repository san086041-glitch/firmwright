"""模拟板（开发 / 演示用，FIRMWRIGHT_SIM_BOARD=1 时启用）。

没有接真板子时，用它验证设备面板、实时串口、崩溃卡片、空闲通知、人工操作卡片这一整条链路。
它不是 QEMU，只模拟"串口上看得到的东西"：

- 默认伪装成一块原生 USB-JTAG 的 ESP32-S3（VID 0x303A / PID 0x1001，序列号 sim…）；
  FIRMWRIGHT_SIM_CHIP=esp32c3 等可以换成其他芯片（2026-10-06）：ROM 横幅、USB 识别信息（ESP32 / C2 是 CP210x 桥，
  S2 是 USB-OTG）、崩溃输出格式（Xtensa 的 Backtrace 行 / RISC-V 的 MEPC 寄存器转储）跟着变
- 复位后按真实格式输出启动日志；固件"运行"时周期性打印 LED 日志
- 编译走真的 idf.py；烧录是模拟的：记下烧录时工程源码的状态
- 烧录时源码里有 SIM_CRASH 标记 → 固件启动后 LoadProhibited 崩溃。崩溃地址取自真实 ELF 里
  app_main / 用户函数的地址，所以解码出的调用栈是真的（文件、行号都对）
- facts.toml 的 pass_marker：源码里有 TEST:…:PASS 的 printf 时，原样输出

所有输出都是这个模块生成的，界面和 trace 里能通过板子别名 "SIM" 认出来。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import cast

from ..platform.base import FlashResult, FlashScope, PortInfo, Stage
from ..platform.esp_idf import chips

SIM_PORT = "SIM1"
SIM_SERIAL = "5153494d0001"  # "QSIM"…

_BOOT_TAIL = (
    "I (24) boot: ESP-IDF v5.5 2nd stage bootloader\r\n"
    "I (25) boot: compile time Oct  3 2026 00:00:00\r\n"
    "I (290) main_task: Calling app_main()\r\n"
)
BOOT = (
    "ESP-ROM:esp32s3-20210327\r\n"
    "Build:Mar 27 2021\r\n"
    "rst:{rst},boot:0x8 (SPI_FAST_FLASH_BOOT)\r\n"
    "SPIWP:0xee\r\n"
    "mode:DIO, clock div:1\r\n" + _BOOT_TAIL
)


def sim_chip() -> chips.Chip:
    return chips.get(os.environ.get("FIRMWRIGHT_SIM_CHIP")) or chips.CHIPS["esp32s3"]


def boot_text(chip: chips.Chip) -> str:
    if chip.id == "esp32s3":
        return BOOT
    if chip.id == "esp32":  # 初代 ESP32 的 ROM 不打印 ESP-ROM:
        return ("ets Jun  8 2016 00:22:57\r\n\r\n" "rst:{rst},boot:0x13 (SPI_FAST_FLASH_BOOT)\r\n"
                "configsip: 0, SPIWP:0xee\r\n" "mode:DIO, clock div:2\r\n" + _BOOT_TAIL)
    return (f"ESP-ROM:{chip.rom_tags[0]}-20220101\r\n" "Build:Jan  1 2022\r\n"
            "rst:{rst},boot:0xc (SPI_FAST_FLASH_BOOT)\r\n" "SPIWP:0xee\r\n" "mode:DIO, clock div:1\r\n" + _BOOT_TAIL)


DEFAULT_HALF_MS = 500
TICK_MS = 10  # ESP-IDF 默认 CONFIG_FREERTOS_HZ=100


# ---------------------------------------------------------------------- 从源码推断固件行为（W7）
# W7 演示里撞到：LED 间隔写死 500 ms、TEST 标记一律 PASS，时序和自检类目标在模拟板上永远验证不了。
# 这里对源码做一个很小的常量求值：#define、单行 return 的函数、简单赋值、三目表达式。
# 算不出来就退回旧行为（500 ms / PASS）；需要时用 .firmwright/sim.toml 的 led_half_ms、[markers] 指定。


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"//[^\n]*", " ", src)


class _CEval:
    def __init__(self, src: str) -> None:
        src = _strip_comments(src)
        self.defines = {m.group(1): m.group(2).strip()
                        for m in re.finditer(r"^[ \t]*#define[ \t]+(\w+)[ \t]+([^\n]+)$", src, re.M)}
        self.funcs = {m.group(1): (m.group(2), m.group(3))
                      for m in re.finditer(r"\b(\w+)\s*\(\s*(?:const\s+)?\w+\s+(\w+)\s*\)\s*\{\s*return\s+([^;]+);\s*\}",
                                           src)}
        self.vars: dict[str, str] = {}
        for m in re.finditer(r"(?:^|[;{}]\s*)(?:[\w\s\*]*?\s)?(\w+)\s*=\s*([^;=][^;]*);", src):
            self.vars[m.group(1)] = m.group(2).strip()

    def value(self, expr: str, depth: int = 0) -> int | None:
        if depth > 10:
            return None
        e = expr.strip()
        e = re.sub(r"\(\s*(?:const\s+)?(?:u?int\d*_t|unsigned|int|long|TickType_t)\s*\)", "", e)  # 类型转换
        e = re.sub(r"\b(\d+)[uUlL]+\b", r"\1", e)
        e = re.sub(r"\bpdMS_TO_TICKS\s*\(", "(", e)
        for _ in range(5):  # 函数调用展开：f(x) → (body[param:=x])
            m = re.search(r"\b(\w+)\s*\(([^()]*)\)", e)
            if not m or m.group(1) not in self.funcs:
                break
            param, body = self.funcs[m.group(1)]
            e = e[:m.start()] + "(" + re.sub(rf"\b{param}\b", f"({m.group(2)})", body) + ")" + e[m.end():]

        def sub(m: re.Match) -> str:
            name = m.group(0)
            src = self.defines.get(name) or self.vars.get(name)
            if src is None:
                raise KeyError(name)
            v = self.value(src, depth + 1)
            if v is None:
                raise KeyError(name)
            return str(v)

        try:
            e = re.sub(r"\b[A-Za-z_]\w*\b", sub, e)
        except KeyError:
            return None
        e = e.replace("&&", " and ").replace("||", " or ")
        e = re.sub(r"!(?!=)", " not ", e)
        if not re.fullmatch(r"[\d\s()+\-*/%<>=!andotr]*", e):
            return None
        try:
            return int(eval(e.replace("/", "//"), {"__builtins__": {}}))  # noqa: S307 —— 只剩数字和运算符
        except Exception:
            return None


def sim_markers(src: str) -> list[str]:
    """源码里 printf 的 TEST 标记行。"%s" + 三目表达式（cond ? "PASS" : "FAIL"）按源码常量求值，算不出来当 PASS。"""
    ev = _CEval(src)
    out = []
    for m in re.finditer(r'"(TEST:[^"\\]*?)\\n"\s*(?:,\s*(.*?))?\)\s*;', _strip_comments(src), re.S):
        text, arg = m.group(1), m.group(2)
        if "%s" in text:
            val = "PASS"
            t = re.fullmatch(r'\s*(.+?)\s*\?\s*"(\w+)"\s*:\s*"(\w+)"\s*', arg or "", re.S)
            if t:
                cond = ev.value(t.group(1))
                if cond is not None:
                    val = t.group(2) if cond else t.group(3)
            text = text.replace("%s", val)
        out.append(text)
    return out


def led_half_ms(src: str) -> int:
    """LED 每次切换之间的延时：vTaskDelay(pdMS_TO_TICKS(x)) / vTaskDelay(x / portTICK_PERIOD_MS) / vTaskDelay(ticks)，
    按 FreeRTOS 的 tick（10 ms）取整，和真板子一样。"""
    ev = _CEval(src)
    for m in re.finditer(r"vTaskDelay\s*\(((?:[^()]|\([^()]*(?:\([^()]*\))?[^()]*\))*)\)", _strip_comments(src)):
        arg = m.group(1).strip()
        if "pdMS_TO_TICKS" in arg or "portTICK_PERIOD_MS" in arg:
            ms = ev.value(re.sub(r"/\s*portTICK_PERIOD_MS", "", arg))
            ticks = None if ms is None else ms // TICK_MS
        else:
            ticks = ev.value(arg)
        if ticks:
            return ticks * TICK_MS
    return DEFAULT_HALF_MS


class SimFirmware:
    """模拟板上"烧录"的固件：烧录时的源码快照 + ELF 里的地址。"""

    def __init__(self) -> None:
        self.crash = False
        self.markers: list[str] = []
        self.addrs: dict[str, int] = {}
        self.flashed = False
        self.crash_fn: str | None = None
        self.led_half_ms = DEFAULT_HALF_MS

    @classmethod
    def from_project(cls, root: Path, nm: str | None) -> SimFirmware:
        fw = cls()
        fw.flashed = True
        src = ""
        for p in (root / "main").glob("*.c"):
            src += p.read_text("utf-8", errors="replace")
        fw.crash = "SIM_CRASH" in src
        # 可选的 .firmwright/sim.toml：crash_if = 描述 bug 写法的正则（匹配到 = 固件里还有这个 bug），
        # crash_fn = 崩溃时第一帧落在哪个函数（地址从 ELF 里取）
        cfg_path = root / ".firmwright" / "sim.toml"
        cfg: dict = {}
        if cfg_path.is_file():
            import tomllib

            cfg = tomllib.loads(cfg_path.read_text("utf-8"))
            if cfg.get("crash_if") and re.search(cfg["crash_if"], src, re.M):
                fw.crash = True
                # fixed_if：常见的修法（判空、指向有效变量）出现就当作修好了
                if cfg.get("fixed_if") and re.search(cfg["fixed_if"], src, re.M):
                    fw.crash = False
            fw.crash_fn = cfg.get("crash_fn")
        fw.markers = sim_markers(src)
        fw.led_half_ms = led_half_ms(src)
        if cfg_path.is_file():
            fw.led_half_ms = int(cfg.get("led_half_ms", fw.led_half_ms))
            for name, val in (cfg.get("markers") or {}).items():  # [markers] half_period = "FAIL"
                fw.markers = [f"TEST:{name}:{val}" if m.startswith(f"TEST:{name}:") else m for m in fw.markers]
        elf = next((root / "build").glob("*.elf"), None) if (root / "build").is_dir() else None
        if nm and elf:
            out = subprocess.run([nm, str(elf)], capture_output=True, text=True,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) == 3:
                    fw.addrs[parts[2]] = int(parts[0], 16)
        return fw


class SimSerial:
    """pyserial 的替身。复位（RTS 拉高）时重新"启动固件"。"""

    def __init__(self, board: SimBoard) -> None:
        self.board = board
        self.dtr = False
        self._rts = False
        self.closed = False

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, v: bool) -> None:
        if v and not self._rts:
            self.board.boot("0xc (RTC_SW_CPU_RST)")
        self._rts = v

    @property
    def in_waiting(self) -> int:
        return len(self.board.buf)

    def read(self, n: int) -> bytes:
        if self.closed:
            raise OSError("closed")
        out = self.board.take(n)
        if not out:
            time.sleep(0.02)
        return out

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self.closed = True


class SimBoard:
    def __init__(self, chip: chips.Chip | None = None) -> None:
        self.chip = chip or sim_chip()
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.fw = SimFirmware()
        self._gen = 0
        self.present = True

    def port_info(self) -> PortInfo:
        desc = f"SIM {self.chip.name} (simulated board)"
        if self.chip.usb_serial_jtag:
            return PortInfo(device=SIM_PORT, vid=chips.ESPRESSIF_VID, pid=chips.USB_JTAG_PID, serial_number=SIM_SERIAL,
                            description=desc)
        if self.chip.usb_otg:  # S2：ROM 的 USB CDC
            return PortInfo(device=SIM_PORT, vid=chips.ESPRESSIF_VID, pid=0x0002, serial_number=SIM_SERIAL,
                            description=desc)
        return PortInfo(device=SIM_PORT, vid=0x10C4, pid=0xEA60, serial_number=SIM_SERIAL, description=desc)  # CP210x

    def push(self, text: str) -> None:
        with self.lock:
            self.buf += text.encode()

    def take(self, n: int) -> bytes:
        with self.lock:
            out = bytes(self.buf[:n])
            del self.buf[:n]
        return out

    # ---- 固件行为

    def boot(self, rst: str = "0x1 (POWERON)") -> None:
        self._gen += 1
        gen = self._gen
        threading.Thread(target=self._run, args=(gen, rst), daemon=True).start()

    def _run(self, gen: int, rst: str) -> None:
        time.sleep(0.15)
        self.push(boot_text(self.chip).format(rst=rst))
        if not self.fw.flashed:
            self.push("I (300) app: (nothing has been flashed to the simulated board yet)\r\n")
            return
        time.sleep(0.1)
        self.push("I (312) blink: app_main started\r\n")
        for m in self.fw.markers:
            self.push(m + "\r\n")
        if self.fw.crash:
            time.sleep(0.2)
            self.push(self.panic_text())
            return
        on = False
        half = max(self.fw.led_half_ms, TICK_MS)
        for i in range(40):
            if gen != self._gen:
                return
            on = not on
            if i < 6:
                self.push(f"I ({800 + i * half}) blink: led {'on' if on else 'off'}\r\n")
            time.sleep(half / 1000)

    def crash_now(self) -> None:
        """立刻崩溃一次（测试空闲通知用）。"""
        self._gen += 1
        self.push(self.panic_text())

    def panic_text(self) -> str:
        a = self.fw.addrs
        user = a.get(self.fw.crash_fn or "") or a.get("blink_set") or a.get("app_main") or 0x42008C1A
        caller = a.get("app_main") or 0x42008C39
        task = a.get("main_task") or 0x4201A3E3
        pcs = [user + 6, caller + 20, task + 8]
        if self.chip.arch == "riscv":
            # RISC-V 的崩溃输出没有 Backtrace 行，只有寄存器转储（真板子还有 Stack memory，这里不模拟，解码退回 MEPC / RA 两帧）
            return (
                "Guru Meditation Error: Core  0 panic'ed (Load access fault). Exception was unhandled.\r\n\r\n"
                "Core  0 register dump:\r\n"
                f"MEPC    : 0x{pcs[0]:08x}  RA      : 0x{pcs[1]:08x}  SP      : 0x4087f0a0  GP      : 0x40811e00\r\n"
                "TP      : 0x4087f2a0  T0      : 0x4fc0a9f0  T1      : 0x00000000  T2      : 0x00000000\r\n"
                "MSTATUS : 0x00001881  MTVEC   : 0x40800001  MCAUSE  : 0x00000005  MTVAL   : 0x00000000\r\n"
                "MHARTID : 0x00000000\r\n\r\n"
                "ELF file SHA256: 51a5e1c0ffee0000\r\n\r\n"
                "Rebooting...\r\n"
            )
        bt = " ".join(f"0x{pc:08x}:0x3fc99e{40 + 32 * i:02x}" for i, pc in enumerate(pcs))
        return (
            "Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.\r\n\r\n"
            "Core  0 register dump:\r\n"
            f"PC      : 0x{pcs[0]:08x}  PS      : 0x00060730  A0      : 0x82008c3c  A1      : 0x3fc99e40\r\n"
            "A2      : 0x00000000  A3      : 0x00000001  A4      : 0x00000000  A5      : 0x00000000\r\n"
            "EXCCAUSE: 0x0000001c  EXCVADDR: 0x00000000  LBEG    : 0x40056f5c  LEND    : 0x40056f72\r\n\r\n\r\n"
            f"Backtrace: {bt}\r\n\r\n\r\n"
            "ELF file SHA256: 51a5e1c0ffee0000\r\n\r\n"
            "Rebooting...\r\n"
        )


class SimHub:
    """装配：给 DeviceManager 的 lister / opener，以及包一层平台适配器把烧录换成模拟的。"""

    def __init__(self, adapter) -> None:
        self.board = SimBoard()
        self.real = adapter

    def lister(self, real_lister):
        def lister() -> list[PortInfo]:
            ports = [p for p in real_lister()]
            if self.board.present:
                ports.append(self.board.port_info())
            return ports

        return lister

    def opener(self, real_opener):
        def opener(port: str, baud: int):
            if port == SIM_PORT:
                return SimSerial(self.board)
            return real_opener(port, baud)

        return opener

    def wrap(self) -> SimAdapter:
        return SimAdapter(self.real, self)


class SimAdapter:
    """把真实的 EspIdfAdapter 包一层：只有烧录模拟板时走假的，其余全部委托给真的。"""

    def __init__(self, real, hub: SimHub) -> None:
        self._real = real
        self._hub = hub
        self.id = real.id

    def __getattr__(self, name):
        return getattr(self._real, name)

    def identify(self, port: PortInfo):
        """模拟板自己知道是什么芯片：直接给出 chip_hint（原来要等一次启动日志或点 Identify，
        设备栏一直显示 "unknown chip"）。真板子照常交给真适配器（USB-JTAG 的 PID 各芯片共用，认不出）。"""
        ident = self._real.identify(port)
        if ident is not None and port.device == SIM_PORT:
            ident = ident.model_copy(update={"chip_hint": self._hub.board.chip.id})
        return ident

    async def flash_image(self, ctx, port: str, archive: Path, scope="app", **kw) -> FlashResult:
        """回退时重烧存档固件。模拟板按回退后的源码重新生成"固件行为"（sim.toml 的正则匹配源码）。"""
        if port != SIM_PORT:
            return await self._real.flash_image(ctx, port, archive, scope, **kw)
        env = await self._real.idf.full()
        nm = shutil.which(self._hub.board.chip.toolprefix + "nm", path=env.get("PATH"))
        await ctx.progress(f"Flashing archived firmware → {port} (simulated board)")
        await asyncio.sleep(1.0)
        self._hub.board.fw = SimFirmware.from_project(ctx.cwd, nm)
        out = FlashResult(op="flash", ok=True, port=port, scope=cast(FlashScope, scope), summary=f"archived firmware {scope} · {port} (simulated board)",
                          stages=[Stage(name="sim-flash", ok=True, duration_ms=1000)], duration_ms=1000)
        fa = archive / "flasher_args.json"
        app = json.loads(fa.read_text("utf-8")).get("app", {}).get("file") if fa.is_file() else None
        if app and (archive / app).is_file():
            out.image_sha256 = hashlib.sha256((archive / app).read_bytes()).hexdigest()
        return out

    async def chip_info(self, ctx, port: str) -> dict:
        if port != SIM_PORT:
            return await self._real.chip_info(ctx, port)
        await asyncio.sleep(0.5)
        c = self._hub.board.chip
        feats = ", ".join(x for x in ("WiFi" if c.wifi else "", "BT" if c.bt == "BT Classic + BLE" else "",
                                       "BLE" if c.bt else "", "IEEE802.15.4" if c.ieee802154 else "") if x)
        return {"ok": True, "chip": c.id, "description": f"{c.name} (revision v0.2) (simulated board)",
                "features": feats or "none", "mac": "53:51:49:4d:00:01", "flash_size": "4MB" if c.id == "esp32c2" else "16MB",
                "crystal": "40MHz", "revision": 2, "summary_line": c.summary()}

    async def read_flash(self, ctx, port: str, dest: Path, *, size: str = "ALL") -> dict:
        if port != SIM_PORT:
            return await self._real.read_flash(ctx, port, dest, size=size)
        await asyncio.sleep(1.0)
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = b"\xe9" + b"\xff" * (64 * 1024 - 1)  # 模拟板：64 KB 的假镜像
        dest.write_bytes(data)
        return {"ok": True, "path": str(dest), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "duration_ms": 1000}

    async def flash(self, ctx, port: str, what, **kw) -> FlashResult:
        if port != SIM_PORT:
            return await self._real.flash(ctx, port, what, **kw)
        info = self._real.detect(ctx.cwd)
        if not info or not info.built:
            return FlashResult(op="flash", ok=False, port=port, scope=what, error_class="not_built",
                               summary="not built")
        env = await self._real.idf.full()
        nm = shutil.which(self._hub.board.chip.toolprefix + "nm", path=env.get("PATH"))
        await ctx.progress(f"Flashing {what} → {port} (simulated board)")
        await asyncio.sleep(1.0)
        self._hub.board.fw = SimFirmware.from_project(ctx.cwd, nm)
        out = FlashResult(op="flash", ok=True, port=port, scope=what, summary=f"{what} · {port} (simulated board) · 1.0s",
                          stages=[Stage(name="sim-flash", ok=True, duration_ms=1000)], duration_ms=1000)
        app = ctx.cwd / "build" / ((self._real._description(ctx.cwd) or {}).get("app_bin") or "-")
        if app.is_file():  # 和真板子一样记下固件哈希（checkpoint 时间线上显示）
            out.image_sha256 = hashlib.sha256(app.read_bytes()).hexdigest()
        return out
