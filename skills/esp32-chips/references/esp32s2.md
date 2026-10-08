---
chip: esp32s2
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32s2

- **CPU**: 1-core Xtensa
- **Radio**: Wi-Fi
- **USB**: USB-OTG — D− / D+ on GPIO19 / GPIO20
- **Bootloader offset**: 0x1000; partition table at 0x8000 by default
- **GPIO**: 47 numbers; usable: GPIO0–21, GPIO26–46
- **Input-only GPIOs**: GPIO46
- **Flash / PSRAM (SPI) pins**: GPIO26–32 — don't use them as GPIO; with octal flash or octal PSRAM also GPIO33–37
- **UART0 (default console)**: TX GPIO43, RX GPIO44
- **JTAG**: MTCK GPIO39, MTDO GPIO40, MTDI GPIO41, MTMS GPIO42
- **Strapping pins** (from the datasheet, to verify): GPIO0, GPIO45, GPIO46 — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 2
- **Toolchain prefix**: xtensa-esp32s2-elf-

## Pitfalls

- Single core, no Bluetooth.
- The native USB port is USB-OTG, not USB-Serial-JTAG: with the default UART console, logs only appear on the UART port; to log over the USB port set CONFIG_ESP_CONSOLE_USB_CDC=y.
- After flashing over native USB the port re-enumerates (the COM number may change).

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_ARBITER, ADC_CALIBRATION_V1, ADC_DIG_CTRL, ADC_DIG_IIR_FILTER, ADC_DMA, ADC_MONITOR, ADC_RTC_CTRL, ADC_SELF_HW_CALI, AES, ASYNC_MEMCPY, BOD, BROWNOUT_RESET, CACHE_WRITEBACK, CCOMP_TIMER, CLK_APLL, CLK_RC_FAST_D256, CLK_TREE, CLK_XTAL32K, CONFIGURABLE_VDDSDIO, CP_DMA, DAC, DEDICATED_GPIO, DEEP_SLEEP, DIG_SIGN, EFUSE, FLASH_ENC, GPSPI, GPTIMER, HMAC, I2C, I2S, LCD_I80, LEDC, LIGHT_SLEEP, MEMPROT, MEMSPI_SRC_FREQ_20M, MEMSPI_SRC_FREQ_26M, MEMSPI_SRC_FREQ_40M, MEMSPI_SRC_FREQ_80M, MPI, MPU, PCNT, PHY, PM, RISCV_COPROC, RMT, RNG, RTCIO_HOLD, RTCIO_INPUT_OUTPUT, RTCIO_WAKE, RTC_FAST_MEM, RTC_MEM, RTC_SLOW_MEM, SDM, SECURE_BOOT, SHA, SPIRAM, SPIRAM_XIP, SPI_FLASH, SPI_HD_BOTH_INOUT, SPI_SCT, SYSTIMER, TEMP_SENSOR, TOUCH_SENSOR, TWAI, UART, ULP, ULP_FSM, USB_OTG, WDT, WIFI, XT_WDT
