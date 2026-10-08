"""ESP-IDF RISC-V（P4）panic 输出的样例（格式照 ESP-IDF v5.x 的 panic handler）。

MEPC / RA 指向 P4 ROM ELF（esp-rom-elfs/esp32p4_rev0_rom.elf）里真实的函数：crc32_le 里出错、返回地址在 ets_delay_us 里。
本机的 RISC-V 编译器不完整（没有 cc1），编不了 P4 固件，所以解码测试用 ROM ELF。
"""


def riscv_panic(mepc: str = "0x4fc05a48", ra: str = "0x4fc0126e", sp: str = "0x4ff3a7f0",
                exc: str = "Load access fault", mcause: str = "0x00000005", mtval: str = "0x00000000") -> str:
    regs = f"""Core  0 register dump:
MEPC    : {mepc}  RA      : {ra}  SP      : {sp}  GP      : 0x4ff0d394
TP      : 0x4ff3a8a0  T0      : 0x00000000  T1      : 0x00000000  T2      : 0x00000000
S0/FP   : 0x00000000  S1      : 0x00000001  A0      : 0x00000000  A1      : 0x00000001
A2      : 0x00000000  A3      : 0x00000000  A4      : 0x00000000  A5      : 0x00000000
A6      : 0x00000000  A7      : 0x00000000  S2      : 0x00000000  S3      : 0x00000000
S4      : 0x00000000  S5      : 0x00000000  S6      : 0x00000000  S7      : 0x00000000
S8      : 0x00000000  S9      : 0x00000000  S10     : 0x00000000  S11     : 0x00000000
T3      : 0x00000000  T4      : 0x00000000  T5      : 0x00000000  T6      : 0x00000000
MSTATUS : 0x00001881  MTVEC   : 0x4ff00003  MCAUSE  : {mcause}  MTVAL   : {mtval}
MHARTID : 0x00000000
"""
    base = int(sp, 16)
    lines = []
    for i in range(8):
        words = " ".join("0x00000000" for _ in range(8))
        lines.append(f"{base + i * 32:08x}: {words}")
    return (f"Guru Meditation Error: Core  0 panic'ed ({exc}). Exception was unhandled.\n\n" + regs +
            "\nStack memory:\n" + "\n".join(lines) + "\n\n\n\nELF file SHA256: 0123456789abcdef\n\nRebooting...\n")


P4_BOOT = """ESP-ROM:esp32p4-eco2-20240710
Build:Jul 10 2024
rst:0x1 (POWERON),boot:0x30f (SPI_FAST_FLASH_BOOT)
I (25) boot: ESP-IDF v5.5 2nd stage bootloader
I (300) main_task: Calling app_main()
"""
