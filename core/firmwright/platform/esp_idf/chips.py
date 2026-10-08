"""ESP-IDF 芯片能力表（2026-10-06：从只支持 S3 / P4 扩到 IDF v5.5 正式支持的 8 款）。

适配器里和芯片有关的判断都查这张表，不再按芯片名写 if：工具链前缀、架构（崩溃解码走哪条路）、
bootloader 偏移（危险规则）、有没有 USB-Serial-JTAG / USB-OTG（认板子、复位、控制台对不对得上）、
启动日志里怎么认出芯片、给模型看的一行概要。

数据来源（本机 C:\\Espressif\\v5.5\\esp-idf）：
- 支持列表：tools/idf_py_actions/constants.py 的 SUPPORTED_TARGETS（运行时读，IDF 升级后能看到新芯片）
- 核数、无线、USB、GPIO 数：components/soc/<chip>/include/soc/soc_caps.h
- bootloader 偏移：components/bootloader/Kconfig.projbuild 的 BOOTLOADER_OFFSET_IN_FLASH
预览芯片（esp32c5 / c61 / h21 / h4，需要 idf.py --preview）按用户决定不在范围内。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Arch = Literal["xtensa", "riscv"]


@dataclass(frozen=True)
class Chip:
    id: str  # idf.py set-target 用的名字，例如 esp32s3
    name: str  # 显示名，例如 ESP32-S3
    arch: Arch
    cores: int
    wifi: str | None  # "Wi-Fi 4" / "Wi-Fi 6" / None
    bt: str | None  # "BT Classic + BLE" / "BLE" / None
    ieee802154: bool  # Thread / Zigbee
    usb_serial_jtag: bool  # 片上 USB-Serial-JTAG（插 USB 口就能烧录、看日志、JTAG 调试）
    usb_otg: bool  # 片上 USB-OTG（TinyUSB / USB CDC 控制台）
    bootloader_offset: int
    gpio_count: int
    rom_tags: tuple[str, ...]  # 启动日志里 "ESP-ROM:<tag>-…" 的 tag
    notes: str = ""  # 给模型的一句最要紧的注意事项

    @property
    def toolprefix(self) -> str:
        # IDF v5 的 Xtensa 工具链是统一的 xtensa-esp-elf，但仍按芯片提供 xtensa-<chip>-elf-* 包装（带正确的 -mdynconfig）
        return f"xtensa-{self.id}-elf-" if self.arch == "xtensa" else "riscv32-esp-elf-"

    def summary(self) -> str:
        """给模型看的一行概要（project_status / chip_info 里用）。"""
        radios = [r for r in (self.wifi, self.bt, "802.15.4 (Thread / Zigbee)" if self.ieee802154 else None) if r]
        usb = ("USB-Serial-JTAG" if self.usb_serial_jtag else "") + (" + USB-OTG" if self.usb_otg else "")
        parts = [f"{self.name}: {self.cores}-core {'Xtensa' if self.arch == 'xtensa' else 'RISC-V'}",
                 ", ".join(radios) if radios else "no radio",
                 f"USB: {usb.strip(' +') or 'none (UART only, via a USB-UART bridge)'}",
                 f"{self.gpio_count} GPIOs", f"bootloader at {self.bootloader_offset:#x}"]
        s = " · ".join(parts)
        return f"{s}. {self.notes}" if self.notes else s


CHIPS: dict[str, Chip] = {c.id: c for c in (
    Chip("esp32", "ESP32", "xtensa", 2, "Wi-Fi 4", "BT Classic + BLE", False, False, False, 0x1000, 40, ("esp32",),
         "GPIO6–11 are wired to the SPI flash; GPIO34–39 are input-only; ADC2 cannot be used while Wi-Fi is on."),
    Chip("esp32s2", "ESP32-S2", "xtensa", 1, "Wi-Fi 4", None, False, False, True, 0x1000, 47, ("esp32s2",),
         "Single core, no Bluetooth; GPIO46 is input-only; the native USB port is USB-OTG (no USB-Serial-JTAG)."),
    Chip("esp32s3", "ESP32-S3", "xtensa", 2, "Wi-Fi 4", "BLE", False, True, True, 0x0, 49, ("esp32s3",),
         "GPIO26–32 (and 33–37 with octal flash / PSRAM) belong to the flash / PSRAM; GPIO19 / 20 are the USB pins."),
    Chip("esp32c2", "ESP32-C2", "riscv", 1, "Wi-Fi 4", "BLE", False, False, False, 0x0, 21, ("esp32c2", "esp8684"),
         "Small RAM (272 KB) and often 2–4 MB flash: watch memory and partition sizes; no native USB."),
    Chip("esp32c3", "ESP32-C3", "riscv", 1, "Wi-Fi 4", "BLE", False, True, False, 0x0, 22, ("esp32c3",),
         "Single core; GPIO12–17 belong to the SPI flash; GPIO18 / 19 are the USB pins."),
    Chip("esp32c6", "ESP32-C6", "riscv", 1, "Wi-Fi 6", "BLE", True, True, False, 0x0, 31, ("esp32c6",),
         "Single HP core plus an LP core; GPIO24–26 and 28–30 belong to the SPI flash; GPIO12 / 13 are the USB pins."),
    Chip("esp32h2", "ESP32-H2", "riscv", 1, None, "BLE", True, True, False, 0x0, 28, ("esp32h2",),
         "No Wi-Fi (BLE + 802.15.4 only); GPIO15–20 belong to the SPI flash; GPIO26 / 27 are the USB pins."),
    Chip("esp32p4", "ESP32-P4", "riscv", 2, None, None, False, True, True, 0x2000, 55, ("esp32p4",),
         "No radio on chip (boards often pair it with an ESP32-C6 via esp_hosted for Wi-Fi); bootloader at 0x2000; "
         "GPIO24 / 25 are USB-Serial-JTAG."),
)}

# 开发板上 USB-UART 桥的 VID（esp32 / c2 只能这样连；很多其他芯片的开发板也另有一个 UART 口）
UART_BRIDGES = {0x10C4: "CP210x", 0x1A86: "CH34x", 0x0403: "FTDI"}
ESPRESSIF_VID = 0x303A
USB_JTAG_PID = 0x1001

_supported_cache: dict[str, tuple[str, ...]] = {}


def supported(idf_path: str | Path | None = None) -> tuple[str, ...]:
    """当前 IDF 正式支持、并且能力表里有的芯片。读不到 IDF 时就用能力表本身。"""
    if not idf_path:
        return tuple(CHIPS)
    key = str(idf_path)
    if key not in _supported_cache:
        names: list[str] = []
        p = Path(idf_path) / "tools" / "idf_py_actions" / "constants.py"
        try:
            if m := re.search(r"^SUPPORTED_TARGETS\s*=\s*\[([^\]]*)\]", p.read_text("utf-8"), re.M):
                names = re.findall(r"['\"](\w+)['\"]", m.group(1))
        except OSError:
            pass
        known = [n for n in names if n in CHIPS]
        _supported_cache[key] = tuple(known) if known else tuple(CHIPS)
    return _supported_cache[key]


def get(chip: str | None) -> Chip | None:
    return CHIPS.get((chip or "").lower().replace("-", ""))


def display_name(chip: str) -> str:
    c = get(chip)
    return c.name if c else chip.upper()


ROM_LINE = re.compile(r"^ESP-ROM:(?P<tag>esp\w+?)(?:-|$)")
ESP32_ROM_LINE = re.compile(r"^ets [A-Z][a-z]{2} +\d+ \d{4}")  # ESP32（初代）的 ROM 不打印 ESP-ROM:，而是 "ets Jun  8 2016 00:22:57"


def chip_from_boot_line(line: str) -> str | None:
    """从启动日志的 ROM 行认出芯片。"""
    if m := ROM_LINE.match(line):
        tag = m.group("tag").lower()
        for c in CHIPS.values():
            if tag in c.rom_tags:
                return c.id
        return tag if tag.startswith("esp32") else None
    if ESP32_ROM_LINE.match(line):
        return "esp32"
    return None


def is_rom_line(line: str) -> bool:
    return line.startswith("ESP-ROM:") or bool(ESP32_ROM_LINE.match(line))


_ESPTOOL_NAME = re.compile(r"ESP32-(S2|S3|C2|C3|C5|C6|C61|H2|P4)\b|ESP8684", re.I)


def chip_from_esptool(desc: str) -> str | None:
    """esptool 的 "Chip is ESP32-D0WD-V3 (revision v3.1)" / "Chip type: ESP32-S3 (QFN56)" → esp32 / esp32s3。
    初代 ESP32 的型号写法是 ESP32-D0WD、ESP32-PICO-D4、ESP32-U4WDH 等，都归到 esp32。"""
    if m := _ESPTOOL_NAME.search(desc):
        return "esp32c2" if m.group(0).upper() == "ESP8684" else "esp32" + m.group(1).lower()
    if re.match(r"\s*ESP32\b", desc, re.I):
        return "esp32"
    return None


def revision_number(desc: str | None) -> int | None:
    """"revision v0.2" → 2，"revision v3.1" → 301（和 esp-rom-elfs 文件名里的 rev 编号一致：主版本 × 100 + 次版本）。"""
    if desc and (m := re.search(r"revision v(\d+)\.(\d+)", desc)):
        return int(m.group(1)) * 100 + int(m.group(2))
    return None


def rom_elf(tools_path: str | Path, chip: str, revision: int | None = None) -> Path | None:
    """esp-rom-elfs 里对应的 ROM ELF：取版本号不超过芯片版本的最大那个；不知道版本时取最小的。
    （和 idf.py monitor 的选法一样；ROM 里的函数在不同版本间基本不变，选错版本也大多能解出名字。）"""
    root = Path(tools_path) / "esp-rom-elfs"
    cands: list[tuple[int, Path]] = []
    for p in root.glob(f"*/{chip}_rev*_rom.elf"):
        if m := re.fullmatch(rf"{chip}_rev(\d+)_rom\.elf", p.name):
            cands.append((int(m.group(1)), p))
    if not cands:
        return None
    cands.sort()
    if revision is None:
        return cands[0][1]
    fit = [c for c in cands if c[0] <= revision]
    return (fit[-1] if fit else cands[0])[1]


Console = Literal["uart", "usb_serial_jtag", "usb_cdc", "none"]


def console_config(sdkconfig_text: str) -> tuple[Console, Console | None]:
    """sdkconfig 里的主控制台和第二控制台（IDF v5：CONFIG_ESP_CONSOLE_* / CONFIG_ESP_CONSOLE_SECONDARY_*）。"""
    def on(key: str) -> bool:
        return re.search(rf"^CONFIG_{key}=y", sdkconfig_text, re.M) is not None

    primary: Console = "uart"
    if on("ESP_CONSOLE_USB_SERIAL_JTAG"):
        primary = "usb_serial_jtag"
    elif on("ESP_CONSOLE_USB_CDC"):
        primary = "usb_cdc"
    elif on("ESP_CONSOLE_NONE"):
        primary = "none"
    secondary: Console | None = "usb_serial_jtag" if on("ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG") else None
    return primary, secondary


Link = Literal["usb_serial_jtag", "usb_otg", "uart_bridge", "unknown"]


def console_mismatch(link: Link, sdkconfig_text: str) -> str | None:
    """板子是从哪个口接的 vs 固件把日志打到哪个口。对不上时 await_marker 会一直等不到输出，
    agent 会误以为固件没跑起来：提前说清楚。"""
    if link == "unknown":
        return None
    primary, secondary = console_config(sdkconfig_text)
    outs = {primary} | ({secondary} if secondary else set())
    if primary == "none":
        return "sdkconfig disables the console (CONFIG_ESP_CONSOLE_NONE): the firmware prints nothing on any port."
    wants: dict[str, Console] = {"usb_serial_jtag": "usb_serial_jtag", "usb_otg": "usb_cdc", "uart_bridge": "uart"}
    want = wants[link]
    if want in outs:
        return None
    where = {"usb_serial_jtag": "the chip's USB-Serial-JTAG port", "usb_otg": "the chip's native USB (USB-OTG) port",
             "uart_bridge": "a USB-UART bridge (UART0)"}[link]
    goes = " and ".join(sorted(o.replace("_", "-") for o in outs))
    fix = {"usb_serial_jtag": "set CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y (or make USB-Serial-JTAG the primary console)",
           "usb_otg": "set CONFIG_ESP_CONSOLE_USB_CDC=y",
           "uart_bridge": "set CONFIG_ESP_CONSOLE_UART_DEFAULT=y"}[link]
    return (f"The board is connected through {where}, but sdkconfig sends console output to {goes}. Boot logs and "
            f"printf output will not appear on this port; to see them, {fix} or plug the board into the other USB port.")
