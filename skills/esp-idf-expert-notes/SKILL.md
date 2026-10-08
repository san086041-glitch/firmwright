---
name: esp-idf-expert-notes
description: Human-expert ESP-IDF notes from the IoT-SkillsBench project (Duke University, Apache-2.0) — GPIO configuration, microsecond timing, FreeRTOS timer-task stack limits, the ADC v5 oneshot / calibration API, and I2C / SPI bus diagnostics. Written for the ESP32-S3; most points apply to all ESP32-series chips. Load it when writing GPIO / timing / ADC / I2C / SPI driver code.
when-to-use: bit-banging or microsecond timing; FreeRTOS software timers; ADC readings; I2C / SPI sensors that do not respond
chips: [esp32, esp32s2, esp32s3, esp32c2, esp32c3, esp32c6, esp32h2, esp32p4]
status: third-party, human-written (not authored by Firmwright)
source: https://github.com/iot-agent/iot-skillsbench (skills-human-expert/esp-idf-framework.md, commit 426cdbb, 2026-07-09)
license: Apache-2.0 (see LICENSE in this folder)
---

> Imported from IoT-SkillsBench ("Skilled AI Agents for Embedded and IoT Systems Development", arXiv:2603.19583), licensed under the Apache License 2.0.
> Changes by Firmwright: the YAML header was replaced (name changed from "ESP-IDF" to avoid clashing with the built-in esp-idf skill; description, when-to-use, chips and source fields added) and this note was added. The body below is unchanged.
> Note: newer ESP-IDF code usually calls `esp_rom_delay_us()` (header `esp_rom_sys.h`) where the text mentions `ets_delay_us()`.

## GPIO Configuration & Management

* **Batch Configuration:** Avoid using consecutive `gpio_set_direction()` calls to initialize multiple pins. Always use `gpio_config_t` with a `pin_bit_mask` to configure pins simultaneously.
* **Prevent Floating States:** Explicitly configure internal resistors (`.pull_up_en`, `.pull_down_en`). If a pin must default to LOW/HIGH, use `gpio_set_level()` immediately after `gpio_config()` to guarantee a deterministic state before peripheral power-on delays.
* **Microsecond State Switching:** Dynamically switching between `GPIO_MODE_INPUT` and `GPIO_MODE_OUTPUT` during microsecond bit-banging is too slow and acquires locks. Instead, configure the pin once as `GPIO_MODE_INPUT_OUTPUT_OD` (Open-Drain). To send a LOW signal, output `0`. To release the bus for input or a HIGH signal, output `1` and read the state via `gpio_get_level()`.
* **Precise Timing & Delays:** Use `ets_delay_us()` for microsecond-level precision; however, always include `#include "rom/ets_sys.h"` to prevent implicit declaration errors in modern IDF versions.
* **Interrupts for Transients:** For catching fast, transient state changes (e.g., <5ms pulses), avoid `while(1)` polling. Prefer Interrupt Service Routines via `gpio_install_isr_service()` and configure edge-triggered interrupts (e.g., `GPIO_INTR_NEGEDGE`).


## Timing, Delays & Microsecond Precision

* **Header Requirements:** Always `#include "rom/ets_sys.h"` when using `ets_delay_us()`. Failing to do so causes implicit declaration compilation errors in newer ESP-IDF 5.x versions. Avoid mixing ROM and ETS delays arbitrarily.
* **RTOS Tick Misalignment Margin:** When a hardware protocol requires a strict minimum millisecond delay, padding is required. `vTaskDelay(pdMS_TO_TICKS(X))` can yield less than `X` ms depending on when the task yields relative to the 100Hz tick. Always add a safety margin (e.g., add 5ms) to guarantee minimum hardware timings under all tick alignments.
* **Avoid Busy-Waiting:** Never use `ets_delay_us()` inside a `while` loop to wait for a GPIO state change. If the peripheral disconnects, the CPU will spin indefinitely, triggering the FreeRTOS Task Watchdog (TWDT) and crashing the system.
* **Hardware Timers for Timeouts:** Use `esp_timer_get_time()` (returns microseconds) to track elapsed time in blocking loops. Implement strict timeout breaks to return `ESP_ERR_TIMEOUT` safely.

## FreeRTOS Task & Timer Management

* **Timer Callback Stack Limits:** The FreeRTOS timer service task (`Tmr Svc`) has a very small default stack size (~2KB). Never perform heavy operations (`snprintf`, float math, hardware I/O, `ets_delay_us`) inside `xTimerCreate` callbacks.
* **Stack Overflow Symptoms:** A crash log showing `***ERROR*** A stack overflow in task Tmr Svc has been detected` indicates blocking code in a timer. Move heavy periodic logic to a dedicated FreeRTOS task with an adequate stack (e.g., 4096 bytes) or the `app_main` loop.

## ADC (Analog-to-Digital Converter) API v5.x+

* **Modern OneShot API:** ESP-IDF v5.0+ deprecated legacy APIs like `adc1_config_width`. Always use `adc_oneshot` and `adc_cali` components. Use `adc_oneshot_unit_init_cfg_t` and `adc_oneshot_chan_cfg_t` for initialization.
* **Calibration Schemes:** Always attempt to calibrate raw readings. For newer chips like the ESP32-S3, utilize the Curve Fitting scheme (`adc_cali_create_scheme_curve_fitting`).
* **Pin Mapping Pitfalls:** Do not assume legacy ADC channel mappings. For example, on the classic ESP32, ADC1_CH0 is GPIO 36, but on the ESP32-S3, ADC1_CH0 is GPIO 1. Mismapped channels will read floating environmental noise (~100-300mV).

## I2C & SPI Bus Diagnostics

* **I2C Bus Scanner:** On `ESP_ERR_NOT_FOUND` during initialization, implement a full I2C bus scan (addresses `0x03` to `0x77`) to isolate whether the issue is an incorrect specific address or a physically dead/disconnected bus.
* **I2C Bus Recovery:** If the I2C bus locks up (slave holding SDA low), implement a manual 9-clock-cycle SCL toggle recovery routine using standard GPIO manipulation to reset the slave's state machine before re-initializing the I2C driver.
* **Wiring Checks:** Always verify that physical pull-up resistors (4.7kΩ - 10kΩ) are installed on I2C lines, as internal ESP32 pull-ups are often too weak for external peripherals.
