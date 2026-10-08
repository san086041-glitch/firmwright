"""shell 工具：在 Windows PowerShell 里执行命令（I11）。

grok 的 shell_state（跨命令保留环境变量）只在 unix 上编译，所以 ESP-IDF 环境不能靠
"先 export 再执行"。这里的做法是：每条命令都带上平台适配器缓存好的 IDF 环境变量
（idf-env-cache），于是 idf.py / esptool 在 shell 里直接可用。
"""

from __future__ import annotations

import os
import time

from pydantic import BaseModel, Field

from ..osal import powershell_argv, run_process
from .base import Tool, ToolCaps, ToolContext, ToolResult

MAX_OUTPUT = 30_000


def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 3]
    tail = text[-(limit * 2 // 3) :]
    return f"{head}\n… ({len(text) - limit} characters omitted) …\n{tail}"


class ShellArgs(BaseModel):
    command: str = Field(description="PowerShell command. Note: this is Windows PowerShell, not bash")
    timeout_s: int = Field(120, description="Timeout in seconds, max 600")
    description: str = Field("", description="One sentence on what this command does (shown to the user)")


class Shell(Tool):
    name = "shell"
    description = (
        "Run a PowerShell command in the working directory and return the exit code and output. The ESP-IDF environment "
        "is loaded, but prefer the dedicated tools (build / flash / await_marker) for building, flashing and waiting for "
        "device output; they return structured results. Do not start programs that never exit (e.g. idf.py monitor). "
        "Search inside the working directory (prefer the grep tool); never recursively scan drive roots (C:\\, D:\\), the "
        "user's home folder or other projects: that takes many minutes and the user cannot interrupt it cleanly."
    )
    Args = ShellArgs
    caps = ToolCaps(lock="session")

    def permission_subject(self, args: ShellArgs) -> str:
        return args.command

    async def run(self, ctx: ToolContext, args: ShellArgs) -> ToolResult:
        env = dict(os.environ)
        if ctx.services and ctx.services.platform:
            try:
                env.update(await ctx.services.platform.env())
            except Exception as e:  # IDF 环境加载失败不影响普通命令
                await ctx.progress(f"ESP-IDF environment not loaded: {e}")
        timeout = max(1, min(args.timeout_s, 600))
        last = [0.0]

        async def on_line(_stream: str, line: str) -> None:
            # 最新一行输出作为进度（最多每秒一次）：长命令在界面上能看出是在干活还是卡住了
            now = time.monotonic()
            if line.strip() and now - last[0] > 1.0:
                last[0] = now
                await ctx.progress(line.strip()[:200])

        res = await run_process(powershell_argv(args.command), cwd=ctx.cwd, env=env, timeout=timeout,
                                cancel=ctx.cancel, on_line=on_line)
        out = clip(res.output.strip())
        if res.timed_out:
            return ToolResult.error(f"Timed out ({timeout}s) and was killed. Output so far:\n{out}", exit_code=None)
        if res.cancelled:
            return ToolResult.error(f"Cancelled: {ctx.cancel.reason}\n{out}", exit_code=None)
        head = f"exit={res.code} ({res.duration_ms} ms)"
        return ToolResult.text(f"{head}\n{out}" if out else head, is_error=res.code != 0, exit_code=res.code)
