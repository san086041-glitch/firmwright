---
chip: esp32h2
generated-by: scripts/gen_chip_cards.py (from the local ESP-IDF sources; strapping pins and notes are hand-written)
---

# esp32h2

- **CPU**: 1-core RISC-V
- **Radio**: Bluetooth, IEEE 802.15.4
- **USB**: USB-Serial-JTAG — D− / D+ on GPIO26 / GPIO27
- **Bootloader offset**: 0x0; partition table at 0x8000 by default
- **GPIO**: 28 numbers; usable: GPIO0–27
- **Input-only GPIOs**: none
- **Flash / PSRAM (SPI) pins**: GPIO15–20 — don't use them as GPIO
- **UART0 (default console)**: TX GPIO24, RX GPIO23
- **JTAG**: MTMS GPIO2, MTDO GPIO3, MTCK GPIO4, MTDI GPIO5 (also reachable over USB-Serial-JTAG without wires)
- **Strapping pins** (from the datasheet, to verify): MTMS (GPIO2), MTDI (GPIO5), GPIO8, GPIO9, GPIO25 — their level at reset selects the boot mode; keep external circuits from pulling them the wrong way at power-up
- **ADC units**: 1
- **Toolchain prefix**: riscv32-esp-elf-

## Pitfalls

- No Wi-Fi: BLE and 802.15.4 (Thread / Zigbee) only; esp_wifi_* APIs are not available.
- GPIO26 / GPIO27 are USB D− / D+ (USB-Serial-JTAG).

## Peripherals (SOC_*_SUPPORTED in soc_caps.h)

ADC, ADC_CALIBRATION_V1, ADC_CALIB_CHAN_COMPENS, ADC_DIG_CTRL, ADC_DIG_IIR_FILTER, ADC_DMA, ADC_MONITOR, ADC_SELF_HW_CALI, AES, AHB_GDMA, ANA_CMPR, APM, APM_CTRL_FILTER, ASSIST_DEBUG, ASYNC_MEMCPY, BLE, BLE_50, BLE_CTE, BLE_DEVICE_PRIVACY, BLE_MESH, BLE_PERIODIC_ADV_ENH, BLE_POWER_CONTROL, BOD, BROWNOUT_RESET, BT, CACHE_FREEZE, CLK_OSC_SLOW, CLK_RC32K, CLK_TREE, CLK_XTAL32K, CRYPTO_DPA_PROTECTION, DEDICATED_GPIO, DEEP_SLEEP, DIG_SIGN, ECC, ECC_EXTENDED_MODES, ECDSA, EFUSE, ETM, FLASH_ENC, GDMA, GPSPI, GPTIMER, HMAC, I2C, I2S, IEEE802154, INT_PLIC, LEDC, LEDC_GAMMA_CURVE_FADE, LIGHT_SLEEP, LP_AON, LP_TIMER, MCPWM, MEMSPI_SRC_FREQ_16M, MEMSPI_SRC_FREQ_32M, MEMSPI_SRC_FREQ_64M, MMU_PAGE_SIZE_8KB, MODEM_CLOCK, MPI, PARLIO, PAU, PCNT, PHY, PM, PMU, RMT, RNG, RTCIO_HOLD, RTC_FAST_MEM, RTC_MEM, SDM, SECURE_BOOT, SHA, SHARED_IDCACHE, SPI_FLASH, SPI_SCT, SYSTIMER, TEMP_SENSOR, TWAI, UART, UHCI, USB_SERIAL_JTAG, VBAT, WDT
