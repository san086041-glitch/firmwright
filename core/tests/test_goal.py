"""W7：子 agent 和 goal 模式（规划者 → 执行者 → 独立验证者，设备判据强制检查）。

模型是按顺序回放的脚本：父会话和子会话共用同一个脚本后端，子会话是同步跑完的，所以顺序是确定的。
"""

import asyncio

from fakes import S3_BOOT, FakeAdapter, FakeSerial, board_port

from firmwright.config import Config, WorktreeConfig
from firmwright.model.fake import ScriptedBackend, call, say
from firmwright.runtime import Runtime

FACTS = 'chip = "esp32s3"\npass_marker = "TEST:.*:PASS"\nfail_marker = ["TEST:.*:FAIL"]\nboot_window_s = 3\nsettle_s = 0.2\n'

PLAN = """<plan>
# Plan: make the device report a passing self-test
## Acceptance criteria
1. After flashing, the serial port shows TEST:selftest:PASS and no crash
## Verification steps
1. [device] build → flash → await_marker(expect="TEST:selftest:PASS")
## Out of scope
- none
## Implementation steps
- [ ] add the self-test
## Risks
- none
</plan>"""

PASS = '<verdict>{"verdict": "pass", "criteria": [{"id": 1, "pass": true, "evidence": "看到 TEST:selftest:PASS"}], "gaps": []}</verdict>'


async def make_rt(tmp_path, steps, *, with_board=True):
    proj = tmp_path / "proj"
    (proj / ".firmwright").mkdir(parents=True)
    (proj / ".firmwright" / "facts.toml").write_text(FACTS)
    (proj / "main.c").write_text("void app_main(void) {}\n")

    def opener(port, baud):
        s = FakeSerial(port, baud)

        def on_reset(ser):  # 固件"行为"：main.c 里有自检就输出 PASS
            ok = "selftest" in (proj / "main.c").read_text()
            ser.push(S3_BOOT + ("TEST:selftest:PASS\n" if ok else "I (400) main: running\n"))

        s.on_reset = on_reset
        return s

    rt = Runtime(Config(worktree=WorktreeConfig(enabled=False)), home=tmp_path / "home")
    rt.platform = FakeAdapter()
    if with_board:
        await rt.start_devices(lister=lambda: [board_port()], opener=opener, scan_interval=0.05)
        await asyncio.sleep(0.2)
    backend = ScriptedBackend(steps)
    s = rt.create_session(proj, backend=backend, board_id="usb-aabbccddeeff" if with_board else None,
                          mode="accept_edits")
    updates = []

    async def emit(u):
        updates.append(u)

    s._emit = emit
    return rt, s, backend, updates, proj


# ---------------------------------------------------------------------- 子 agent


async def test_explore_subagent_has_own_context_and_read_only_tools(tmp_path):
    def child_reads(req):
        assert "explore, read-only investigation" in req.system  # 子 agent 的角色提示
        names = {t.name for t in req.tools}
        assert "write_file" not in names and "shell" not in names and "spawn_subagent" not in names
        assert "read_file" in names
        return call("read_file", {"path": "main.c"})

    steps = [
        call("spawn_subagent", {"prompt": "看看 main.c 里 app_main 做了什么", "description": "看 main.c",
                                "subagent_type": "explore"}),
        child_reads,
        say("结论：app_main 是空函数（main.c:1）"),
        say("子 agent 说 app_main 是空的"),
    ]
    rt, s, backend, updates, proj = await make_rt(tmp_path, steps, with_board=False)
    try:
        r = await s.prompt("帮我看一下 main.c")
        assert r.stop_reason == "end_turn"
        # 父会话只拿到子 agent 的结论，没有子 agent 的工具调用过程
        tool_result = s.history[2].content[0]
        assert "结论：app_main 是空函数" in tool_result.content[0].text and "explore" in tool_result.content[0].text
        assert not any(getattr(b, "name", "") == "read_file" for m in s.history for b in m.content)
        # 子会话有自己的存储
        sub = list((s.store.root / "subagents").iterdir())
        assert len(sub) == 1 and (sub[0] / "history.jsonl").exists()
        kinds = [u["sessionUpdate"] for u in updates]
        assert "_fwr/subagent" in kinds and "_fwr/subagent_update" in kinds
        forwarded = [u for u in updates if u["sessionUpdate"] == "_fwr/subagent_update"
                     and u["update"]["sessionUpdate"] == "tool_call"]
        assert forwarded[0]["update"]["toolCallId"].startswith(sub[0].name + ":")
    finally:
        await rt.stop()


# ---------------------------------------------------------------------- goal 模式


async def test_goal_planner_implementer_verifier_pass(tmp_path):
    steps = [
        say(PLAN),  # 规划者
        call("write_file", {"path": "main.c", "content": 'void app_main(void) { printf("selftest"); }\n'}),  # 执行者
        call("goal_report", {"status": "complete", "summary": "加了自检", "evidence": "自己看到了 PASS"}),
        say("完成"),
        call("flash", {}),  # 验证者自己烧录、看设备输出
        call("await_marker", {}),
        say(PASS),
    ]
    rt, s, backend, updates, proj = await make_rt(tmp_path, steps)
    try:
        g = rt.start_goal(s.id, "让设备输出自检通过", max_rounds=3)
        st = await asyncio.wait_for(g.task, 30)
        assert st.status == "done", st
        assert st.criteria == ["After flashing, the serial port shows TEST:selftest:PASS and no crash"] and st.device_steps
        assert st.round == 1 and st.verdicts[0]["verdict"] == "pass"
        assert st.verdicts[0]["device"] == {"flash": True, "await_marker": True}
        # 验证者不能改代码
        verifier_req = backend.requests[4]
        assert "independent verifier" in verifier_req.system
        names = {t.name for t in verifier_req.tools}
        assert {"flash", "await_marker", "build"} <= names and not names & {"write_file", "edit_file", "shell"}
        # 验证者看不到执行者的对话，只拿到声明
        assert "a claim, not evidence" in verifier_req.messages[0].content[-1].text
        assert not any(getattr(b, "name", "") == "write_file" for m in verifier_req.messages for b in m.content)
        assert "goal_report" not in s.registry.names()  # 目标结束后撤掉
        saved = list((s.store.root / "goals").glob("*/goal.json"))
        assert saved and (saved[0].parent / "plan.md").exists()
        assert [u["goal"]["status"] for u in updates if u["sessionUpdate"] == "_fwr/goal"][-1] == "done"
    finally:
        await rt.stop()


async def test_device_oracle_rejects_pass_without_device_evidence(tmp_path):
    """验证者只看代码就判通过 → 设备判据不认，改判未通过，执行者再来一轮。"""
    steps = [
        say(PLAN),
        call("write_file", {"path": "main.c", "content": 'void app_main(void) { printf("selftest"); }\n'}),
        call("goal_report", {"status": "complete", "summary": "加了自检"}),
        say("完成"),
        call("read_file", {"path": "main.c"}),  # 验证者 1：只读了代码
        say(PASS),
        # 第 2 轮：执行者收到"设备判据未满足"的反馈
        lambda req: (call("goal_report", {"status": "complete", "summary": "请在设备上验证"})
                     if "device oracle not satisfied" in str(req.messages[-1].content) else say("没看到反馈")),
        say("好"),
        call("flash", {}),  # 验证者 2：这次烧录并观察
        call("await_marker", {}),
        say(PASS),
    ]
    rt, s, backend, updates, proj = await make_rt(tmp_path, steps)
    try:
        g = rt.start_goal(s.id, "让设备输出自检通过", max_rounds=3)
        st = await asyncio.wait_for(g.task, 30)
        assert st.status == "done" and st.round == 2
        assert st.verdicts[0]["verdict"] == "fail" and st.verdicts[0]["oracle_override"]
        assert st.verdicts[1]["verdict"] == "pass"
        rows = (s.store.root / "trace.jsonl").read_text("utf-8")
        assert '"oracle_override"' in rows
    finally:
        await rt.stop()


async def test_goal_blocked_pauses_and_no_report_runs_out(tmp_path):
    steps = [say(PLAN), call("goal_report", {"status": "blocked", "summary": "需要用户按 BOOT 键"}), say("等你")]
    rt, s, backend, updates, proj = await make_rt(tmp_path, steps, with_board=False)
    try:
        st = await asyncio.wait_for(rt.start_goal(s.id, "目标 A", max_rounds=3).task, 30)
        assert st.status == "paused" and "BOOT" in st.message
    finally:
        await rt.stop()

    # 一直不调用 goal_report、也不改代码：进展判断关掉时把轮数用完 → failed（对照组）
    steps = [say(PLAN), say("我做了一点"), say("又做了一点")]
    rt, s, backend, updates, proj = await make_rt(tmp_path / "b", steps, with_board=False)
    rt.config.features.verify_progress_judge = False
    try:
        st = await asyncio.wait_for(rt.start_goal(s.id, "目标 B", max_rounds=2).task, 30)
        assert st.status == "failed" and st.round == 2
        assert "without calling goal_report" in backend.requests[-1].messages[-1].content[-1].text
    finally:
        await rt.stop()


async def test_progress_judge_pauses_after_two_rounds_without_progress(tmp_path):
    """进展判断开着：连续 2 轮没有任何可测的进展（不改代码、不碰设备）→ 暂停，不把 5 轮耗完。"""
    steps = [say(PLAN), say("我想想"), say("还在想"), say("不该走到这里")]
    rt, s, backend, updates, proj = await make_rt(tmp_path, steps, with_board=False)
    try:
        st = await asyncio.wait_for(rt.start_goal(s.id, "目标 C", max_rounds=5).task, 30)
        assert st.status == "paused" and st.round == 2 and "No progress in the last 2 rounds" in st.message
        assert [n.progress for n in st.notes] == [False, False] and "no code changes" in st.notes[0].why
        # 第 2 轮的指令里带着进展记录和"换个办法"的提醒
        directive = backend.requests[-1].messages[-1].content[-1].text
        assert "Progress so far (measured by Firmwright)" in directive and "Round 1:" in directive
        assert "Try a different approach" in directive
    finally:
        await rt.stop()


def test_round_note_is_measured_from_tool_results():
    from firmwright.model.types import Message, ReminderBlock, TextBlock, ToolCallBlock, ToolResultBlock
    from firmwright.session.goal import collect_round

    def res(cid, name, text, err=False):
        return ToolResultBlock(call_id=cid, name=name, content=[TextBlock(text=text)], is_error=err)

    msgs = [
        Message(role="assistant", content=[ToolCallBlock(id="c1", name="edit_file", arguments={"path": "main/blink.c"})]),
        Message(role="tool", content=[res("c1", "edit_file", "edited")]),
        Message(role="assistant", content=[ToolCallBlock(id="c2", name="build"), ToolCallBlock(id="c3", name="flash"),
                                           ToolCallBlock(id="c4", name="await_marker")]),
        Message(role="tool", content=[res("c2", "build", "ok"), res("c3", "flash", "ok"),
                                      res("c4", "await_marker", "✗ Failure line seen: TEST:led_period:FAIL\nRecent output:…", True)]),
        Message(role="user", content=[ReminderBlock(source="device_event", text="[device event] panic · critical · board b\nLoadProhibited")]),
        Message(role="assistant", content=[TextBlock(text="The period is still wrong; I will check the timer next.")]),
    ]
    n = collect_round(2, msgs, None)
    assert n.files == ["main/blink.c"] and n.builds == 1 and n.build_ok and n.flashes == 1 and n.flash_ok
    assert n.device == "fail" and n.device_line == "TEST:led_period:FAIL" and n.crashes == 1
    assert "timer" in n.note
    line = n.line()
    assert "changed main/blink.c" in line and "device fail (TEST:led_period:FAIL)" in line and "1 crash event" in line


async def test_progress_judge_catches_edit_revert_loops():
    """代码改回到以前出现过的状态、设备表现也一样：不算进展。"""
    from firmwright.session.goal import GoalRunner, RoundNote
    from firmwright.trace import Trace

    class S:
        trace = Trace(None, "s")

        async def emit(self, u):
            pass

    g = GoalRunner(S(), lambda *a: None, "x")  # type: ignore[arg-type]

    def note(r, tree, passed, line):
        return RoundNote(round=r, tree=tree, files=["main.c"], passed=passed, verdict="fail", device="fail", device_line=line)

    assert await g._record(note(1, "A", 1, "TEST:a:FAIL (123)")) is None and g.state.notes[0].progress
    assert await g._record(note(2, "B", 2, "TEST:a:FAIL (456)")) is None and g.state.notes[1].progress  # 多过了一条
    assert await g._record(note(3, "A", 2, "TEST:a:FAIL (789)")) is None  # 改回了 A，时间戳不同但表现一样
    assert not g.state.notes[2].progress and "went back to a state from an earlier round" in g.state.notes[2].why
    msg = await g._record(note(4, "B", 1, "TEST:a:FAIL (999)"))
    assert msg and "No progress in the last 2 rounds" in msg


def test_parse_plan_and_verdict():
    from firmwright.session.goal import parse_plan, parse_verdict

    plan, crit, dev = parse_plan(PLAN)
    assert plan.startswith("# Plan") and crit == ["After flashing, the serial port shows TEST:selftest:PASS and no crash"] and dev
    _, _, dev2 = parse_plan("<plan>## 验收标准\n1. 编译通过\n## 验证步骤\n1. [静态] 编译\n</plan>")
    assert dev2 is False
    assert parse_verdict(PASS)["verdict"] == "pass"
    assert parse_verdict("随便说说")["verdict"] == "unverifiable"


# ---------------------------------------------------------------------- P4（RISC-V）


def test_riscv_panic_keeps_dump_for_gdb():
    from fixtures_riscv import P4_BOOT, riscv_panic

    from firmwright.platform.esp_idf.decode import fault_evidence
    from firmwright.platform.esp_idf.logparse import EspLogParser

    p = EspLogParser("b")
    evs = []
    for line in (P4_BOOT + riscv_panic(exc="Store access fault", mcause="0x00000007")).splitlines():
        evs += p.feed(line)
    evs += p.tick(1e9)
    assert p.chip == "esp32p4"
    ev = next(e for e in evs if e.kind == "panic")
    assert "Store access fault" in ev.summary and "NULL pointer" in ev.summary
    dump = ev.detail["panic_dump"]
    assert dump.startswith("Core  0 register dump:") and "Stack memory:" in dump and "ELF file" not in dump
    assert [f.pc for f in ev.backtrace.frames] == ["0x4fc05a48", "0x4fc0126e"]  # 解码前先放 MEPC / RA
    fe = fault_evidence(ev)
    assert fe.arch == "riscv" and "NULL pointer" in fe.explanation


async def test_riscv_unwind_with_gdb_on_rom_elf():
    """真的跑 riscv32-esp-elf-gdb + esp_idf_panic_decoder（本机 ESP-IDF 自带）。没有 ESP-IDF 时跳过。"""
    import glob
    from pathlib import Path

    import pytest
    from fixtures_riscv import riscv_panic

    from firmwright.config import Config
    from firmwright.platform.esp_idf.adapter import EspIdfAdapter
    from firmwright.platform.esp_idf.env import IdfEnv, load_eim
    from firmwright.platform.esp_idf.logparse import EspLogParser

    cfg = Config()
    roms = glob.glob(r"C:\Espressif\tools\esp-rom-elfs\*\esp32p4_rev0_rom.elf")
    if not cfg.idf.eim_json.is_file() or not roms:
        pytest.skip("本机没有 ESP-IDF / P4 ROM ELF")
    p = EspLogParser("b")
    evs = []
    for line in riscv_panic().splitlines():
        evs += p.feed(line)
    evs += p.tick(1e9)
    ev = next(e for e in evs if e.kind == "panic")
    adapter = EspIdfAdapter(IdfEnv(load_eim(cfg.idf.eim_json, None), Path(r"D:\HardwareCodingAgent\dev-home\idf-env-cache")))
    bt = await adapter.unwind(ev, Path(roms[0]), Path(roms[0]).parent)
    assert bt.decoded and bt.raw.startswith("gdb bt")
    assert [f.function for f in bt.frames[:2]] == ["crc32_le", "ets_delay_us"]


def test_p4_bootloader_write_is_dangerous():
    from firmwright.permissions.engine import PermissionEngine
    from firmwright.platform.esp_idf.adapter import EspIdfAdapter
    from firmwright.tools.shell import Shell

    eng = PermissionEngine(risk_rules=EspIdfAdapter.risk_rules(None))
    sh = Shell()
    for cmd in ["esptool.py --chip esp32p4 write_flash 0x2000 bootloader.bin",
                "esptool.py write_flash 0x8000 partition-table.bin",
                "esptool.py --force write_flash 0x10000 app.bin",
                "otatool.py erase_otadata"]:
        d = eng.evaluate(sh, sh.parse({"command": cmd}), mode="always_approve", cwd=__import__("pathlib").Path("."))
        assert d.action == "ask" and d.risk == "dangerous", cmd
    ok = eng.evaluate(sh, sh.parse({"command": "esptool.py write_flash 0x10000 app.bin"}), mode="always_approve",
                      cwd=__import__("pathlib").Path("."))
    assert ok.risk != "dangerous"
