"""把 idf.py / esptool 的原始输出整理成结构化结果（D19）。

编译：gcc 诊断（文件 / 行 / 列 / 消息）、链接错误、CMake 错误、分区放不下。
烧录：按 esptool 的报错归类成稳定的 error_class，并给出 next_actions（参照 esparagus）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, cast

from ..base import Diagnostic, NextAction

Severity = Literal["error", "warning", "note"]

GCC = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<sev>fatal error|error|warning|note):\s*(?P<msg>.*)$"
)
UNDEF_REF = re.compile(r"undefined reference to [`'](?P<sym>[^'`]+)'")
LD_LOC = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+?):(?P<line>\d+):\(")
CMAKE_ERR = re.compile(r"^CMake Error at (?P<file>.+?):(?P<line>\d+)")
OVERFLOW = re.compile(r"region [`'](?P<region>[\w.]+)' overflowed by (?P<n>\d+) bytes")
STALE_BUILD = re.compile(r"Build directory '(?P<dir>[^']+)' configured for project '(?P<other>[^']+)'")
APP_TOO_BIG = re.compile(r"(?:app partition is too small|Error: app partition is too small)", re.I)


def _norm(path: str, root: Path | None) -> str:
    p = path.strip().replace("\\", "/")
    if root:
        r = str(root.resolve()).replace("\\", "/")
        if p.lower().startswith(r.lower() + "/"):
            return p[len(r) + 1 :]
    return p


def parse_build_output(text: str, root: Path | None = None) -> tuple[list[Diagnostic], str | None]:
    """返回 (诊断列表, error_class)。error_class 为 None 表示没识别出失败原因。"""
    diags: list[Diagnostic] = []
    seen: set[tuple] = set()
    cls: str | None = None
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        line = raw.strip()
        if m := GCC.match(line):
            sev = m.group("sev")
            sev = "error" if sev == "fatal error" else sev
            key = (m.group("file"), m.group("line"), m.group("msg"))
            if key in seen:
                continue
            seen.add(key)
            diags.append(Diagnostic(file=_norm(m.group("file"), root), line=int(m.group("line")),
                                    col=int(m.group("col")) if m.group("col") else None,
                                    severity=cast(Severity, sev), message=m.group("msg").strip()))
            if sev == "error":
                cls = cls or "build_error"
        elif m := UNDEF_REF.search(line):
            loc = LD_LOC.match(line)
            key = ("ld", m.group("sym"))
            if key in seen:
                continue
            seen.add(key)
            diags.append(Diagnostic(file=_norm(loc.group("file"), root) if loc else None,
                                    line=int(loc.group("line")) if loc else None,
                                    message=f"undefined reference to `{m.group('sym')}' (link error: the function or variable is declared but not "
                                            "defined, or its component is missing from REQUIRES / SRCS)"))
            cls = cls or "link_error"
        elif m := OVERFLOW.search(line):
            diags.append(Diagnostic(message=f"Memory region {m.group('region')} overflowed by {m.group('n')} bytes"))
            cls = cls or "memory_overflow"
        elif m := CMAKE_ERR.match(line):
            detail = " ".join(x.strip() for x in lines[i + 1 : i + 6] if x.strip())[:400]
            diags.append(Diagnostic(file=_norm(m.group("file"), root), line=int(m.group("line")),
                                    message=f"CMake error: {detail}"))
            cls = cls or "cmake_error"
        elif m := STALE_BUILD.search(line):
            diags.append(Diagnostic(message=f"The build directory belongs to another project ({m.group('other')}); a fullclean is needed"))
            cls = cls or "stale_build_dir"
        elif APP_TOO_BIG.search(line):
            diags.append(Diagnostic(message=line[:300]))
            cls = cls or "app_too_large"
    return diags, cls


def build_next_actions(cls: str | None) -> list[NextAction]:
    if cls == "app_too_large":
        return [NextAction(kind="increase_partition",
                           description="The app is larger than its partition: use a larger partition table (e.g. partitions_singleapp_large) or shrink the firmware")]
    if cls == "memory_overflow":
        return [NextAction(kind="reduce_memory", description="Reduce static memory / IRAM usage, or move functions out of IRAM")]
    if cls == "stale_build_dir":
        return [NextAction(kind="fullclean", description="Delete the whole build directory with clean(full=true), then rebuild")]
    if cls == "cmake_error":
        return [NextAction(kind="fix_cmake", description="Check SRCS / REQUIRES and component names in CMakeLists.txt")]
    return []


# ---------------------------------------------------------------- esptool

FLASH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("port_busy", re.compile(r"could not open port .*(PermissionError|Access is denied|拒绝访问|in use)", re.I)),
    ("port_not_found", re.compile(r"could not open port .*(FileNotFoundError|cannot find|找不到)|"
                                  r"does not exist|No serial ports found", re.I)),
    ("chip_mismatch", re.compile(r"This chip is (?P<actual>ESP[\w-]+),? not (?P<want>ESP[\w-]+)|Wrong --chip", re.I)),
    ("wrong_boot_mode", re.compile(r"Wrong boot mode detected", re.I)),
    ("sync_failed", re.compile(r"Failed to connect to ESP|No serial data received|Timed out waiting for packet header",
                               re.I)),
    ("flash_verify_failed", re.compile(r"MD5 of file does not match|Hash of data does not match|verify failed", re.I)),
    ("connection_lost", re.compile(r"chip stopped responding|Serial data stream stopped|device reports readiness to read"
                                   r" but returned no data|ClearCommError failed", re.I)),
    ("image_too_large", re.compile(r"exceeds .*partition|File .* is too large|will not fit", re.I)),
    ("not_built", re.compile(r"(No such file or directory|does not exist).*\.bin|Run 'idf.py build'", re.I)),
]


def classify_flash(text: str) -> tuple[str | None, list[NextAction], str]:
    """返回 (error_class, next_actions, 一句话说明)。"""
    for cls, rx in FLASH_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        if cls == "port_busy":
            return cls, [
                NextAction(kind="close_other_monitor", description="The serial port is in use by another program (a serial terminal, another idf.py monitor); close it and retry",
                           human=True)], "serial port busy"
        if cls == "port_not_found":
            return cls, [NextAction(kind="replug", description="Serial port not found: check the USB cable is plugged in (it must be a data cable) and the board is powered",
                                    human=True)], "serial port not found"
        if cls == "chip_mismatch":
            actual = m.groupdict().get("actual") or "?"
            return cls, [NextAction(kind="set_target",
                                    description=f"The board is actually {actual}, which does not match the project target: change it with set_target and rebuild")], \
                f"chip mismatch (the board is {actual})"
        if cls in ("sync_failed", "wrong_boot_mode"):
            return cls, [NextAction(kind="enter_download_mode",
                                    description="The chip did not enter download mode: hold BOOT, press RESET once, release BOOT, then retry",
                                    human=True)], "could not sync with the chip"
        if cls == "flash_verify_failed":
            return cls, [NextAction(kind="retry", description="Write verification failed: retry at a lower baud rate; if it still fails, change the USB cable / port")], "write verification failed"
        if cls == "connection_lost":
            return cls, [NextAction(kind="replug", description="Disconnected during flashing: replug USB and retry", human=True)], "connection lost"
        if cls == "image_too_large":
            return cls, [NextAction(kind="increase_partition", description="The firmware is larger than the partition")], "firmware too large"
        if cls == "not_built":
            return cls, [NextAction(kind="build", description="Nothing has been built yet; build first")], "not built"
    return None, [], ""
