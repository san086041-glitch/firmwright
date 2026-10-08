---
chip: esp32p4
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32p4

- **CPU**: 2-core RISC-V + LP core
- **Radio**: none on chip
- **USB**: USB-Serial-JTAG, USB-OTG — D− / D+ on GPIO24 / GPIO25
- **Bootloader offset**: 0x2000; partition table at 0x8000 by default
- **GPIO**: 55 numbers; usable: GPIO0–54
- **Input-only GPIOs**: none
- **Flash / PSRAM pins**: dedicated pins, not GPIOs
- **UART0 (default console)**: TX GPIO37, RX GPIO38
- **JTAG**: MTCK GPIO2, MTDI GPIO3, MTMS GPIO4, MTDO GPIO5 (also reachable over USB-Serial-JTAG without wires)
- **Strapping pins** (from the datasheet, to verify): GPIO34, GPIO35, GPIO36, GPIO37, GPIO38 — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 2
- **Toolchain prefix**: riscv32-esp-elf-

## Pitfalls

- No radio on the chip: for Wi-Fi / BLE, boards pair it with a co-processor (often an ESP32-C6) via esp_hosted / esp_wifi_remote.
- The bootloader lives at 0x2000 (not 0x0).
- Early (v0.x) and later (v1.x+) silicon revisions differ; CONFIG_ESP32P4_REV_MIN_* must not exceed the board's revision.
- USB-Serial-JTAG is on GPIO24 / GPIO25; the high-speed USB-OTG has dedicated pins.

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_CALIBRATION_V1, ADC_CALIB_CHAN_COMPENS, ADC_DIG_CTRL, ADC_DMA, ADC_RTC_CTRL, ADC_SELF_HW_CALI, AES, AHB_GDMA, ANA_CMPR, APM, ASSIST_DEBUG, ASYNC_MEMCPY, AXI_GDMA, BITSCRAMBLER, BOD, BRANCH_PREDICTOR, BROWNOUT_RESET, CACHE_FREEZE, CACHE_WRITEBACK, CLK_APLL, CLK_MPLL, CLK_RC32K, CLK_SDIO_PLL, CLK_TREE, CLK_XTAL32K, DCDC, DEBUG_PROBE, DEDICATED_GPIO, DEEP_SLEEP, DIG_SIGN, DMA2D, DW_GDMA, ECC, ECC_EXTENDED_MODES, EFUSE, EMAC, EMAC_IEEE1588V2, ETM, FLASH_ENC, GDMA, GPSPI, GPTIMER, GP_LDO, HMAC, I2C, I2S, I3C_MASTER, INT_CLIC, INT_HW_NESTED, ISP, ISP_BF, ISP_CCM, ISP_COLOR, ISP_DEMOSAIC, ISP_DVP, ISP_LSC, ISP_SHARPEN, JPEG_CODEC, JPEG_DECODE, JPEG_ENCODE, LCDCAM, LCDCAM_CAM, LCDCAM_I80_LCD, LCDCAM_RGB_LCD, LCD_I80, LCD_RGB, LEDC, LEDC_GAMMA_CURVE_FADE, LIGHT_SLEEP, LP_ADC, LP_CORE, LP_GPIO_MATRIX, LP_I2C, LP_I2S, LP_PERIPHERALS, LP_SPI, LP_TIMER, LP_VAD, MCPWM, MEMSPI_SRC_FREQ_120M, MEMSPI_SRC_FREQ_20M, MEMSPI_SRC_FREQ_40M, MEMSPI_SRC_FREQ_80M, MEM_TCM, MIPI_CSI, MIPI_DSI, MPI, PARLIO, PAU, PCNT, PM, PMU, PPA, RMT, RNG, RTCIO_EDGE_WAKE, RTCIO_HOLD, RTCIO_INPUT_OUTPUT, RTCIO_WAKE, RTC_FAST_MEM, RTC_MEM, SDM, SDMMC_HOST, SDMMC_UHS_I, SECURE_BOOT, SHA, SHARED_IDCACHE, SIMD_INSTRUCTION, SPIRAM, SPIRAM_XIP, SPI_FLASH, SYSTIMER, TEMP_SENSOR, TOUCH_PROXIMITY_MEAS_DONE, TOUCH_SENSOR, TWAI, UART, UHCI, ULP, ULP_LP_UART, USB_OTG, USB_SERIAL_JTAG, VBAT, WDT, WIRELESS_HOST
