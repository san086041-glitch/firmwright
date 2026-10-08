---
chip: esp32c2
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32c2

- **CPU**: 1-core RISC-V
- **Radio**: Wi-Fi, Bluetooth
- **USB**: none (boards use a USB-UART bridge)
- **Bootloader offset**: 0x0; partition table at 0x8000 by default
- **GPIO**: 21 numbers; usable: GPIO0–20
- **Input-only GPIOs**: none
- **Flash / PSRAM (SPI) pins**: GPIO12–17 — don't use them as GPIO
- **UART0 (default console)**: TX GPIO20, RX GPIO19
- **JTAG**: MTMS GPIO4, MTDI GPIO5, MTCK GPIO6, MTDO GPIO7
- **Strapping pins** (from the datasheet, to verify): GPIO8, GPIO9 — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 1
- **Toolchain prefix**: riscv32-esp-elf-

## Pitfalls

- Only 272 KB of SRAM and usually 2–4 MB of flash: watch heap usage and the app partition size.
- No native USB: boards use a USB-UART bridge.
- Many boards use a 26 MHz crystal; the crystal setting in sdkconfig must match (CONFIG_XTAL_FREQ_*).

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_CALIBRATION_V1, ADC_DIG_CTRL, ADC_DIG_IIR_FILTER, ADC_MONITOR, ADC_SELF_HW_CALI, AHB_GDMA, ASSIST_DEBUG, ASYNC_MEMCPY, BLE, BLE_50, BLE_DEVICE_PRIVACY, BLE_PERIODIC_ADV_ENH, BLUFI, BOD, BROWNOUT_RESET, BT, CACHE_FREEZE, CLK_OSC_SLOW, CLK_RC_FAST_D256, CLK_TREE, DEDICATED_GPIO, DEEP_SLEEP, ECC, EFUSE, FLASH_ENC, GDMA, GPSPI, GPTIMER, I2C, LEDC, LIGHT_SLEEP, MEMSPI_SRC_FREQ_15M, MEMSPI_SRC_FREQ_20M, MEMSPI_SRC_FREQ_30M, MEMSPI_SRC_FREQ_60M, PHY, PM, RNG, SECURE_BOOT, SHA, SHARED_IDCACHE, SPI_FLASH, SPI_SCT, SYSTIMER, TEMP_SENSOR, UART, WDT, WIFI
