"""ACP 服务（I03 / I13，§4.5）：核心作为 sidecar，通过 stdio 上的 JSON-RPC 2.0 和界面通信。

标准 ACP 方法（参照 grok 的 xai-acp-lib）：
    initialize · session/new · session/load · session/prompt · session/cancel · session/set_mode
    核心 → 界面：session/update（通知）、session/request_permission（请求）
嵌入式扩展方法，前缀 _fwr/（参照 grok 的 x.ai/ 前缀；ACP 约定扩展方法以下划线开头）：
    界面 → 核心：_fwr/sessions/list · _fwr/models/list · _fwr/boards/list · _fwr/session/bind_board
                 _fwr/session/set_model · _fwr/session/close · _fwr/firmware/size · _fwr/serial/tail
                 _fwr/settings/get · _fwr/settings/set · _fwr/boards/update · _fwr/events/recent
                 2026-10-05：_fwr/boards/rescan · identify；_fwr/session/bind_board 加 take（移板子）
                 W5：_fwr/project/inspect · init_git · branches；_fwr/checkpoints/list · preview · restore；
                     _fwr/session/diff · export_patch · merge · apply · discard
                 W6：_fwr/session/compact · _fwr/context/info
                 W7：_fwr/goal/start · stop · get
    核心 → 界面：_fwr/boards/changed · _fwr/serial/chunk · _fwr/event（通知）
                 _fwr/human/request（请求：人工操作卡片，D13）

约定：stdout 只留给 ACP（一行一条 JSON），日志写 stderr。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import traceback
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from .. import __version__
from ..config import effort_options
from ..context.skills import SkillTool
from ..device.events import DeviceEvent
from ..model.types import CancelToken, ImageBlock, ReminderBlock
from ..runtime import Runtime
from ..session.agent import PermissionReply, PermissionRequest, Session
from ..session.store import SessionMeta, SessionStore
from ..tools.base import HumanAction, HumanReply, ToolContext
from ..trace import Trace
from ..workspace import worktree

log = logging.getLogger("firmwright.acp")

PROTOCOL_VERSION = 1


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


Send = Callable[[dict[str, Any]], Awaitable[None]]


class AcpServer:
    def __init__(self, runtime: Runtime, send: Send) -> None:
        self.rt = runtime
        self._send = send
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._turns: dict[str, asyncio.Task] = {}
        self.rt.on_notify = self._on_idle_notify
        self._unlisten: Callable[[], None] | None = None
        self._inflight: set[asyncio.Task] = set()
        self.request_exit: Callable[[], None] | None = None  # serve_stdio 设置：让核心以退出码 75 结束（重启）

    # ------------------------------------------------------------------ 传输

    async def send(self, msg: dict[str, Any]) -> None:
        msg["jsonrpc"] = "2.0"
        await self._send(msg)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.send({"method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self.send({"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def handle(self, msg: dict[str, Any]) -> None:
        """处理一条收到的消息：响应（回给我们发出的请求）、请求或通知。"""
        if "method" not in msg:
            fut = self._pending.get(msg.get("id"))  # type: ignore[arg-type]
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(RpcError(msg["error"].get("code", -32000), msg["error"].get("message", "")))
                else:
                    fut.set_result(msg.get("result"))
            return
        method, params, rid = msg["method"], msg.get("params") or {}, msg.get("id")
        handler = self._handlers().get(method)
        if handler is None:
            if rid is not None:
                await self.send({"id": rid, "error": {"code": -32601, "message": f"Unknown method {method}"}})
            return
        if rid is None:  # 通知
            try:
                await handler(params)
            except Exception:
                log.exception("Handling notification %s failed", method)
            return
        # session/prompt 会跑很久，不能阻塞其他消息（比如 session/cancel），所以每个请求单独一个 task
        task = asyncio.create_task(self._respond(rid, method, handler, params))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def drain(self, timeout: float = 10) -> None:
        """stdin 关闭后，等正在处理的请求回完再退出。"""
        if self._inflight:
            await asyncio.wait(list(self._inflight), timeout=timeout)

    async def _respond(self, rid, method, handler, params) -> None:
        try:
            result = await handler(params)
            await self.send({"id": rid, "result": result})
        except RpcError as e:
            await self.send({"id": rid, "error": {"code": e.code, "message": e.message, "data": e.data}})
        except Exception as e:
            log.error("%s failed: %s", method, traceback.format_exc())
            await self.send({"id": rid, "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"}})

    def _handlers(self) -> dict[str, Callable[[dict], Awaitable[Any]]]:
        return {
            "initialize": self.initialize,
            "session/new": self.session_new,
            "session/load": self.session_load,
            "session/prompt": self.session_prompt,
            "session/cancel": self.session_cancel,
            "session/set_mode": self.session_set_mode,
            "_fwr/sessions/list": self.sessions_list,
            "_fwr/models/list": self.models_list,
            "_fwr/boards/list": self.boards_list,
            "_fwr/boards/rescan": self.boards_rescan,
            "_fwr/boards/identify": self.boards_identify,
            "_fwr/boards/reset": self.boards_reset,
            "_fwr/boards/update": self.boards_update,
            "_fwr/session/bind_board": self.session_bind_board,
            "_fwr/session/set_model": self.session_set_model,
            "_fwr/session/set_effort": self.session_set_effort,
            "_fwr/session/close": self.session_close,
            "_fwr/firmware/size": self.firmware_size,
            "_fwr/serial/tail": self.serial_tail,
            "_fwr/events/recent": self.events_recent,
            "_fwr/settings/get": self.settings_get,
            "_fwr/settings/set": self.settings_set,
            "_fwr/sim/crash": self.sim_crash,
            "_fwr/sim/plug": self.sim_plug,
            # W5 工作区
            "_fwr/project/inspect": self.project_inspect,
            "_fwr/project/init_git": self.project_init_git,
            "_fwr/project/branches": self.project_branches,
            "_fwr/checkpoints/list": self.checkpoints_list,
            "_fwr/checkpoints/restore": self.checkpoints_restore,
            "_fwr/checkpoints/preview": self.checkpoints_preview,
            "_fwr/session/diff": self.session_diff,
            "_fwr/session/export_patch": self.session_export_patch,
            "_fwr/session/merge": self.session_merge,
            "_fwr/session/apply": self.session_apply,
            "_fwr/session/interrupt": self.session_interrupt,
            "_fwr/session/discard": self.session_discard,
            # W6 上下文
            "_fwr/session/compact": self.session_compact,
            "_fwr/context/info": self.context_info,
            # W7 goal 模式
            "_fwr/goal/start": self.goal_start,
            "_fwr/goal/stop": self.goal_stop,
            "_fwr/goal/get": self.goal_get,
            "_fwr/subagent/stop": self.subagent_stop,
            "_fwr/models/detail": self.models_detail,
            "_fwr/models/save": self.models_save,
            "_fwr/models/delete": self.models_delete,
            "_fwr/models/test": self.models_test,
            # 首次启动向导（2026-10-06）
            "_fwr/setup/status": self.setup_status,
            "_fwr/setup/idf/inspect": self.setup_idf_inspect,
            "_fwr/setup/idf/select": self.setup_idf_select,
            "_fwr/setup/dismiss": self.setup_dismiss,
            # 设置页补全（2026-10-06）
            "_fwr/mcp/save": self.mcp_save,
            "_fwr/mcp/delete": self.mcp_delete,
            "_fwr/core/about": self.core_about,
            "_fwr/core/restart": self.core_restart,
        }

    # ------------------------------------------------------------------ 扩展：首次启动向导（2026-10-06）

    async def setup_status(self, p: dict) -> dict:
        """向导要的全部现状。p.scan=True 时顺带列出本机找到的 IDF（读几个 json，不运行脚本）。"""
        from ..platform.esp_idf.discover import discover

        res: dict[str, Any] = {"idf": self.rt.idf_status(), "models": len(self.rt.config.models),
                               "devices": self.rt.devices is not None, "dismissed": bool(self.rt.ui.setup_dismissed)}
        if p.get("scan"):
            found = await asyncio.to_thread(discover, self.rt.config.idf.eim_json)
            res["candidates"] = [c.model_dump() for c in found]
        return res

    async def setup_idf_inspect(self, p: dict) -> dict:
        from ..platform.esp_idf.discover import inspect_folder

        folder = Path(p.get("path") or "")
        if not folder.is_dir():
            raise RpcError(-32602, f"Folder not found: {folder}")
        return {"candidates": [c.model_dump() for c in await asyncio.to_thread(inspect_folder, folder)]}

    async def setup_idf_select(self, p: dict) -> dict:
        from ..ui_settings import IdfChoice

        try:
            choice = IdfChoice.model_validate({"source": p.get("source"), "path": p.get("path"), "id": p.get("id"),
                                               "eim_json": p.get("eimJson"), "tools_path": p.get("toolsPath")})
            live = await asyncio.to_thread(self.rt.select_idf, choice)
        except ValidationError as e:
            raise RpcError(-32602, str(e)) from e
        except RuntimeError as e:
            raise RpcError(-32029, str(e)) from e
        if live and self.rt.devices is None:  # 原来没有平台：设备层现在才能启动
            devices = await self.rt.start_devices()
            if devices:
                self._unlisten = devices.listen(self._on_device)
        return {"restartRequired": not live, **(await self.setup_status({}))}

    async def setup_dismiss(self, p: dict) -> dict:
        self.rt.ui.setup_dismissed = True
        self.rt.ui.save(self.rt.home)
        return {}

    # ------------------------------------------------------------------ 扩展：后台子 agent（2026-10-05）

    async def subagent_stop(self, p: dict) -> dict:
        s = self._session(p["sessionId"])
        ok = bool(s.background and s.background.stop(p["subagentId"], "stopped by the user"))
        return {"stopped": ok}

    # ------------------------------------------------------------------ 扩展：goal 模式（W7）

    async def goal_start(self, p: dict) -> dict:
        self._session(p["sessionId"])
        try:
            g = self.rt.start_goal(p["sessionId"], p.get("objective", ""), max_rounds=int(p.get("maxRounds", 5)))
        except (RuntimeError, ValueError) as e:
            raise RpcError(-32028, str(e)) from e
        return {"goal": g.state.model_dump(mode="json")}

    async def goal_stop(self, p: dict) -> dict:
        return {"stopped": self.rt.stop_goal(p["sessionId"])}

    async def goal_get(self, p: dict) -> dict:
        g = self.rt.goals.get(p["sessionId"])
        if g is not None:
            return {"goal": g.state.model_dump(mode="json"), "active": self.rt.goal_active(p["sessionId"]) is not None}
        s = self._session(p["sessionId"])
        latest = sorted((s.store.root / "goals").glob("*/goal.json")) if s.store else []
        if latest:
            return {"goal": json.loads(latest[-1].read_text("utf-8")), "active": False}
        return {"goal": None, "active": False}

    # ------------------------------------------------------------------ 扩展：上下文（W6）

    async def session_compact(self, p: dict) -> dict:
        s = self._session(p["sessionId"])
        try:
            return await s.compact(p.get("instructions") or "")
        except RuntimeError as e:
            raise RpcError(-32027, str(e)) from e

    async def context_info(self, p: dict) -> dict:
        """界面上的"上下文"面板：用量、skill、记忆、MCP。"""
        s = self._session(p["sessionId"])
        tools = s.registry.specs()
        skill = cast("SkillTool | None", s.registry.get("skill"))
        memory = s.registry.get("memory_search")
        mem_store = getattr(memory, "store", None)
        return {
            "used": s.usage.current(s.history, s.system_prompt, tools),
            "window": s.backend.caps.context_window,
            "messages": len(s.history),
            "tools": len(tools),
            "features": self.rt.config.features.model_dump(),
            "skills": [{"name": x.name, "description": x.description, "scope": x.scope, "path": x.path,
                        "loaded": x.name in skill.catalog.loaded}
                       for x in skill.catalog.skills.values()] if skill else [],
            "memory": {"root": str(mem_store.root), "index": mem_store.index_text()} if mem_store else None,
            "mcp": self.rt.mcp.status(),
            "compactionDir": str(s.compaction_dir) if s.compaction_dir else None,
        }

    # ------------------------------------------------------------------ 扩展：工作区（W5）

    async def project_inspect(self, p: dict) -> dict:
        """新建会话页选好文件夹后立刻调用：是不是工程、git 状态、初始化会提交多少文件（2026-10-05）。"""
        cwd = Path(p["cwd"])
        if not cwd.is_dir():
            return {"exists": False}
        info = await worktree.inspect(cwd)
        root = Path(info.repo_root or cwd)
        res: dict[str, Any] = {"exists": True, "repo": info.model_dump(), "hasGitignore": (root / ".gitignore").exists(),
                               "worktreeEnabled": self.rt.config.worktree.enabled,
                               "worktreeRoot": str(self.rt.config.worktree.root), "project": None, "candidates": []}
        if self.rt.platform:
            proj = self.rt.platform.detect(cwd)
            res["project"] = proj.model_dump() if proj else None
            if proj is None:
                res["candidates"] = await asyncio.to_thread(self._find_projects, cwd)
        if not info.is_git or info.head is None:
            res["scan"] = await asyncio.to_thread(worktree.scan_folder, root)
        res["spaces"] = self._space_check(cwd, info.subdir)
        return res

    def _space_check(self, project: Path, subdir: str) -> dict:
        """ESP-IDF 不支持路径里有空格（esp-sr 等组件把 "-L <路径>" 拼成一个参数，链接器在空格处拆开）。
        真机实测（2026-10-05）：工程放在 "firmwright 1005" 下面，编译到链接阶段才失败，agent 还想用 subst 绕过。
        分别看在副本里编译的路径（worktree 根 + 仓库内的子目录）和原地工作时的路径，返回第一个带空格的文件夹名。"""
        def first_space(parts) -> str | None:
            return next((x for x in parts if " " in x), None)

        wt = first_space([*Path(self.rt.config.worktree.root).parts, *subdir.split("/")])
        here = first_space(project.resolve().parts)
        return {"worktree": wt, "inPlace": here}

    def _find_projects(self, folder: Path, depth: int = 2) -> list[str]:
        """文件夹本身不是工程时，在两层以内的子目录里找工程（真机实测：用户选了工程的上一级）。"""
        found: list[str] = []
        skip = {".git", "build", "managed_components", "node_modules", "components"}

        def walk(d: Path, level: int) -> None:
            if level > depth or len(found) >= 8:
                return
            try:
                subs = sorted(x for x in d.iterdir() if x.is_dir() and x.name not in skip and not x.name.startswith("."))
            except OSError:
                return
            for x in subs:
                if self.rt.platform and self.rt.platform.detect(x):
                    found.append(str(x))
                else:
                    walk(x, level + 1)

        walk(folder, 1)
        return found

    async def project_init_git(self, p: dict) -> dict:
        try:
            info = await self.rt.init_git(Path(p["cwd"]), new_project=bool(p.get("newProject")))
        except worktree.EmptyProject as e:
            raise RpcError(-32027, f"The folder has no files to commit ({e}). Copy your project in first, then create the session.") from e
        except Exception as e:
            raise RpcError(-32024, f"git init failed: {e}") from e
        return {"repo": info.model_dump()}

    async def project_branches(self, p: dict) -> dict:
        meta = self.rt.metas.get(p["sessionId"])
        if meta is None or not meta.repo_root:
            return {"branches": [], "default": None}
        return {"branches": await worktree.list_branches(Path(meta.repo_root)), "default": meta.target_branch}

    def _session(self, sid: str) -> Session:
        s = self.rt.sessions.get(sid)
        if s is None:
            raise RpcError(-32602, f"Session {sid} is not loaded")
        return s

    async def checkpoints_list(self, p: dict) -> dict:
        s = self._session(p["sessionId"])
        ck = s.checkpoints
        if ck is None:
            meta = self.rt.metas[s.id]
            why = ("The session has ended" if meta.state != "active" else
                   "The project is not a git repository, so there are no checkpoints" if not meta.repo_root
                   else "Checkpoints are turned off")
            return {"enabled": False, "reason": why, "entries": []}
        entries = []
        for e in ck.entries:
            d = e.model_dump(mode="json")
            fw = ck.firmware_at(e.seq)
            d["boardFirmware"] = fw.model_dump(mode="json") if fw else None  # 这个点上板子跑的固件
            entries.append(d)
        return {"enabled": self.rt.config.features.checkpoint_enabled, "entries": entries,
                "canReflash": bool(s.board_id and self.rt.devices)}

    async def checkpoints_restore(self, p: dict) -> dict:
        try:
            return await self.rt.restore_checkpoint(p["sessionId"], int(p["seq"]), reflash=bool(p.get("reflash")))
        except (RuntimeError, ValueError, KeyError) as e:
            raise RpcError(-32025, str(e)) from e

    async def checkpoints_preview(self, p: dict) -> dict:
        self._session(p["sessionId"])
        try:
            return await self.rt.preview_restore(p["sessionId"], int(p["seq"]))
        except (RuntimeError, ValueError, KeyError) as e:
            raise RpcError(-32025, str(e)) from e

    async def session_diff(self, p: dict) -> dict:
        self._session(p["sessionId"])
        try:
            return await self.rt.session_diff(p["sessionId"])
        except RuntimeError as e:
            raise RpcError(-32025, str(e)) from e

    async def session_export_patch(self, p: dict) -> dict:
        self._session(p["sessionId"])
        try:
            return await self.rt.export_patch(p["sessionId"], Path(p["path"]) if p.get("path") else None)
        except RuntimeError as e:
            raise RpcError(-32025, str(e)) from e

    async def session_merge(self, p: dict) -> dict:
        from ..workspace.finish import MergeError

        self._session(p["sessionId"])
        try:
            res = await self.rt.merge_session(p["sessionId"], target=p.get("targetBranch"), message=p.get("message"))
        except MergeError as e:
            raise RpcError(-32026, str(e), {"kind": e.kind, "conflicts": e.conflicts}) from e
        except (RuntimeError, KeyError) as e:
            raise RpcError(-32025, str(e)) from e
        return {**res, "_meta": {"fwr": self._meta_dict(p["sessionId"])}}

    async def session_apply(self, p: dict) -> dict:
        from ..workspace.finish import MergeError

        self._session(p["sessionId"])
        try:
            res = await self.rt.apply_session(p["sessionId"])
        except MergeError as e:
            raise RpcError(-32026, str(e), {"kind": e.kind, "conflicts": e.conflicts}) from e
        except (RuntimeError, KeyError) as e:
            raise RpcError(-32025, str(e)) from e
        return {**res, "_meta": {"fwr": self._meta_dict(p["sessionId"])}}

    async def session_interrupt(self, p: dict) -> dict:
        """停掉正在执行的那一步（轮次继续；插话在下一步送达）。界面上的"立即送达"和工具行上的"停止"。"""
        return {"stopped": self._session(p["sessionId"]).interrupt_tools()}

    async def session_discard(self, p: dict) -> dict:
        self._session(p["sessionId"])
        try:
            res = await self.rt.discard_session(p["sessionId"])
        except (RuntimeError, KeyError) as e:
            raise RpcError(-32025, str(e)) from e
        return {**res, "_meta": {"fwr": self._meta_dict(p["sessionId"])}}

    # ------------------------------------------------------------------ 标准 ACP

    async def initialize(self, p: dict) -> dict:
        self.rt.start_mcp()
        if self.rt.devices is None and self.rt.platform is not None:
            devices = await self.rt.start_devices()
            if devices:
                self._unlisten = devices.listen(self._on_device)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": True, "audio": False, "embeddedContext": False},
            },
            "authMethods": [],
            "_meta": {"fwr": {"version": __version__, "platform": self.rt.platform.id if self.rt.platform else None,
                              "home": str(self.rt.home), "sim": self.rt.sim is not None,
                              "idf": self.rt.idf_status(), "setupDismissed": bool(self.rt.ui.setup_dismissed)}},
        }

    def _callbacks(self, sid_ref: list[str]):
        async def emit(update: dict) -> None:
            await self.notify("session/update", {"sessionId": sid_ref[0], "update": update})

        async def ask_permission(req: PermissionRequest) -> PermissionReply:
            options = [
                {"optionId": "allow_once", "name": "Allow once", "kind": "allow_once"},
                {"optionId": "allow_always", "name": "Allow for this session", "kind": "allow_always"},
                {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
            ]
            if req.risk == "dangerous":  # 危险操作不提供"本会话都允许"
                options = [o for o in options if o["optionId"] != "allow_always"]
            res = await self.request("session/request_permission", {
                "sessionId": sid_ref[0],
                "toolCall": {"toolCallId": req.tool_call_id, "title": req.title, "rawInput": req.arguments,
                             "status": "pending"},
                "options": options,
                "_meta": {"fwr": {"reason": req.reason, "risk": req.risk, "subjects": req.subjects, "tool": req.tool}},
            })
            outcome = (res or {}).get("outcome") or {}
            # 界面回的 optionId 只认这三个，别的一律当拒绝（原来原样返回）
            opt = outcome.get("optionId") if outcome.get("outcome") == "selected" else None
            return opt if opt in ("allow_once", "allow_always") else "reject"

        async def ask_human(action: HumanAction) -> HumanReply:
            res = await self.request("_fwr/human/request", {"sessionId": sid_ref[0], **action.model_dump()})
            return HumanReply.model_validate(res or {"done": False})

        return emit, ask_permission, ask_human

    async def session_new(self, p: dict) -> dict:
        """§5.2 新建会话：是 git 仓库就建 worktree（I09）；不是仓库时返回 -32020 / 没有提交时 -32021，
        界面问用户要不要初始化 git，或者选择直接在工程目录里工作（_meta.fwr.isolation = "in_place"）。"""
        cwd = Path(p.get("cwd") or "")
        if not cwd.is_dir():
            raise RpcError(-32602, f"Project folder does not exist: {cwd}")
        meta = (p.get("_meta") or {}).get("fwr", {})
        try:
            self.rt.backend_for(meta.get("modelId"))  # 先检查模型配置，免得建了 worktree 才发现模型不对
        except ValueError as e:
            raise RpcError(-32602, str(e)) from e
        await self.rt.mcp.ready(timeout=15)  # MCP 服务器还在连接时稍等，免得新会话缺工具
        sid = self.rt.new_session_id()
        try:
            workspace, repo = await self.rt.prepare_workspace(cwd, sid, isolation=meta.get("isolation"),
                                                              carry_dirty=meta.get("carryDirty", True))
        except worktree.NotGitRepo as e:
            raise RpcError(-32020, "This folder is not a git repository yet", {"reason": "not_git", "repo": (
                await worktree.inspect(cwd)).model_dump()}) from e
        except worktree.NoCommits as e:
            raise RpcError(-32021, "The git repository has no commits yet", {"reason": "no_commits", "repo": (
                await worktree.inspect(cwd)).model_dump()}) from e
        except Exception as e:
            raise RpcError(-32022, f"Creating the worktree failed: {e}") from e
        sid_ref = [sid]
        emit, ask_perm, ask_human = self._callbacks(sid_ref)
        try:
            s = self.rt.create_session(cwd, model_id=meta.get("modelId"), board_id=meta.get("boardId"),
                                       mode=meta.get("mode"), title=meta.get("title", ""), emit=emit,
                                       ask_permission=ask_perm, ask_human=ask_human, session_id=sid,
                                       workspace=workspace, repo=repo)
        except ValueError as e:
            raise RpcError(-32602, str(e)) from e
        if (eff := meta.get("effort")) and eff != "default" and eff in effort_options(
                self.rt.config.models[self.rt.metas[s.id].model_id]):
            s.effort = self.rt.metas[s.id].effort = eff
            if s.store:
                s.store.save_meta(self.rt.metas[s.id])
        if s.checkpoints and self.rt.config.features.checkpoint_enabled:
            try:
                await s.checkpoints.ensure_base()
            except Exception as e:
                log.warning("Recording the base checkpoint failed: %s", e)
        if workspace is not None:
            if workspace.seeded:
                s.trace.record("worktree_seeded", files=workspace.seeded)
            if workspace.carried:
                s.trace.record("worktree_carried", files=workspace.carried[:200], count=len(workspace.carried))
            if not any(x.name not in (".gitignore", ".git", ".firmwright") for x in Path(workspace.cwd).iterdir()):
                # 空文件夹里新建工程（2026-10-05）：告诉 agent 这是新工程，在工作目录里建，不要去别处找
                s.inject(ReminderBlock(source="new_project", text=(
                    "The working directory is empty: the user started a new project here. Create it in the working "
                    "directory itself (for example `idf.py create-project` or by copying an example from the ESP-IDF "
                    "examples folder, then set_target), build and flash it there. Do not look for an existing project "
                    "elsewhere. When the session finishes, the user applies the new project into their empty folder.")))
                s.trace.record("new_project")
            self.rt.start_prebuild(s.id)
        extra = {"dirty": repo.dirty, "dirtyCount": repo.dirty_count} if workspace is not None and repo.dirty else {}
        return {"sessionId": s.id, "modes": self._modes(s), "_meta": {"fwr": {**self._meta_dict(s.id), **extra}}}

    def _modes(self, s: Session) -> dict:
        names = {"default": "Ask before edits", "accept_edits": "Auto-accept edits", "plan": "Plan (read-only)",
                 "always_approve": "Approve all (still asks for dangerous hardware ops and writes outside the working directory)"}
        return {"currentModeId": s.mode, "availableModes": [{"id": k, "name": v} for k, v in names.items()]}

    def _meta_dict(self, sid: str) -> dict:
        meta = self.rt.metas[sid]
        s = self.rt.sessions[sid]
        d = meta.model_dump()
        d["status"] = s.status
        d["boardId"] = s.board_id
        return d

    def _find_store(self, sid: str) -> SessionStore | None:
        for p in (self.rt.home / "sessions").glob(f"*/{sid}"):
            return SessionStore(p)
        return None

    async def session_load(self, p: dict) -> Any:
        sid = p["sessionId"]
        if sid not in self.rt.sessions:
            store = self._find_store(sid)
            meta = store.load_meta() if store else None
            if meta is None:
                raise RpcError(-32602, f"Session {sid} not found")
            sid_ref = [sid]
            emit, ask_perm, ask_human = self._callbacks(sid_ref)
            board = meta.board_id
            if board and self.rt.devices and board not in self.rt.devices.boards:
                board = None
            try:
                self.rt.create_session(Path(meta.project_root), model_id=meta.model_id, board_id=board,
                                       mode=meta.permission_mode, emit=emit, ask_permission=ask_perm,
                                       ask_human=ask_human, session_id=sid)
            except Exception as e:  # 板子被占用等：先不绑板子
                log.warning("Binding the board while restoring session %s failed: %s", sid, e)
                self.rt.create_session(Path(meta.project_root), model_id=meta.model_id, mode=meta.permission_mode,
                                       emit=emit, ask_permission=ask_perm, ask_human=ask_human, session_id=sid)
            if meta.board_id and self.rt.sessions[sid].board_id is None and meta.state == "active":
                # 板子暂时不在（掉线 / 正在崩溃重启）或被占用：等它连上、空出来再自动绑回来（2026-10-05 真机：
                # 重启桌面端时板子在崩溃循环里，会话就一直没绑板子，回退时"重烧"也灰着）
                self.rt.want_board(sid, meta.board_id)
        s = self.rt.sessions[sid]
        # 按 ui-events 回放界面（grok 的 session/load 重放）
        if s.store:
            # 合并流式片段后一次发完（_fwr/replay），界面一次折叠、只渲染一次；原来逐条发，1.3 万条要 26 秒
            updates = s.store.load_ui(compact=not s.running)
            last_status = next((u.get("status", "idle") for u in reversed(updates)
                                if u.get("sessionUpdate") == "_fwr/status"), "idle")
            await self.notify("_fwr/replay", {"sessionId": sid, "updates": updates})
            # 上次核心退出时还在跑的子 agent（后台的、或前台被打断的）：核心重启后它们已经不在了，卡片标成中断
            live = set(s.background.runs) if s.background else set()
            sub_status: dict[str, dict] = {}
            for u in updates:
                if u.get("sessionUpdate") == "_fwr/subagent":
                    sub_status[u["subagentId"]] = u
            for cid, u in sub_status.items():
                if u.get("status") == "running" and cid not in live and not s.running:
                    await s.emit({**u, "status": "done", "stop": "interrupted",
                                  "text": "Interrupted: the core process exited while this sub-agent was running."})
            # 上次是在任务进行中被打断的（核心崩溃 / 被杀）：补一个回合结束，界面上的转圈和"运行中"才能收尾
            if last_status != "idle" and not s.running:
                rec = s.store.recovered
                note = "The previous task was interrupted (the core process exited unexpectedly)" + (
                    f"; on recovery {rec.get('bad_lines', 0)} corrupted records were repaired and "
                    f"{rec.get('filled_results', 0)} unexecuted tool results were filled in"
                    if rec else "")
                s.trace.record("recovered", **(rec or {}), last_status=last_status)
                await s.emit({"sessionUpdate": "_fwr/turn_end", "stopReason": "interrupted", "usage": None, "error": note})
                await s.emit({"sessionUpdate": "_fwr/status", "status": "idle"})
        return {"modes": self._modes(s), "_meta": {"fwr": self._meta_dict(sid)}}

    async def session_prompt(self, p: dict) -> dict:
        sid = p["sessionId"]
        s = self.rt.sessions.get(sid)
        if s is None:
            raise RpcError(-32602, f"Session {sid} is not loaded")
        if s.readonly:
            raise RpcError(-32023, s.readonly)
        texts, images = [], []
        if self.rt.goal_active(sid):
            # 目标进行中：用户的话作为插话交给执行者（正在跑就注入下一步，两轮之间就在下一轮开头看到）
            text = "\n".join(b.get("text", "") for b in p.get("prompt") or [] if b.get("type") == "text")
            s.inject(ReminderBlock(source="interjection", text="User note (goal in progress): " + text))
            await s.emit({"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": text}})
            return {"stopReason": "end_turn", "_meta": {"fwr": {"interjected": True, "goal": True}}}
        for block in p.get("prompt") or []:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "image":
                images.append(ImageBlock(media_type=block.get("mimeType", "image/png"), data=block.get("data", ""),
                                         alt=block.get("uri", "image pasted by the user")))
        if s.running:
            # 正在跑：作为插话注入下一步（grok interjection）。_meta.fwr.now = true：停掉当前这一步，马上送达
            s.inject(ReminderBlock(source="interjection", text="User note: " + "\n".join(texts)))
            stopped = s.interrupt_tools() if (p.get("_meta") or {}).get("fwr", {}).get("now") else []
            return {"stopReason": "end_turn", "_meta": {"fwr": {"interjected": True, "stopped": stopped}}}
        task = asyncio.create_task(s.prompt("\n".join(texts), images))
        self._turns[sid] = task
        try:
            r = await task
        finally:
            self._turns.pop(sid, None)
        stop = {"end_turn": "end_turn", "cancelled": "cancelled", "max_steps": "max_turn_requests"}.get(
            r.stop_reason, "end_turn")
        return {"stopReason": stop, "_meta": {"fwr": {"stop": r.stop_reason, "usage": r.usage, "error": r.error,
                                                      "steps": r.steps}}}

    async def session_cancel(self, p: dict) -> None:
        s = self.rt.sessions.get(p.get("sessionId", ""))
        if s:
            s.cancel("cancelled by the user")
            # 等待中的权限 / 人工请求也一并取消
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_result({"outcome": {"outcome": "cancelled"}, "done": False, "note": "cancelled"})

    async def session_set_mode(self, p: dict) -> None:
        s = self.rt.sessions[p["sessionId"]]
        mode = p["modeId"]
        if mode not in ("default", "accept_edits", "plan", "always_approve"):
            raise RpcError(-32602, f"Unknown mode {mode}")
        s.mode = mode
        meta = self.rt.metas[s.id]
        meta.permission_mode = mode
        if s.store:
            s.store.save_meta(meta)
        await s.emit({"sessionUpdate": "current_mode_update", "currentModeId": mode})

    # ------------------------------------------------------------------ 扩展：会话 / 模型

    async def sessions_list(self, p: dict) -> dict:
        out = []
        root = self.rt.home / "sessions"
        for meta_path in root.glob("*/*/meta.json") if root.exists() else []:
            try:
                meta = SessionMeta.model_validate_json(meta_path.read_text("utf-8"))
            except Exception:
                continue
            d = meta.model_dump()
            s = self.rt.sessions.get(meta.id)
            d["loaded"] = s is not None
            d["status"] = s.status if s else "idle"
            d["mtime"] = meta_path.parent.joinpath("ui-events.jsonl").stat().st_mtime if meta_path.parent.joinpath(
                "ui-events.jsonl").exists() else meta_path.stat().st_mtime
            out.append(d)
        out.sort(key=lambda d: d["mtime"], reverse=True)
        return {"sessions": out}

    async def models_list(self, p: dict) -> dict:
        return {"default": self.rt.config.defaults.model,
                "models": [{"id": k, "model": v.model or k, "vision": v.vision, "contextWindow": v.context_window,
                            "efforts": effort_options(v)}
                           for k, v in self.rt.config.models.items()]}

    # ---- 设置页的模型管理（2026-10-05）：密钥只进不出，界面只知道"有没有"

    async def models_detail(self, p: dict) -> dict:
        return {"default": self.rt.config.defaults.model, "models": self.rt.list_models(),
                "configPath": str(self.rt.home / "config.toml")}

    async def models_save(self, p: dict) -> dict:
        try:
            return {"model": self.rt.save_model(p.get("id", ""), p.get("fields") or {}, p.get("apiKey") or None)}
        except ValidationError as e:  # 先于 ValueError：pydantic 的 ValidationError 是它的子类
            raise RpcError(-32602, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e
        except ValueError as e:
            raise RpcError(-32602, str(e)) from e

    async def models_delete(self, p: dict) -> dict:
        try:
            self.rt.delete_model(p["id"])
        except (ValueError, KeyError) as e:
            raise RpcError(-32602, str(e)) from e
        return {}

    async def models_test(self, p: dict) -> dict:
        """测试连接：已保存的模型按 id；表单里还没保存的，用表单内容 + 临时密钥（只在这次请求里用）。"""
        from ..config import ModelConfig
        from ..ui_settings import test_model

        mid = p.get("id") or "draft"
        api_key = (p.get("apiKey") or "").strip() or None
        if p.get("fields"):
            fields = {k: v for k, v in p["fields"].items() if v not in (None, "")}
            if api_key:
                fields["key_ref"] = "inline"  # 占位：密钥直接传给后端，不落地、不进环境变量
            elif mid in self.rt.config.models:
                fields["key_ref"] = self.rt.config.models[mid].key_ref
            else:
                raise RpcError(-32602, "Enter an API key to test")
            try:
                cfg = ModelConfig.model_validate(fields)
            except ValidationError as e:
                raise RpcError(-32602, str(e)) from e
        elif mid in self.rt.config.models:
            cfg = self.rt.config.models[mid]
        else:
            raise RpcError(-32602, f"No model {mid}")
        return await test_model(cfg, mid, api_key=api_key)

    async def session_set_model(self, p: dict) -> None:
        s = self.rt.sessions[p["sessionId"]]
        backend, api_model, mid = self.rt.backend_for(p["modelId"])
        s.backend, s.model = backend, api_model
        meta = self.rt.metas[s.id]
        meta.model_id = mid
        # 换模型后，原来的思考程度新模型不支持就回到默认
        if meta.effort and meta.effort not in effort_options(self.rt.config.models[mid]):
            meta.effort = s.effort = None
        if s.store:
            s.store.save_meta(meta)

    async def session_set_effort(self, p: dict) -> dict:
        """思考程度（2026-10-05）：None / "default" = 用服务商默认，不发参数。"""
        s = self._session(p["sessionId"])
        meta = self.rt.metas[s.id]
        effort = p.get("effort") or None
        if effort == "default":
            effort = None
        if effort and effort not in effort_options(self.rt.config.models[meta.model_id]):
            raise RpcError(-32602, f"Model {meta.model_id} does not support thinking effort {effort!r}")
        s.effort = meta.effort = effort
        if s.store:
            s.store.save_meta(meta)
        return {"effort": effort}

    async def session_close(self, p: dict) -> None:
        self.rt.close_session(p["sessionId"])

    async def session_bind_board(self, p: dict) -> dict:
        from ..device.manager import BoardBusy

        try:
            self.rt.bind_board(p["sessionId"], p.get("boardId"), take=bool(p.get("take")))
        except BoardBusy as e:
            raise RpcError(-32010, str(e)) from e
        return {"_meta": {"fwr": self._meta_dict(p["sessionId"])}}

    # ------------------------------------------------------------------ 扩展：设备

    def _boards(self) -> list[dict]:
        if not self.rt.devices:
            return []
        return [b.model_dump() for b in self.rt.devices.boards.values()]

    async def boards_list(self, p: dict) -> dict:
        return {"boards": self._boards(), "available": self.rt.devices is not None}

    async def boards_rescan(self, p: dict) -> dict:
        """设备栏的"重新扫描"（真机实测第 7 条：识别失败时用户没有补救手段）。"""
        if self.rt.devices:
            await self.rt.devices.scan()
        return {"boards": self._boards()}

    async def boards_identify(self, p: dict) -> dict:
        """设备栏的"识别芯片"：esptool flash_id（会复位板子）。板子的会话正在执行时不做。"""
        from ..device.manager import BoardBusy

        devices, pa = self.rt.devices, self.rt.platform
        if not devices or not pa or not hasattr(pa, "chip_info"):
            raise RpcError(-32010, "Device support is not available")
        b = devices.boards.get(p["boardId"])
        if b is None or b.state == "disconnected" or not b.port:
            raise RpcError(-32010, "The board is not connected")
        if b.owner_session and (why := self.rt._busy_reason(b.owner_session)):
            raise RpcError(-32010, str(BoardBusy(f"{devices.owner_text(b)} {why}; try again when it is idle")))
        ctx = ToolContext(session_id="", cwd=self.rt.home, cancel=CancelToken(), trace=Trace(None, ""))
        async with devices.exclusive(b.id, state="busy") as bb:
            info = await pa.chip_info(ctx, bb.port or "")
        if not info.get("ok"):
            raise RpcError(-32010, f"esptool could not read the chip: {info.get('summary')}")
        if info.get("chip"):
            devices.set_chip(devices.boards[b.id], info["chip"])
        if info.get("mac"):  # 没有可靠序列号的 UART 桥板子：按 MAC 认回以前记下的同一块板子
            devices.set_mac(devices.boards[b.id], info["mac"])
        return {"info": info}

    async def boards_reset(self, p: dict) -> dict:
        """串口面板空着时的"复位板子"（2026-10-06）：已经在运行的固件不会再打印启动日志，复位一次就能看到。
        和 Identify 一样：板子的会话正在执行时不做（会打断 agent 正在等的输出）。"""
        from ..device.manager import BoardBusy

        devices = self.rt.devices
        if not devices:
            raise RpcError(-32010, "Device support is not available")
        b = devices.boards.get(p["boardId"])
        if b is None or b.state == "disconnected" or not b.port:
            raise RpcError(-32010, "The board is not connected")
        if b.owner_session and (why := self.rt._busy_reason(b.owner_session)):
            raise RpcError(-32010, str(BoardBusy(f"{devices.owner_text(b)} {why}; try again when it is idle")))
        try:
            await devices.reset(b.id)
        except Exception as e:
            raise RpcError(-32010, f"Reset failed: {type(e).__name__}: {e}") from e
        return {"ok": True}

    async def boards_update(self, p: dict) -> dict:
        if not self.rt.devices:
            raise RpcError(-32000, "The device manager is not running")
        fields = {k: v for k, v in p.items() if k in ("alias", "chip", "idle_policy")}
        b = self.rt.devices.update_board(p["boardId"], **fields)
        return {"board": b.model_dump()}

    async def serial_tail(self, p: dict) -> dict:
        if not self.rt.devices:
            return {"text": ""}
        # 还没有日志文件：给界面空串（界面显示空状态和"Reset board"）；read_log 的占位文字是给 agent 看的
        if not self.rt.devices._log_path(p["boardId"]).is_file():
            return {"text": ""}
        return {"text": self.rt.devices.read_log(p["boardId"], tail=int(p.get("lines", 300)))}

    async def events_recent(self, p: dict) -> dict:
        if not self.rt.devices:
            return {"events": []}
        evs = list(self.rt.devices.recent.get(p["boardId"], ()))[-int(p.get("limit", 50)):]
        return {"events": [e.model_dump(mode="json") for e in evs]}

    async def firmware_size(self, p: dict) -> dict:
        if not self.rt.platform:
            return {"size": None}
        sid = p["sessionId"]
        s = self.rt.sessions.get(sid)
        if s is not None:
            cwd, trace = s.cwd, s.trace
        else:  # 核心刚重启、会话还没 load 完：从磁盘上的 meta 找工程目录
            store = self._find_store(sid)
            meta = store.load_meta() if store else None
            if meta is None:
                raise RpcError(-32602, f"Session {sid} not found")
            cwd, trace = Path(meta.cwd), Trace(None, sid)
        ctx = ToolContext(session_id=sid, cwd=cwd, cancel=CancelToken(), trace=trace)
        rep = await self.rt.platform.size(ctx)
        return {"size": rep.model_dump() if rep else None, "flashed": self._flashed_size(s) if s else None}

    @staticmethod
    def _flashed_size(s: Session) -> dict | None:
        """板子上那份固件（本会话最近一次烧录）的 app 大小：从烧录时的存档里读，不重新编译、不跑 idf.py size。
        界面用它显示"比上次烧录大了多少"（2026-10-06，界面改进第 12 项）。"""
        ck = s.checkpoints
        fw = s._turn_firmware or (ck.firmware_at(len(ck.entries) - 1) if ck and ck.entries else None)
        if fw is None or not fw.archive:
            return None
        arch = Path(fw.archive)
        try:
            app = json.loads((arch / "flasher_args.json").read_text("utf-8")).get("app", {}).get("file")
            size = (arch / app).stat().st_size if app else None
        except (OSError, ValueError, AttributeError):
            return None
        if size is None:
            return None
        return {"appBinSize": size, "turn": fw.turn, "at": fw.at, "source": fw.source}

    async def settings_get(self, p: dict) -> dict:
        cfg, rt = self.rt.config, self.rt
        running = {n: s for s in rt.mcp.status() for n in [s["name"]]}
        mcp = []
        for name, srv in cfg.mcp.servers.items():
            mcp.append({"name": name, "source": "config.toml" if name in rt.toml_mcp else "settings",
                        "command": srv.command, "args": srv.args, "env": srv.env, "cwd": srv.cwd, "enabled": srv.enabled,
                        "status": running.get(name)})
        for name, srv in rt.ui.mcp_servers.items():  # 界面加的、还没重启所以没在用的
            if name not in cfg.mcp.servers:
                mcp.append({"name": name, "source": "settings", "command": srv.command, "args": srv.args, "env": srv.env,
                            "cwd": srv.cwd, "enabled": srv.enabled, "status": None, "pending": True})
        return {"idlePolicy": cfg.defaults.idle_policy,
                "permissionMode": cfg.defaults.permission_mode,
                "defaultModel": cfg.defaults.model,
                "configPath": str(rt.home / "config.toml"),
                "buildJobs": cfg.build.jobs, "buildJobsAuto": cfg.build.resolved_jobs() if cfg.build.jobs == 0 else None,
                "cpuCount": os.cpu_count() or 1,
                "worktreeRoot": str(cfg.worktree.root),
                "rules": {k: {"config": rt.toml_rules[k], "settings": getattr(rt.ui.rules, k)} for k in ("allow", "ask", "deny")},
                "mcp": mcp,
                "idf": rt.idf_status()}

    async def core_about(self, p: dict) -> dict:
        rt = self.rt
        return {"version": __version__, "python": sys.version.split()[0], "home": str(rt.home),
                "paths": {"data": str(rt.home), "sessions": str(rt.home / "sessions"), "memory": str(rt.home / "memory"),
                          "config": str(rt.home / "config.toml"), "settings": str(rt.home / "settings.json"),
                          "worktrees": str(rt.config.worktree.root)},
                "idf": rt.idf_status()}

    async def core_restart(self, p: dict) -> dict:
        """设置页的"重启核心"（换 ESP-IDF、改 MCP 服务器后）。核心退出码 75，桌面端的自动重启把它拉起来，
        界面按"核心重启过"重新加载会话。有会话在执行时拒绝（会打断它）。"""
        busy = self.rt.busy_sessions()
        if busy and not p.get("force"):
            raise RpcError(-32030, f"Sessions are still working: {', '.join(busy)}. Wait for them or stop them first.")
        if self.request_exit is None:
            raise RpcError(-32030, "Restart is not available in this mode")
        asyncio.get_running_loop().call_later(0.2, self.request_exit)
        return {"restarting": True}

    # ---- 模拟板（FIRMWRIGHT_SIM_BOARD=1，开发 / 演示用）

    async def sim_crash(self, p: dict) -> dict:
        if not self.rt.sim:
            raise RpcError(-32000, "The simulated board is not enabled (FIRMWRIGHT_SIM_BOARD=1)")
        self.rt.sim.board.crash_now()
        return {}

    async def sim_plug(self, p: dict) -> dict:
        if not self.rt.sim:
            raise RpcError(-32000, "The simulated board is not enabled (FIRMWRIGHT_SIM_BOARD=1)")
        self.rt.sim.board.present = bool(p.get("present", True))
        return {"present": self.rt.sim.board.present}

    async def settings_set(self, p: dict) -> dict:
        # 写进 settings.json（config.toml 由用户手写，程序不改它，避免覆盖注释）
        try:
            self.rt.set_defaults(model=p.get("defaultModel"), idle_policy=p.get("idlePolicy"))
            self.rt.update_general(build_jobs=p.get("buildJobs"), worktree_root=p.get("worktreeRoot"),
                                   permission_mode=p.get("permissionMode"))
            if isinstance(p.get("rules"), dict):
                r = p["rules"]
                self.rt.set_rules(list(r.get("allow") or []), list(r.get("ask") or []), list(r.get("deny") or []))
        except (KeyError, ValueError, OSError) as e:
            raise RpcError(-32602, str(e.args[0]) if isinstance(e, KeyError) else str(e)) from e
        return await self.settings_get({})

    async def mcp_save(self, p: dict) -> dict:
        try:
            self.rt.save_mcp(p.get("name", ""), p.get("server") or {})
        except ValidationError as e:
            raise RpcError(-32602, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())) from e
        except ValueError as e:
            raise RpcError(-32602, str(e)) from e
        return await self.settings_get({})

    async def mcp_delete(self, p: dict) -> dict:
        try:
            self.rt.delete_mcp(p.get("name", ""))
        except (KeyError, ValueError) as e:
            raise RpcError(-32602, str(e.args[0]) if isinstance(e, KeyError) else str(e)) from e
        return await self.settings_get({})

    # ------------------------------------------------------------------ 设备事件 → 界面

    def _on_device(self, kind: str, payload: Any) -> None:
        loop = asyncio.get_running_loop()
        if kind == "state":
            loop.create_task(self.notify("_fwr/boards/changed", {"board": payload.model_dump()}))
        elif kind == "event":
            ev: DeviceEvent = payload
            route = self.rt.router.log[-1][1].action if self.rt.router and self.rt.router.log and \
                self.rt.router.log[-1][0] == ev.id else None
            loop.create_task(self.notify("_fwr/event", {"event": ev.model_dump(mode="json"), "route": route}))
        elif kind == "serial":
            loop.create_task(self.notify("_fwr/serial/chunk", {"boardId": payload["board_id"], "text": payload["text"]}))
        elif kind == "error":
            loop.create_task(self.notify("_fwr/device_error", payload))

    def _on_idle_notify(self, ev: DeviceEvent) -> None:
        """空闲时的崩溃通知（I04）：先解码调用栈，再推给界面；界面负责系统通知和"让 agent 处理"按钮。"""

        async def go() -> None:
            decoded = ev
            board = self.rt.devices.boards.get(ev.board_id) if self.rt.devices else None
            owner = board.owner_session if board else None
            s = self.rt.sessions.get(owner) if owner else None
            if s and self.rt.platform and ev.backtrace:
                info = self.rt.platform.detect(s.cwd)
                if info and info.elf:
                    try:
                        bt = await self.rt.platform.unwind(ev, Path(info.elf), s.cwd)
                        decoded = ev.model_copy(update={"backtrace": bt})
                        if self.rt.devices:
                            self.rt.devices.events[ev.id] = decoded
                    except Exception:
                        pass
            await self.notify("_fwr/notify", {"event": decoded.model_dump(mode="json"), "sessionId": owner})

        asyncio.get_running_loop().create_task(go())


# ---------------------------------------------------------------------- stdio 入口

RESTART_EXIT_CODE = 75  # 设置页"重启核心"：桌面端看到这个退出码就知道是有意重启（sidecar 照常自动拉起）
_exit_code = 0


async def serve_stdio(runtime: Runtime | None = None) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    out = sys.stdout.buffer
    write_lock = asyncio.Lock()

    async def send(msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        async with write_lock:
            out.write(data)
            out.flush()

    def reader() -> None:  # Windows 上 asyncio 读 stdin 管道不可靠，用线程读
        for line in sys.stdin.buffer:
            loop.call_soon_threadsafe(queue.put_nowait, line)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=reader, daemon=True, name="acp-stdin").start()
    rt = runtime or Runtime()
    server = AcpServer(rt, send)

    def request_exit() -> None:
        global _exit_code
        _exit_code = RESTART_EXIT_CODE
        queue.put_nowait(None)

    server.request_exit = request_exit
    try:
        while True:
            line = await queue.get()
            if line is None:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                await send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON parse error"}})
                continue
            await server.handle(msg)
        await server.drain()
    finally:
        await rt.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(serve_stdio())
    if _exit_code:
        sys.exit(_exit_code)


if __name__ == "__main__":
    main()
