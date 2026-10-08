"""生成 skills/esp32-chips：每款芯片一张事实卡（2026-10-06，全系列芯片支持）。

能从本机 ESP-IDF 源码读到的都自动提取（不靠记忆）：
- 支持列表：tools/idf_py_actions/constants.py 的 SUPPORTED_TARGETS
- 能力：components/soc/<chip>/include/soc/soc_caps.h（核数、SOC_*_SUPPORTED、GPIO 数和可用 / 可输出掩码、ADC）
- flash 引脚：soc/<chip>/include/soc/spi_pins.h；UART0：uart_pins.h；USB：usb_pins.h / register/soc/io_mux_reg.h；
  JTAG：register/soc/io_mux_reg.h（或 include/soc/io_mux_reg.h）
- bootloader 偏移：components/bootloader/Kconfig.projbuild
只有 strapping 引脚是手写表（来自各芯片数据手册，卡片里标为待核对）。

用法：python scripts/gen_chip_cards.py [IDF 路径]（默认读 C:/Espressif/tools/eim_idf.json 里选中的 IDF）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "skills" / "esp32-chips"

# 数据手册 "Strapping Pins" 一节（手写，待核对）。MTMS / MTDI 等 JTAG 脚的 GPIO 号由脚本从 io_mux_reg.h 查出来
STRAPPING = {
    "esp32": ["GPIO0", "GPIO2", "GPIO5", "MTDI", "MTDO"],
    "esp32s2": ["GPIO0", "GPIO45", "GPIO46"],
    "esp32s3": ["GPIO0", "GPIO3", "GPIO45", "GPIO46"],
    "esp32c2": ["GPIO8", "GPIO9"],
    "esp32c3": ["GPIO2", "GPIO8", "GPIO9"],
    "esp32c6": ["MTMS", "MTDI", "GPIO8", "GPIO9", "GPIO15"],
    "esp32h2": ["MTMS", "MTDI", "GPIO8", "GPIO9", "GPIO25"],
    "esp32p4": ["GPIO34", "GPIO35", "GPIO36", "GPIO37", "GPIO38"],
}

# 手写的补充说明：只放 IDF 头文件里读不出来、但最容易踩的坑（待用户审阅）
EXTRA = {
    "esp32": ["ADC2 cannot be read while Wi-Fi is running; use ADC1 (GPIO32–39) for analog inputs.",
              "GPIO34–39 have no output driver and no internal pull-up / pull-down.",
              "Modules with PSRAM (WROVER) also use GPIO16 / GPIO17 for the PSRAM.",
              "There is no native USB: boards connect through a USB-UART bridge (CP210x / CH340); auto-reset uses DTR / RTS.",
              "Two chip revisions families: v1.x and v3.x; esptool prints the revision. Some boards need the BOOT button held for flashing."],
    "esp32s2": ["Single core, no Bluetooth.",
                "The native USB port is USB-OTG, not USB-Serial-JTAG: with the default UART console, logs only appear on the UART port; "
                "to log over the USB port set CONFIG_ESP_CONSOLE_USB_CDC=y.",
                "After flashing over native USB the port re-enumerates (the COM number may change)."],
    "esp32s3": ["GPIO19 / GPIO20 are USB D− / D+ (USB-Serial-JTAG and USB-OTG share them).",
                "Modules with octal flash or octal PSRAM (e.g. N16R8) also use GPIO33–37.",
                "The default console is UART0; the USB-Serial-JTAG port also shows logs because "
                "CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG is on by default."],
    "esp32c2": ["Only 272 KB of SRAM and usually 2–4 MB of flash: watch heap usage and the app partition size.",
                "No native USB: boards use a USB-UART bridge.",
                "Many boards use a 26 MHz crystal; the crystal setting in sdkconfig must match (CONFIG_XTAL_FREQ_*)."],
    "esp32c3": ["Single-core RISC-V; code written for dual-core chips (xTaskCreatePinnedToCore with core 1) must use core 0 or tskNO_AFFINITY.",
                "GPIO18 / GPIO19 are USB D− / D+ (USB-Serial-JTAG); using them as GPIO breaks USB flashing and logging."],
    "esp32c6": ["Has an LP (low-power) RISC-V core in addition to the HP core; the LP core is programmed separately (ulp / lp_core).",
                "Wi-Fi 6 + BLE + 802.15.4 (Thread / Zigbee) share one radio; coexistence must be enabled when using several.",
                "GPIO12 / GPIO13 are USB D− / D+ (USB-Serial-JTAG)."],
    "esp32h2": ["No Wi-Fi: BLE and 802.15.4 (Thread / Zigbee) only; esp_wifi_* APIs are not available.",
                "GPIO26 / GPIO27 are USB D− / D+ (USB-Serial-JTAG)."],
    "esp32p4": ["No radio on the chip: for Wi-Fi / BLE, boards pair it with a co-processor (often an ESP32-C6) via esp_hosted / esp_wifi_remote.",
                "The bootloader lives at 0x2000 (not 0x0).",
                "Early (v0.x) and later (v1.x+) silicon revisions differ; CONFIG_ESP32P4_REV_MIN_* must not exceed the board's revision.",
                "USB-Serial-JTAG is on GPIO24 / GPIO25; the high-speed USB-OTG has dedicated pins."],
}


def idf_path() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1])
    cfg = json.loads(Path("C:/Espressif/tools/eim_idf.json").read_text("utf-8"))
    sel = next(i for i in cfg["idfInstalled"] if i["id"] == cfg["idfSelectedId"])
    return Path(sel["path"])


def defines(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    out = {}
    for m in re.finditer(r"^\s*#define\s+(\w+)\s+(.+?)\s*(?://.*|/\*.*)?$", path.read_text("utf-8", errors="replace"), re.M):
        out[m.group(1)] = m.group(2).strip()
    return out


def ev(expr: str, env: dict[str, str], depth: int = 0) -> int | None:
    """求值 soc_caps.h 里的简单常量表达式：BITn、ULL 后缀、~ & | << 和对其他宏的引用。"""
    if depth > 8:
        return None
    e = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)[uUlL]+\b", r"\1", expr)
    e = re.sub(r"\bBIT(\d+)\b", r"(1<<\1)", e)

    def sub(m: re.Match) -> str:
        v = env.get(m.group(0))
        if v is None:
            raise KeyError(m.group(0))
        r = ev(v, env, depth + 1)
        if r is None:
            raise KeyError(m.group(0))
        return str(r)

    try:
        e = re.sub(r"\b[A-Za-z_]\w*\b", sub, e)
        if not re.fullmatch(r"[0-9xa-fA-F\s()+\-*<>&|~]*", e):
            return None
        return int(eval(e, {"__builtins__": {}}))  # noqa: S307 —— 只剩数字和位运算
    except Exception:
        return None


def pins(mask: int, count: int) -> list[int]:
    return [i for i in range(count) if mask >> i & 1]


def ranges(nums: list[int]) -> str:
    if not nums:
        return "none"
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:] + [None]:  # type: ignore[list-item]
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(f"GPIO{start}" if start == prev else f"GPIO{start}–{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(out)


def bootloader_offsets(idf: Path) -> dict[str, int]:
    text = (idf / "components" / "bootloader" / "Kconfig.projbuild").read_text("utf-8")
    block = text[text.find("config BOOTLOADER_OFFSET_IN_FLASH"):][:800]
    out: dict[str, int] = {}
    default = 0
    for m in re.finditer(r"default (0x[0-9a-fA-F]+)(?: if (.+))?", block):
        val = int(m.group(1), 16)
        if m.group(2):
            for t in re.findall(r"IDF_TARGET_(\w+)", m.group(2)):
                out.setdefault(t.lower(), val)
        else:
            default = val
            break
    out["_default"] = default
    return out


def jtag_pins(soc: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in (soc / "register" / "soc" / "io_mux_reg.h", soc / "include" / "soc" / "io_mux_reg.h"):
        if not p.is_file():
            continue
        t = p.read_text("utf-8", errors="replace")
        for rx in (r"IO_MUX_GPIO(\d+)_REG\s+PERIPHS_IO_MUX_(MTMS|MTDI|MTCK|MTDO)_U",
                   r"FUNC_(MTMS|MTDI|MTCK|MTDO)_GPIO(\d+)\b", r"FUNC_GPIO(\d+)_(MTMS|MTDI|MTCK|MTDO)\b"):
            for m in re.finditer(rx, t):
                a, b = m.groups()
                name, num = (b, a) if a.isdigit() else (a, b)
                out.setdefault(name, int(num))
    return out


def usb_pins(soc: Path) -> tuple[int, int] | None:
    env: dict[str, str] = {}
    for p in soc.rglob("*.h"):
        if p.name in ("usb_pins.h", "io_mux_reg.h", "gpio_pins.h"):
            env.update(defines(p))
    for dm, dp in (("USB_INT_PHY0_DM_GPIO_NUM", "USB_INT_PHY0_DP_GPIO_NUM"), ("USBPHY_DM_NUM", "USBPHY_DP_NUM")):
        if dm in env and dp in env:
            a, b = ev(env[dm], env), ev(env[dp], env)
            if a is not None and b is not None:
                return a, b
    return None


def card(idf: Path, chip: str, offsets: dict[str, int]) -> tuple[str, dict]:
    soc = idf / "components" / "soc" / chip
    caps = defines(soc / "include" / "soc" / "soc_caps.h")
    count = ev(caps.get("SOC_GPIO_PIN_COUNT", "0"), caps) or 0
    valid = ev(caps.get("SOC_GPIO_VALID_GPIO_MASK", "0"), caps) or 0
    outp = ev(caps.get("SOC_GPIO_VALID_OUTPUT_GPIO_MASK", "0"), caps) or 0
    cores = ev(caps.get("SOC_CPU_CORES_NUM", "1"), caps) or 1
    feats = sorted(k[4:-10] for k, v in caps.items() if k.startswith("SOC_") and k.endswith("_SUPPORTED") and v.strip("() ") == "1")
    arch = "Xtensa" if chip in ("esp32", "esp32s2", "esp32s3") else "RISC-V"  # 之后的新芯片都是 RISC-V
    spi = defines(soc / "include" / "soc" / "spi_pins.h")
    spi.setdefault("GPIO_NUM_INVALID", "(-1)")  # P4 的 flash 走专用引脚，头文件里全是 GPIO_NUM_INVALID
    octal_keys = ("MSPI_IOMUX_PIN_NUM_D4", "MSPI_IOMUX_PIN_NUM_D5", "MSPI_IOMUX_PIN_NUM_D6", "MSPI_IOMUX_PIN_NUM_D7",
                  "MSPI_IOMUX_PIN_NUM_DQS")
    flash = sorted({n for k, v in spi.items() if k.startswith("MSPI_IOMUX_PIN_NUM_") and k not in octal_keys
                    and (n := ev(v, spi)) is not None and n >= 0})
    octal = sorted({n for k in octal_keys if k in spi and (n := ev(spi[k], spi)) is not None and n >= 0})
    uart = defines(soc / "include" / "soc" / "uart_pins.h")
    u0 = (ev(uart.get("U0TXD_GPIO_NUM", "x"), uart), ev(uart.get("U0RXD_GPIO_NUM", "x"), uart))
    jtag = jtag_pins(soc)
    if chip == "esp32" and not jtag:
        jtag = {"MTDI": 12, "MTCK": 13, "MTMS": 14, "MTDO": 15}  # ESP32 的 JTAG 脚是固定的 GPIO12–15（技术参考手册 IO_MUX 一章）
    usb = usb_pins(soc)
    boff = offsets.get(chip, offsets["_default"])
    strap = []
    for s in STRAPPING.get(chip, []):
        strap.append(f"{s} (GPIO{jtag[s]})" if s in jtag else s)
    adc_units = ev(caps.get("SOC_ADC_PERIPH_NUM", "0"), caps)

    has = set(feats)
    radios = [r for r, k in (("Wi-Fi", "WIFI"), ("Bluetooth", "BT"), ("IEEE 802.15.4", "IEEE802154")) if k in has]
    lines = [
        "---",
        f"chip: {chip}",
        "generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)",
        "---",
        "",
        f"# {chip}",
        "",
        f"- **CPU**: {cores}-core {arch}" + (" + LP core" if "LP_CORE" in has else "") + (", FPU" if "CPU_HAS_FPU" in caps else ""),
        f"- **Radio**: {', '.join(radios) if radios else 'none on chip'}",
        "- **USB**: " + (", ".join(x for x in ("USB-Serial-JTAG" if "USB_SERIAL_JTAG" in has else "",
                                                "USB-OTG" if "USB_OTG" in has else "") if x) or "none (boards use a USB-UART bridge)")
        + (f" — D− / D+ on GPIO{usb[0]} / GPIO{usb[1]}" if usb else ""),
        f"- **Bootloader offset**: {boff:#x}; partition table at 0x8000 by default",
        f"- **GPIO**: {count} numbers; usable: {ranges(pins(valid, count))}",
        f"- **Input-only GPIOs**: {ranges([p for p in pins(valid, count) if not outp >> p & 1])}",
        (f"- **Flash / PSRAM (SPI) pins**: {ranges(flash)} — don't use them as GPIO"
         + (f"; with octal flash or octal PSRAM also {ranges(octal)}" if octal else "")
         if flash else "- **Flash / PSRAM pins**: dedicated pins, not GPIOs"),
        f"- **UART0 (default console)**: TX GPIO{u0[0]}, RX GPIO{u0[1]}" if None not in u0 else "- **UART0**: see datasheet",
        "- **JTAG**: " + (", ".join(f"{k} GPIO{v}" for k, v in sorted(jtag.items(), key=lambda kv: kv[1])) if jtag else "see datasheet")
        + (" (also reachable over USB-Serial-JTAG without wires)" if "USB_SERIAL_JTAG" in has else ""),
        f"- **Strapping pins** (from the datasheet, to verify): {', '.join(strap) if strap else 'see datasheet'} — "
        "their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up",
        f"- **ADC units**: {adc_units}",
        f"- **Toolchain prefix**: {'xtensa-' + chip + '-elf-' if arch == 'Xtensa' else 'riscv32-esp-elf-'}",
        "",
        "## Pitfalls",
        "",
        *[f"- {x}" for x in EXTRA.get(chip, [])],
        "",
        "## Peripherals (SOC_*_SUPPORTED in soc_caps.h)",
        "",
        ", ".join(feats),
        "",
    ]
    meta = {"arch": arch, "cores": cores, "radios": radios, "boff": boff}
    return "\n".join(lines), meta


def main() -> None:
    idf = idf_path()
    consts = (idf / "tools" / "idf_py_actions" / "constants.py").read_text("utf-8")
    targets = re.findall(r"'(\w+)'", re.search(r"^SUPPORTED_TARGETS\s*=\s*\[([^\]]*)\]", consts, re.M).group(1))  # type: ignore[union-attr]
    offsets = bootloader_offsets(idf)
    (OUT / "references").mkdir(parents=True, exist_ok=True)
    rows = []
    for chip in targets:
        text, meta = card(idf, chip, offsets)
        (OUT / "references" / f"{chip}.md").write_text(text, "utf-8")
        rows.append(f"| {chip} | {meta['cores']}-core {meta['arch']} | {', '.join(meta['radios']) or '—'} | {meta['boff']:#x} "
                    f"| `references/{chip}.md` |")
        print("wrote", chip)
    version = re.search(r"v\d+\.\d+", str(idf))
    skill = [
        "---",
        "name: esp32-chips",
        "description: One fact card per ESP32-series chip supported by ESP-IDF (CPU, radio, USB, bootloader offset, usable / "
        "input-only / flash / strapping / USB / JTAG / UART pins, peripherals, pitfalls). Load it before choosing pins, "
        "porting code between chips, or when the target chip is not one you know well.",
        "when-to-use: choosing GPIOs; porting between chips; set_target; a board whose chip differs from the last project; "
        "questions like \"does this chip have Wi-Fi / USB / a second core\"",
        f"chips: [{', '.join(targets)}]",
        "status: generated from the local ESP-IDF sources by scripts/gen_chip_cards.py; strapping pins and pitfalls are "
        "hand-written drafts pending review",
        "---",
        "",
        "# ESP32-series chip cards",
        "",
        f"Generated from ESP-IDF {version.group(0) if version else ''} sources (soc_caps.h, pin headers, bootloader Kconfig). "
        "Read the card for the target chip with read_file; the module / board datasheet is authoritative for which pins "
        "the module itself already uses.",
        "",
        "| Chip | CPU | Radio | Bootloader | Card |",
        "|---|---|---|---|---|",
        *rows,
        "",
        "Card path: this skill's folder + the path in the last column.",
        "",
    ]
    (OUT / "SKILL.md").write_text("\n".join(skill), "utf-8")
    print("wrote SKILL.md")


if __name__ == "__main__":
    main()
