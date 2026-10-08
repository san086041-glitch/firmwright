"""2026-10-05 真机实测反馈：agent 跑一条整盘搜索的命令 10 分钟，用户发的话一直送不到。

- "立即送达 / 停止这一步"：停掉正在执行的工具（非 interruptible 的也行），轮次继续，模型看到用户停了它和用户的话
- 烧录不会被停掉
- 路径放在变量里的整盘搜索（$roots=@('D:\\','C:\\Users')）在默认模式下要询问
"""

import asyncio

from test_loop import Wait, make
from test_paths import setup, sh

from firmwright.model.fake import call, say
from firmwright.model.types import ReminderBlock, ToolCallBlock
from firmwright.permissions.engine import PermissionEngine
from firmwright.tools.base import CancelToken, Tool, ToolCaps, ToolRegistry, ToolResult
from firmwright.trace import read_trace


class SlowCmd(Wait):
    name = "slow"
    caps = ToolCaps(read_only=True)  # 不是 interruptible：设备事件打断不了它，但用户可以


class FakeFlash(Tool):
    name = "flash"
    description = "flash"
    Args = Wait.Args
    caps = ToolCaps()

    async def run(self, ctx, args):
        await asyncio.sleep(args.seconds)
        return ToolResult.text("flashed" if not ctx.cancel.cancelled else "cut")


async def test_user_can_stop_a_long_step_and_deliver_a_note(tmp_path):
    s, backend, _ = make(tmp_path, [call("slow", {"seconds": 30}), say("ok, only the project folder then")],
                         registry=ToolRegistry([SlowCmd()]))

    async def user_sends_now():
        await asyncio.sleep(0.2)
        s.inject(ReminderBlock(source="interjection", text="User note: only look at sdkconfig"))
        assert s.interrupt_tools() == ["slow"]

    t = asyncio.create_task(user_sends_now())
    r = await asyncio.wait_for(s.prompt("search everything"), 5)
    await t
    assert r.stop_reason == "end_turn" and r.text == "ok, only the project folder then"
    msgs = backend.requests[1].messages
    result = msgs[-2].content[0]
    assert result.is_error and "The user stopped this step" in "".join(b.text for b in result.content)
    assert "only look at sdkconfig" in msgs[-1].content[0].text


async def test_flash_is_never_interrupted(tmp_path):
    s, backend, _ = make(tmp_path, [call("flash", {"seconds": 0.5}), say("done")],
                         registry=ToolRegistry([FakeFlash()]), mode="always_approve")

    async def user_sends_now():
        await asyncio.sleep(0.1)
        assert s.interrupt_tools() == []

    t = asyncio.create_task(user_sends_now())
    await asyncio.wait_for(s.prompt("flash"), 5)
    await t
    assert "flashed" in backend.requests[1].messages[-1].content[0].content[0].text


async def test_stop_waits_for_flash_but_cancels_other_steps(tmp_path):
    """2026-10-08：用户点"停止"（取消整轮）也不杀烧录；同一步里别的工具照常停掉，轮次在这一步之后结束。"""
    s, backend, _ = make(tmp_path, [call("flash", {"seconds": 0.5}), say("never reached")],
                         registry=ToolRegistry([FakeFlash()]), mode="always_approve")

    async def user_stops():
        await asyncio.sleep(0.1)
        s.cancel("cancelled by the user")

    t = asyncio.create_task(user_stops())
    r = await asyncio.wait_for(s.prompt("flash"), 5)
    await t
    assert r.stop_reason == "cancelled" and len(backend.requests) == 1
    result = s.history[-1].content[0]
    assert "flashed" in result.content[0].text and not result.is_error
    assert any(x["kind"] == "cancel_deferred" for x in read_trace(s.store.trace_path))

    # 对照：不受保护的工具照常被停掉
    (tmp_path / "b").mkdir()
    s2, _, _ = make(tmp_path / "b",[call("slow", {"seconds": 30}), say("never reached")],
                    registry=ToolRegistry([SlowCmd()]))
    t = asyncio.create_task(user_stops_after(s2))
    r = await asyncio.wait_for(s2.prompt("search"), 5)
    await t
    assert r.stop_reason == "cancelled" and "this turn was cancelled" in s2.history[-1].content[0].content[0].text


async def user_stops_after(s, delay: float = 0.1):
    await asyncio.sleep(delay)
    s.cancel("cancelled by the user")


async def test_flash_not_started_after_stop(tmp_path):
    """停止之后还没开始的烧录不再开始（比如排在设备锁后面）。"""
    s, _, _ = make(tmp_path, [say("x")], registry=ToolRegistry([FakeFlash()]), mode="always_approve")
    cancel = CancelToken()
    cancel.cancel("cancelled by the user")
    tool = s.registry.get("flash")
    assert tool is not None
    block = ToolCallBlock(id="c1", name="flash", arguments={"seconds": 0.1})
    res = await s._execute(block, tool, tool.parse(block.arguments), cancel)
    assert res.is_error and "Not executed" in res.text_content()


def test_drive_scan_through_a_variable_asks(tmp_path):
    cwd, *_, policy = setup(tmp_path)
    e = PermissionEngine(paths=policy)
    cmd = ("$roots=@('C:\\fwr','D:\\','C:\\Users'); foreach($r in $roots){ Get-ChildItem $r -Recurse -File | "
           "Select-String -Pattern 'FMM_BOOT_SOUND' -List }")
    assert sh(e, cwd, cmd, "default").action == "ask"
    # 路径放在变量里的普通外部读取：默认模式下也认得出来
    d = sh(e, cwd, f"$r='{tmp_path}\\elsewhere'; Get-Content $r\\notes.txt", "default")
    assert d.action == "ask" and "outside the working directory" in d.reason
    # 决定（2026-10-05）：递归扫描整个盘 / 用户目录，Approve all 也要问；扫描工作目录、读单个外部文件不受影响
    d = sh(e, cwd, cmd, "always_approve")
    assert d.action == "ask" and "whole drive or the user folder" in d.reason
    for c in ["Get-ChildItem $env:USERPROFILE -Recurse -Filter *.c", "dir /s D:\\", "Get-ChildItem ~ -r"]:
        assert sh(e, cwd, c, "always_approve").action == "ask", c
    assert sh(e, cwd, "Get-ChildItem . -Recurse -Filter *.c", "always_approve").action == "allow"
    assert sh(e, cwd, "Get-ChildItem D:\\ ", "always_approve").action == "allow"  # 不递归：只列一层
    # 本会话允许过的同一条命令不再问
    e.session_exact.add(cmd)
    assert sh(e, cwd, cmd, "always_approve").action == "allow"
    # 正则里的 '\\build\\' 不是 UNC 路径
    assert sh(e, cwd, "Get-ChildItem . -Recurse | Where-Object { $_.FullName -notmatch '\\\\build\\\\' }", "default").action == "allow"
