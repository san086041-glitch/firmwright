---
name: esp-idf
description: Chip-independent ESP-IDF practice for every ESP32-series target (ESP32, S2, S3, C2, C3, C6, H2, P4) — common driver / FreeRTOS patterns, initialization order, how the board is connected and where the console goes, and how to read crash output on both Xtensa and RISC-V chips. Load it for any ESP-IDF coding or crash analysis; then load esp32-chips (and the chip's own skill if one exists) for chip-specific pins and pitfalls.
when-to-use: any ESP-IDF task; first task on a chip you have not worked with; Guru Meditation / watchdog / abort output; no output after flashing
chips: [esp32, esp32s2, esp32s3, esp32c2, esp32c3, esp32c6, esp32h2, esp32p4]
status: draft (written by Claude, pending review by the user; generalized from the esp32-s3 / esp32-p4 drafts)
---

# ESP-IDF (all ESP32-series chips)

Chip-specific facts (pins, radios, USB, bootloader offset) are in the **esp32-chips** skill; read the target chip's card before choosing pins. Items marked "(to verify)" have not been checked against every chip.

## 1. Before writing code

- **Target chip**: `project_status` shows the project's target and the bound board's chip. If they differ, run `set_target <chip>` (it regenerates sdkconfig and forces a full build). Never edit `CONFIG_IDF_TARGET` by hand.
- **Know the chip's shape**: core count (ESP32 / S3 / P4 are dual-core; S2 / C2 / C3 / C6 / H2 are single-core), radio (H2 has no Wi-Fi; S2 has no Bluetooth; P4 has no radio at all), USB (ESP32 / C2 have none). Code ported from another chip often breaks on exactly these.
- **Single-core chips**: `xTaskCreatePinnedToCore(..., 1)` fails or asserts; use core 0 or `tskNO_AFFINITY`. `CONFIG_FREERTOS_UNICORE` is implied.

## 2. Common programming patterns (same API on every chip)

- **GPIO**: `gpio_config_t` with `pin_bit_mask = 1ULL << pin` (pin numbers above 31 exist on several chips, so `1 << pin` overflows). Check the chip card for input-only, flash, USB and strapping pins first.
- **Delays**: `vTaskDelay(pdMS_TO_TICKS(ms))`; the default tick is 100 Hz, so delays under 10 ms become 0 ticks. Microsecond waits: `esp_rom_delay_us`, briefly.
- **Tasks**: stack sizes are in bytes. `app_main` runs in the main task (3584-byte stack by default, `CONFIG_ESP_MAIN_TASK_STACK_SIZE`); big local arrays and float `printf` eat it.
- **Errors**: `ESP_ERROR_CHECK` aborts with the error name and file:line; use `ESP_RETURN_ON_ERROR` / explicit checks where a reboot is not acceptable.
- **Logging**: `ESP_LOGI(TAG, ...)`. Give self-tests a stable marker line (`TEST:<name>:PASS` / `FAIL`) matching `.firmwright/facts.toml` so `await_marker` can judge it.
- **Interrupts**: `gpio_install_isr_service(0)` then `gpio_isr_handler_add`; ISRs only hand work to tasks (`xQueueSendFromISR`, `xTaskNotifyFromISR`); no printf / blocking / malloc. ISRs that may run while flash is written need `IRAM_ATTR`.
- **PWM / LED**: LEDC (`ledc_timer_config` → `ledc_channel_config` → `ledc_set_duty` + `ledc_update_duty`).
- **ADC**: IDF v5 `adc_oneshot_*` driver. The number of ADC units and their channels differ per chip (chip card); on ESP32 / S2 / S3, ADC2 is unreliable while Wi-Fi runs.
- **Radios**: only call `esp_wifi_*` on chips with Wi-Fi, BLE APIs on chips with BLE; on P4 Wi-Fi goes through a co-processor (esp_hosted / esp_wifi_remote).

## 3. Initialization constraints

- **NVS**: on `ESP_ERR_NVS_NO_FREE_PAGES` / `ESP_ERR_NVS_NEW_VERSION_FOUND`, `nvs_flash_erase()` and init again (erasing loses settings: ask first).
- **Wi-Fi order**: `nvs_flash_init` → `esp_netif_init` → `esp_event_loop_create_default` → `esp_wifi_init`.
- **sdkconfig must match the hardware**: flash size, PSRAM mode (quad / octal), crystal frequency (C2 boards are often 26 MHz), minimum chip revision (P4). Mismatches show up as boot errors, not build errors.
- **Firmware too large**: enlarge the app partition (e.g. `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE`); a changed partition table needs `flash(scope=all)` (dangerous level). C2 boards often have only 2–4 MB flash.

## 4. How the board is connected, and where the logs go

There are three ways a board reaches the PC (`project_status` says which one):

| Connection | Chips | Notes |
|---|---|---|
| USB-Serial-JTAG (on-chip) | S3, C3, C6, H2, P4 | Flash, logs and JTAG over one cable; the port re-enumerates after flashing. Logs appear here when the console is USB-Serial-JTAG or it is the secondary console (the default on these chips) |
| USB-OTG CDC (on-chip) | S2 (S3 / P4 can too) | Logs appear only with `CONFIG_ESP_CONSOLE_USB_CDC=y`; re-enumerates after flashing |
| USB-UART bridge (CP210x / CH340 / FTDI) | ESP32, C2 (and the "UART" port of many other boards) | Logs come from UART0 (the default console); auto-reset uses DTR / RTS; some boards need BOOT held to flash |

If flashing succeeds but `await_marker` sees no output at all, suspect a console / port mismatch before suspecting the code: Firmwright reports it in `flash` and `project_status` when it can tell.

## 5. Reading crashes

`diagnose_crash` collects the evidence and decodes the backtrace; this is how to read what it returns.

**Xtensa (ESP32, S2, S3)** — prints a `Backtrace:` line (PC:SP pairs).

| Output | Most likely | First step |
|---|---|---|
| `LoadProhibited` / `StoreProhibited`, `EXCVADDR` near 0 | NULL pointer read / write | First user frame in the decoded backtrace |
| same, odd `EXCVADDR` | Dangling pointer, out-of-bounds, use after free | Backtrace; heap poisoning if needed |
| `InstrFetchProhibited` / `IllegalInstruction` | Bad function pointer, corrupted return address | Check callbacks and stack size |
| `Cache disabled but cached memory region accessed` | ISR / its data in flash during a flash write | `IRAM_ATTR`, data in DRAM |
| `Double exception` | Usually a stack overflow | Enlarge the task stack |

**RISC-V (C2, C3, C6, H2, P4)** — no `Backtrace:` line; prints registers plus `Stack memory:`. `MEPC` = faulting PC, `RA` = return address, `MTVAL` = faulting address, `MCAUSE` = cause (2 illegal instruction, 5 load access fault, 7 store access fault). Firmwright unwinds the full stack with gdb + esp_idf_panic_decoder (like `idf.py monitor`); without the stack dump only MEPC / RA can be decoded.

| Output | Most likely | First step |
|---|---|---|
| `Load access fault` / `Store access fault`, `MTVAL` near 0 | NULL pointer | First user frame |
| `Instruction access fault` / `Illegal instruction` | Bad function pointer, corrupted stack | Check callbacks and stack size |
| `Stack protection fault` | Stack overflow (to verify: exact wording per chip) | Enlarge the stack |

**Both architectures**:

| Output | Most likely | First step |
|---|---|---|
| `A stack overflow in task <t>` / `Stack canary watchpoint triggered` | That task's stack is too small | Enlarge it; make big arrays static or heap |
| `task_wdt: Task watchdog got triggered` | A task never yields (busy loop) | `vTaskDelay` in the loop |
| `Interrupt wdt timeout` | ISR too long / interrupts disabled too long | Shorten the ISR |
| `ESP_ERROR_CHECK failed ... abort() was called` | An API returned an error | Error name + file:line; usually init order |
| `assert failed:` | Assertion | file:line |
| `Brownout detector was triggered` | Power supply | `ask_human`: different cable / port / supply |
| boot log repeating | Crash-reboot loop | `read_log` the first crash |
| `waiting for download` | Stuck in download mode (BOOT / strapping pin) | `ask_human`: release BOOT, press RESET |

Frames that land in ROM are decoded with the chip's ROM ELF where available and folded as internal frames.

## 6. Manuals

For registers, pin muxing and timing, read the official PDFs rather than memory: the chip's *Technical Reference Manual* (IO MUX and GPIO Matrix, Interrupt Matrix, Watchdog Timers, System and Memory), *Datasheet* (Strapping Pins, pin definitions) and *Errata*. Use `read_file` without arguments for the outline, then `pages` or `query`. Downloads: espressif.com → Support → Documents.
