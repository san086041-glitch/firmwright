"""操作系统抽象（I11）：子进程、进程树终止、shell 选择。第一版只有 Windows 实现，
其他平台以后在这里补，不改调用方。

包名用 osal（OS abstraction layer）而不是方案里的 os/，避免和标准库 os 混淆。
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..model.types import CancelToken

IS_WINDOWS = sys.platform == "win32"


@dataclass
class ProcResult:
    code: int | None  # None = 被取消或超时后杀掉
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0

    @property
    def output(self) -> str:
        if self.stderr and self.stdout:
            return self.stdout + "\n" + self.stderr
        return self.stdout or self.stderr


def kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:  # pragma: no cover
        import signal

        try:
            # 只在 POSIX 上走到；Windows 版的 os / signal 没有这几个名字
            os.killpg(os.getpgid(pid), signal.SIGKILL)  # pyright: ignore[reportAttributeAccessIssue]
        except ProcessLookupError:
            pass


LineCallback = Callable[[str, str], Awaitable[None] | None]  # (stream 名, 行)
_LINE_SPLIT = re.compile(rb"(\r\n|\n|\r)")


async def run_process(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    cancel: CancelToken | None = None,
    on_line: LineCallback | None = None,
    stdin: bytes | None = None,
) -> ProcResult:
    """运行子进程，逐行回调输出；超时或取消时杀掉整个进程树。输出按 UTF-8 解码（替换非法字节）。"""
    t0 = time.monotonic()
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:  # pragma: no cover
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env is not None else None,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    out: list[str] = []
    err: list[str] = []

    async def pump(stream: asyncio.StreamReader, sink: list[str], name: str) -> None:
        # 按块读、按 \n 或单独的 \r 分行：esptool 的进度用 \r 原地刷新、整个过程不换行，
        # readline() 遇到超过 64 KB 的"一行"会抛 ValueError（2026-10-05 真机 read_flash 失败的原因），
        # 而且按 \r 分开才能把进度实时报给界面
        # 单独的 \r 是"原地刷新"：收集的输出里后一段覆盖前一段（和终端里看到的一样），回调每段都收到
        buf = b""
        overwrite = False
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                break
            data = buf + chunk
            hold = b""
            if data.endswith(b"\r"):  # 可能是 \r\n 被切在两块之间：留到下一块再判断
                data, hold = data[:-1], b"\r"
            parts = _LINE_SPLIT.split(data)  # [段, 分隔符, 段, 分隔符, ..., 剩下的]
            buf = parts.pop() + hold
            for i in range(0, len(parts), 2):
                # 刚回到行首、什么都没写就又遇到分隔符：不产生新行（"…\r" 后面紧跟 "\n" 只是行尾）
                if not (parts[i] == b"" and overwrite):
                    await emit(parts[i], sink, name, overwrite)
                overwrite = parts[i + 1] == b"\r"
        if buf.rstrip(b"\r"):
            await emit(buf.rstrip(b"\r"), sink, name, overwrite)

    async def emit(raw: bytes, sink: list[str], name: str, overwrite: bool) -> None:
        text = raw.decode("utf-8", "replace")
        if overwrite and sink:
            sink[-1] = text
        else:
            sink.append(text)
        if on_line:
            r = on_line(name, text)
            if asyncio.iscoroutine(r):
                await r

    if stdin is not None and proc.stdin:
        proc.stdin.write(stdin)
        await proc.stdin.drain()
        proc.stdin.close()

    pumps = asyncio.gather(pump(proc.stdout, out, "stdout"), pump(proc.stderr, err, "stderr"))  # type: ignore[arg-type]
    waiter = asyncio.ensure_future(proc.wait())
    watchers: list[asyncio.Future] = [waiter]
    cancel_task = asyncio.ensure_future(cancel.wait()) if cancel else None
    if cancel_task:
        watchers.append(cancel_task)
    done, _ = await asyncio.wait(watchers, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    timed_out = not done
    cancelled = bool(cancel_task and cancel_task in done and waiter not in done)
    if timed_out or cancelled:
        kill_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), 10)
        except TimeoutError:
            pass
    if cancel_task and not cancel_task.done():
        cancel_task.cancel()
    try:
        await asyncio.wait_for(pumps, 10)
    except TimeoutError:
        pumps.cancel()
    return ProcResult(
        code=None if (timed_out or cancelled) else proc.returncode,
        stdout="\n".join(out),
        stderr="\n".join(err),
        duration_ms=int((time.monotonic() - t0) * 1000),
        timed_out=timed_out,
        cancelled=cancelled,
    )


def powershell_exe() -> str:
    """grok 在 Windows 上的探测顺序是 pwsh → powershell → Git Bash（xai-grok-config/src/shell.rs）。"""
    import shutil

    return shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"


def powershell_argv(command: str) -> list[str]:
    # 强制 UTF-8 输出，否则 Windows PowerShell 5.1 按系统代码页（中文 / 日文系统各不相同）输出
    prefix = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$ProgressPreference='SilentlyContinue';"
    )
    return [powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-Command", prefix + command]
