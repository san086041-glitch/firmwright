"""EspIdfAdapter：第一版唯一的平台适配器（I06）。

操作划分照搬 ESP-IDF 官方 MCP：set_target / build / flash / clean；项目状态对应它的 project://status。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from ...config import BuildConfig, app_home
from ...device.events import Backtrace, DeviceEvent
from ...facts import Facts
from ...osal import ProcResult, run_process
from ...osal.filelock import file_lock
from ...permissions.engine import RiskRule
from ..base import (
    BoardIdentity,
    BuildResult,
    FaultEvidence,
    FlashResult,
    FlashScope,
    NextAction,
    OpResult,
    PortInfo,
    ProjectInfo,
    SizeReport,
    Stage,
)
from . import chips, decode
from .chips import ESPRESSIF_VID, UART_BRIDGES, USB_JTAG_PID
from .env import IdfEnv
from .logparse import EspLogParser
from .parse import build_next_actions, classify_flash, parse_build_output

FLASH_TARGET: dict[str, str] = {"app": "app-flash", "all": "flash", "bootloader": "bootloader-flash",
                                "partition_table": "partition-table-flash", "erase_all": "erase-flash"}
SECURITY_KEYS = re.compile(r"^CONFIG_(SECURE_BOOT|SECURE_FLASH_ENC_ENABLED|FLASH_ENCRYPTION_ENABLED)=y", re.M)
PROGRESS = re.compile(r"^\[(\d+)/(\d+)\]")
APP_SIZE = re.compile(r"binary size (0x[0-9a-f]+) bytes\. Smallest app partition is (0x[0-9a-f]+) bytes", re.I)


class EspIdfAdapter:
    id = "esp-idf"

    def __init__(self, idf: IdfEnv, build: BuildConfig | None = None, lock_path: Path | None = None) -> None:
        self.idf = idf
        self.build_cfg = build or BuildConfig()
        self.lock_path = lock_path or app_home() / "build.lock"

    @contextlib.asynccontextmanager
    async def _build_slot(self, ctx):
        """编译类操作排队（跨进程）：同一时间只跑一个，避免几份全量编译同时创建大量进程。"""
        if not self.build_cfg.exclusive:
            yield
            return

        async def on_wait() -> None:
            await ctx.progress("Another build is running; waiting in the queue…")

        async with file_lock(self.lock_path, cancel=ctx.cancel, on_wait=on_wait):
            yield

    def _stale_build_dir(self, root: Path) -> str | None:
        """build 目录属于别的工程（CMake 记下了工程的绝对路径）。idf.py 会报这个错，直接调 ninja 时要自己查。"""
        cache = root / "build" / "CMakeCache.txt"
        if not cache.is_file():
            return None
        m = re.search(r"^CMAKE_HOME_DIRECTORY:INTERNAL=(.+)$", cache.read_text("utf-8", errors="replace"), re.M)
        if not m:
            return None
        other = Path(m.group(1).strip())
        try:
            same = other.resolve() == root.resolve()
        except OSError:
            same = str(other).lower() == str(root).lower()
        if same:
            return None
        return (f"Build directory '{root / 'build'}' configured for project '{other}' not '{root}'. "
                "Run 'idf.py fullclean' to start again.")

    async def _ninja(self, ctx, target: str, *, timeout: float, progress_prefix: str = "",
                     extra_env: dict[str, str] | None = None):
        """直接调 ninja，带 -j 限制并行数（idf.py 自己不支持传 -j）。build 目录没配置过时先 idf.py reconfigure。"""
        build = ctx.cwd / "build"
        if msg := self._stale_build_dir(ctx.cwd):
            return ProcResult(code=2, stdout=msg, stderr="", duration_ms=0)
        if not (build / "build.ninja").is_file():
            res = await self._idf(ctx, ["reconfigure"], timeout=600, progress_prefix="configure")
            if not res.ok:
                return res
        env = await self.idf.full()
        env.update(extra_env or {})
        ninja = shutil.which("ninja", path=env.get("PATH") or env.get("Path")) or "ninja"
        argv = [ninja, "-C", str(build), "-j", str(self.build_cfg.resolved_jobs()), target]
        return await run_process(argv, cwd=ctx.cwd, env=env, timeout=timeout, cancel=ctx.cancel,
                                 on_line=_progress_cb(ctx, progress_prefix))

    # ------------------------------------------------------------------ 工程

    def detect(self, root: Path) -> ProjectInfo | None:
        cm = root / "CMakeLists.txt"
        if not cm.is_file() or "project.cmake" not in cm.read_text("utf-8", errors="replace"):
            return None
        info = ProjectInfo(root=str(root), idf_version=self.idf.install.id)
        m = re.search(r"project\(\s*([\w-]+)", cm.read_text("utf-8", errors="replace"))
        info.name = m.group(1) if m else None
        sdk = root / "sdkconfig"
        if sdk.is_file():
            if t := re.search(r'^CONFIG_IDF_TARGET="(\w+)"', sdk.read_text("utf-8", errors="replace"), re.M):
                info.target = t.group(1)
        desc = self._description(root)
        if desc:
            elf = root / "build" / desc.get("app_elf", "")
            info.built = elf.is_file()
            info.elf = str(elf) if info.built else None
            info.target = info.target or desc.get("target")
        status = root / "build" / ".fwr-last-build.json"
        if status.is_file():
            info.last_build_ok = json.loads(status.read_text("utf-8")).get("ok")
        return info

    @staticmethod
    def _description(root: Path) -> dict | None:
        p = root / "build" / "project_description.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text("utf-8"))
        except ValueError:
            return None

    async def env(self) -> dict[str, str]:
        return await self.idf.delta()

    async def _idf(self, ctx, args: list[str], *, timeout: float, progress_prefix: str = ""):
        env = await self.idf.full()
        argv = [*self.idf.idf_py(), "-C", str(ctx.cwd), "-B", str(ctx.cwd / "build"), *args]
        return await run_process(argv, cwd=ctx.cwd, env=env, timeout=timeout, cancel=ctx.cancel,
                                 on_line=_progress_cb(ctx, progress_prefix))

    @staticmethod
    def _write_log(ctx, name: str, text: str) -> str:
        d = ctx.cwd / "build"
        d.mkdir(exist_ok=True)
        p = d / f"fwr-{name}.log"
        p.write_text(text, "utf-8")
        return str(p)

    def supported_chips(self) -> tuple[str, ...]:
        return chips.supported(self.idf.install.path)

    def chip_summary(self, chip: str | None) -> str | None:
        c = chips.get(chip)
        return c.summary() if c else None

    def console_check(self, root: Path, link: str) -> str | None:
        """板子的连接口和 sdkconfig 的控制台输出口对不上时，返回给模型 / 用户的说明。"""
        sdk = root / "sdkconfig"
        if not sdk.is_file():
            return None
        return chips.console_mismatch(link if link in ("usb_serial_jtag", "usb_otg", "uart_bridge") else "unknown",  # type: ignore[arg-type]
                                      sdk.read_text("utf-8", errors="replace"))

    async def set_target(self, ctx, chip: str) -> OpResult:
        chip = chip.lower().replace("-", "")
        if chip not in self.supported_chips():
            return OpResult(op="set_target", ok=False, error_class="unsupported_chip",
                            summary=f"Supported targets: {', '.join(self.supported_chips())}")
        async with self._build_slot(ctx):
            res = await self._idf(ctx, ["set-target", chip], timeout=600, progress_prefix="set-target")
        log = self._write_log(ctx, "set-target", res.output)
        ok = res.ok
        diags, cls = parse_build_output(res.output, ctx.cwd)
        return OpResult(op="set_target", ok=ok, summary=f"target → {chip}" if ok else "",
                        stages=[Stage(name="set-target", ok=ok, duration_ms=res.duration_ms)],
                        error_class=None if ok else (cls or _proc_class(res) or "set_target_failed"),
                        diagnostics=diags, log_tail="" if ok else _tail(res.output), log_path=log,
                        duration_ms=res.duration_ms)

    async def build(self, ctx) -> BuildResult:
        async with self._build_slot(ctx):
            res = await self._ninja(ctx, "all", timeout=1800, progress_prefix="build")
        log = self._write_log(ctx, "build", res.output)
        diags, cls = parse_build_output(res.output, ctx.cwd)
        ok = res.ok
        if ok:
            cls = None
        else:
            cls = cls or _proc_class(res) or "build_failed"
        out = BuildResult(op="build", ok=ok, stages=[Stage(name="build", ok=ok, duration_ms=res.duration_ms)],
                          error_class=cls, diagnostics=diags, next_actions=build_next_actions(cls),
                          log_tail="" if ok or any(d.severity == "error" for d in diags) else _tail(res.output),
                          log_path=log, duration_ms=res.duration_ms)
        n_err = sum(d.severity == "error" for d in diags)
        n_warn = sum(d.severity == "warning" for d in diags)
        out.summary = f"{res.duration_ms / 1000:.1f}s · {n_err} errors {n_warn} warnings"
        if ok:
            desc = self._description(ctx.cwd) or {}
            if desc.get("app_elf"):
                out.elf = str(ctx.cwd / "build" / desc["app_elf"])
                out.app_bin = str(ctx.cwd / "build" / desc.get("app_bin", ""))
            out.size = SizeReport()
            if m := APP_SIZE.search(res.output):
                out.size.app_bin_size, out.size.app_partition_size = int(m.group(1), 16), int(m.group(2), 16)
        (ctx.cwd / "build").mkdir(exist_ok=True)
        (ctx.cwd / "build" / ".fwr-last-build.json").write_text(json.dumps({"ok": ok, "error_class": cls}), "utf-8")
        return out

    async def size(self, ctx) -> SizeReport | None:
        async with self._build_slot(ctx):
            # 照 idf.py size --format json2 的做法（core_ext.size_target）：
            # ESP_IDF_SIZE_NG=1 选新版 esp-idf-size（旧版不认 json2），再跑 ninja 的 size 目标
            # 先 all（和 idf.py size 一样；已经编译过时是空操作，顺带打印 app / 分区大小那一行）
            built = await self._ninja(ctx, "all", timeout=1800, progress_prefix="build")
            res = await self._ninja(ctx, "size", timeout=300,
                                    extra_env={"SIZE_OUTPUT_FORMAT": "json2", "ESP_IDF_SIZE_NG": "1"})
        if not res.ok:
            return None
        rep = SizeReport()
        text = res.stdout
        start = text.find("\n{")
        if start >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(text[start + 1 :])
                for item in data.get("layout", []):
                    name = item.get("name", "")
                    if name == "Flash Code":
                        rep.flash_code = item.get("used")
                    elif name == "Flash Data":
                        rep.flash_data = item.get("used")
                    elif name in ("IRAM",):
                        rep.iram_used, rep.iram_total = item.get("used"), item.get("total")
                    elif name in ("DRAM", "DIRAM"):
                        rep.dram_used, rep.dram_total = item.get("used"), item.get("total")
                        if name == "DIRAM":
                            rep.ram_label = "Internal RAM"
                # S3 的 "IRAM" 是固定 16 KB 的区域（向量表 + 一部分 IRAM 代码），链接器总会填满，
                # 放不下的 IRAM 代码溢出到 DIRAM。真正有限制意义的是 DIRAM，所以有 DIRAM 时不单独报 IRAM
                if rep.ram_label == "Internal RAM":
                    rep.iram_used = rep.iram_total = None
                if rep.dram_total is None:
                    # 其他芯片（例如 P4）内存区域的名字不同：取总量最大的、名字里带 RAM / MEM 的片上区域（W7，待真机核对）
                    cands = [i for i in data.get("layout", []) if i.get("total") and
                             re.search(r"RAM|MEM", i.get("name", ""), re.I) and "RTC" not in i.get("name", "")
                             and "PSRAM" not in i.get("name", "")]
                    if cands:
                        big = max(cands, key=lambda i: i["total"])
                        rep.dram_used, rep.dram_total, rep.ram_label = big.get("used"), big["total"], big["name"]
            except ValueError:
                pass
        if m := APP_SIZE.search(built.output):
            rep.app_bin_size, rep.app_partition_size = int(m.group(1), 16), int(m.group(2), 16)
        return rep

    async def clean(self, ctx, full: bool = False) -> OpResult:
        res = await self._idf(ctx, ["fullclean" if full else "clean"], timeout=300)
        diags, cls = parse_build_output(res.output, ctx.cwd)
        cls = None if res.ok else (cls or _proc_class(res) or "clean_failed")
        return OpResult(op="clean", ok=res.ok, summary="fullclean" if full else "clean",
                        stages=[Stage(name="fullclean" if full else "clean", ok=res.ok, duration_ms=res.duration_ms)],
                        error_class=cls, diagnostics=diags, next_actions=build_next_actions(cls),
                        log_tail="" if res.ok else _tail(res.output), duration_ms=res.duration_ms)

    def security_enabled(self, root: Path) -> list[str]:
        sdk = root / "sdkconfig"
        if not sdk.is_file():
            return []
        return SECURITY_KEYS.findall(sdk.read_text("utf-8", errors="replace"))

    async def flash(self, ctx, port: str, what: FlashScope, *, baud: int = 460800) -> FlashResult:
        # 和 idf.py flash 一样：设 ESPPORT / ESPBAUD 再跑 ninja 的烧录目标（源码有改动时会先增量编译，所以也要排队）
        env = {"ESPPORT": port, "ESPBAUD": str(baud)}
        targets = ["erase-flash", "flash"] if what == "erase_all" else [FLASH_TARGET[what]]
        async with self._build_slot(ctx):
            res = None
            for t in targets:
                res = await self._ninja(ctx, t, timeout=600, progress_prefix="flash", extra_env=env)
                if not res.ok:
                    break
        assert res is not None  # targets 至少一个
        log = self._write_log(ctx, "flash", res.output)
        ok = res.ok and ("Hash of data verified" in res.output or "Leaving..." in res.output or what == "erase_all")
        cls, actions, why = (None, [], "") if ok else classify_flash(res.output)
        if not ok and not cls:
            cls = _proc_class(res) or "flash_failed"
        out = FlashResult(op="flash", ok=ok, port=port, scope=what,
                          summary=f"{what} · {port} · {res.duration_ms / 1000:.1f}s" if ok else why,
                          stages=[Stage(name=FLASH_TARGET.get(what, what), ok=ok, duration_ms=res.duration_ms)],
                          error_class=cls, next_actions=actions, log_tail="" if ok else _tail(res.output, 25),
                          log_path=log, duration_ms=res.duration_ms)
        if ok:
            desc = self._description(ctx.cwd) or {}
            binp = ctx.cwd / "build" / desc.get("app_bin", "")
            if binp.is_file():
                out.image_sha256 = hashlib.sha256(binp.read_bytes()).hexdigest()
        return out

    # ------------------------------------------------------------------ 工作区（W5）

    def workspace_excludes(self) -> list[str]:
        """不进 checkpoint、回退时也不动的路径（git pathspec glob）：编译产物和下载的组件。"""
        return ["**/build/**", "**/managed_components/**", "**/sdkconfig.old"]

    def worktree_seeds(self) -> list[str]:
        """新建 worktree 时，从主工程拷过去的文件（只拷 worktree 里没有的，也就是没被 git 跟踪的）。
        没有 sdkconfig 时 IDF 会按默认值重新生成，目标芯片可能变成 esp32，编出来的固件就不对了。"""
        return ["sdkconfig", ".firmwright", "dependencies.lock", "managed_components"]

    def gitignore_template(self) -> str:
        return "build/\nsdkconfig.old\nmanaged_components/\n"

    def archive_image(self, root: Path, dest: Path) -> bool:
        """烧录成功后存档：flasher_args.json + 其中列出的 bin（bootloader、分区表、app，合计约 1–2 MB）。"""
        build = root / "build"
        fa = build / "flasher_args.json"
        if not fa.is_file():
            return False
        files = json.loads(fa.read_text("utf-8")).get("flash_files") or {}
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fa, dest / "flasher_args.json")
        for rel in files.values():
            src = build / rel
            if src.is_file():
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / rel)
        return True

    async def flash_image(self, ctx, port: str, archive: Path, scope: FlashScope = "app", *,
                          baud: int = 460800) -> FlashResult:
        """用存档的文件重烧（回退 checkpoint 时"烧回当时的固件"）。直接调 esptool，不经过 build 目录。"""
        fa = json.loads((archive / "flasher_args.json").read_text("utf-8"))
        extra = fa.get("extra_esptool_args") or {}
        if scope == "app" and fa.get("app"):
            files = {fa["app"]["offset"]: fa["app"]["file"]}
        else:
            files = fa.get("flash_files") or {}
        missing = [f for f in files.values() if not (archive / f).is_file()]
        if missing:
            return FlashResult(op="flash", ok=False, port=port, scope=scope, error_class="archive_missing",
                               summary=f"The firmware archive is incomplete; missing {', '.join(missing)}")
        argv = [self.idf.install.python, "-m", "esptool", "--chip", extra.get("chip", "auto"), "-p", port,
                "-b", str(baud), "--before", extra.get("before", "default_reset"),
                "--after", extra.get("after", "hard_reset"), "write_flash", *fa.get("write_flash_args", [])]
        for off, f in files.items():
            argv += [off, f]
        env = await self.idf.full()
        res = await run_process(argv, cwd=archive, env=env, timeout=600, cancel=ctx.cancel,
                                on_line=_progress_cb(ctx, ""))
        ok = res.ok and ("Hash of data verified" in res.output or "Leaving..." in res.output)
        cls, actions, why = (None, [], "") if ok else classify_flash(res.output)
        out = FlashResult(op="flash", ok=ok, port=port, scope=scope,
                          summary=f"archived firmware {scope} · {port} · {res.duration_ms / 1000:.1f}s" if ok else why,
                          stages=[Stage(name="esptool write_flash", ok=ok, duration_ms=res.duration_ms)],
                          error_class=cls or (None if ok else _proc_class(res) or "flash_failed"),
                          next_actions=actions, log_tail="" if ok else _tail(res.output, 25),
                          duration_ms=res.duration_ms)
        app = fa.get("app", {}).get("file")
        if ok and app and (archive / app).is_file():
            out.image_sha256 = hashlib.sha256((archive / app).read_bytes()).hexdigest()
        return out

    # ------------------------------------------------------------------ 读板子（2026-10-05 决定 4）

    async def _esptool(self, ctx, port: str, *args: str, timeout: float = 120, baud: int = 460800):
        argv = [self.idf.install.python, "-m", "esptool", "-p", port, "-b", str(baud),
                "--before", "default_reset", "--after", "hard_reset", *args]
        env = await self.idf.full()
        return await run_process(argv, cwd=ctx.cwd, env=env, timeout=timeout, cancel=ctx.cancel,
                                 on_line=_progress_cb(ctx, ""))

    async def chip_info(self, ctx, port: str) -> dict:
        """esptool flash_id：芯片型号、版本、特性、MAC、flash 大小。会复位芯片（进下载模式再复位回来）。"""
        res = await self._esptool(ctx, port, "flash_id")
        out = res.output
        info: dict = {"ok": res.ok and ("Chip is" in out or "Chip type" in out)}

        def grab(rx: str) -> str | None:
            m = re.search(rx, out, re.M)
            return m.group(1).strip() if m else None

        desc = grab(r"^Chip (?:is|type:)\s*(.+)$")
        info.update(description=desc, features=grab(r"^Features:\s*(.+)$"), mac=grab(r"^MAC:\s*([0-9a-fA-F:]+)"),
                    flash_size=grab(r"^Detected flash size:\s*(\S+)"), crystal=grab(r"^Crystal (?:is|frequency:)\s*(\S+)"))
        if desc:
            # 初代 ESP32 的型号是 ESP32-D0WD-V3 / ESP32-PICO-D4 这类，不能按 "ESP32-<字母><数字>" 直接拼芯片名
            info["chip"] = chips.chip_from_esptool(desc) or chips.chip_from_esptool(grab(r"Detecting chip type\.*\s*(.+)$") or "")
            info["revision"] = chips.revision_number(desc)
            if s := self.chip_summary(info["chip"]):
                info["summary_line"] = s
        if not info["ok"]:
            cls, actions, why = classify_flash(out)
            info.update(error_class=cls or _proc_class(res) or "esptool_failed", summary=why or _tail(out, 8),
                        next_actions=[a.model_dump() for a in actions])
        return info

    async def read_flash(self, ctx, port: str, dest: Path, *, size: str = "ALL") -> dict:
        """esptool read_flash 0 <size>：把整片 flash 读成一个文件（覆盖烧录前的备份）。16 MB 约 1–3 分钟。"""
        dest.parent.mkdir(parents=True, exist_ok=True)
        res = await self._esptool(ctx, port, "read_flash", "0", size, str(dest), timeout=900, baud=921600)
        ok = res.ok and dest.is_file() and dest.stat().st_size > 0
        out: dict = {"ok": ok, "path": str(dest), "duration_ms": res.duration_ms}
        if ok:
            data = dest.read_bytes()
            out.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        else:
            cls, actions, why = classify_flash(res.output)
            out.update(error_class=cls or _proc_class(res) or "esptool_failed", summary=why or _tail(res.output, 8),
                       next_actions=[a.model_dump() for a in actions])
        return out

    # ------------------------------------------------------------------ 设备

    def log_parser(self, board_id: str, chip: str | None, facts: Facts | None = None, log_file: str = "") -> EspLogParser:
        return EspLogParser(board_id, log_file, facts)

    async def fault_evidence(self, ev: DeviceEvent) -> FaultEvidence:
        return decode.fault_evidence(ev)

    async def unwind(self, ev: DeviceEvent, elf: Path, project_root: Path | None = None, *,
                     revision: int | None = None) -> Backtrace:
        root = project_root or elf.parent.parent
        desc = self._description(root) or {}
        target = desc.get("target") or (self.detect(root) or ProjectInfo(root=str(root))).target
        chip = chips.get(target)
        prefix = desc.get("monitor_toolprefix") or (chip.toolprefix if chip else "riscv32-esp-elf-")
        env = await self.idf.full()
        if ev.detail.get("panic_dump"):  # RISC-V（C / H / P 系列）：用 gdb + esp_idf_panic_decoder 回溯完整调用栈（W7）
            bt = await decode.unwind_riscv(ev, elf, toolprefix=prefix, env=env, python=self.idf.install.python,
                                           idf_path=self.idf.install.path, project_root=root)
            if bt is not None:
                return bt
        # 落在 ROM 里的帧用 esp-rom-elfs 里的 ROM ELF 再解一次（和 idf.py monitor 一样）
        rom = chips.rom_elf(self.idf.install.tools_path, chip.id, revision) if chip else None
        if rom is None and chip:
            rom = chips.rom_elf(Path(self.idf.install.tools_path) / "tools", chip.id, revision)
        return await decode.unwind(ev, elf, toolprefix=prefix, env=env, idf_path=self.idf.install.path,
                                   project_root=root, rom_elf=rom)

    def identify(self, port: PortInfo) -> BoardIdentity | None:
        """D03：用 USB 序列号认板子。连接方式有三种（2026-10-06 扩到全系列）：
        - USB-Serial-JTAG（S3 / C3 / C6 / H2 / P4，PID 0x1001）：序列号就是芯片 MAC，烧录后重新枚举也不变；
        - 原生 USB-OTG（S2 的 ROM / TinyUSB CDC，Espressif VID 其他 PID）：也会重新枚举；
        - USB-UART 桥（ESP32 / C2 只能这样接，很多开发板另有一个 UART 口）：CH340 没有序列号，
          有些 CP210x 所有板子都是 0001——这时按 COM 口认（Windows 按 USB 物理口分配 COM 号），chip_info 读到 MAC 后再合并。"""
        if port.vid == ESPRESSIF_VID:
            usb_jtag = port.pid == USB_JTAG_PID
            link = "usb_serial_jtag" if usb_jtag else "usb_otg"
            if port.serial_number and port.serial_number.strip() not in ("0", "0001", "123456"):
                return BoardIdentity(id=f"usb-{port.serial_number.replace(':', '').lower()}", usb_jtag=usb_jtag,
                                     link=link, native_usb=True)
            return BoardIdentity(id=f"port-{port.device.lower()}", usb_jtag=usb_jtag, stable=False, link=link,
                                 native_usb=True)
        if port.vid in UART_BRIDGES:
            sn = (port.serial_number or "").strip()
            if sn and sn not in ("0001", "0"):
                return BoardIdentity(id=f"usb-{port.vid:04x}{port.pid or 0:04x}-{sn.lower()}", link="uart_bridge")
            return BoardIdentity(id=f"port-{port.device.lower()}", stable=False, link="uart_bridge")
        return None

    def read_roots(self) -> list[Path]:
        """工作目录以外 agent 不用询问就能读的地方：IDF 源码（头文件、示例、组件）和工具链（W7）。"""
        return [Path(self.idf.install.path), Path(self.idf.install.tools_path)]

    def risk_rules(self) -> list[RiskRule]:
        """§5.5 危险操作默认分级（D15）。forbidden 放宽不了；dangerous 只有显式 allow 规则能放宽。"""
        F, D = "forbidden", "dangerous"
        return [
            RiskRule("shell", r"\bespefuse|\befuse[-_]|burn[_-](efuse|key|block)", F, "Burning eFuses is irreversible"),
            RiskRule("shell", r"\bespsecure|secure[-_]boot|flash[-_]encryption|encrypted[-_]flash|"
                              r"--encrypt\b|burn[_-]key", F, "Secure Boot / flash encryption / key burning is irreversible"),
            RiskRule("shell", r"erase[-_]flash|erase[-_]region|erase[-_]otadata", D, "A full chip erase loses all data, including NVS"),
            RiskRule("shell", r"erase[-_]partition|parttool.*(erase|write)", D, "Erasing or writing a partition (e.g. NVS) loses data"),
            RiskRule("shell", r"bootloader[-_]flash|partition[-_]table[-_]flash|write[-_]flash\s+.*0x0+\b|"
                              r"^idf\.py(\s+-\S+(\s+\S+)?)*\s+flash\b", D, "Writes the bootloader or the partition table"),
            # bootloader 偏移因芯片而异（chips.Chip.bootloader_offset）：ESP32 / S2 在 0x1000，P4 在 0x2000，其余 0x0；
            # 分区表默认在 0x8000
            RiskRule("shell", r"write[-_]flash\s+.*\b0x0*(1000|2000|8000)\b", D,
                     "Writes the bootloader (0x1000 on ESP32 / S2, 0x2000 on P4) or the partition table (0x8000)"),
            RiskRule("shell", r"\besptool(\.py)?\b.*\s--force\b", D, "esptool --force skips chip and image checks"),
            RiskRule("shell", r"\botatool(\.py)?\b.*\b(erase|write)", D, "Erases or writes an OTA partition or otadata"),
            RiskRule("shell", r"^git push\b", D, "Pushes to a remote repository"),
            RiskRule("edit_file", r"CONFIG_(SECURE|FLASH_ENCRYPTION|EFUSE)", D, "Changes security-related sdkconfig options", field="args"),
            RiskRule("write_file", r"CONFIG_(SECURE|FLASH_ENCRYPTION|EFUSE)", D, "Changes security-related sdkconfig options", field="args"),
        ]


def _progress_cb(ctx, prefix: str):
    """把进度转成工具进度（最多每 0.5 秒一次）：ninja 的 [n/N]，以及 esptool 的阶段和百分比
    （真机实测：烧录时界面上只有 "flash 1/2"，看不出烧到哪了）。"""
    last = [0.0, ""]

    async def report(text: str, force: bool = False) -> None:
        now = time.monotonic()
        if text != last[1] and (force or now - last[0] > 0.5):
            last[0], last[1] = now, text
            await ctx.progress(text)

    async def on_line(_stream: str, line: str) -> None:
        line = line.strip()
        if m := ESPTOOL_PCT.search(line):
            what = "Reading flash" if line[:1].isdigit() else "Writing"
            if w := re.match(r"Writing at (0x[0-9a-f]+)", line):
                what = f"Writing at {w.group(1)}"
            await report(f"{what} · {m.group(1)}%", force=m.group(1) == "100")
        elif ESPTOOL_STAGE.match(line):
            await report(line.rstrip("."), force=True)
        elif prefix and (m := PROGRESS.match(line)):
            await report(f"{prefix} {m.group(1)}/{m.group(2)}")

    return on_line


ESPTOOL_PCT = re.compile(r"\((\d{1,3}) ?%\)")
ESPTOOL_STAGE = re.compile(r"(Connecting|Chip is|Chip type|Detecting chip|Uploading stub|Changing baud|Configuring flash|"
                           r"Compressed \d+ bytes|Hash of data verified|Hard resetting|Read \d+ bytes|Erasing)")


def _tail(text: str, n: int = 15) -> str:
    return "\n".join(text.strip().splitlines()[-n:])


def _proc_class(res) -> str | None:
    if res.timed_out:
        return "timeout"
    if res.cancelled:
        return "cancelled"
    return None


def flash_needs(scope: FlashScope) -> str:
    return {"app": "normal", "all": "dangerous", "bootloader": "dangerous",
            "partition_table": "dangerous", "erase_all": "dangerous"}[scope]


__all__ = ["EspIdfAdapter", "NextAction", "flash_needs"]
