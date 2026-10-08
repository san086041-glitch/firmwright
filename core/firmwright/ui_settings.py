"""界面里改的设置（2026-10-05）：模型、默认模型、空闲策略。存在 <应用数据>\\settings.json。

config.toml 是用户手写的（有注释），程序从不改写它；界面里的修改另存一个文件，启动时合并：
- config.toml 里定义的模型在界面上只读（"在 config.toml 里改"），界面添加的模型不能和它重名
- 界面添加的模型，API Key 存 Windows 凭据管理器（D14，keyring:firmwright/<模型 id>），
  settings.json 里只有引用，没有密钥；界面也拿不到密钥，只知道"有没有"
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .config import Config, ModelConfig, SecretError, make_backend, resolve_key
from .mcp.client import McpServerConfig
from .model.types import (
    CancelToken,
    Message,
    ModelError,
    ModelRequest,
    TextBlock,
    TextDelta,
    ToolCallDone,
    ToolSpec,
)

log = logging.getLogger("firmwright")

FILE = "settings.json"
KEYRING_SERVICE = "firmwright"
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class IdfChoice(BaseModel):
    """向导 / 设置页选的 ESP-IDF（2026-10-06）。source=eim 用 eim_json + id；其他用 path（+ tools_path）。"""

    source: Literal["eim", "legacy", "folder"]
    path: str
    id: str | None = None
    eim_json: str | None = None
    tools_path: str | None = None


class RuleLists(BaseModel):
    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class UiSettings(BaseModel):
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    default_model: str | None = None
    idle_policy: Literal["ignore", "notify"] | None = None
    idf: IdfChoice | None = None
    setup_dismissed: bool | None = None  # 首次启动向导点过"以后再说"或走完了
    # 2026-10-06 设置页补全。标量设置：界面改过的优先于 config.toml；
    # 权限规则、MCP 服务器：和 config.toml 里的合并（那边的在界面上只读，和模型一样）
    build_jobs: int | None = None
    worktree_root: str | None = None
    permission_mode: Literal["default", "accept_edits", "plan", "always_approve"] | None = None
    rules: RuleLists = Field(default_factory=RuleLists)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    @classmethod
    def load(cls, home: Path) -> UiSettings:
        p = home / FILE
        if not p.is_file():
            return cls()
        try:
            return cls.model_validate(json.loads(p.read_text("utf-8")))
        except (ValueError, ValidationError) as e:  # 坏了也不能让核心起不来：记日志，当作没有
            log.warning("Ignoring unreadable %s: %s", p, e)
            return cls()

    def save(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        tmp = home / (FILE + ".tmp")
        tmp.write_text(self.model_dump_json(indent=1, exclude_none=True), "utf-8")
        tmp.replace(home / FILE)


def apply(config: Config, ui: UiSettings) -> None:
    """把界面里的设置合并进配置。config.toml 里的模型优先（界面保存时已经拦住重名）。"""
    for k, m in ui.models.items():
        config.models.setdefault(k, m)
    if ui.default_model and ui.default_model in config.models:
        config.defaults.model = ui.default_model
    if ui.idle_policy:
        config.defaults.idle_policy = ui.idle_policy
    if ui.idf:  # 界面上明确选过的 IDF 优先于 config.toml（选的时候就是要换掉现在这个）
        apply_idf(config, ui.idf)
    if ui.build_jobs is not None:
        config.build.jobs = ui.build_jobs
    if ui.worktree_root:
        config.worktree.root = Path(ui.worktree_root)
    if ui.permission_mode:
        config.defaults.permission_mode = ui.permission_mode
    for kind in ("allow", "ask", "deny"):
        mine = getattr(config.permissions, kind)
        mine.extend(r for r in getattr(ui.rules, kind) if r not in mine)
    for k, m in ui.mcp_servers.items():
        config.mcp.servers.setdefault(k, m)


def apply_idf(config: Config, c: IdfChoice) -> None:
    if c.source == "eim" and c.eim_json:
        config.idf.eim_json, config.idf.idf_id, config.idf.path = Path(c.eim_json), c.id, None
    else:
        config.idf.path = Path(c.path)
        config.idf.tools_path = Path(c.tools_path) if c.tools_path else None


def key_ref_for(model_id: str) -> str:
    return f"keyring:{KEYRING_SERVICE}/{model_id}"


def store_key(model_id: str, key: str) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, model_id, key)


def delete_key(model_id: str) -> None:
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(KEYRING_SERVICE, model_id)
    except PasswordDeleteError:
        pass


def has_key(key_ref: str) -> bool:
    try:
        return bool(resolve_key(key_ref))
    except (SecretError, OSError):
        return False


def describe(model_id: str, cfg: ModelConfig, *, source: str) -> dict[str, Any]:
    """给界面看的模型信息（不含密钥）。"""
    return {"id": model_id, "source": source, "model": cfg.model or model_id, "baseUrl": cfg.base_url,
            "contextWindow": cfg.context_window, "vision": cfg.vision, "reasoning": cfg.reasoning,
            "effortStyle": cfg.effort_style, "keyRef": cfg.key_ref, "hasKey": has_key(cfg.key_ref)}


PING = ToolSpec(name="ping", description="Health check. Call it exactly once with ok=true.",
                parameters={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})


async def test_model(cfg: ModelConfig, model_id: str, *, api_key: str | None = None,
                     timeout: float = 45.0) -> dict[str, Any]:
    """测试连接：发一个带 ping 工具的最小请求，同时检查能不能连上、会不会调用工具（agent 离不开工具调用）。"""
    t0 = time.monotonic()
    try:
        backend, api_model = make_backend(cfg, model_id, api_key=api_key)
    except (SecretError, OSError, ValueError) as e:
        return {"ok": False, "stage": "key", "error": str(e)}
    if hasattr(backend, "max_retries"):
        backend.max_retries = 0  # 测试要马上给结果：连不上就是连不上，不按 agent 运行时那样退避重试（1+2+4+8 秒）
    if isinstance(getattr(backend, "timeout", None), (int, float)):
        backend.timeout = min(backend.timeout, timeout)
    req = ModelRequest(model=api_model, system="You are a connectivity check.", tools=[PING], max_tokens=200,
                       messages=[Message(role="user", content=[TextBlock(text="Call the ping tool now.")])])
    text, called, error = "", False, None

    async def run() -> None:
        nonlocal text, called, error
        async for ev in backend.stream(req, cancel=CancelToken()):
            if isinstance(ev, TextDelta):
                text += ev.text
            elif isinstance(ev, ToolCallDone) and ev.call.name == "ping":
                called = True
            elif isinstance(ev, ModelError):
                error = ev.message + (f" (HTTP {ev.status})" if ev.status else "")

    try:
        await asyncio.wait_for(run(), timeout)
    except TimeoutError:
        error = f"No complete answer within {timeout:.0f} s"
    except Exception as e:  # 网络错误等
        error = f"{type(e).__name__}: {e}"
    ms = int((time.monotonic() - t0) * 1000)
    if error:
        return {"ok": False, "stage": "request", "error": error, "latencyMs": ms}
    return {"ok": True, "toolCalls": called, "latencyMs": ms, "text": text[:300],
            "warning": None if called else "The model answered but did not call the tool. Firmwright needs function "
                                            "calling; this model (or endpoint) may not support it."}
