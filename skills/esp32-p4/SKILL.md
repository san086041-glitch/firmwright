---
name: esp32-p4
description: Key points for ESP-IDF development on the ESP32-P4 (RISC-V, high-performance dual core plus a low-power core, no on-chip Wi-Fi / Bluetooth), how it differs from the S3, how to read RISC-V crash output, and which manual chapter to check. Load it when the target is P4 or when analyzing a P4 crash.
when-to-use: target chip is esp32p4; P4 boot, flashing, crashes (Load access fault and similar); when networking is needed
chips: [esp32p4]
status: draft (written by Claude, pending review by the user; the plan requires skills to be written by a human). There are fewer P4 items than S3 ones, and many need checking on a real board
---

# ESP32-P4 skill

Structured after IoT-SkillsBench: 1. common programming patterns; 2. initialization constraints; 3. known failure modes. Then pointers to the manuals.
The P4 is newer than the S3 and its documentation changes quickly; items marked "(to verify)" should be checked against the docs for the current ESP-IDF version and a real board.

## 1. Common programming patterns

- Peripheral driver APIs (GPIO, LEDC, UART, `adc_oneshot_*`, `esp_err_t` + `ESP_ERROR_CHECK`, `ESP_LOGx`, FreeRTOS) are the same as on other chips; see the esp-idf skill. Pin facts are in the esp32-chips skill (`references/esp32p4.md`).
- **The CPU is RISC-V**: the toolchain prefix is `riscv32-esp-elf-` (S3: `xtensa-esp32s3-elf-`). Inline assembly and Xtensa-specific code (`xthal_*`, special registers) cannot be reused as is.
- **No on-chip Wi-Fi / Bluetooth**: when networking is needed, boards usually pair the P4 with a co-processor such as an ESP32-C6 and use it through the esp-hosted / esp_wifi_remote components (to verify: the board's documentation and the ESP-IDF examples are authoritative). Don't call `esp_wifi_init` expecting it to work as on the S3.
- **Multimedia peripherals** are the P4's focus (MIPI-CSI / DSI, H.264, PPA, JPEG and so on, to verify). For such code start from the ESP-IDF examples; don't invent register-level code.

## 2. Initialization constraints

- **ESP-IDF version**: the P4 was added relatively recently (officially supported since ESP-IDF v5.3, to verify). This machine has v5.5. Run `set_target esp32p4` before building.
- **Chip revision**: early and later P4 revisions differ. The boot log shows the chip revision; it must match the sdkconfig settings for the minimum supported revision, otherwise the firmware may not run on this board (to verify: the exact option names are in menuconfig).
- **Pins**: strapping pins, pins taken by flash / PSRAM, and USB pins differ from the S3. **Don't reuse the S3 pin table**; check "Strapping Pins" and the pin definitions in the P4 datasheet.
- The flash size and PSRAM settings in sdkconfig must match the module (as on the S3).

## 3. Known failure modes

RISC-V crash output differs from Xtensa:

| Seen on the serial port | Most likely | Do this first |
|---|---|---|
| `Guru Meditation Error: Core 0 panic'ed (Load access fault)` / `(Store access fault)`, `MTVAL` near 0 | NULL pointer read / write | `diagnose_crash`; `MEPC` is the faulting instruction's address, `RA` the return address |
| `(Instruction access fault)` / `(Illegal instruction)` | Bad function pointer, jump to an invalid address, corrupted stack | Check callback pointers and stack overflow |
| `Stack protection fault` / `stack overflow` (to verify: the exact wording on P4) | The task's stack is too small | Enlarge the stack |
| `task_wdt`, `Interrupt wdt timeout`, `ESP_ERROR_CHECK failed`, `assert failed`, `Brownout` | Same as on other chips | See the tables in the esp-idf skill |

- **Registers**: `MEPC` = faulting PC, `MCAUSE` = exception cause number, `MTVAL` = faulting address (for memory access exceptions), `RA` = return address, `SP` = stack pointer.
- **Backtrace**: the P4 does not print a `Backtrace:` line like the S3; it prints the registers plus a `Stack memory:` dump. Like `idf.py monitor`, `diagnose_crash` uses gdb + esp_idf_panic_decoder to unwind the full call stack from that dump; if the serial output is incomplete (no Stack memory), only the `MEPC` and `RA` frames can be decoded. To inspect variables or single-step, use GDB over the on-chip USB-JTAG (Firmwright does not attach a debugger yet).
- Tool-level issues such as opening the serial port, re-enumeration after flashing and download mode are the same as on the S3 (the P4 also has USB-Serial-JTAG).

## 4. Manual pointers (the official documents are authoritative)

- *ESP32-P4 Technical Reference Manual*: "IO MUX and GPIO Matrix", "Interrupt Matrix" (the P4 interrupt controller differs from the S3), "Watchdog Timers", "System and Memory" (which address range an MTVAL / MEPC falls in), "USB Serial/JTAG Controller".
- *ESP32-P4 Datasheet*: "Strapping Pins", pin definitions.
- *ESP32-P4 Errata*: differences between chip revisions and known defects (especially important on the P4).
- RISC-V privileged architecture spec: what the `mcause` numbers mean (e.g. 5 = load access fault, 7 = store access fault, 2 = illegal instruction).
- Where the files are and how to read them: as in the esp32-s3 skill (`read_file` for the outline first, then `pages`, or `query` to find pages).
