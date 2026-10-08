---
chip: esp32s3
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32s3

- **CPU**: 2-core Xtensa
- **Radio**: Wi-Fi, Bluetooth
- **USB**: USB-Serial-JTAG, USB-OTG — D− / D+ on GPIO19 / GPIO20
- **Bootloader offset**: 0x0; partition table at 0x8000 by default
- **GPIO**: 49 numbers; usable: GPIO0–21, GPIO26–48
- **Input-only GPIOs**: none
- **Flash / PSRAM (SPI) pins**: GPIO26–32 — don't use them as GPIO; with octal flash or octal PSRAM also GPIO33–37
- **UART0 (default console)**: TX GPIO43, RX GPIO44
- **JTAG**: MTCK GPIO39, MTDO GPIO40, MTDI GPIO41, MTMS GPIO42 (also reachable over USB-Serial-JTAG without wires)
- **Strapping pins** (from the datasheet, to verify): GPIO0, GPIO3, GPIO45, GPIO46 — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 2
- **Toolchain prefix**: xtensa-esp32s3-elf-

## Pitfalls

- GPIO19 / GPIO20 are USB D− / D+ (USB-Serial-JTAG and USB-OTG share them).
- Modules with octal flash or octal PSRAM (e.g. N16R8) also use GPIO33–37.
- The default console is UART0; the USB-Serial-JTAG port also shows logs because CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG is on by default.

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_ARBITER, ADC_CALIBRATION_V1, ADC_DIG_CTRL, ADC_DIG_IIR_FILTER, ADC_DMA, ADC_MONITOR, ADC_RTC_CTRL, ADC_SELF_HW_CALI, AES, AHB_GDMA, ASYNC_MEMCPY, BLE, BLE_50, BLE_DEVICE_PRIVACY, BLE_MESH, BLUFI, BOD, BROWNOUT_RESET, BT, CACHE_FREEZE, CACHE_WRITEBACK, CCOMP_TIMER, CLK_RC_FAST_D256, CLK_TREE, CLK_XTAL32K, CONFIGURABLE_VDDSDIO, DEDICATED_GPIO, DEEP_SLEEP, DIG_SIGN, EFUSE, FLASH_ENC, GDMA, GPSPI, GPTIMER, HMAC, I2C, I2S, LCDCAM, LCDCAM_I80_LCD, LCDCAM_RGB_LCD, LCD_I80, LCD_RGB, LEDC, LIGHT_SLEEP, MCPWM, MEMPROT, MEMSPI_SRC_FREQ_120M, MEMSPI_SRC_FREQ_20M, MEMSPI_SRC_FREQ_40M, MEMSPI_SRC_FREQ_80M, MPI, MPU, PCNT, PHY, PM, RISCV_COPROC, RMT, RNG, RTCIO_HOLD, RTCIO_INPUT_OUTPUT, RTCIO_WAKE, RTC_FAST_MEM, RTC_MEM, RTC_SLOW_MEM, SDM, SDMMC_HOST, SECURE_BOOT, SHA, SIMD_INSTRUCTION, SPIRAM, SPIRAM_XIP, SPI_FLASH, SPI_SCT, SYSTIMER, TEMP_SENSOR, TOUCH_PROXIMITY_MEAS_DONE, TOUCH_SENSOR, TWAI, UART, UHCI, ULP, ULP_FSM, USB_OTG, USB_SERIAL_JTAG, WDT, WIFI, XT_WDT
