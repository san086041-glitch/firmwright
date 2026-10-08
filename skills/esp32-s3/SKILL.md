---
name: esp32-s3
description: Common patterns, initialization constraints and known failure modes for ESP-IDF development on the ESP32-S3 (dual-core Xtensa LX7), plus how to read its crash output and which chapter of the chip manuals to check. Load it when writing or changing S3 drivers, peripherals or task code, or when analyzing an S3 crash.
when-to-use: target chip is esp32s3; GPIO / ADC / LEDC / UART / interrupt / task / NVS code; crashes such as Guru Meditation, watchdogs, stack overflows
chips: [esp32s3]
status: draft (written by Claude, pending review by the user; the plan requires skills to be written by a human)
---

# ESP32-S3 skill

Structured after IoT-SkillsBench: 1. common programming patterns; 2. initialization constraints; 3. known failure modes. Then pointers to the manuals.
Chip-independent ESP-IDF practice (including RISC-V crash reading and board connections) is in the esp-idf skill; the generated pin card is in esp32-chips (`references/esp32s3.md`).
Items marked "(to verify)" have not yet been checked one by one against a real board or the official documentation.

## 1. Common programming patterns

- **GPIO**: configure direction, pulls and interrupts in one go with `gpio_config_t`; write `pin_bit_mask` as `1ULL << pin` (S3 GPIO numbers go above 31, so `1 << pin` overflows). Simple single-pin use: `gpio_reset_pin` → `gpio_set_direction` → `gpio_set_level`.
- **Delays**: `vTaskDelay(pdMS_TO_TICKS(ms))`. The default tick is 100 Hz (`CONFIG_FREERTOS_HZ`), so delays under 10 ms round down to 0 ticks; for microsecond waits use `esp_rom_delay_us`, but never busy-wait for long inside a task.
- **Tasks**: `xTaskCreatePinnedToCore` can pin a task to a core; the stack size is in bytes (ESP-IDF's FreeRTOS differs from upstream here). `app_main` runs in the main task, whose stack defaults to 3584 bytes (`CONFIG_ESP_MAIN_TASK_STACK_SIZE`); large arrays and floating-point `printf` both eat stack.
- **Error handling**: wrap APIs that return `esp_err_t` in `ESP_ERROR_CHECK`; on failure it aborts and prints the error name and file:line. Where a failure must not reboot the chip, use `ESP_RETURN_ON_ERROR` or check the result yourself.
- **Logging**: `ESP_LOGI(TAG, …)` and friends; the `I (time) TAG: …` lines on the serial port come from these. Give self-tests a stable marker line (e.g. `TEST:<name>:PASS` / `FAIL`) that matches the project's `.firmwright/facts.toml`, so `await_marker` can judge the result.
- **Interrupts**: call `gpio_install_isr_service(0)` first, then `gpio_isr_handler_add`. Do the bare minimum inside an ISR: hand work to a task with `xQueueSendFromISR` / `xTaskNotifyFromISR`; no `printf`, no blocking, no `malloc`.
- **LED / PWM**: use LEDC (`ledc_timer_config` → `ledc_channel_config` → `ledc_set_duty` + `ledc_update_duty`).
- **ADC**: ESP-IDF v5 uses the new `adc_oneshot_*` driver (the old `adc1_get_raw` is deprecated). ADC1 is on GPIO1–10, ADC2 on GPIO11–20; **ADC2 is not reliable while Wi-Fi is on**, so prefer ADC1.

## 2. Initialization constraints

- **Pins you cannot use freely**:
  - Strapping pins GPIO0, GPIO3, GPIO45, GPIO46: their level at power-up selects the boot mode and the flash voltage, so external circuits must not pull them to the wrong level at power-up.
  - GPIO26–32 are usually taken by the module's SPI flash / PSRAM; modules with Octal PSRAM or Octal flash (e.g. R8 in the part number) also use GPIO33–37 (to verify: the specific module's datasheet is authoritative).
  - GPIO19 / GPIO20 are USB D− / D+. When the board downloads and logs over native USB (USB-Serial-JTAG), don't use them as ordinary GPIOs.
  - GPIO43 / GPIO44 are the default UART0 TX / RX (used when logging goes through a USB-to-UART bridge chip).
  - The S3 has no GPIO22–25.
- **NVS**: when `nvs_flash_init()` returns `ESP_ERR_NVS_NO_FREE_PAGES` or `ESP_ERR_NVS_NEW_VERSION_FOUND`, call `nvs_flash_erase()` and initialize again (the standard pattern in the official examples). Erasing NVS loses stored settings, so it is an "ask" level operation.
- **Before Wi-Fi**: `nvs_flash_init` → `esp_netif_init` → `esp_event_loop_create_default` → `esp_wifi_init`, in exactly this order.
- **sdkconfig must match the hardware**: the flash size (`CONFIG_ESPTOOLPY_FLASHSIZE`) and PSRAM type (Quad / Octal, `CONFIG_SPIRAM_MODE_*`) must match the module, otherwise boot errors or PSRAM init failures follow. Change the target chip with `set_target`; never edit `CONFIG_IDF_TARGET` by hand.
- **Firmware too large**: when the build says the app is larger than its partition, change the partition table (e.g. `CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE`) or enlarge the factory partition; after changing the partition table you must `flash(scope=all)`, which is a "dangerous" level operation.

## 3. Known failure modes (when you see these strings)

| Seen on the serial port | Most likely | Do this first |
|---|---|---|
| `Guru Meditation Error … (LoadProhibited)` / `(StoreProhibited)`, `EXCVADDR` near 0 | NULL pointer read / write | `diagnose_crash`, look at the first user-code frame, find the uninitialized pointer |
| `(LoadProhibited)` / `(StoreProhibited)`, `EXCVADDR` is an odd address | Dangling pointer, out-of-bounds access, use after free | Read the backtrace; if needed reproduce with `CONFIG_HEAP_POISONING_*` |
| `(InstrFetchProhibited)` / `(IllegalInstruction)` | Bad function pointer, or a corrupted stack broke the return address | Check callback pointers; check for stack overflow |
| `Cache disabled but cached memory region accessed` | An ISR (or something it calls) lives in flash while flash is being written | Mark the ISR `IRAM_ATTR`, keep its data in DRAM |
| `Stack canary watchpoint triggered (task)` or `A stack overflow in task <task>` | That task's stack is too small | Enlarge the stack; make big arrays static or heap-allocated |
| `task_wdt: Task watchdog got triggered` | A task held the CPU too long and starved the IDLE task (5 s by default) | Add `vTaskDelay` to the loop; don't busy-wait |
| `Interrupt wdt timeout on CPU0/1` | Too much work in an ISR, or interrupts disabled too long | Shorten the ISR |
| `ESP_ERROR_CHECK failed: esp_err_t 0x… (ESP_ERR_…)` + `abort() was called` | An API returned an error | Read the error name and file:line; check that API's preconditions (usually init order) |
| `assert failed:` | An assertion failed (in IDF or your code) | Read file:line |
| `Brownout detector was triggered` | Insufficient supply (weak USB cable / port, current spikes from peripherals) | Ask the user to change the cable / port or use external power (`ask_human`); this is not a code problem |
| The boot log repeats, `rst:0xc (RTC_SW_CPU_RST)` keeps looping | Crash-reboot loop | Capture the full output of the first crash (`read_log`) |
| `waiting for download` | The chip is stuck in download mode (BOOT held, or GPIO0 pulled low) | Ask the user to release BOOT and press RESET |

Tool-level pitfalls (Firmwright has hit these itself):
- The build says `Build directory … configured for project … not …`: the build directory belongs to another project (a worktree / a copy); follow the `fullclean` next_action.
- Native-USB boards re-enumerate after flashing and the COM port number may change; the device manager identifies boards by USB serial number, so ignore the port number.
- Native-USB serial output is dropped while nobody is reading; the flash tool resets the chip once more after monitoring resumes, so `await_marker` sees the full boot log.

## 4. Manual pointers (the official documents are authoritative)

For register, pin-mux or timing details, read the official PDFs instead of relying on memory:
- *ESP32-S3 Technical Reference Manual*: look up the chapters "IO MUX and GPIO Matrix" (pin mux), "Interrupt Matrix", "Timer Group" and "Watchdog Timers", "UART Controller", "USB Serial/JTAG Controller", "LED PWM Controller", "System and Memory" (address map: where an EXCVADDR falls).
- *ESP32-S3 Datasheet*: "Strapping Pins", pin definitions, electrical characteristics.
- *ESP32-S3 Errata*: when something behaves strangely, check for a matching silicon issue first.
- Where the files are: after the user downloads them they usually sit in the project's `docs/` or in `<app data>\manuals\`. Call `read_file` without arguments first to see the bookmark outline and page numbers, then read with `pages`; if you don't know the chapter, find pages with `query`.
- Download: the Documentation page on espressif.com (Support → Documents), choose ESP32-S3.
