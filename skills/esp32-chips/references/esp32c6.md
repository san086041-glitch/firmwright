---
chip: esp32c6
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32c6

- **CPU**: 1-core RISC-V + LP core
- **Radio**: Wi-Fi, Bluetooth, IEEE 802.15.4
- **USB**: USB-Serial-JTAG — D− / D+ on GPIO12 / GPIO13
- **Bootloader offset**: 0x0; partition table at 0x8000 by default
- **GPIO**: 31 numbers; usable: GPIO0–30
- **Input-only GPIOs**: none
- **Flash / PSRAM (SPI) pins**: GPIO24–26, GPIO28–30 — don't use them as GPIO
- **UART0 (default console)**: TX GPIO16, RX GPIO17
- **JTAG**: MTMS GPIO4, MTDI GPIO5, MTCK GPIO6, MTDO GPIO7 (also reachable over USB-Serial-JTAG without wires)
- **Strapping pins** (from the datasheet, to verify): MTMS (GPIO4), MTDI (GPIO5), GPIO8, GPIO9, GPIO15 — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 1
- **Toolchain prefix**: riscv32-esp-elf-

## Pitfalls

- Has an LP (low-power) RISC-V core in addition to the HP core; the LP core is programmed separately (ulp / lp_core).
- Wi-Fi 6 + BLE + 802.15.4 (Thread / Zigbee) share one radio; coexistence must be enabled when using several.
- GPIO12 / GPIO13 are USB D− / D+ (USB-Serial-JTAG).

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_CALIBRATION_V1, ADC_CALIB_CHAN_COMPENS, ADC_DIG_CTRL, ADC_DIG_IIR_FILTER, ADC_DMA, ADC_MONITOR, ADC_SELF_HW_CALI, AES, AHB_GDMA, APM, APM_CTRL_FILTER, APM_LP_APM0, ASSIST_DEBUG, ASYNC_MEMCPY, BLE, BLE_50, BLE_DEVICE_PRIVACY, BLE_MESH, BLE_PERIODIC_ADV_ENH, BLE_POWER_CONTROL, BLUFI, BOD, BROWNOUT_RESET, BT, CACHE_FREEZE, CLK_OSC_SLOW, CLK_RC32K, CLK_TREE, CLK_XTAL32K, CRYPTO_DPA_PROTECTION, DEDICATED_GPIO, DEEP_SLEEP, DIG_SIGN, ECC, EFUSE, ETM, FLASH_ENC, GDMA, GPSPI, GPTIMER, HMAC, I2C, I2S, IEEE802154, INT_PLIC, LEDC, LEDC_GAMMA_CURVE_FADE, LIGHT_SLEEP, LP_AON, LP_CORE, LP_I2C, LP_PERIPHERALS, LP_TIMER, MCPWM, MEMSPI_SRC_FREQ_20M, MEMSPI_SRC_FREQ_40M, MEMSPI_SRC_FREQ_80M, MMU_PAGE_SIZE_8KB, MODEM_CLOCK, MPI, PARLIO, PAU, PCNT, PHY, PM, PMU, RMT, RNG, RTCIO_EDGE_WAKE, RTCIO_HOLD, RTCIO_INPUT_OUTPUT, RTCIO_WAKE, RTC_FAST_MEM, RTC_MEM, SDIO_SLAVE, SDM, SECURE_BOOT, SHA, SHARED_IDCACHE, SPI_FLASH, SPI_SCT, SYSTIMER, TEMP_SENSOR, TWAI, UART, UHCI, ULP, ULP_LP_UART, USB_SERIAL_JTAG, WDT, WIFI
