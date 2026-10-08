"""后台子 agent（2026-10-05）：父 agent 不等它，结果按事件路由的规则送达——
父会话在执行 → 注入；父会话空闲 → 排队 + 通知界面，不自动开新一轮（I04）。"""

import asyncio

from test_goal import make_rt

from firmwright.model.fake import ScriptedBackend, call, say
from firmwright.model.types import ModelCaps

EXPLORE = "explore, read-only investigation"


class Routed:
    """父会话和子会话各用一份脚本（按子 agent 的角色提示区分）；子会话可以先卡在闸门上。"""

    def __init__(self, parent_steps, child_steps, gate: asyncio.Event | None = None, child_delay: float = 0,
                 parent_delay: float = 0) -> None:
        self.parent = ScriptedBackend(parent_steps)
        self.child = ScriptedBackend(child_steps)
        self.gate = gate
        self.child_delay = child_delay
        self.parent_delay = parent_delay  # 父会话第一步之后的每一步先等一会（让子会话先做完）
        self.caps = ModelCaps()

    async def stream(self, req, *, cancel):
        is_child = EXPLORE in req.system
        if is_child and self.gate is not None:
            await self.gate.wait()
        if is_child and self.child_delay:
            await asyncio.sleep(self.child_delay)
        if not is_child and self.parent_delay and self.parent.requests:
            await asyncio.sleep(self.parent_delay)
        async for ev in (self.child if is_child else self.parent).stream(req, cancel=cancel):
            yield ev


def spawn_bg(desc="watch the serial port"):
    return call("spawn_subagent", {"prompt": "Watch the device output and report anything odd", "description": desc,
                                   "subagent_type": "explore", "background": True})


async def test_parent_waits_with_check_subagents_and_gets_the_report_once(tmp_path):
    rt, s, _, updates, _ = await make_rt(tmp_path, [], with_board=False)
    # 子 agent 慢一点：父 agent 调 check_subagents 开始等的时候，它还在跑
    s.backend = Routed([spawn_bg(), call("check_subagents", {"wait_s": 5}), say("the watcher found nothing odd")],
                       [say("Report: 3 boots, no crash")], child_delay=0.3)
    r = await s.prompt("watch the board in the background")
    assert r.stop_reason == "end_turn"
    results = {b.name: b.content[0].text for m in s.history if m.role == "tool" for b in m.content}
    assert "Started background sub-agent" in results["spawn_subagent"]
    check_res = results["check_subagents"]
    assert "Report: 3 boots, no crash" in check_res and "finished" in check_res
    # 报告已经通过 check_subagents 交出去了：不再以 reminder 重复注入
    assert not [p for p in s._pending if p.source == "subagent"]
    run = next(iter(s.background.runs.values()))
    assert run.delivered and not run.running
    kinds = [(u.get("status"), u.get("background")) for u in updates if u.get("sessionUpdate") == "_fwr/subagent"]
    assert kinds == [("running", True), ("done", True)]


async def test_report_is_injected_when_it_finishes_while_the_parent_is_running(tmp_path):
    """子 agent 先做完、父 agent 还在跑：报告作为 reminder 注入父 agent 的下一步。"""
    rt, s, _, _, _ = await make_rt(tmp_path, [], with_board=False)
    s.backend = Routed([spawn_bg(), call("read_file", {"path": "main.c"}), say("noted the watcher's report")],
                       [say("Report: all quiet")], parent_delay=0.2)
    await s.prompt("start a watcher and keep reading code")
    reqs = s.backend.parent.requests
    seen = [any("all quiet" in getattr(b, "text", "") for m in r.messages for b in m.content) for r in reqs]
    assert seen[-1] and not seen[0]
    assert not [p for p in s._pending if p.source == "subagent"]


async def test_report_is_queued_when_the_parent_is_idle_and_blocks_restore_while_running(tmp_path):
    gate = asyncio.Event()
    rt, s, _, updates, _ = await make_rt(tmp_path, [], with_board=False)
    s.backend = Routed([spawn_bg(), say("started a watcher"), say("ok, the watcher saw a brownout")],
                       [say("Report: one brownout at 12 s")], gate=gate)
    try:
        await s.prompt("start a watcher")
        assert not s.running and s.background.active()
        # 后台子 agent 还在跑：回退 / 合并 / 丢弃会换掉它脚下的文件，要拒绝
        try:
            rt._require_idle(s.id)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as e:
            assert "Background sub-agents" in str(e)
        assert rt._busy_reason(s.id) == "has background sub-agents running"

        gate.set()
        run = next(iter(s.background.runs.values()))
        await asyncio.wait_for(run.done.wait(), 5)
        await asyncio.sleep(0.05)
        # 父会话空闲：不自动开新一轮，报告排队，界面收到"等你决定"的提示
        assert not s.running
        assert any(p.source == "subagent" and "one brownout" in p.text for p in s._pending)
        assert any(u.get("sessionUpdate") == "_fwr/subagent" and u.get("awaitingParent") for u in updates)
        # 下一轮开头交给模型
        await s.prompt("continue")
        last_req = s.backend.parent.requests[-1]
        assert any("one brownout" in getattr(b, "text", "") for m in last_req.messages for b in m.content)
    finally:
        gate.set()


async def test_stop_subagent_cancels_a_background_run(tmp_path):
    gate = asyncio.Event()
    rt, s, _, _, _ = await make_rt(tmp_path, [], with_board=False)
    s.backend = Routed([spawn_bg(), say("started")], [say("never reached")], gate=gate)
    await s.prompt("start a watcher")
    run = next(iter(s.background.runs.values()))
    await asyncio.sleep(0.05)  # 子会话已经开始这一轮（卡在闸门上）
    assert s.background.stop(run.id, "stopped by the user")
    gate.set()
    await asyncio.wait_for(run.done.wait(), 5)
    assert run.result["stop"] == "cancelled"
    assert not s.background.active()
