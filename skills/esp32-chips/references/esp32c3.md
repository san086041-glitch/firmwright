---
chip: esp32c3
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32c3

- **CPU**: 1-core RISC-V
- **Radio**: Wi-Fi, Bluetooth
- **USB**: USB-Serial-JTAG — D− / D+ on GPIO18 / GPIO19
- **Bootloader offset**: 0x0; partition table at 0x8000 by default
- **GPIO**: 22 numbers; usable: GPIO0–21
- **Input-only GPIOs**: none
- **Flash / PSRAM (SPI) pins**: GPIO12–17 — don't use them as GPIO
- **UART0 (default console)**: TX GPIO21, RX GPIO20
- **JTAG**: MTMS GPIO4, MTDI GPIO5, MTCK GPIO6, MTDO GPIO7 (also reachable over USB-Serial-JTAG without wires)
- **Strapping pins** (from the datasheet, to verify): GPIO2, GPIO8, GPIO9 — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 2
- **Toolchain prefix**: riscv32-esp-elf-

## Pitfalls

- Single-core RISC-V; code written for dual-core chips (xTaskCreatePinnedToCore with core 1) must use core 0 or tskNO_AFFINITY.
- GPIO18 / GPIO19 are USB D− / D+ (USB-Serial-JTAG); using them as GPIO breaks USB flashing and logging.

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_ARBITER, ADC_CALIBRATION_V1, ADC_DIG_CTRL, ADC_DIG_IIR_FILTER, ADC_DMA, ADC_MONITOR, ADC_SELF_HW_CALI, AES, AHB_GDMA, ASSIST_DEBUG, ASYNC_MEMCPY, BLE, BLE_50, BLE_DEVICE_PRIVACY, BLE_MESH, BLUFI, BOD, BROWNOUT_RESET, BT, CACHE_FREEZE, CLK_RC_FAST_D256, CLK_TREE, CLK_XTAL32K, DEDICATED_GPIO, DEEP_SLEEP, DIG_SIGN, EFUSE, FLASH_ENC, GDMA, GPSPI, GPTIMER, HMAC, I2C, I2S, LEDC, LIGHT_SLEEP, MEMPROT, MEMSPI_SRC_FREQ_20M, MEMSPI_SRC_FREQ_26M, MEMSPI_SRC_FREQ_40M, MEMSPI_SRC_FREQ_80M, MPI, PHY, PM, RMT, RNG, RTC_FAST_MEM, RTC_MEM, SDM, SECURE_BOOT, SHA, SHARED_IDCACHE, SPI_FLASH, SPI_SCT, SYSTIMER, TEMP_SENSOR, TWAI, UART, UHCI, USB_SERIAL_JTAG, WDT, WIFI, XT_WDT
