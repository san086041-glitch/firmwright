"""MCP 客户端：stdio 传输（一行一条 JSON-RPC 2.0 消息）。只实现 agent 需要的部分：
initialize → notifications/initialized → tools/list（分页）→ tools/call；收到 notifications/tools/list_changed 时重新拉工具列表。
不依赖 mcp SDK（方案的依赖清单里没有它），协议本身很小。

grok 的事实（记忆 grok-build-embedded-feasibility）：它的 MCP 客户端只处理 list_changed 和 elicitation 两类通知，
MCP 工具不能主动唤醒 agent。这里也一样：设备事件走我们自己的事件路由，MCP 只当"被调用的工具"。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any

from pydantic import BaseModel, Field

from ..osal import IS_WINDOWS, kill_tree

log = logging.getLogger("firmwright.mcp")

PROTOCOL_VERSION = "2025-06-18"


class McpServerConfig(BaseModel):
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True
    timeout: float = 120.0  # 单次 tools/call 的超时（秒）
    read_only: list[str] = Field(default_factory=list)  # 额外声明为只读的工具名（服务器没给 readOnlyHint 时用）


class McpToolInfo(BaseModel):
    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    read_only: bool = False


class McpError(RuntimeError):
    pass


def exit_reason(stderr: list[str]) -> str:
    """服务器退出时 stderr 里最有用的一行：最后一条像错误的（Node 的最后一行是版本号，Python 的是异常），
    没有就用最后一行。"""
    lines = [x.strip() for x in stderr if x.strip()]
    for x in reversed(lines):
        if re.search(r"error|exception|cannot|not found|no such|denied", x, re.IGNORECASE):
            return x[:300]
    return lines[-1][:300] if lines else ""


class McpClient:
    def __init__(self, name: str, cfg: McpServerConfig) -> None:
        self.name = name
        self.cfg = cfg
        self.proc: asyncio.subprocess.Process | None = None
        self.tools: list[McpToolInfo] = []
        self.server_info: dict[str, Any] = {}
        self.error: str | None = None
        self._next = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._stderr: list[str] = []
        self._write_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self, timeout: float = 30) -> None:
        exe = shutil.which(self.cfg.command) or self.cfg.command  # Windows 上 npx 实际是 npx.cmd
        env = {**os.environ, **self.cfg.env}
        kwargs: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        self.proc = await asyncio.create_subprocess_exec(
            exe, *self.cfg.args, cwd=self.cfg.cwd, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024, **kwargs)
        self._reader = asyncio.create_task(self._read_loop())
        asyncio.create_task(self._drain_stderr())
        res = await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "firmwright", "version": "0.1.1"}}, timeout=timeout)
        self.server_info = res.get("serverInfo") or {}
        await self.notify("notifications/initialized", {})
        await self.refresh_tools()

    async def refresh_tools(self) -> None:
        tools: list[McpToolInfo] = []
        cursor = None
        while True:
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=30)
            for t in res.get("tools") or []:
                ann = t.get("annotations") or {}
                tools.append(McpToolInfo(
                    server=self.name, name=t["name"], description=t.get("description") or "",
                    input_schema=t.get("inputSchema") or {"type": "object", "properties": {}},
                    read_only=bool(ann.get("readOnlyHint")) or t["name"] in self.cfg.read_only))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        self.tools = tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments}, timeout=self.cfg.timeout)

    # ------------------------------------------------------------------ JSON-RPC

    async def _send(self, msg: dict[str, Any]) -> None:
        if not self.alive or self.proc is None or self.proc.stdin is None:
            raise McpError(f"MCP server {self.name} is not running" + (f": {self.error}" if self.error else ""))
        # 纯 ASCII（\u 转义）：服务器的 stdin 编码设置不对（Windows 上常见 GBK）也不会乱码
        data = (json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=True) + "\n").encode("ascii")
        async with self._write_lock:
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
        self._next += 1
        rid = self._next
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError as e:
            raise McpError(f"MCP server {self.name}: {method} timed out ({timeout:.0f}s)") from e
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"method": method, "params": params})

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.debug("MCP %s non-JSON output: %s", self.name, line[:200])
                continue
            if "method" in msg and "id" in msg:  # 服务器发来的请求（sampling、elicitation…）：不支持，明确拒绝
                await self._send({"id": msg["id"], "error": {"code": -32601, "message": "client does not support this"}})
            elif "method" in msg:
                if msg["method"] == "notifications/tools/list_changed":
                    asyncio.create_task(self._safe_refresh())
            else:
                fut = self._pending.get(msg.get("id"))
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(McpError(str(msg["error"].get("message") or msg["error"])))
                    else:
                        fut.set_result(msg.get("result") or {})
        self.error = "process exited" + (f": {exit_reason(list(self._stderr))}" if self._stderr else "")
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(McpError(f"MCP server {self.name}: {self.error}"))

    async def _safe_refresh(self) -> None:
        with contextlib.suppress(Exception):
            await self.refresh_tools()

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while line := await self.proc.stderr.readline():
            self._stderr = [*self._stderr[-50:], line.decode("utf-8", "replace").rstrip()]

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                if self.proc.stdin:
                    self.proc.stdin.close()
                await asyncio.wait_for(self.proc.wait(), 3)
            if self.proc.returncode is None:
                kill_tree(self.proc.pid)
        if self._reader:
            self._reader.cancel()
