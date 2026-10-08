---
chip: esp32
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32

- **CPU**: 2-core Xtensa
- **Radio**: Wi-Fi, Bluetooth
- **USB**: none (boards use a USB-UART bridge)
- **Bootloader offset**: 0x1000; partition table at 0x8000 by default
- **GPIO**: 40 numbers; usable: GPIO0–23, GPIO25–27, GPIO32–39
- **Input-only GPIOs**: GPIO34–39
- **Flash / PSRAM (SPI) pins**: GPIO6–11 — don't use them as GPIO
- **UART0 (default console)**: TX GPIO1, RX GPIO3
- **JTAG**: MTDI GPIO12, MTCK GPIO13, MTMS GPIO14, MTDO GPIO15
- **Strapping pins** (from the datasheet, to verify): GPIO0, GPIO2, GPIO5, MTDI (GPIO12), MTDO (GPIO15) — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 2
- **Toolchain prefix**: xtensa-esp32-elf-

## Pitfalls

- ADC2 cannot be read while Wi-Fi is running; use ADC1 (GPIO32–39) for analog inputs.
- GPIO34–39 have no output driver and no internal pull-up / pull-down.
- Modules with PSRAM (WROVER) also use GPIO16 / GPIO17 for the PSRAM.
- There is no native USB: boards connect through a USB-UART bridge (CP210x / CH340); auto-reset uses DTR / RTS.
- Two chip revisions families: v1.x and v3.x; esptool prints the revision. Some boards need the BOOT button held for flashing.

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_DIG_CTRL, ADC_DMA, ADC_RTC_CTRL, AES, BLE, BLE_MESH, BLUFI, BOD, BROWNOUT_RESET, BT, BT_CLASSIC, BT_H2C_ENC_KEY_CTRL_ENH_VSC, CCOMP_TIMER, CLK_APLL, CLK_RC_FAST_D256, CLK_TREE, CLK_XTAL32K, CONFIGURABLE_VDDSDIO, DAC, DEEP_SLEEP, EFUSE, EMAC, FLASH_ENC, GPSPI, GPTIMER, I2C, I2S, LCD_I80, LEDC, LIGHT_SLEEP, MCPWM, MEMSPI_SRC_FREQ_20M, MEMSPI_SRC_FREQ_26M, MEMSPI_SRC_FREQ_40M, MEMSPI_SRC_FREQ_80M, MPI, MPU, PCNT, PHY, PM, RMT, RNG, RTCIO_HOLD, RTCIO_INPUT_OUTPUT, RTCIO_WAKE, RTC_FAST_MEM, RTC_MEM, RTC_SLOW_MEM, SDIO_SLAVE, SDM, SDMMC_HOST, SECURE_BOOT, SHA, SHARED_IDCACHE, SPIRAM, SPI_AS_CS, SPI_FLASH, SPI_HD_BOTH_INOUT, TOUCH_SENSOR, TWAI, UART, ULP, ULP_FSM, WDT, WIFI
