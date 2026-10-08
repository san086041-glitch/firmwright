import asyncio
from pathlib import Path

from pydantic import BaseModel

from firmwright.config import PermissionConfig
from firmwright.model.fake import ScriptedBackend, call, calls, say
from firmwright.model.types import ReminderBlock, ToolResultBlock
from firmwright.permissions.engine import PermissionEngine
from firmwright.session.agent import Session
from firmwright.session.store import SessionStore
from firmwright.tools.base import Tool, ToolCaps, ToolRegistry, ToolResult
from firmwright.tools.fs import EditFile, Grep, ListDir, ReadFile, WriteFile


def make(tmp_path: Path, steps, **kw) -> tuple[Session, ScriptedBackend, list[dict]]:
    backend = ScriptedBackend(steps)
    updates: list[dict] = []

    async def emit(u):
        updates.append(u)

    reg = kw.pop("registry", None) or ToolRegistry([ReadFile(), WriteFile(), EditFile(), ListDir(), Grep()])
    s = Session(id="s1", cwd=tmp_path, backend=backend, model="fake", registry=reg,
                store=SessionStore(tmp_path / ".sess"), emit=emit, **kw)
    return s, backend, updates


async def test_plain_reply(tmp_path):
    s, _, updates = make(tmp_path, [say("你好")])
    r = await s.prompt("hi")
    assert r.stop_reason == "end_turn" and r.text == "你好"
    assert any(u["sessionUpdate"] == "agent_message_chunk" for u in updates)
    # 历史落盘，可以恢复
    assert len(SessionStore(tmp_path / ".sess").load_history()) == 2


async def test_read_then_edit_with_accept_edits(tmp_path):
    (tmp_path / "main.c").write_text("int x = 1;\n")
    s, backend, _ = make(tmp_path, [
        call("read_file", {"path": "main.c"}),
        call("edit_file", {"path": "main.c", "old_string": "x = 1", "new_string": "x = 2"}),
        say("改好了"),
    ], mode="accept_edits")
    r = await s.prompt("把 x 改成 2")
    assert r.stop_reason == "end_turn"
    assert (tmp_path / "main.c").read_text() == "int x = 2;\n"
    # 第二次请求里能看到 read_file 的结果（带行号）
    tool_msg = backend.requests[1].messages[-1]
    assert isinstance(tool_msg.content[0], ToolResultBlock)
    assert "int x = 1" in tool_msg.content[0].content[0].text


async def test_edit_needs_approval_in_default_mode(tmp_path):
    (tmp_path / "a.txt").write_text("old")
    asked = []

    async def ask(req):
        asked.append(req)
        return "reject"

    s, _, _ = make(tmp_path, [
        call("edit_file", {"path": "a.txt", "old_string": "old", "new_string": "new"}),
        say("好的"),
    ], ask_permission=ask)
    await s.prompt("改")
    assert asked and asked[0].tool == "edit_file"
    assert (tmp_path / "a.txt").read_text() == "old"


async def test_deny_rule_and_plan_mode(tmp_path):
    perms = PermissionEngine(PermissionConfig(deny=["Read(secret*)"]))
    (tmp_path / "secret.txt").write_text("k")
    s, backend, _ = make(tmp_path, [call("read_file", {"path": "secret.txt"}), say("ok")], permissions=perms)
    await s.prompt("读")
    res = backend.requests[1].messages[-1].content[0]
    assert res.is_error and "deny" in res.content[0].text

    s2, backend2, _ = make(tmp_path, [call("write_file", {"path": "b.txt", "content": "x"}), say("ok")],
                           mode="plan")
    await s2.prompt("写")
    assert not (tmp_path / "b.txt").exists()
    # 计划模式下只把只读工具给模型
    assert {t.name for t in backend2.requests[0].tools} == {"read_file", "list_dir", "grep"}


async def test_bad_args_and_unknown_tool(tmp_path):
    s, backend, _ = make(tmp_path, [
        calls([("read_file", {}), ("nope", {})]),
        say("ok"),
    ])
    await s.prompt("x")
    res = backend.requests[1].messages[-1].content
    assert res[0].is_error and "Invalid arguments" in res[0].content[0].text
    assert res[1].is_error and "No tool named" in res[1].content[0].text


async def test_loop_guard_warns_then_stops(tmp_path):
    (tmp_path / "f").write_text("x")
    s, backend, _ = make(tmp_path, [call("read_file", {"path": "f"}) for _ in range(10)])
    r = await s.prompt("loop")
    assert r.stop_reason == "loop_guard" and r.steps == 5
    # 第 4 次请求里带有提醒
    reminders = [b for m in backend.requests[3].messages for b in m.content if isinstance(b, ReminderBlock)]
    assert any(b.source == "loop_guard" for b in reminders)


class WaitArgs(BaseModel):
    seconds: float


class Wait(Tool):
    name = "wait"
    description = "等待"
    Args = WaitArgs
    caps = ToolCaps(read_only=True, interruptible=True)

    async def run(self, ctx, args):
        try:
            await asyncio.wait_for(ctx.cancel.wait(), args.seconds)
            return ToolResult.text(f"提前结束：{ctx.cancel.reason}")
        except TimeoutError:
            return ToolResult.text("等满了")


async def test_device_event_interrupts_waiting_tool(tmp_path):
    reg = ToolRegistry([Wait()])
    s, backend, _ = make(tmp_path, [call("wait", {"seconds": 30}), say("看到崩溃了")], registry=reg)

    async def crash_later():
        await asyncio.sleep(0.2)
        s.inject(ReminderBlock(source="device_event", text="panic: LoadProhibited"), interrupt=True)

    t = asyncio.create_task(crash_later())
    r = await asyncio.wait_for(s.prompt("等设备"), 5)
    await t
    assert r.stop_reason == "end_turn"
    second = backend.requests[1].messages
    assert "提前结束：a new device event arrived" in second[-2].content[0].content[0].text
    assert isinstance(second[-1].content[0], ReminderBlock) and "LoadProhibited" in second[-1].content[0].text
    assert any(row["kind"] == "interrupt" for row in s.trace.memory) or s.store


async def test_event_during_final_answer_keeps_turn_going(tmp_path):
    s, backend, _ = make(tmp_path, [])

    def first(req):
        s.inject(ReminderBlock(source="device_event", text="wdt reset"))
        return say("完成了")

    backend.steps = [first, say("设备复位了，我来看看")]
    r = await s.prompt("x")
    assert r.text == "设备复位了，我来看看" and r.steps == 2


async def test_cancel(tmp_path):
    reg = ToolRegistry([Wait()])
    s, _, _ = make(tmp_path, [call("wait", {"seconds": 30}), say("不该到这")], registry=reg)

    async def cancel_later():
        await asyncio.sleep(0.2)
        s.cancel()

    asyncio.create_task(cancel_later())
    r = await asyncio.wait_for(s.prompt("x"), 5)
    assert r.stop_reason == "cancelled"
