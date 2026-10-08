"""核心运行时：把配置、平台适配器、设备管理器、事件路由、会话装配到一起。

ACP 服务（界面入口）和 scripts/run_task.py（评测 / 脚本入口，I02）共用它。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .config import Config, ModelConfig, app_home, make_backend
from .context.memory import MEMORY_PROMPT, MemoryGet, MemorySearch, MemoryStore, Remember
from .context.skills import SkillCatalog, SkillTool, builtin_dir, discover
from .device.events import DeviceEvent
from .device.manager import Board, DeviceManager
from .device.router import EventRouter
from .facts import Facts
from .mcp.tools import McpManager
from .model.types import CancelToken, ModelBackend, ReminderBlock
from .permissions.engine import PermissionEngine
from .permissions.paths import PathPolicy, dotenv_files
from .platform.base import PlatformAdapter
from .services import Services
from .session.agent import AskHuman, AskPermission, Emit, Session
from .session.goal import GoalRunner
from .session.prompt import load_project_rules, system_prompt
from .session.store import SessionMeta, SessionStore, project_key
from .session.subagent import (
    EXPLORE_TOOLS,
    READ_TOOLS,
    ROLE_PROMPT,
    VERIFIER_TOOLS,
    CheckSubagents,
    SpawnSubagent,
    StopSubagent,
)
from .tools.base import ToolContext, ToolRegistry
from .tools.fs import EditFile, Grep, ListDir, ReadFile, WriteFile
from .tools.hw import hardware_tools
from .tools.shell import Shell
from .ui_settings import MODEL_ID, UiSettings, delete_key, describe, key_ref_for, store_key
from .ui_settings import apply as apply_ui_settings
from .workspace import finish, worktree
from .workspace.checkpoint import Checkpointer
from .workspace.worktree import RepoInfo, WorktreeInfo

log = logging.getLogger("firmwright")

_ENDED = {"merged": "This session was merged; it cannot continue",
          "applied": "This session's changes were applied to the project folder; it cannot continue",
          "discarded": "This session was discarded; it cannot continue"}


RAW_EVENT_BYTES = 6000  # context.log_digest 关掉时，每个事件附带的串口原文上限


def _port_lost(res) -> bool:
    """烧录失败是因为串口在烧录过程中不见了 / 打不开（板子正在重启、USB 重新枚举）。"""
    return getattr(res, "error_class", None) in ("port_not_found", "port_busy", "connection_lost", "sync_failed")


def dev_boards_requested() -> bool:
    return os.environ.get("FIRMWRIGHT_SIM_BOARD") == "1" or bool(os.environ.get("FIRMWRIGHT_QEMU_BOARD"))


def dev_boards_allowed() -> bool:
    """模拟板 / QEMU 板只在开发和评测时用（2026-10-06：正式版隐藏模拟板）。PyInstaller 打包的版本有 sys.frozen；
    桌面端发布版启动核心时也会去掉这些环境变量（sidecar.ts），这里是第二道。"""
    return not getattr(sys, "frozen", False)


def default_tools(*, hardware: bool = True) -> ToolRegistry:
    tools = [ReadFile(), WriteFile(), EditFile(), ListDir(), Grep(), Shell()]
    if hardware:
        tools += hardware_tools()
    return ToolRegistry(tools)


class Runtime:
    def __init__(self, config: Config | None = None, *, home: Path | None = None) -> None:
        self.home = home or app_home()
        self.config = config or Config.load(self.home / "config.toml")
        # 界面里加的模型和改的设置（settings.json），合并进来；config.toml 里的模型在界面上只读
        self.toml_models = set(self.config.models)
        self.toml_rules = {k: list(getattr(self.config.permissions, k)) for k in ("allow", "ask", "deny")}
        self.toml_mcp = set(self.config.mcp.servers)
        self.ui = UiSettings.load(self.home)
        apply_ui_settings(self.config, self.ui)
        self.platform: PlatformAdapter | None = None
        self.devices: DeviceManager | None = None
        self.router: EventRouter | None = None
        self.sessions: dict[str, Session] = {}
        self.metas: dict[str, SessionMeta] = {}
        self.on_notify: Callable[[DeviceEvent], None] | None = None  # 空闲时的崩溃通知（I04）→ 界面
        self.sim = None  # 模拟板（FIRMWRIGHT_SIM_BOARD=1）
        self.qemu = None  # QEMU 板（FIRMWRIGHT_QEMU_BOARD=<chip>，评测用）
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._prebuilds: dict[str, asyncio.Task] = {}
        self._restoring: set[str] = set()  # 正在回退的会话（回退含重烧，可能要几十秒；不允许叠着点）
        self._wanted_boards: dict[str, str] = {}  # 板子 id → 等它连上后要自动绑回去的会话
        self.goals: dict[str, GoalRunner] = {}  # W7：每个会话最多一个进行中的目标
        self.mcp = McpManager(self.config.mcp.servers, self.config.mcp.search_threshold)
        self._init_platform()

    def _resolve_idf(self):
        """按 [idf] 配置找 IDF（写法见 config.IdfConfig）。返回 IdfCandidate，找不到时抛 RuntimeError（英文，给界面）。"""
        from .platform.esp_idf import discover as d

        cfg = self.config.idf
        if cfg.path is not None:
            c = d.folder_candidate(cfg.path, cfg.tools_path)
            if not c.usable:
                raise RuntimeError(c.problem)
            return c
        if cfg.eim_json.is_file():
            cands = d.eim_candidates(cfg.eim_json)
            c = next((x for x in cands if x.id == cfg.idf_id), None) if cfg.idf_id else (cands[0] if cands else None)
            if c is None:
                raise RuntimeError(f"No ESP-IDF installation {cfg.idf_id} in {cfg.eim_json}" if cfg.idf_id
                                   else f"No ESP-IDF installation listed in {cfg.eim_json}")
            if not c.usable:
                raise RuntimeError(c.problem)
            return c
        if "eim_json" in cfg.model_fields_set:  # 配置里明确写了这个文件：照写的来，不去别处找
            raise RuntimeError(f"{cfg.eim_json} not found")
        found = [c for c in d.discover(cfg.eim_json) if c.usable]
        if not found:
            raise RuntimeError("ESP-IDF was not found on this computer")
        return found[0]

    def _init_platform(self) -> None:
        from .platform.esp_idf.adapter import EspIdfAdapter
        from .platform.esp_idf.env import IdfEnv

        self.idf = None
        self.idf_error = None
        try:
            self.idf = self._resolve_idf()
        except RuntimeError as e:
            self.idf_error = str(e)
            log.warning("ESP-IDF features are unavailable: %s", e)
            return
        self.platform = EspIdfAdapter(IdfEnv(self.idf.install(), self.home / "idf-env-cache"),
                                      build=self.config.build, lock_path=self.home / "build.lock")

    def select_idf(self, choice) -> bool:
        """向导 / 设置页选了一个 IDF：存进 settings.json。还没有平台时立刻生效（返回 True）；
        已经在用别的 IDF 时要重启核心才生效（会话、设备都绑着原来的适配器），返回 False。"""
        from .ui_settings import apply_idf

        apply_idf(self.config, choice)
        if self.platform is None:
            self._init_platform()
            if self.platform is None:
                raise RuntimeError(self.idf_error or "This ESP-IDF installation cannot be used")
            self.ui.idf = choice
            self.ui.save(self.home)
            return True
        # 先验证能用再保存，免得存了一个坏的、下次启动起不来
        from .platform.esp_idf import discover as d

        c = (d.folder_candidate(Path(choice.path), Path(choice.tools_path) if choice.tools_path else None)
             if choice.source != "eim" else
             next((x for x in d.eim_candidates(Path(choice.eim_json or "")) if x.id == choice.id), None))
        if c is None or not c.usable:
            raise RuntimeError(c.problem if c else f"No ESP-IDF installation {choice.id} in {choice.eim_json}")
        self.ui.idf = choice
        self.ui.save(self.home)
        return False

    def idf_status(self) -> dict:
        """给界面的 IDF 现状（向导、欢迎页、设置页）。"""
        c = self.idf
        return {"active": None if c is None else {"source": c.source, "id": c.id, "path": c.path, "version": c.version,
                                                  "warning": c.warning, "eimJson": c.eim_json, "toolsPath": c.tools_path},
                "error": self.idf_error}

    async def start_devices(self, **kw) -> DeviceManager | None:
        if self.platform is None:
            return None
        if dev_boards_requested() and not dev_boards_allowed():
            log.warning("Ignoring FIRMWRIGHT_SIM_BOARD / FIRMWRIGHT_QEMU_BOARD: development boards are off in release builds")
        elif os.environ.get("FIRMWRIGHT_SIM_BOARD") == "1" and not kw:
            # 开发 / 演示用的模拟板（device/sim.py）：没接真板子时验证设备面板这条链路
            from .device.manager import list_ports, open_serial
            from .device.sim import SimHub

            self.sim = SimHub(self.platform)
            self.platform = cast("PlatformAdapter", self.sim.wrap())  # 包装器把其余方法转给真适配器（__getattr__）
            # FIRMWRIGHT_SIM_ONLY=1：只有模拟板，不枚举真实串口（桌面端开着、占着真板子时，再起一个核心调界面用）
            real = (lambda: []) if os.environ.get("FIRMWRIGHT_SIM_ONLY") == "1" else list_ports
            kw = {"lister": self.sim.lister(real), "opener": self.sim.opener(open_serial)}
        elif (qchip := os.environ.get("FIRMWRIGHT_QEMU_BOARD")) and not kw:
            # 评测用的 QEMU 板（device/qemu.py，2026-10-06 评测方案）：跑真固件的模拟器，接成一块普通的板子
            from .device.manager import list_ports, open_serial
            from .device.qemu import QemuHub

            self.qemu = QemuHub(self.platform, qchip)
            self.platform = cast("PlatformAdapter", self.qemu.wrap())
            # FIRMWRIGHT_QEMU_ONLY=1：不枚举真实串口（评测时不碰用户接着的真板子）
            real = (lambda: []) if os.environ.get("FIRMWRIGHT_QEMU_ONLY") == "1" else list_ports
            kw = {"lister": self.qemu.lister(real), "opener": self.qemu.opener(open_serial)}
        self.devices = DeviceManager(self.platform, home=self.home, facts_for=self._facts_for_board, **kw)
        self.devices.owner_label = self.session_title
        self.router = EventRouter(
            get_board=self.devices.boards.get,
            get_session=self.sessions.get,
            notify=self._notify_idle,
            global_policy=lambda: self.config.defaults.idle_policy,
            raw_log=None if self.config.features.context_log_digest else self._raw_event_log,
        )
        self.devices.listen(self._on_device)
        await self.devices.start()
        return self.devices

    def start_mcp(self) -> None:
        """配置里的 MCP 服务器在后台连接（ACP initialize 时调用；不阻塞界面启动）。"""
        if self.mcp.configs:
            self.mcp.start()

    async def stop(self) -> None:
        await self.mcp.stop()
        if self.devices:
            await self.devices.stop()
        if self.qemu is not None:
            self.qemu.close()

    def _facts_for_board(self, board: Board) -> Facts | None:
        if board.owner_session and (s := self.sessions.get(board.owner_session)):
            return s.services.facts
        return None

    def _raw_event_log(self, ev: DeviceEvent) -> str:
        """context.log_digest 关掉时：事件对应的串口原文（最多 6 KB）。"""
        if not (self.devices and ev.log_ref):
            return ""
        return self.devices.read_log(ev.board_id, ev.log_ref, max_bytes=RAW_EVENT_BYTES)

    def want_board(self, session_id: str, board_id: str) -> None:
        """会话想要的板子暂时不在：记下来，板子连上且没被别的会话占用时自动绑定。"""
        self._wanted_boards[board_id] = session_id
        if self.devices and (b := self.devices.boards.get(board_id)):
            self._maybe_rebind(b)

    def _maybe_rebind(self, b) -> None:
        sid = self._wanted_boards.get(b.id)
        if sid is None or b.state == "disconnected" or not b.port or b.owner_session:
            return
        s = self.sessions.get(sid)
        self._wanted_boards.pop(b.id, None)
        if s is None or s.board_id is not None or self.metas[sid].state != "active":
            return
        try:
            self.bind_board(sid, b.id)
        except Exception as e:
            log.warning("Re-binding board %s to session %s failed: %s", b.id, sid, e)
            return
        s.trace.record("board_rebound", board=b.id)
        asyncio.ensure_future(s.emit({"sessionUpdate": "_fwr/notice", "tone": "info",
                                      "text": f"Board {b.alias} is connected again and was bound back to this session."}))

    def _on_device(self, kind: str, payload) -> None:
        if kind == "state" and self._wanted_boards and payload.id in self._wanted_boards:
            b = self.devices.boards.get(payload.id) if self.devices else None
            if b is not None:
                # 不能在这里直接绑：绑定会再发一条"已绑定"的状态，先于这条（未绑定）的旧状态到达界面，
                # 界面最后停在"未绑定"（2026-10-05 真机：核心已绑回，界面一直显示 waiting for board）。
                # 等这次通知分发完再绑
                asyncio.get_running_loop().call_soon(self._maybe_rebind, b)
        if kind == "event" and self.router:
            route = self.router.route(payload)
            # 板子可能已经不在列表里（pyright 查出来的：原来直接 .owner_session，异常被设备通知吞掉，事件就没进 trace）
            board = self.devices.boards.get(payload.board_id) if self.devices else None
            owner = board.owner_session if board else None
            if owner and (s := self.sessions.get(owner)):
                s.trace.record("device_event", event=payload.kind, severity=payload.severity, id=payload.id,
                               summary=payload.summary, route=route.action)

    def _notify_idle(self, ev: DeviceEvent) -> None:
        if self.on_notify:
            self.on_notify(ev)

    # ------------------------------------------------------------------ 会话

    # ------------------------------------------------------------------ 模型设置（界面，2026-10-05）

    def list_models(self) -> list[dict]:
        return [describe(k, v, source="config.toml" if k in self.toml_models else "settings")
                for k, v in self.config.models.items()]

    def save_model(self, model_id: str, fields: dict, api_key: str | None = None) -> dict:
        """界面添加 / 修改模型。密钥存凭据管理器；没给新密钥时保留原来的。"""
        model_id = model_id.strip()
        if not MODEL_ID.match(model_id):
            raise ValueError("Model name: letters, digits, '.', '_' or '-' (up to 64), starting with a letter or digit")
        if model_id in self.toml_models:
            raise ValueError(f"{model_id} is defined in config.toml; edit it there")
        allowed = {"model", "base_url", "context_window", "vision", "reasoning", "echo_reasoning", "effort_style",
                   "parallel_tool_calls", "temperature"}
        data = {k: v for k, v in fields.items() if k in allowed and v not in (None, "")}
        if not str(data.get("base_url", "")).startswith(("http://", "https://")):
            raise ValueError("Base URL must start with http:// or https://")
        cfg = ModelConfig.model_validate({**data, "key_ref": key_ref_for(model_id)})
        if api_key:
            store_key(model_id, api_key.strip())
        elif model_id not in self.ui.models:
            raise ValueError("An API key is required for a new model")
        self.ui.models[model_id] = cfg
        self.config.models[model_id] = cfg
        if not self.config.defaults.model:
            self.config.defaults.model = self.ui.default_model = model_id
        self.ui.save(self.home)
        return describe(model_id, cfg, source="settings")

    def delete_model(self, model_id: str) -> None:
        if model_id in self.toml_models:
            raise ValueError(f"{model_id} is defined in config.toml; remove it there")
        if model_id not in self.ui.models:
            raise KeyError(f"No model {model_id}")
        in_use = [s for s, m in self.metas.items() if m.model_id == model_id and s in self.sessions]
        if in_use:
            raise ValueError(f"{model_id} is used by {len(in_use)} open session(s); switch them to another model first")
        del self.ui.models[model_id]
        self.config.models.pop(model_id, None)
        if self.config.defaults.model == model_id:
            self.config.defaults.model = next(iter(self.config.models), None)
            self.ui.default_model = self.config.defaults.model
        self.ui.save(self.home)
        delete_key(model_id)

    def set_defaults(self, *, model: str | None = None, idle_policy: str | None = None) -> None:
        """界面改的默认设置写进 settings.json（原来空闲策略只在内存里改，重启就没了）。"""
        if model is not None:
            if model not in self.config.models:
                raise KeyError(f"No model {model}")
            self.config.defaults.model = self.ui.default_model = model
        if idle_policy in ("ignore", "notify"):
            self.config.defaults.idle_policy = self.ui.idle_policy = idle_policy  # type: ignore[assignment]
        self.ui.save(self.home)

    # ------------------------------------------------------------------ 设置页的其他设置（2026-10-06）

    def update_general(self, *, build_jobs: int | None = None, worktree_root: str | None = None,
                       permission_mode: str | None = None) -> None:
        """编译并行数立刻生效（适配器和配置共用同一个 BuildConfig）；worktree 根目录、默认权限模式对新会话生效。"""
        if build_jobs is not None:
            if not 0 <= int(build_jobs) <= 64:
                raise ValueError("Build jobs: 0 (automatic) to 64")
            self.config.build.jobs = self.ui.build_jobs = int(build_jobs)
        if worktree_root is not None:
            root = Path(worktree_root.strip().strip('"'))
            if not root.is_absolute() or not root.drive:
                raise ValueError("The worktree folder must be an absolute path such as C:\\fwr\\wt")
            if " " in str(root):
                raise ValueError("ESP-IDF cannot build in paths with spaces; choose a folder without spaces")
            if self._inside_protected(root):
                raise ValueError("The worktree folder cannot be inside Firmwright's data folder")
            root.mkdir(parents=True, exist_ok=True)
            self.config.worktree.root = root
            self.ui.worktree_root = str(root)
        if permission_mode is not None:
            if permission_mode not in ("default", "accept_edits", "plan", "always_approve"):
                raise ValueError(f"Unknown permission mode {permission_mode}")
            self.config.defaults.permission_mode = self.ui.permission_mode = permission_mode  # type: ignore[assignment]
        self.ui.save(self.home)

    def _inside_protected(self, p: Path) -> bool:
        try:
            p.resolve().relative_to(self.home.resolve())
            return True
        except ValueError:
            return False

    def set_rules(self, allow: list[str], ask: list[str], deny: list[str]) -> None:
        """界面里加的权限规则（config.toml 里的只读、排在前面）。已加载的会话立刻换上新规则，
        "本会话都允许"的那些保留。"""
        from .permissions.engine import Rule

        lists = {"allow": allow, "ask": ask, "deny": deny}
        clean: dict[str, list[str]] = {}
        for kind, rules in lists.items():
            out: list[str] = []
            for r in rules:
                r = r.strip()
                if not r or r in out or r in self.toml_rules[kind]:
                    continue
                Rule.parse(r)  # 写错了抛 ValueError
                out.append(r)
            clean[kind] = out
        from .ui_settings import RuleLists

        self.ui.rules = RuleLists(**clean)
        for kind in lists:
            setattr(self.config.permissions, kind, [*self.toml_rules[kind], *clean[kind]])
        self.ui.save(self.home)
        for s in self.sessions.values():
            eng = s.permissions
            eng.allow = [Rule.parse(r) for r in self.config.permissions.allow]
            eng.ask = [Rule.parse(r) for r in self.config.permissions.ask]
            eng.deny = [Rule.parse(r) for r in self.config.permissions.deny]

    def save_mcp(self, name: str, server: dict) -> None:
        """界面添加 / 修改 MCP 服务器（重启核心后生效：MCP 连接在启动时建立）。"""
        from .mcp.client import McpServerConfig

        name = name.strip()
        if not MODEL_ID.match(name):
            raise ValueError("Server name: letters, digits, '.', '_' or '-' (up to 64)")
        if name in self.toml_mcp:
            raise ValueError(f"{name} is defined in config.toml; edit it there")
        cfg = McpServerConfig.model_validate(server)
        if not cfg.command.strip():
            raise ValueError("Command is required")
        self.ui.mcp_servers[name] = cfg
        self.ui.save(self.home)

    def delete_mcp(self, name: str) -> None:
        if name in self.toml_mcp:
            raise ValueError(f"{name} is defined in config.toml; remove it there")
        if self.ui.mcp_servers.pop(name, None) is None:
            raise KeyError(f"No MCP server {name}")
        self.ui.save(self.home)

    def busy_sessions(self) -> list[str]:
        """正在执行 / 有目标 / 有后台子 agent 的会话标题（重启核心前检查）。"""
        return [self.session_title(sid) for sid in self.sessions if self._busy_reason(sid)]

    def backend_for(self, model_id: str | None) -> tuple[ModelBackend, str, str]:
        mid = model_id or self.config.defaults.model or next(iter(self.config.models), None)
        if not mid or mid not in self.config.models:
            raise ValueError(f"Model {mid!r} is not configured; add it under [models.<name>] in {self.home / 'config.toml'}")
        backend, api_model = make_backend(self.config.models[mid], mid)
        return backend, api_model, mid

    def create_session(
        self,
        project_root: Path,
        *,
        model_id: str | None = None,
        board_id: str | None = None,
        mode: str | None = None,
        title: str = "",
        emit: Emit | None = None,
        ask_permission: AskPermission | None = None,
        ask_human: AskHuman | None = None,
        backend: ModelBackend | None = None,
        session_id: str | None = None,
        workspace: WorktreeInfo | None = None,
        repo: RepoInfo | None = None,
    ) -> Session:
        """装配一个会话。新会话的 worktree 由 prepare_workspace 事先建好（git 操作是异步的）。"""
        project_root = project_root.resolve()
        sid = session_id or uuid.uuid4().hex[:6]
        if backend is None:
            backend, api_model, mid = self.backend_for(model_id)
        else:
            api_model = mid = model_id or "custom"
        store = SessionStore(self.home / "sessions" / project_key(project_root) / sid)
        meta = store.load_meta()
        if meta is None:
            meta = SessionMeta(
                id=sid, title=title or project_root.name, project_root=str(project_root), cwd=str(project_root),
                board_id=board_id, model_id=mid, permission_mode=mode or self.config.defaults.permission_mode,
                created_at=datetime.now(UTC).isoformat(),
            )
            if workspace is not None:  # W5：在 worktree 里工作
                meta.isolation, meta.cwd, meta.worktree = "worktree", workspace.cwd, workspace.path
                meta.branch, meta.base_commit = workspace.branch, workspace.base_commit
                meta.target_branch, meta.repo_root = workspace.target_branch, workspace.repo_root
                meta.carried = workspace.carried
            elif repo is not None and repo.head:
                meta.repo_root, meta.base_commit, meta.target_branch = repo.repo_root, repo.head, repo.branch
        store.save_meta(meta)
        rules = self.platform.risk_rules() if self.platform else []
        registry, catalog, memory = self._context_tools(meta)
        paths = self.path_policy(store.root)
        session = Session(
            id=sid, cwd=Path(meta.cwd), backend=backend, model=api_model, registry=registry,
            system_prompt=system_prompt(log_digest=self.config.features.context_log_digest) + (MEMORY_PROMPT if memory else ""),
            permissions=PermissionEngine(self.config.permissions, rules, paths), mode=meta.permission_mode,  # type: ignore[arg-type]
            store=store, services=Services(platform=self.platform, devices=self.devices, facts=Facts.load(Path(meta.cwd)),
                                           paths=paths, log_digest=self.config.features.context_log_digest),
            board_id=None, features=self.config.features, emit=emit, ask_permission=ask_permission,
            ask_human=ask_human, max_steps=self.config.defaults.max_steps,
        )
        session.effort = meta.effort
        git_root = meta.worktree or meta.repo_root
        if git_root and meta.state == "active":
            session.checkpoints = Checkpointer(store.root, Path(git_root), sid, platform=self.platform,
                                               excludes=self.platform.workspace_excludes() if self.platform else [])
        if self.config.features.subagents:
            session.registry.add(SpawnSubagent(self._make_child))
            session.registry.add(CheckSubagents())
            session.registry.add(StopSubagent())
        session.preface = lambda first: self._preface(session, catalog, memory, first)
        session.state_fn = lambda: self._state_text(session)
        if meta.state != "active":
            session.readonly = _ENDED.get(meta.state, "This session has ended")
        self.sessions[sid] = session
        self.metas[sid] = meta
        if board_id and meta.state == "active":
            self.bind_board(sid, board_id)
        return session

    def path_policy(self, session_dir: Path) -> PathPolicy:
        """W7：应用数据目录和密钥文件受保护；本会话自己的目录（压缩存档）和 skill 目录可以读。"""
        read_roots = [builtin_dir(), *self.config.skills.paths, *self.config.permissions.read_roots]
        if self.platform and hasattr(self.platform, "read_roots"):
            read_roots += self.platform.read_roots()
        return PathPolicy(
            protected=list(dict.fromkeys([self.home, *dotenv_files([m.key_ref for m in self.config.models.values()])])),
            readable_inside_protected=[session_dir, self.home / "skills"],
            read_roots=read_roots,
        )

    # ------------------------------------------------------------------ goal 模式（W7）

    def goal_active(self, session_id: str) -> GoalRunner | None:
        g = self.goals.get(session_id)
        return g if g and g.task and not g.task.done() else None

    def start_goal(self, session_id: str, objective: str, *, max_rounds: int = 5) -> GoalRunner:
        s = self._require_idle(session_id)
        if self.goal_active(session_id):
            raise RuntimeError("This session already has a goal in progress")
        if not objective.strip():
            raise ValueError("The goal cannot be empty")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        runner = GoalRunner(s, self._make_child, objective.strip(), max_rounds=max(1, min(max_rounds, 20)),
                            device_oracle=self.config.features.verify_device_oracle,
                            progress_judge=self.config.features.verify_progress_judge,
                            store_dir=s.store.root / "goals" / stamp if s.store else None)
        self.goals[session_id] = runner
        runner.start()
        return runner

    def stop_goal(self, session_id: str) -> bool:
        g = self.goal_active(session_id)
        if g:
            g.stop()
        return g is not None

    # ------------------------------------------------------------------ 子 agent（W7）

    def _make_child(self, parent: Session, kind: str, description: str) -> Session:
        """子会话：自己的历史和存储（父会话目录下的 subagents\\<id>\\），共享工作目录、板子、权限规则。"""
        parent.child_seq += 1
        cid = f"{parent.id}.{kind[:3]}{parent.child_seq}"
        store = SessionStore(parent.store.root / "subagents" / cid) if parent.store else None
        meta = self.metas.get(parent.id)
        if meta is not None:
            registry, catalog, memory = self._context_tools(meta)
        else:  # 测试里直接构造的会话
            registry, catalog, memory = parent.registry.only(set(parent.registry.names())), None, None
            registry.remove("spawn_subagent")
        if kind == "explore":
            registry, mode = registry.only(EXPLORE_TOOLS), "plan"
        elif kind in ("plan", "planner"):
            registry, mode = registry.only(READ_TOOLS), "plan"
        elif kind == "verifier":
            registry, mode = registry.only(VERIFIER_TOOLS), ("default" if parent.mode == "plan" else parent.mode)
        else:
            mode = parent.mode
        label = f"{kind}·{description}"

        async def emit(u: dict) -> None:
            # 子 agent 的工具调用、状态转给父会话的界面（id 加前缀，避免和父会话的工具调用重名）；文字增量不转
            kind_u = u.get("sessionUpdate")
            if kind_u in ("tool_call", "tool_call_update"):
                u = {**u, "toolCallId": f"{cid}:{u.get('toolCallId')}"}
            elif kind_u not in ("_fwr/compacted", "_fwr/turn_end"):
                return
            await parent.emit({"sessionUpdate": "_fwr/subagent_update", "subagentId": cid, "label": label, "update": u})

        async def ask_permission(req):
            if parent.ask_permission_cb is None:
                return "reject"
            req = req.model_copy(update={"tool_call_id": f"{cid}:{req.tool_call_id}",
                                         "title": f"[sub-agent {label}] {req.title}"})
            return await parent.ask_permission_cb(req)

        async def ask_human(action):
            if parent.ask_human_cb is None:
                from .tools.base import HumanReply

                return HumanReply(done=False, note="no UI attached")
            return await parent.ask_human_cb(action.model_copy(update={"title": f"[sub-agent {label}] {action.title}"}))

        child = Session(
            id=cid, cwd=parent.cwd, backend=parent.backend, model=parent.model, registry=registry,
            permissions=parent.permissions, mode=mode, store=store, services=parent.services,  # type: ignore[arg-type]
            board_id=parent.board_id, features=parent.features, emit=emit, ask_permission=ask_permission,
            ask_human=ask_human, max_steps=self.config.defaults.max_steps,
            system_prompt=parent.system_prompt + ROLE_PROMPT.get(kind, ""))
        child.parent_id = parent.id
        child.preface = lambda first: self._preface(child, catalog, memory, first)
        child.state_fn = parent.state_fn
        return child

    # ------------------------------------------------------------------ 上下文（W6）

    def _context_tools(self, meta: SessionMeta) -> tuple[ToolRegistry, SkillCatalog | None, MemoryStore | None]:
        """每个会话的工具：基础工具 + skill + 记忆 + MCP。功能开关关掉的那一层不注册（对照实验用）。"""
        registry = default_tools(hardware=bool(self.platform))
        feats = self.config.features
        catalog = memory = None
        if feats.context_skills:
            skills = discover(Path(meta.cwd), self.home / "skills", disabled=self.config.skills.disabled)
            for extra in self.config.skills.paths:
                skills += [s for s in discover(None, extra, builtin=Path("__none__"))
                           if s.name not in {x.name for x in skills}]
            catalog = SkillCatalog(skills)
            if catalog.skills:
                registry.add(SkillTool(catalog))
        if feats.context_memory:
            root = Path(meta.repo_root or meta.project_root)  # 同一个仓库的 worktree 共用工程记忆
            memory = MemoryStore(self.home / "memory", project_key(root), root.name)
            for t in (MemorySearch(memory), MemoryGet(memory), Remember(memory)):
                registry.add(t)
        for t in self.mcp.registry_tools():
            registry.add(t)
        return registry, catalog, memory

    def _preface(self, s: Session, catalog: SkillCatalog | None, memory: MemoryStore | None,
                 first_prompt: str) -> list[ReminderBlock]:
        """第一轮（以及压缩之后）注入的内容：项目规则、skill 清单、MCP 服务器、记忆。"""
        out: list[ReminderBlock] = []
        if rules := load_project_rules(s.cwd):
            out.append(rules)
        if catalog and (lst := catalog.listing(self._board_chip(s))):
            out.append(lst)
        if lst := self.mcp.listing():
            out.append(lst)
        if memory and (mem := memory.preface(first_prompt)):
            out.append(mem)
        return out

    def _board_chip(self, s: Session) -> str | None:
        if self.devices and s.board_id and (b := self.devices.boards.get(s.board_id)):
            return b.chip
        info = self.platform.detect(s.cwd) if self.platform else None
        return info.target if info else None

    def _state_text(self, s: Session) -> str:
        """压缩时附上的当前状态（新提）：摘要可能漏掉的硬件状态，直接从系统里读出来。"""
        meta = self.metas.get(s.id)
        lines = ["Current state (generated by the system at compaction time; more reliable than the summary):"]
        if meta and meta.isolation == "worktree":
            lines.append(f"- Working directory: {s.cwd} (git worktree, branch {meta.branch}, "
                         f"from {meta.target_branch or 'the commit at that time'})")
        else:
            lines.append(f"- Working directory: {s.cwd}")
        board = self.devices.boards.get(s.board_id) if self.devices and s.board_id else None
        if board:
            lines.append(f"- Bound board: {board.alias} ({board.chip or 'unknown chip'}, {board.port or 'not connected'}, "
                         f"state {board.state})")
            if self.devices:
                crashes = [e for e in list(self.devices.recent.get(board.id, ()))[-20:] if e.severity == "critical"]
                if crashes:
                    e = crashes[-1]
                    lines.append(f"- Latest critical event: {e.id} {e.kind} · {e.summary} "
                                 f"({e.at.isoformat(timespec='seconds')})")
        else:
            lines.append("- No board bound")
        if self.platform and (info := self.platform.detect(s.cwd)):
            ok = {True: "succeeded", False: "failed", None: "unknown"}[info.last_build_ok]
            lines.append(f"- Project: target {info.target or 'not set'}, built {'yes' if info.built else 'no'}, "
                         f"last build {ok}")
        if s.checkpoints and s.checkpoints.entries:
            last = s.checkpoints.entries[-1]
            fw = s.checkpoints.firmware_at(last.seq)
            lines.append(f"- Checkpoint: at #{last.seq} ({last.kind})" +
                         (f", last restored to #{last.restored_to}" if last.kind == "restore" else ""))
            if fw:
                lines.append(f"- Firmware on the board: flashed in turn {fw.turn} ({fw.scope}), "
                             f"sha256 {(fw.sha256 or 'unknown')[:12]}")
        if s.services.facts:
            f = s.services.facts
            lines.append(f"- facts.toml: pass_marker={f.pass_marker!r}, fail_marker={f.fail_marker!r}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 工作区（W5）

    def _repo_lock(self, repo: str | None) -> asyncio.Lock:
        """同一个仓库的 worktree 增删、合并串行（git 的 worktree 元数据和分支引用是共享的）。"""
        return self._repo_locks.setdefault((repo or "").lower(), asyncio.Lock())

    async def prepare_workspace(self, project_root: Path, session_id: str, *, isolation: str | None = None,
                                carry_dirty: bool = True) -> tuple[WorktreeInfo | None, RepoInfo]:
        """新会话：是 git 仓库就建 worktree（I09）。不是仓库 / 没有提交时抛 NotGitRepo / NoCommits，
        由界面问用户要不要初始化（§5.2）。isolation="in_place" 表示用户选择直接在工程目录里工作。"""
        repo = await worktree.inspect(project_root)
        use_wt = self.config.worktree.enabled and isolation != "in_place"
        if not use_wt:
            return None, repo
        if not repo.is_git:
            raise worktree.NotGitRepo(str(project_root))
        if repo.head is None:
            raise worktree.NoCommits(repo.repo_root or str(project_root))
        async with self._repo_lock(repo.repo_root):
            info = await worktree.create(project_root, session_id, root=self.config.worktree.root,
                                         seeds=self.platform.worktree_seeds() if self.platform else [],
                                         carry_dirty=carry_dirty)
        return info, repo

    async def init_git(self, project_root: Path, *, new_project: bool = False) -> RepoInfo:
        tmpl = self.platform.gitignore_template() if self.platform else ""
        return await worktree.init_repo(project_root, tmpl, new_project=new_project)

    def new_session_id(self) -> str:
        while True:
            sid = uuid.uuid4().hex[:6]
            if not (self.config.worktree.root / sid).exists() and not list(
                    (self.home / "sessions").glob(f"*/{sid}")):
                return sid

    def start_prebuild(self, session_id: str) -> asyncio.Task | None:
        """§5.2：新建会话后在后台先编译一次，第一次让 agent 编译时就不用等全量编译。"""
        s = self.sessions.get(session_id)
        if not (s and self.platform and self.config.worktree.prebuild):
            return None
        platform = self.platform
        info = platform.detect(s.cwd)
        if info is None:
            return None
        cancel = CancelToken()

        async def run() -> None:
            async def progress(text: str) -> None:
                await s.emit({"sessionUpdate": "_fwr/prebuild", "status": "running", "text": text})

            await s.emit({"sessionUpdate": "_fwr/prebuild", "status": "running", "text": "Initial background build…"})
            ctx = ToolContext(session_id=s.id, cwd=s.cwd, cancel=cancel, trace=s.trace, progress_cb=progress,
                              services=s.services)
            try:
                res = await platform.build(ctx)
                s.trace.record("prebuild", ok=res.ok, summary=res.summary, duration_ms=res.duration_ms)
                text = res.summary or res.error_class or ""
                if cancel.cancelled:  # 会话结束 / 关闭时取消的，不算编译失败
                    text = f"cancelled ({cancel.reason})"
                await s.emit({"sessionUpdate": "_fwr/prebuild", "status": "ok" if res.ok else "failed",
                              "text": text, "durationMs": res.duration_ms})
            except Exception as e:
                await s.emit({"sessionUpdate": "_fwr/prebuild", "status": "failed", "text": f"{type(e).__name__}: {e}"})
            finally:
                self._prebuilds.pop(s.id, None)

        task = asyncio.create_task(run())
        task.cancel_token = cancel  # type: ignore[attr-defined]
        self._prebuilds[s.id] = task
        return task

    async def _stop_prebuild(self, session_id: str) -> None:
        task = self._prebuilds.pop(session_id, None)
        if task and not task.done():
            task.cancel_token.cancel("session ended")  # type: ignore[attr-defined]
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, 30)

    def _require_idle(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise KeyError(f"Session {session_id} is not loaded")
        if s.running:
            raise RuntimeError("The session is running; wait for it to finish or cancel it first")
        if s.background and s.background.active():
            # 后台子 agent 和会话共用工作目录：回退 / 合并 / 丢弃会把它脚下的文件换掉
            raise RuntimeError("Background sub-agents are still running in this session; wait for them or stop them first")
        if s.readonly:
            raise RuntimeError(s.readonly)
        return s

    async def restore_checkpoint(self, session_id: str, seq: int, *, reflash: bool = False) -> dict:
        """D05：回退到某个 checkpoint；用户选择时把当时的固件重新烧回去。"""
        s = self._require_idle(session_id)
        if not s.checkpoints:
            raise RuntimeError("This session has no checkpoints (the project is not a git repository)")
        # 2026-10-05 真机：重烧期间弹层关了再打开、又点了一次，两个回退叠在一起排队烧录
        if session_id in self._restoring:
            raise RuntimeError("A restore is already in progress for this session; wait for it to finish")
        self._restoring.add(session_id)
        try:
            return await self._restore_checkpoint(s, seq, reflash=reflash)
        finally:
            self._restoring.discard(session_id)

    async def _restore_checkpoint(self, s: Session, seq: int, *, reflash: bool) -> dict:
        ck = s.checkpoints
        assert ck is not None  # restore_checkpoint 已经检查过
        fw = ck.firmware_at(seq)
        entry = await ck.restore(seq, turn=s.trace.turn)
        unlinked = list(ck.unlinked)
        s.trace.record("restore", to=seq, seq=entry.seq, commit=entry.commit, unlinked=unlinked)
        flash: dict | None = None
        if reflash:
            flash = await self._reflash(s, fw)
            if flash.get("ok") and fw:
                rec = ck.record_flash(turn=s.trace.turn, cwd=s.cwd, sha256=flash.get("image_sha256") or
                                                 fw.sha256, scope=fw.scope, chip=fw.chip, board_id=s.board_id,
                                                 port=flash.get("port"), source="restore",
                                                 archive_from=Path(fw.archive) if fw.archive else None)
                ck.attach_firmware(entry.seq, rec)
        target = ck.entries[seq]
        note = (f"The user restored the working directory to checkpoint #{seq} (the state at the end of turn "
                f"{target.turn}, commit {target.commit[:10]}). File changes after that point were undone, but the "
                "conversation was kept; file contents you read earlier may be stale, so re-read them when needed. ")
        if unlinked:
            note += ("Directory links in the working directory were removed before restoring (links are never part of "
                     f"checkpoints): {', '.join(unlinked)}. Do not recreate links to folders outside the working directory. ")
        if flash is not None:
            note += (f"The board was reflashed with the firmware from that point "
                     f"(sha256 {str(flash.get('image_sha256') or '')[:12]}). "
                     if flash.get("ok") else f"Reflashing the firmware from that point failed: {flash.get('summary')}. ")
            note += "Build outputs were not restored; build before the next flash."
        s.inject(ReminderBlock(source="checkpoint", text=note))
        await s.emit({"sessionUpdate": "_fwr/checkpoint", "entry": ck.entries[entry.seq].model_dump(mode="json"),
                      "flash": flash})
        return {"entry": ck.entries[entry.seq].model_dump(mode="json"), "flash": flash, "unlinked": unlinked}

    async def preview_restore(self, session_id: str, seq: int) -> dict:
        s = self.sessions[session_id]
        if not s.checkpoints:
            raise RuntimeError("This session has no checkpoints (the project is not a git repository)")
        return await s.checkpoints.preview(seq)

    async def _reflash(self, s: Session, fw) -> dict:
        if fw is None:
            return {"ok": False, "summary": "No firmware was flashed before this point"}
        if not fw.archive:
            return {"ok": False, "summary": "The firmware archive from that point was pruned (only the latest 20 are kept)"}
        if not (self.devices and s.board_id and self.platform):
            return {"ok": False, "summary": "No board is bound to the session"}
        board = self.devices.boards.get(s.board_id)
        if board is None:
            return {"ok": False, "summary": "The board is not connected"}
        ctx = ToolContext(session_id=s.id, cwd=s.cwd, cancel=CancelToken(), trace=s.trace, services=s.services,
                          board_id=s.board_id)
        # 板子正在崩溃重启（USB-JTAG 会短暂掉线）时等它回来，掉线就再试一次（2026-10-05 真机：
        # 重烧那一刻板子正好在重启，"serial port not found" 直接失败，20 秒后板子就回来了）
        res = None
        for attempt in (1, 2):
            if not await self._wait_connected(board.id, 20):
                return {"ok": False, "summary": "The board is not connected (waited 20 s); replug it and restore again"}
            async with self.devices.exclusive(board.id) as b:
                res = await self.platform.flash_image(ctx, b.port or "", Path(fw.archive), fw.scope)
            if res.ok or attempt == 2 or not _port_lost(res):
                break
            s.trace.record("reflash_retry", error=res.error_class, summary=res.summary)
        assert res is not None
        if res.ok and board.id in self.devices.monitors:
            await asyncio.sleep(0.3)
            self.devices.marks[board.id] = time.monotonic()
            if why := await self.devices.reset_after_flash(board.id):
                s.trace.record("post_flash_reset_failed", error=why)
        s.trace.record("reflash", ok=res.ok, sha256=res.image_sha256, archive=fw.archive)
        return res.model_dump()

    async def _wait_connected(self, board_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            b = self.devices.boards.get(board_id) if self.devices else None
            if b and b.port and b.state != "disconnected":
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def session_diff(self, session_id: str) -> dict:
        s = self.sessions[session_id]
        if not s.checkpoints:
            raise RuntimeError("This session has no checkpoints (the project is not a git repository)")
        return await s.checkpoints.diff_base()

    async def export_patch(self, session_id: str, dest: Path | None = None) -> dict:
        s = self.sessions[session_id]
        if not s.checkpoints:
            raise RuntimeError("This session has no checkpoints (the project is not a git repository)")
        dest = dest or s.checkpoints.dir / "exports" / f"fwr-{session_id}.patch"
        res = await finish.export_patch(s.checkpoints, dest)
        s.trace.record("export_patch", path=str(dest), files=len(res["files"]))
        return res

    async def merge_session(self, session_id: str, *, target: str | None = None, message: str | None = None) -> dict:
        s = self._require_idle(session_id)
        meta = self.metas[session_id]
        if meta.isolation != "worktree" or not (s.checkpoints and meta.repo_root and meta.base_commit):
            raise RuntimeError("Only sessions that work in a worktree can be merged")
        target = target or meta.target_branch
        if not target:
            raise finish.MergeError("The project was not on any branch when the session was created (detached HEAD); "
                                    "choose a branch to merge into",
                                    kind="no_target")
        repo = Path(meta.repo_root)
        async with self._repo_lock(meta.repo_root):
            res = await finish.merge(s.checkpoints, repo=repo, base_commit=meta.base_commit, target=target,
                                     message=message or meta.title or f"Session {session_id}",
                                     session_path=Path(meta.worktree) if meta.worktree else None)
        s.trace.record("merge", target=target, commit=res.get("commit"), files=len(res.get("files", [])),
                       skipped=res.get("skipped"))
        res["cleanup"] = await self._end_session(s, "merged", merged_commit=res.get("commit"))
        return res

    async def apply_session(self, session_id: str) -> dict:
        """收尾：把改动作为未提交改动套回用户的工程文件夹（不提交），然后结束会话。"""
        s = self._require_idle(session_id)
        meta = self.metas[session_id]
        if meta.isolation != "worktree" or not (s.checkpoints and meta.repo_root):
            raise RuntimeError("Only sessions that work in a worktree can be applied")
        async with self._repo_lock(meta.repo_root):
            res = await finish.apply_to_folder(s.checkpoints, Path(meta.repo_root))
        s.trace.record("apply", folder=meta.repo_root, files=len(res.get("files", [])))
        res["cleanup"] = await self._end_session(s, "applied")
        return res

    async def discard_session(self, session_id: str) -> dict:
        s = self._require_idle(session_id)
        if self.metas[session_id].isolation != "worktree":
            raise RuntimeError("This session works directly in the project folder; there is no worktree to discard")
        s.trace.record("discard")
        return {"cleanup": await self._end_session(s, "discarded")}

    async def _end_session(self, s: Session, state: str, *, merged_commit: str | None = None) -> list[str]:
        """合并完成或确认丢弃后：删 worktree、会话分支和 checkpoint 引用（D06：只有这两种情况才删）。"""
        meta = self.metas[s.id]
        await self._stop_prebuild(s.id)
        if s.background:
            s.background.stop_all("the session ended")
        problems: list[str] = []
        if s.checkpoints:
            with contextlib.suppress(Exception):
                await s.checkpoints.delete_refs()
        if meta.worktree and meta.repo_root:
            async with self._repo_lock(meta.repo_root):
                problems = await worktree.remove(Path(meta.repo_root), Path(meta.worktree), meta.branch)
        if self.devices and s.board_id:
            self.devices.release(s.board_id, s.id)
            s.board_id = None
        meta.state, meta.ended_at, meta.merged_commit = state, datetime.now(UTC).isoformat(), merged_commit
        meta.board_id = None
        if s.store:
            s.store.save_meta(meta)
        s.checkpoints = None
        s.readonly = _ENDED.get(state, "This session has ended")
        await s.emit({"sessionUpdate": "_fwr/session_ended", "state": state, "mergedCommit": merged_commit,
                      "problems": problems})
        return problems

    def bind_board(self, session_id: str, board_id: str | None, *, take: bool = False) -> None:
        """take=True：板子在别的会话手里时移过来（2026-10-05 决定 3）。原会话正在执行 / 有目标在跑时不移；
        移走后给原会话插一条说明，免得它的 agent 以为板子掉线了。"""
        s = self.sessions[session_id]
        # 用户自己选了板子（或选了"不绑"）：取消这个会话"等板子连上再自动绑"的请求
        for bid in [k for k, v in self._wanted_boards.items() if v == session_id]:
            self._wanted_boards.pop(bid, None)
        prev_owner = None
        if board_id and self.devices:
            b = self.devices.boards.get(board_id)
            prev_owner = b.owner_session if b and b.owner_session != session_id else None
        if self.devices and s.board_id and s.board_id != board_id:
            self.devices.release(s.board_id, session_id)
        if board_id and self.devices:
            if take:
                self.devices.transfer(board_id, session_id, from_busy=self._busy_reason)
            else:
                self.devices.acquire(board_id, session_id)  # 被占用时抛 BoardBusy
        s.board_id = board_id
        self._save_board(session_id, board_id)
        if take and board_id and prev_owner and prev_owner in self.sessions:
            old = self.sessions[prev_owner]
            old.board_id = None
            self._save_board(prev_owner, None)
            alias = self.devices.boards[board_id].alias if self.devices else board_id
            old.inject(ReminderBlock(source="device", text=(
                f'The user moved board {alias} to another session ("{self.metas[session_id].title}"). This session has no '
                "board now; device tools will fail until the user binds one. This is not a hardware disconnect.")))
            asyncio.ensure_future(old.emit({"sessionUpdate": "_fwr/notice", "tone": "warn",
                                            "text": f'Board {alias} was moved to session "{self.metas[session_id].title}".'}))

    def _save_board(self, session_id: str, board_id: str | None) -> None:
        meta = self.metas[session_id]
        meta.board_id = board_id
        s = self.sessions[session_id]
        if s.store:
            s.store.save_meta(meta)

    def _busy_reason(self, session_id: str) -> str | None:
        s = self.sessions.get(session_id)
        if s is None:
            return None
        if self.goal_active(session_id):
            return "has a goal in progress"
        if s.running:
            return "is running"
        if s.background and s.background.active():
            return "has background sub-agents running"
        return None

    def session_title(self, session_id: str | None) -> str:
        meta = self.metas.get(session_id or "")
        return meta.title if meta else (session_id or "")

    def close_session(self, session_id: str) -> None:
        """从内存里卸载会话（界面关闭标签页）。worktree 保留，之后还能 session/load。"""
        task = self._prebuilds.pop(session_id, None)
        if task and not task.done():
            task.cancel_token.cancel("session closed")  # type: ignore[attr-defined]
        s = self.sessions.pop(session_id, None)
        if s and s.background:
            s.background.stop_all("the session was closed")
        if s and s.board_id and self.devices:
            self.devices.release(s.board_id, session_id)
        self.metas.pop(session_id, None)
