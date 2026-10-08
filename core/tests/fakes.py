"""测试用的假硬件：假串口（回放录制的日志）、假端口列表、假平台适配器。"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from firmwright.facts import Facts
from firmwright.platform.base import BoardIdentity, FlashResult, PortInfo, ProjectInfo, Stage
from firmwright.platform.esp_idf.decode import fault_evidence
from firmwright.platform.esp_idf.logparse import EspLogParser

S3_BOOT = """ESP-ROM:esp32s3-20210327
Build:Mar 27 2021
rst:0x1 (POWERON),boot:0x8 (SPI_FAST_FLASH_BOOT)
SPIWP:0xee
mode:DIO, clock div:1
I (24) boot: ESP-IDF v5.5 2nd stage bootloader
I (290) main_task: Calling app_main()
I (312) blink: app_main started
"""

S3_PANIC = """Guru Meditation Error: Core  0 panic'ed (LoadProhibited). Exception was unhandled.

Core  0 register dump:
PC      : 0x42008c1d  PS      : 0x00060730  A0      : 0x82008c3c  A1      : 0x3fc99e40
A2      : 0x00000000  A3      : 0x00000001  A4      : 0x00000000  A5      : 0x00000000
EXCCAUSE: 0x0000001c  EXCVADDR: 0x00000000  LBEG    : 0x40056f5c  LEND    : 0x40056f72


Backtrace: 0x42008c1a:0x3fc99e40 0x42008c39:0x3fc99e60 0x4201a3e3:0x3fc99e80 0x4037a1fd:0x3fc99eb0




ELF file SHA256: 4f2b3c1d

Rebooting...
"""

S3_REBOOT = """ESP-ROM:esp32s3-20210327
rst:0xc (RTC_SW_CPU_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)
I (24) boot: ESP-IDF v5.5 2nd stage bootloader
"""


class FakeSerial:
    """read() 依次吐出预先排好的数据；push() 在测试中途再塞数据（模拟设备输出）。"""

    def __init__(self, port: str, baud: int, script: list[bytes] | None = None) -> None:
        self.port, self.baud = port, baud
        self._data = bytearray()
        self._lock = threading.Lock()
        self.dtr = False
        self.rts = False
        self.closed = False
        self.resets = 0
        self.on_reset = None
        for chunk in script or []:
            self.push(chunk)

    def push(self, data: bytes | str) -> None:
        if isinstance(data, str):
            data = data.encode()
        with self._lock:
            self._data += data

    @property
    def in_waiting(self) -> int:
        return len(self._data)

    def read(self, n: int) -> bytes:
        if self.closed:
            raise OSError("closed")
        with self._lock:
            if not self._data:
                pass
            out = bytes(self._data[:n])
            del self._data[:n]
        if not out:
            time.sleep(0.01)
        return out

    def write(self, data: bytes) -> int:
        return len(data)

    def close(self) -> None:
        self.closed = True

    def __setattr__(self, k, v):
        if k == "rts" and v is True and getattr(self, "on_reset", None):
            object.__setattr__(self, "resets", self.resets + 1)
            self.on_reset(self)
        object.__setattr__(self, k, v)


class FakeAdapter:
    """平台适配器的假实现：识别 VID 0x303A，日志解析用真的 EspLogParser。"""

    id = "fake"

    def __init__(self, flash_ok: bool = True) -> None:
        self.flash_ok = flash_ok
        self.flash_calls: list[tuple[str, str]] = []
        self.reflashed: list[str] = []

    def identify(self, p: PortInfo):
        if p.vid == 0x303A:
            return BoardIdentity(id=f"usb-{p.serial_number}", usb_jtag=True, link="usb_serial_jtag", native_usb=True)
        return None

    def log_parser(self, board_id, chip, facts=None, log_file=""):
        return EspLogParser(board_id, log_file, facts)

    def detect(self, root: Path):
        return ProjectInfo(root=str(root), name="demo", target="esp32s3", built=True)

    def security_enabled(self, root):
        return []

    # ---- 2026-10-06 全系列芯片
    def supported_chips(self):
        from firmwright.platform.esp_idf import chips

        return tuple(chips.CHIPS)

    def chip_summary(self, chip):
        from firmwright.platform.esp_idf import chips

        c = chips.get(chip)
        return c.summary() if c else None

    def console_check(self, root, link):
        return None

    # ---- W5：固件"内容"就是烧录时 main.c 的文本，方便断言重烧的是哪个版本
    def workspace_excludes(self):
        return ["**/build/**"]

    def worktree_seeds(self):
        return ["sdkconfig", ".firmwright"]

    def gitignore_template(self):
        return "build/\n"

    def archive_image(self, root: Path, dest: Path) -> bool:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "fw.txt").write_text((root / "main.c").read_text() if (root / "main.c").exists() else "")
        return True

    async def flash_image(self, ctx, port, archive: Path, scope="app"):
        self.reflashed.append((archive / "fw.txt").read_text())
        return FlashResult(op="flash", ok=True, port=port, scope=scope, image_sha256="archived")

    async def flash(self, ctx, port, what):
        self.flash_calls.append((port, what))
        if self.flash_ok:
            import hashlib

            src = (ctx.cwd / "main.c").read_bytes() if (ctx.cwd / "main.c").exists() else b""
            return FlashResult(op="flash", ok=True, port=port, scope=what, stages=[Stage(name="app-flash", ok=True)],
                               image_sha256=hashlib.sha256(src).hexdigest())
        from firmwright.platform.base import NextAction

        return FlashResult(op="flash", ok=False, port=port, scope=what, error_class="sync_failed",
                           next_actions=[NextAction(kind="enter_download_mode", description="按住 BOOT 再按 RESET",
                                                    human=True)])

    async def fault_evidence(self, ev):
        return fault_evidence(ev)

    async def unwind(self, ev, elf, project_root=None):
        return ev.backtrace

    async def env(self):
        return {}

    def risk_rules(self):
        from firmwright.platform.esp_idf.adapter import EspIdfAdapter

        return EspIdfAdapter.risk_rules(self)


def board_port(serial="aabbccddeeff", device="COM7") -> PortInfo:
    return PortInfo(device=device, vid=0x303A, pid=0x1001, serial_number=serial, description="USB JTAG/serial debug unit")


FACTS = Facts(chip="esp32s3", boot_banner="app_main started", pass_marker=["TEST:.*:PASS"],
              fail_marker=["TEST:.*:FAIL"], boot_window_s=3)
