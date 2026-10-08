"""把 MCP 服务器的工具接进工具注册表。参照 grok：

- 工具少时直接展开：每个 MCP 工具一个 `mcp__<服务器>__<工具>`（grok 的 server__tool 命名）。
- 工具多时（超过 [mcp] search_threshold，默认 30）只给模型两个工具：search_tool（BM25 检索工具说明）和
  use_tool（按名字调用），并在第一条消息里用 system-reminder 列出接了哪些服务器
  （grok common/xai-tool-runtime/src/search.rs、builder.rs 的 search_tool / use_tool）。工具描述不全塞进请求，省 token。
- 权限：服务器标了 readOnlyHint（或配置里列为只读）的按只读放行；其余按普通操作，默认模式下要确认。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from ..model.types import ImageBlock, ReminderBlock, TextBlock, ToolSpec
from ..tools.base import Tool, ToolCaps, ToolContext, ToolResult
from .client import McpClient, McpError, McpServerConfig, McpToolInfo

log = logging.getLogger("firmwright.mcp")


def qualified(server: str, tool: str) -> str:
    name = f"mcp__{server}__{tool}"
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64]


# ---------------------------------------------------------------------- BM25


_TOKEN = re.compile(r"[A-Za-z0-9]+|[^\x00-\x7f]")


def tokenize(text: str) -> list[str]:
    """英文按单词（再拆 snake_case / camelCase），中文按单字。"""
    out: list[str] = []
    for t in _TOKEN.findall(text):
        if t.isascii():
            parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", t) or [t]
            out += [p.lower() for p in parts]
            if len(parts) > 1:
                out.append(t.lower())
        else:
            out.append(t)
    return out


class Bm25:
    def __init__(self, docs: list[str], k1: float = 1.2, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(d)) for d in docs]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = sum(self.lens) / len(self.lens) if self.lens else 0
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for d, ln in zip(self.docs, self.lens, strict=True):
            s = 0.0
            for t in q:
                f = d.get(t, 0)
                if f:
                    s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
            out.append(s)
        return out


# ---------------------------------------------------------------------- 管理器


class McpManager:
    """运行时级别：配置里的 MCP 服务器在核心启动后在后台连接，所有会话共用。"""

    def __init__(self, servers: dict[str, McpServerConfig], search_threshold: int = 30) -> None:
        self.configs = {k: v for k, v in servers.items() if v.enabled}
        self.clients: dict[str, McpClient] = {}
        self.search_threshold = search_threshold
        self._started: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._started is None:
            self._started = asyncio.create_task(self._start_all())
        return self._started

    async def _start_all(self) -> None:
        async def one(name: str, cfg: McpServerConfig) -> None:
            c = McpClient(name, cfg)
            self.clients[name] = c
            try:
                await c.start()
                log.info("MCP %s connected: %d tools", name, len(c.tools))
            except Exception as e:
                c.error = f"{type(e).__name__}: {e}"
                log.warning("MCP %s failed to connect: %s", name, c.error)
                await c.stop()

        await asyncio.gather(*(one(n, c) for n, c in self.configs.items()))

    async def ready(self, timeout: float = 30) -> None:
        if self._started:
            await asyncio.wait({self._started}, timeout=timeout)

    def all_tools(self) -> list[McpToolInfo]:
        return [t for c in self.clients.values() if c.alive for t in c.tools]

    def find(self, qname: str) -> tuple[McpClient, McpToolInfo] | None:
        for c in self.clients.values():
            for t in c.tools:
                if qualified(c.name, t.name) == qname or f"{c.name}/{t.name}" == qname:
                    return c, t
        return None

    def status(self) -> list[dict[str, Any]]:
        return [{"name": n, "alive": c.alive, "tools": len(c.tools), "error": c.error,
                 "server": c.server_info.get("name")} for n, c in self.clients.items()]

    def registry_tools(self) -> list[Tool]:
        """注册进会话的工具：少就直接展开，多就换成 search_tool + use_tool。"""
        tools = self.all_tools()
        if not tools:
            return []
        if len(tools) <= self.search_threshold:
            return [McpTool(self, t) for t in tools]
        return [SearchTool(self), UseTool(self)]

    def listing(self) -> ReminderBlock | None:
        alive = [c for c in self.clients.values() if c.alive and c.tools]
        if not alive or len(self.all_tools()) <= self.search_threshold:
            return None
        lines = [f"- {c.name}: {len(c.tools)} tools ({', '.join(t.name for t in c.tools[:8])}"
                 f"{'…' if len(c.tools) > 8 else ''})" for c in alive]
        return ReminderBlock(source="mcp", text=(
            "These MCP servers are connected. Their tools are not listed directly: find them by purpose with search_tool, "
            "then call them by name with use_tool.\n" + "\n".join(lines)))

    async def stop(self) -> None:
        await asyncio.gather(*(c.stop() for c in self.clients.values()), return_exceptions=True)


async def call(mgr: McpManager, client: McpClient, info: McpToolInfo, args: dict[str, Any]) -> ToolResult:
    try:
        res = await client.call_tool(info.name, args)
    except McpError as e:
        return ToolResult.error(str(e), server=client.name)
    content: list[TextBlock | ImageBlock] = []
    for c in res.get("content") or []:
        if c.get("type") == "text":
            content.append(TextBlock(text=c.get("text", "")))
        elif c.get("type") == "image":
            content.append(ImageBlock(media_type=c.get("mimeType", "image/png"), data=c.get("data", ""),
                                      alt=f"image returned by {client.name}/{info.name}"))
        elif c.get("type") == "resource":
            r = c.get("resource") or {}
            content.append(TextBlock(text=r.get("text") or f"[resource {r.get('uri')}]"))
    if res.get("structuredContent") is not None and not content:
        content.append(TextBlock(text=json.dumps(res["structuredContent"], ensure_ascii=False, indent=1)))
    if not content:
        content.append(TextBlock(text="(no output)"))
    text = "\n".join(c.text for c in content if isinstance(c, TextBlock))
    if len(text) > 50_000:  # 工具输出太长时截断，避免一次塞满上下文
        content = [TextBlock(text=text[:50_000] + "\n… (MCP tool output exceeded 50,000 characters and was truncated)")]
    return ToolResult(content=content, is_error=bool(res.get("isError")), meta={"server": client.name})


class McpTool(Tool):
    """一个 MCP 工具的包装：参数直接用服务器给的 JSON Schema，不经过 pydantic 校验。"""

    Args = BaseModel  # 占位；spec / parse 都覆盖了

    def __init__(self, mgr: McpManager, info: McpToolInfo) -> None:
        self.mgr = mgr
        self.info = info
        self.name = qualified(info.server, info.name)  # type: ignore[misc]
        self.description = f"[MCP · {info.server}] {info.description}"  # type: ignore[misc]
        self.caps = ToolCaps(read_only=info.read_only, risk="safe" if info.read_only else "normal")  # type: ignore[misc]

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description[:2000], parameters=self.info.input_schema)

    def parse(self, raw: dict[str, Any]) -> Any:
        return _Raw(raw)

    def permission_subject(self, args: Any) -> str:
        return json.dumps(args.data, ensure_ascii=False)[:300]

    async def run(self, ctx: ToolContext, args: Any) -> ToolResult:
        client = self.mgr.clients.get(self.info.server)
        if client is None or not client.alive:
            return ToolResult.error(f"MCP server {self.info.server} is not connected")
        return await call(self.mgr, client, self.info, args.data)


class _Raw:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def model_dump(self) -> dict[str, Any]:
        return self.data


class SearchToolArgs(BaseModel):
    query: str = Field(description="What you want to do, or words likely in the tool name (any language)")
    limit: int = Field(8, description="Maximum number of results")


class SearchTool(Tool):
    name = "search_tool"
    description = ("Search MCP server tools by purpose (BM25). Returns tool names, descriptions and argument formats; "
                   "then call one with use_tool.")
    Args = SearchToolArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def __init__(self, mgr: McpManager) -> None:
        self.mgr = mgr

    def permission_subject(self, args: SearchToolArgs) -> str:
        return args.query

    async def run(self, ctx: ToolContext, args: SearchToolArgs) -> ToolResult:
        tools = self.mgr.all_tools()
        if not tools:
            return ToolResult.error("No MCP server is connected")
        idx = Bm25([f"{t.server} {t.name} {t.name} {t.description}" for t in tools])
        ranked = sorted(zip(idx.scores(args.query), tools, strict=True), key=lambda x: -x[0])
        hits = [(s, t) for s, t in ranked if s > 0][: max(1, min(args.limit, 20))]
        if not hits:
            return ToolResult.text(f"No matching tools (out of {len(tools)}). Try different words.")
        lines = [f"- {qualified(t.server, t.name)} ({t.server}, score {s:.2f}): {t.description[:300]}\n"
                 f"  arguments: {json.dumps(t.input_schema.get('properties', {}), ensure_ascii=False)[:600]}"
                 for s, t in hits]
        return ToolResult.text(f"{len(tools)} tools in total; the {len(hits)} most relevant:\n" + "\n".join(lines))


class UseToolArgs(BaseModel):
    name: str = Field(description="Tool name as returned by search_tool (mcp__server__tool)")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Arguments following the tool's schema")


class UseTool(Tool):
    name = "use_tool"
    description = "Call an MCP tool (find it with search_tool first)."
    Args = UseToolArgs
    caps = ToolCaps(risk="normal")

    def __init__(self, mgr: McpManager) -> None:
        self.mgr = mgr

    def permission_subject(self, args: UseToolArgs) -> str:
        return args.name

    def risk_for(self, args: UseToolArgs):
        found = self.mgr.find(args.name)
        return "safe" if found and found[1].read_only else "normal"

    async def run(self, ctx: ToolContext, args: UseToolArgs) -> ToolResult:
        found = self.mgr.find(args.name)
        if found is None:
            return ToolResult.error(f"No such MCP tool: {args.name}. Use search_tool first.")
        client, info = found
        if not client.alive:
            return ToolResult.error(f"MCP server {client.name} is not connected")
        return await call(self.mgr, client, info, args.arguments)
