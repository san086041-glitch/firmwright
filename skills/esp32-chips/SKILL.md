---
name: esp32-chips
description: One fact card per ESP32-series chip supported by ESP-IDF (CPU, radio, USB, bootloader offset, usable / input-only / flash / strapping / USB / JTAG / UART pins, peripherals, pitfalls). Load it before choosing pins, porting code between chips, or when the target chip is not one you know well.
when-to-use: choosing GPIOs; porting between chips; set_target; a board whose chip differs from the last project; questions like "does this chip have Wi-Fi / USB / a second core"
chips: [esp32, esp32s2, esp32c3, esp32s3, esp32c2, esp32c6, esp32h2, esp32p4]
status: generated from the local ESP-IDF sources by scripts/gen_chip_cards.py; strapping pins and pitfalls are hand-written drafts pending review
---

# ESP32-series chip cards

Generated from ESP-IDF v5.5 sources (soc_caps.h, pin headers, bootloader Kconfig). Read the card for the target chip with read_file; the module / board datasheet is authoritative for which pins the module itself already uses.

| Chip | CPU | Radio | Bootloader | Card |
|---|---|---|---|---|
| esp32 | 2-core Xtensa | Wi-Fi, Bluetooth | 0x1000 | `references/esp32.md` |
| esp32s2 | 1-core Xtensa | Wi-Fi | 0x1000 | `references/esp32s2.md` |
| esp32c3 | 1-core RISC-V | Wi-Fi, Bluetooth | 0x0 | `references/esp32c3.md` |
| esp32s3 | 2-core Xtensa | Wi-Fi, Bluetooth | 0x0 | `references/esp32s3.md` |
| esp32c2 | 1-core RISC-V | Wi-Fi, Bluetooth | 0x0 | `references/esp32c2.md` |
| esp32c6 | 1-core RISC-V | Wi-Fi, Bluetooth, IEEE 802.15.4 | 0x0 | `references/esp32c6.md` |
| esp32h2 | 1-core RISC-V | Bluetooth, IEEE 802.15.4 | 0x0 | `references/esp32h2.md` |
| esp32p4 | 2-core RISC-V | — | 0x2000 | `references/esp32p4.md` |

Card path: this skill's folder + the path in the last column.
