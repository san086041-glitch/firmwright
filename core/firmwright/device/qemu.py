"""QEMU 板（评测用，2026-10-06 评测方案：QEMU 为主）。

FIRMWRIGHT_QEMU_BOARD=esp32s3（或 esp32 / esp32c3）时启用。和模拟板（sim.py）不同，这里跑的是**真固件**：
烧录 = 把编译产物合并成整片 flash 镜像，再用这个镜像（重新）启动 Espressif 的 QEMU；串口、复位都接到 QEMU 上。
对设备管理器来说它就是一块普通的板子，所以事件解析、崩溃解码、await_marker、checkpoint 重烧全部走真实代码路径。

启动参数照抄 ESP-IDF 的 `idf.py qemu`（tools/idf_py_actions/qemu_ext.py）：
- 机型 `-M <chip>`、默认 eFuse 镜像（从 qemu_ext.py 里读，不自己编）、`-drive file=<flash>,if=mtd`
- 关掉定时器组的看门狗（idf.py 的默认做法；FIRMWRIGHT_QEMU_WDT=1 可以打开，看门狗类评测题要用）
- 串口：`-serial tcp:127.0.0.1:<端口>,server=on,wait=on`——QEMU 等我们连上才开始运行，启动日志不会丢
  （和 idf.py qemu monitor 一样）；复位：`-monitor tcp:…` 上发 `system_reset`
芯片：IDF v5.5 的 QEMU 支持 esp32 / esp32s3 / esp32c3（本机装的是 esp_develop_9.0.0_20240606）。
QEMU 板在设备管理器里伪装成 UART 桥（CP210x）上的板子：QEMU 的串口就是 UART0，控制台检查因此是对的。
"""

from __future__ import annotations

import binascii
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import cast

from ..config import app_home
from ..osal import run_process
from ..platform.base import FlashResult, FlashScope, PortInfo, Stage
from ..platform.esp_idf import chips

QEMU_PORT = "QEMU1"
QEMU_CHIPS = {"esp32": ("qemu-xtensa", "qemu-system-xtensa"), "esp32s3": ("qemu-xtensa", "qemu-system-xtensa"),
              "esp32c3": ("qemu-riscv32", "qemu-system-riscv32")}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def find_qemu(tools_path: str, chip: str) -> Path | None:
    pkg, prog = QEMU_CHIPS[chip]
    for root in (Path(tools_path), Path(tools_path) / "tools"):  # EIM 布局 / idf_tools 布局
        for exe in sorted(root.glob(f"{pkg}/*/qemu/bin/{prog}.exe")) + sorted(root.glob(f"{pkg}/*/qemu/bin/{prog}")):
            return exe
    return shutil.which(prog)  # type: ignore[return-value]


def qemu_target(idf_path: str, chip: str) -> tuple[str, bytes, str, str]:
    """从 IDF 的 qemu_ext.py 读出 (机型参数, 默认 eFuse, 下载模式参数, eFuse 设备名)。"""
    # qemu_ext.py 里的 QemuTarget 数据类和 QEMU_TARGETS 字典是纯数据（字符串 + binascii.unhexlify）：
    # 只把这两段拿出来执行，不导入整个模块（它依赖 click 等 idf.py 的运行环境）
    src = (Path(idf_path) / "tools" / "idf_py_actions" / "qemu_ext.py").read_text("utf-8")
    start = src.find("class QemuTarget")
    table = src.find("QEMU_TARGETS")
    end = src.find("\n}", table)
    if start < 0 or table < 0 or end < 0:
        raise RuntimeError("Unrecognized qemu_ext.py layout")
    from dataclasses import dataclass

    ns: dict = {"binascii": binascii, "dataclass": dataclass, "Dict": dict}
    exec(compile("@dataclass\n" + src[start:end + 2], "qemu_ext.py", "exec"), ns)  # noqa: S102 —— 本机 IDF 安装里的数据定义
    t = ns["QEMU_TARGETS"].get(chip)
    if t is None:
        raise RuntimeError(f"QEMU does not support {chip} in this ESP-IDF")
    return t.qemu_args, t.default_efuse, t.boot_mode_arg, t.efuse_device or f"nvram.{chip}.efuse"


class QemuMachine:
    """一个 QEMU 进程。烧录 = 换镜像重启；复位 = monitor 的 system_reset。"""

    def __init__(self, chip: str, *, idf_path: str, tools_path: str, workdir: Path | None = None) -> None:
        if chip not in QEMU_CHIPS:
            raise ValueError(f"QEMU supports {', '.join(QEMU_CHIPS)}, not {chip}")
        self.chip = chip
        self.idf_path = idf_path
        self.exe = find_qemu(tools_path, chip)
        self.workdir = workdir or app_home() / "qemu" / chip
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.image = self.workdir / "flash.bin"
        self.efuse = self.workdir / "efuse.bin"
        self.proc: subprocess.Popen | None = None
        self.serial_port = 0
        self.monitor_port = 0
        self.lock = threading.Lock()
        self.generation = 0  # 每次（重新）启动加一；串口发现变了就重连
        self.wdt = os.environ.get("FIRMWRIGHT_QEMU_WDT") == "1"
        self._kill_stale()

    # ---- 进程

    def _kill_stale(self) -> None:
        """上一次核心被强杀时留下的 QEMU（pid 文件）先结束掉，否则它占着 flash 镜像。"""
        pidf = self.workdir / "qemu.pid"
        if pidf.is_file():
            with contextlib.suppress(Exception):
                pid = int(pidf.read_text().strip())
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    os.kill(pid, 9)
            pidf.unlink(missing_ok=True)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
                with contextlib.suppress(Exception):
                    self.proc.wait(5)
            self.proc = None
            (self.workdir / "qemu.pid").unlink(missing_ok=True)

    def start(self) -> None:
        if not self.exe:
            raise RuntimeError(f"QEMU for {self.chip} is not installed (idf_tools.py install {QEMU_CHIPS[self.chip][0]})")
        if not self.image.is_file():
            raise RuntimeError("Nothing has been flashed to the QEMU board yet")
        self.stop()
        args, efuse, _boot, efuse_dev = qemu_target(self.idf_path, self.chip)
        if not self.efuse.is_file():
            self.efuse.write_bytes(efuse)
        self.serial_port, self.monitor_port = _free_port(), _free_port()
        argv = [str(self.exe), *args.split(),
                "-drive", f"file={self.image},if=mtd,format=raw",
                "-drive", f"file={self.efuse},if=none,format=raw,id=efuse",
                "-global", f"driver={efuse_dev},property=drive,value=efuse",
                "-nographic", "-serial", f"tcp:127.0.0.1:{self.serial_port},server=on,wait=on",
                "-monitor", f"tcp:127.0.0.1:{self.monitor_port},server=on,wait=off"]
        if not self.wdt:
            argv += ["-global", f"driver=timer.{self.chip}.timg,property=wdt_disable,value=true"]
        with self.lock:
            log = (self.workdir / "qemu.log").open("wb")
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            (self.workdir / "qemu.pid").write_text(str(self.proc.pid))
            self.generation += 1

    def reset(self) -> None:
        """monitor 的 system_reset：相当于按 RESET。"""
        if not self.running:
            return
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", self.monitor_port), timeout=2) as s:
            s.sendall(b"system_reset\n")
            time.sleep(0.1)

    def connect_serial(self, timeout: float = 5.0) -> socket.socket | None:
        if not self.running:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                s = socket.create_connection(("127.0.0.1", self.serial_port), timeout=1)
                s.settimeout(0.05)
                return s
            except OSError:
                time.sleep(0.1)
        return None


class QemuSerial:
    """pyserial 的替身：读写 QEMU 的串口 socket。QEMU 重启（重新烧录）后自动重连；RTS 上升沿 = 复位。"""

    def __init__(self, machine: QemuMachine) -> None:
        self.m = machine
        self.dtr = False
        self._rts = False
        self.closed = False
        self._sock: socket.socket | None = None
        self._gen = -1

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, v: bool) -> None:
        if v and not self._rts:
            self.m.reset()
        self._rts = v

    @property
    def in_waiting(self) -> int:
        return 0

    def _ensure(self) -> socket.socket | None:
        if self._sock is not None and self._gen == self.m.generation and self.m.running:
            return self._sock
        if self._sock is not None:
            with contextlib.suppress(Exception):
                self._sock.close()
            self._sock = None
        if self.m.running:
            self._sock = self.m.connect_serial(timeout=1.0)
            self._gen = self.m.generation
        return self._sock

    def read(self, n: int) -> bytes:
        if self.closed:
            raise OSError("closed")
        s = self._ensure()
        if s is None:
            time.sleep(0.1)  # 还没烧录 / QEMU 没在跑：像一块没有输出的板子
            return b""
        try:
            data = s.recv(max(n, 4096))
        except TimeoutError:
            return b""
        except OSError:
            self._sock = None
            return b""
        if not data:  # QEMU 关掉了连接（进程退出 / 重启）
            self._sock = None
            time.sleep(0.05)
        return data

    def write(self, data: bytes) -> int:
        s = self._ensure()
        if s is None:
            return 0
        with contextlib.suppress(OSError):
            s.sendall(data)
        return len(data)

    def close(self) -> None:
        self.closed = True
        if self._sock is not None:
            with contextlib.suppress(Exception):
                self._sock.close()
        self._sock = None


class QemuHub:
    """装配：给 DeviceManager 的 lister / opener，以及包一层平台适配器把烧录换成"合并镜像 + 重启 QEMU"。"""

    def __init__(self, adapter, chip: str) -> None:
        self.real = adapter
        self.machine = QemuMachine(chip, idf_path=adapter.idf.install.path, tools_path=adapter.idf.install.tools_path)

    def port_info(self) -> PortInfo:
        # 伪装成 CP210x 桥上的板子：QEMU 的串口就是 UART0（控制台检查按 UART 判断）
        return PortInfo(device=QEMU_PORT, vid=0x10C4, pid=0xEA60, serial_number=f"qemu{self.machine.chip}",
                        description=f"QEMU {chips.display_name(self.machine.chip)} (emulated board)")

    def lister(self, real_lister):
        def lister() -> list[PortInfo]:
            return [*real_lister(), self.port_info()]

        return lister

    def opener(self, real_opener):
        def opener(port: str, baud: int):
            if port == QEMU_PORT:
                return QemuSerial(self.machine)
            return real_opener(port, baud)

        return opener

    def wrap(self) -> QemuAdapter:
        return QemuAdapter(self.real, self)

    def close(self) -> None:
        self.machine.stop()


class QemuAdapter:
    """把真实的 EspIdfAdapter 包一层：烧录 QEMU 板时走"合并镜像 + 重启 QEMU"，其余全部委托给真的。"""

    def __init__(self, real, hub: QemuHub) -> None:
        self._real = real
        self._hub = hub
        self.id = real.id

    def __getattr__(self, name):
        return getattr(self._real, name)

    def identify(self, port):
        """QEMU 板自己知道是什么芯片：发现时就给出 chip_hint（和模拟板一样，不再显示 unknown chip）。"""
        ident = self._real.identify(port)
        if ident is not None and port.device == QEMU_PORT:
            ident = ident.model_copy(update={"chip_hint": self._hub.machine.chip})
        return ident

    async def _merge(self, ctx, cwd: Path, argv_tail: list[str], size: str) -> tuple[bool, str, int]:
        m = self._hub.machine
        tmp = m.workdir / "flash.new.bin"
        argv = [self._real.idf.install.python, "-m", "esptool", f"--chip={m.chip}", "merge_bin", f"--output={tmp}",
                f"--fill-flash-size={size}", *argv_tail]
        env = await self._real.idf.full()
        res = await run_process(argv, cwd=cwd, env=env, timeout=120, cancel=ctx.cancel)
        if not res.ok or not tmp.is_file():
            return False, res.output, res.duration_ms
        m.stop()
        os.replace(tmp, m.image)
        m.start()
        return True, res.output, res.duration_ms

    @staticmethod
    def _flash_size(root: Path) -> str:
        sdk = root / "sdkconfig"
        if sdk.is_file():
            if mm := re.search(r'^CONFIG_ESPTOOLPY_FLASHSIZE="(\w+)"', sdk.read_text("utf-8", errors="replace"), re.M):
                return mm.group(1)
        return "4MB"

    async def flash(self, ctx, port: str, what, **kw) -> FlashResult:
        if port != QEMU_PORT:
            return await self._real.flash(ctx, port, what, **kw)
        m = self._hub.machine
        info = self._real.detect(ctx.cwd)
        if info is not None and info.target and info.target != m.chip:
            return FlashResult(op="flash", ok=False, port=port, scope=what, error_class="chip_mismatch",
                               summary=f"The project targets {info.target}; this QEMU board emulates {m.chip}")
        await ctx.progress(f"Building and writing the flash image → {port} (QEMU)")
        async with self._real._build_slot(ctx):  # 和真烧录一样：源码有改动时先增量编译
            built = await self._real._ninja(ctx, "all", timeout=1800, progress_prefix="build")
        if not built.ok:
            return FlashResult(op="flash", ok=False, port=port, scope=what, error_class="build_error",
                               summary="The build failed; run build to see the errors", log_tail=built.output[-2000:])
        build = ctx.cwd / "build"
        if what == "erase_all":
            m.stop()
            m.image.write_bytes(b"\xff" * (4 * 1024 * 1024))
        ok, out, ms = await self._merge(ctx, build, ["@flash_args"], self._flash_size(ctx.cwd))
        res = FlashResult(op="flash", ok=ok, port=port, scope=cast(FlashScope, what),
                          summary=(f"{what} · {port} (QEMU: whole flash image rewritten) · {ms / 1000:.1f}s" if ok
                                   else "esptool merge_bin failed"),
                          stages=[Stage(name="merge_bin + restart QEMU", ok=ok, duration_ms=ms)],
                          error_class=None if ok else "flash_failed", log_tail="" if ok else out[-2000:],
                          duration_ms=ms)
        desc = self._real._description(ctx.cwd) or {}
        app = build / desc.get("app_bin", "-")
        if ok and app.is_file():
            res.image_sha256 = hashlib.sha256(app.read_bytes()).hexdigest()
        return res

    async def flash_image(self, ctx, port: str, archive: Path, scope="app", **kw) -> FlashResult:
        """回退 checkpoint 时重烧存档的固件：按 flasher_args.json 把存档里的 bin 合并成镜像。"""
        if port != QEMU_PORT:
            return await self._real.flash_image(ctx, port, archive, scope, **kw)
        fa = json.loads((archive / "flasher_args.json").read_text("utf-8"))
        files = fa.get("flash_files") or {}
        tail = [*fa.get("write_flash_args", [])]
        for off, f in sorted(files.items(), key=lambda kv: int(kv[0], 16)):
            tail += [off, f]
        size = next((tail[i + 1] for i, a in enumerate(tail[:-1]) if a == "--flash_size"), "4MB")
        size = size if size != "keep" else "4MB"
        ok, out, ms = await self._merge(ctx, archive, tail, size)
        res = FlashResult(op="flash", ok=ok, port=port, scope=cast(FlashScope, scope),
                          summary=f"archived firmware · {port} (QEMU) · {ms / 1000:.1f}s" if ok else "merge_bin failed",
                          stages=[Stage(name="merge_bin + restart QEMU", ok=ok, duration_ms=ms)],
                          error_class=None if ok else "flash_failed", log_tail="" if ok else out[-2000:], duration_ms=ms)
        app = fa.get("app", {}).get("file")
        if ok and app and (archive / app).is_file():
            res.image_sha256 = hashlib.sha256((archive / app).read_bytes()).hexdigest()
        return res

    async def chip_info(self, ctx, port: str) -> dict:
        if port != QEMU_PORT:
            return await self._real.chip_info(ctx, port)
        c = chips.CHIPS[self._hub.machine.chip]
        return {"ok": True, "chip": c.id, "description": f"{c.name} (QEMU emulated board)", "features": "emulated",
                "mac": None, "flash_size": "4MB", "crystal": "40MHz", "summary_line": c.summary()}

    async def read_flash(self, ctx, port: str, dest: Path, *, size: str = "ALL") -> dict:
        if port != QEMU_PORT:
            return await self._real.read_flash(ctx, port, dest, size=size)
        m = self._hub.machine
        if not m.image.is_file():
            return {"ok": False, "error_class": "empty", "summary": "Nothing has been flashed to the QEMU board yet"}
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(m.image, dest)
        data = dest.read_bytes()
        return {"ok": True, "path": str(dest), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "duration_ms": 0}
