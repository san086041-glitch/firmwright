"""平台适配器接口（I06，D09，§4.3）。

编译、烧录、日志解析、崩溃解码、危险规则、环境变量都归适配器。第一版只有 EspIdfAdapter；
以后接 STM32 / PlatformIO 只需新增一个实现。
结果统一为 OpResult（D19，结构参照 esparagus 的 JSON 报告）：
稳定的 error_class + 机器可读的 next_actions，不把原始输出直接丢给模型。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ..device.events import Backtrace, DeviceEvent
    from ..permissions.engine import RiskRule


class Stage(BaseModel):
    name: str
    ok: bool
    duration_ms: int = 0
    attempts: int = 1


class NextAction(BaseModel):
    kind: str  # enter_download_mode / replug / set_target / retry / increase_partition / fix_code ...
    description: str
    human: bool = False  # True → 转成人工操作卡片（D13）


class Diagnostic(BaseModel):
    file: str | None = None
    line: int | None = None
    col: int | None = None
    severity: Literal["error", "warning", "note"] = "error"
    message: str


class OpResult(BaseModel):
    op: str
    ok: bool
    summary: str = ""
    stages: list[Stage] = Field(default_factory=list)
    error_class: str | None = None  # port_busy / sync_failed / flash_verify_failed / build_error ...
    next_actions: list[NextAction] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    log_tail: str = ""  # 只在没有结构化诊断时给少量原文
    log_path: str | None = None
    duration_ms: int = 0

    def for_model(self) -> str:
        """给模型看的文字版：结构化、简短。"""
        lines = [f"{self.op}: {'succeeded' if self.ok else 'failed'}" + (f" · {self.summary}" if self.summary else "")]
        if self.error_class:
            lines.append(f"error_class: {self.error_class}")
        errs = [d for d in self.diagnostics if d.severity == "error"]
        warns = [d for d in self.diagnostics if d.severity == "warning"]
        for d in errs[:20]:
            loc = f"{d.file}:{d.line}:{d.col or 0}" if d.file else "(no location)"
            lines.append(f"  error  {loc}: {d.message}")
        if warns:
            lines.append(f"  plus {len(warns)} warnings" + (":" if len(warns) <= 5 else " (first 5):"))
            for d in warns[:5]:
                lines.append(f"  warning {d.file}:{d.line}: {d.message}")
        for a in self.next_actions:
            lines.append(f"next_action: {a.kind}{' [needs a human]' if a.human else ''} — {a.description}")
        if self.log_tail and not errs:
            lines.append("End of output:\n" + self.log_tail)
        if self.log_path:
            lines.append(f"Full log: {self.log_path}")
        return "\n".join(lines)


class SizeReport(BaseModel):
    app_bin_size: int | None = None
    app_partition_size: int | None = None
    flash_code: int | None = None
    flash_data: int | None = None
    iram_used: int | None = None
    iram_total: int | None = None
    ram_label: str = "DRAM"  # 有 DIRAM（IRAM / DRAM 共用）的芯片显示"内部RAM"
    dram_used: int | None = None
    dram_total: int | None = None

    def summary(self) -> str:
        parts = []
        if self.app_bin_size and self.app_partition_size:
            parts.append(f"app {self.app_bin_size // 1024} KB / partition {self.app_partition_size // 1024} KB "
                         f"({100 * self.app_bin_size // self.app_partition_size}%)")
        if self.iram_used is not None and self.iram_total:
            parts.append(f"IRAM {100 * self.iram_used // self.iram_total}%")
        if self.dram_used is not None and self.dram_total:
            parts.append(f"{self.ram_label} {100 * self.dram_used // self.dram_total}%")
        return ", ".join(parts)


class BuildResult(OpResult):
    elf: str | None = None
    app_bin: str | None = None
    size: SizeReport | None = None


FlashScope = Literal["app", "all", "bootloader", "partition_table", "erase_all"]


class FlashResult(OpResult):
    port: str | None = None
    scope: FlashScope = "app"
    image_sha256: str | None = None


class ProjectInfo(BaseModel):
    root: str
    name: str | None = None
    target: str | None = None  # 当前 sdkconfig 里的芯片
    idf_version: str | None = None
    built: bool = False
    elf: str | None = None
    last_build_ok: bool | None = None


class PortInfo(BaseModel):
    device: str  # COM7
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    description: str = ""
    location: str | None = None


class BoardIdentity(BaseModel):
    id: str  # D03：USB 序列号，稳定；拿不到序列号时退化为 port:COMx
    chip_hint: str | None = None
    usb_jtag: bool = False  # 原生 USB-Serial-JTAG（烧录后会重新枚举）
    stable: bool = True
    # 2026-10-06 全系列：连接方式。usb_serial_jtag / usb_otg（芯片自带 USB，烧录后重新枚举）/ uart_bridge（CP210x、CH340…）
    link: Literal["usb_serial_jtag", "usb_otg", "uart_bridge", "unknown"] = "unknown"
    native_usb: bool = False


class FaultEvidence(BaseModel):
    exception: str | None = None  # LoadProhibited / Load access fault ...
    explanation: str = ""
    registers: dict[str, str] = Field(default_factory=dict)
    fault_address: str | None = None
    core: int | None = None
    arch: Literal["xtensa", "riscv", "unknown"] = "unknown"


class LogParser(Protocol):
    def feed(self, line: str, start: int, end: int) -> list[DeviceEvent]: ...
    def tick(self, now: float) -> list[DeviceEvent]: ...


class PlatformAdapter(Protocol):
    """I06：第一版只有 EspIdfAdapter。"""

    id: str

    def detect(self, root: Path) -> ProjectInfo | None: ...
    async def env(self) -> dict[str, str]: ...
    async def set_target(self, ctx, chip: str) -> OpResult: ...
    async def build(self, ctx) -> BuildResult: ...
    async def flash(self, ctx, port: str, what: FlashScope) -> FlashResult: ...
    async def clean(self, ctx, full: bool = False) -> OpResult: ...
    def security_enabled(self, root: Path) -> list[str]: ...  # 开启了 Secure Boot / Flash 加密的 sdkconfig 选项（禁止级）
    async def chip_info(self, ctx, port: str) -> dict: ...  # 2026-10-05：芯片型号、MAC、flash 大小
    async def read_flash(self, ctx, port: str, dest: Path, *, size: str = "ALL") -> dict: ...  # 覆盖烧录前的备份
    async def size(self, ctx) -> SizeReport | None: ...
    def log_parser(self, board_id: str, chip: str | None, facts=None) -> LogParser: ...
    async def fault_evidence(self, ev: DeviceEvent) -> FaultEvidence: ...
    async def unwind(self, ev: DeviceEvent, elf: Path, project_root: Path | None = None, *,
                     revision: int | None = None) -> Backtrace: ...
    # 2026-10-06 全系列芯片
    def supported_chips(self) -> tuple[str, ...]: ...
    def chip_summary(self, chip: str | None) -> str | None: ...  # 给模型的一行芯片概要
    def console_check(self, root: Path, link: str) -> str | None: ...  # 连接口和控制台输出口对不上时的说明
    def risk_rules(self) -> list[RiskRule]: ...
    def read_roots(self) -> list[Path]: ...  # W7：工作目录以外可以直接读的目录（SDK 源码、工具链）
    def identify(self, port: PortInfo) -> BoardIdentity | None: ...
    # W5 工作区
    def workspace_excludes(self) -> list[str]: ...  # 不进 checkpoint 的路径（git pathspec glob）
    def worktree_seeds(self) -> list[str]: ...  # 新建 worktree 时从主工程拷过去的未跟踪文件
    def gitignore_template(self) -> str: ...  # 用户确认初始化 git 时，没有 .gitignore 就写这个
    def archive_image(self, root: Path, dest: Path) -> bool: ...  # 烧录后存档固件
    async def flash_image(self, ctx, port: str, archive: Path, scope: FlashScope = "app") -> FlashResult: ...
