import asyncio
import json
import subprocess
import sys
from pathlib import Path

from firmwright.acp.server import AcpServer
from firmwright.config import Config, IdfConfig, WorktreeConfig
from firmwright.model.fake import ScriptedBackend, call, say
from firmwright.model.types import TextBlock
from firmwright.runtime import Runtime


class Client:
    """测试用的"界面"：收集核心发来的消息，自动回答权限请求。"""

    def __init__(self, tmp_path: Path, steps, *, approve="allow_once", worktree: WorktreeConfig | None = None):
        # 不加载 ESP-IDF；W5 之前的用例不建 worktree（工程目录不是 git 仓库）
        cfg = Config(idf=IdfConfig(eim_json=tmp_path / "none.json"),
                     worktree=worktree or WorktreeConfig(enabled=False))
        self.rt = Runtime(cfg, home=tmp_path / "home")
        self.backend = ScriptedBackend(steps)
        self.rt.backend_for = lambda mid: (self.backend, "fake", mid or "fake")
        self.inbox: list[dict] = []
        self.approve = approve
        self.server = AcpServer(self.rt, self._recv)
        self._id = 0
        self._waiters: dict[int, asyncio.Future] = {}

    async def _recv(self, msg: dict) -> None:
        msg = json.loads(json.dumps(msg, default=str))
        self.inbox.append(msg)
        if "method" in msg and "id" in msg:  # 核心发来的请求
            if msg["method"] == "session/request_permission":
                result = {"outcome": {"outcome": "selected", "optionId": self.approve}}
            else:
                result = {"done": True}
            asyncio.get_running_loop().create_task(self.server.handle({"jsonrpc": "2.0", "id": msg["id"],
                                                                       "result": result}))
        elif "id" in msg and msg["id"] in self._waiters:
            self._waiters.pop(msg["id"]).set_result(msg)

    async def call(self, method, params=None):
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._waiters[self._id] = fut
        await self.server.handle({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}})
        res = await asyncio.wait_for(fut, 10)
        if "error" in res:
            raise RuntimeError(res["error"])
        return res["result"]

    def updates(self, sid=None):
        return [m["params"]["update"] for m in self.inbox
                if m.get("method") == "session/update" and (sid is None or m["params"]["sessionId"] == sid)]


async def test_initialize_new_prompt_and_permission(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("old")
    c = Client(tmp_path, [call("edit_file", {"path": "a.txt", "old_string": "old", "new_string": "new"}), say("改好了")])
    init = await c.call("initialize", {"protocolVersion": 1})
    assert init["protocolVersion"] == 1 and init["agentCapabilities"]["loadSession"]
    new = await c.call("session/new", {"cwd": str(proj), "mcpServers": []})
    sid = new["sessionId"]
    assert new["modes"]["currentModeId"] == "default"
    res = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "改一下"}]})
    assert res["stopReason"] == "end_turn"
    assert (proj / "a.txt").read_text() == "new"
    perm = [m for m in c.inbox if m.get("method") == "session/request_permission"]
    assert perm and perm[0]["params"]["toolCall"]["title"].startswith("edit_file")
    kinds = [u["sessionUpdate"] for u in c.updates(sid)]
    assert kinds[0] == "_fwr/status" and "tool_call" in kinds and "agent_message_chunk" in kinds
    done = [u for u in c.updates(sid) if u["sessionUpdate"] == "tool_call_update" and u.get("status") == "completed"]
    assert done and "diff" in done[0]["rawOutput"]  # 界面要用 diff 渲染


async def test_reject_and_mode_and_list_and_load(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("old")
    c = Client(tmp_path, [call("edit_file", {"path": "a.txt", "old_string": "old", "new_string": "new"}), say("好")],
               approve="reject")
    await c.call("initialize", {})
    sid = (await c.call("session/new", {"cwd": str(proj)}))["sessionId"]
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "改"}]})
    assert (proj / "a.txt").read_text() == "old"
    await c.call("session/set_mode", {"sessionId": sid, "modeId": "accept_edits"})
    assert c.rt.sessions[sid].mode == "accept_edits"
    lst = await c.call("_fwr/sessions/list")
    assert lst["sessions"][0]["id"] == sid and lst["sessions"][0]["permission_mode"] == "accept_edits"

    # 新的"界面"（模拟核心重启）：session/load 回放 ui-events
    c2 = Client(tmp_path, [])
    await c2.call("initialize", {})
    res = await c2.call("session/load", {"sessionId": sid, "cwd": str(proj)})
    assert res["modes"]["currentModeId"] == "accept_edits"
    # 回放是一条 _fwr/replay，流式片段已合并
    batches = [m for m in c2.inbox if m.get("method") == "_fwr/replay"]
    assert len(batches) == 1
    replayed = batches[0]["params"]["updates"]
    assert any(u["sessionUpdate"] == "user_message_chunk" for u in replayed)
    kinds = [u["sessionUpdate"] for u in replayed]
    assert all(not (a == b and a in ("agent_message_chunk", "agent_thought_chunk")) for a, b in zip(kinds, kinds[1:], strict=False))
    assert len(c2.rt.sessions[sid].history) == len(c.rt.sessions[sid].history)


async def test_cancel_and_interjection(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    gate = asyncio.Event()

    def slow(req):
        return call("shell", {"command": "Start-Sleep -Seconds 20"})

    c = Client(tmp_path, [slow, say("不该到这")], approve="allow_once")
    await c.call("initialize", {})
    sid = (await c.call("session/new", {"cwd": str(proj)}))["sessionId"]
    task = asyncio.create_task(c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "x"}]}))
    for _ in range(100):
        if any(u.get("sessionUpdate") == "tool_call_update" and u.get("status") == "in_progress"
               for u in c.updates(sid)):
            break
        await asyncio.sleep(0.05)
    # 运行中再发一条 = 插话
    r = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "顺便看看日志"}]})
    assert r["_meta"]["fwr"]["interjected"]
    await c.server.handle({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}})
    res = await asyncio.wait_for(task, 15)
    assert res["stopReason"] == "cancelled"
    gate.set()


def test_stdio_roundtrip(tmp_path):
    """真的起一个子进程，走 stdin / stdout（Electron 就是这样启动核心的）。"""
    env = {"FIRMWRIGHT_HOME": str(tmp_path / "home"), "PYTHONIOENCODING": "utf-8", "SYSTEMROOT": "C:\\Windows"}
    proc = subprocess.run(
        [sys.executable, "-m", "firmwright.acp"],
        input=(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
               + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "_fwr/models/list", "params": {}}) + "\n").encode(),
        capture_output=True, timeout=60, env=env, cwd=Path(__file__).parents[1],
    )
    lines = [json.loads(x) for x in proc.stdout.decode("utf-8").splitlines() if x.strip()]
    ids = {m.get("id"): m for m in lines if "id" in m}
    assert ids[1]["result"]["protocolVersion"] == 1
    assert "models" in ids[2]["result"]


async def test_load_survives_killed_process(tmp_path):
    """核心在任务中途被杀：ui-events 末尾是 NUL，history 最后一条工具调用没有结果。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.txt").write_text("x")
    c = Client(tmp_path, [call("read_file", {"path": "a.txt"}), say("ok")])
    await c.call("initialize", {})
    sid = (await c.call("session/new", {"cwd": str(proj)}))["sessionId"]
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "读"}]})
    store = c.rt.sessions[sid].store
    # 模拟：最后一步 assistant 发了工具调用，还没写结果就被杀；文件尾部留下 NUL
    from firmwright.model.types import Message, ToolCallBlock

    store.append_message(Message(role="user", content=[TextBlock(text="再读一次")]))
    store.append_message(Message(role="assistant", content=[ToolCallBlock(id="cX", name="read_file",
                                                                            arguments={"path": "a.txt"})]))
    store.append_ui({"sessionUpdate": "_fwr/status", "status": "running"})
    with store.history_path.open("ab") as f:
        f.write(b"\x00" * 64)
    with store.ui_path.open("ab") as f:
        f.write(b'{"sessionUpdate": "agent_mes' + b"\x00" * 64)

    c2 = Client(tmp_path, [say("继续")])
    await c2.call("initialize", {})
    await c2.call("session/load", {"sessionId": sid, "cwd": str(proj)})
    s = c2.rt.sessions[sid]
    assert s.history[-1].role == "tool" and "session was interrupted" in s.history[-1].content[0].content[0].text
    ups = [u for u in c2.updates(sid)]
    assert any(u.get("stopReason") == "interrupted" for u in ups)
    assert ups[-1] == {"sessionUpdate": "_fwr/status", "status": "idle"}
    # 修好之后还能继续对话（协议上工具调用都有结果，不会 400）
    r = await c2.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "继续"}]})
    assert r["stopReason"] == "end_turn"


def test_coalesce_ui_merges_stream_chunks():
    from firmwright.session.store import coalesce_ui

    t = lambda s: {"type": "text", "text": s}  # noqa: E731
    rows = [
        {"sessionUpdate": "user_message_chunk", "content": t("hi")},
        *[{"sessionUpdate": "agent_thought_chunk", "content": t(c)} for c in "think"],
        *[{"sessionUpdate": "agent_message_chunk", "content": t(c)} for c in "ok"],
        {"sessionUpdate": "_fwr/prebuild", "status": "running", "text": "1"},
        {"sessionUpdate": "_fwr/prebuild", "status": "running", "text": "2"},
        {"sessionUpdate": "_fwr/prebuild", "status": "ok", "text": ""},
        {"sessionUpdate": "_fwr/prebuild", "status": "ok", "text": "again"},
        {"sessionUpdate": "tool_call", "toolCallId": "a"},
        {"sessionUpdate": "tool_call", "toolCallId": "b"},
    ]
    out = coalesce_ui(rows)
    assert [u["sessionUpdate"] for u in out] == ["user_message_chunk", "agent_thought_chunk", "agent_message_chunk",
                                                 "_fwr/prebuild", "_fwr/prebuild", "tool_call", "tool_call"]
    assert out[1]["content"]["text"] == "think" and out[2]["content"]["text"] == "ok"
    assert out[3]["status"] == "ok" and out[4]["text"] == "again"  # 完成的那条不会被后面的覆盖
