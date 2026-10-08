"""W2 演示的无硬件版本：板子一启动就崩溃，agent 收到事件后定位并修复。

真实的 Runtime + DeviceManager + EventRouter + Session，只把串口、平台适配器和模型换成假的。
"""

import asyncio

from fakes import S3_BOOT, S3_PANIC, FakeAdapter, FakeSerial, board_port

from firmwright.config import Config
from firmwright.model.fake import ScriptedBackend, call, say
from firmwright.model.types import ReminderBlock, ToolResultBlock
from firmwright.runtime import Runtime

FACTS = """chip = "esp32s3"
boot_banner = "app_main started"
pass_marker = "TEST:.*:PASS"
fail_marker = ['TEST:.*:FAIL']
boot_window_s = 3
"""


async def setup(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".firmwright").mkdir(parents=True)
    (proj / ".firmwright" / "facts.toml").write_text(FACTS)
    (proj / "main.c").write_text("int *p = NULL;\nvoid app_main(void) { *p = 1; }\n")
    serials: dict[str, FakeSerial] = {}
    adapter = FakeAdapter()

    def opener(port, baud):
        s = FakeSerial(port, baud)

        # 模拟固件：复位后按 main.c 当前内容决定输出崩溃还是通过
        def on_reset(ser):
            src = (proj / "main.c").read_text()
            ser.push(S3_BOOT + (S3_PANIC if "if (p)" not in src else "TEST:null_check:PASS\n"))

        s.on_reset = on_reset
        serials[port] = s
        return s

    rt = Runtime(Config(), home=tmp_path / "home")
    rt.platform = adapter
    await rt.start_devices(lister=lambda: [board_port()], opener=opener, scan_interval=0.05)
    return rt, proj, serials


async def test_crash_event_drives_fix(tmp_path):
    rt, proj, serials = await setup(tmp_path)
    try:
        steps = [
            call("flash", {}),
            call("await_marker", {}),  # → 崩溃，结果里带事件
            lambda req: call("diagnose_crash", {"event_id": _event_id(req)}),
            call("edit_file", {"path": "main.c", "old_string": "*p = 1;", "new_string": "if (p) { *p = 1; }"}),
            call("flash", {}),
            call("await_marker", {}),
            say("修好了：空指针解引用，已加判空，设备输出 PASS"),
        ]
        backend = ScriptedBackend(steps)
        s = rt.create_session(proj, backend=backend, board_id="usb-aabbccddeeff", mode="accept_edits")
        assert rt.devices.boards["usb-aabbccddeeff"].owner_session == s.id
        r = await asyncio.wait_for(s.prompt("板子一启动就崩，修一下"), 30)
        assert r.stop_reason == "end_turn", r
        results = [b for m in s.history if m.role == "tool" for b in m.content if isinstance(b, ToolResultBlock)]
        texts = [b.content[0].text for b in results]
        assert results[1].is_error and results[1].content and "crashed" in texts[1]
        assert "NULL pointer" in texts[2]
        assert "✓" in texts[5]
        # 同一次崩溃的摘要只出现一次：要么在注入的 reminder 里，要么在 await_marker 的结果里
        reminders = [b.text for m in s.history for b in m.content if isinstance(b, ReminderBlock)]
        assert sum("CPU exception LoadProhibited" in t for t in reminders + texts[:2]) == 1
        kinds = [row["kind"] for row in _trace(s)]
        assert "device_event" in kinds and "permission" in kinds
    finally:
        await rt.stop()


async def test_crash_while_agent_works_is_injected(tmp_path):
    rt, proj, serials = await setup(tmp_path)
    try:
        def crash_now(req):
            serials["COM7"].push(S3_BOOT + S3_PANIC)
            return call("read_file", {"path": "main.c"})

        async def slow(req):
            return say("x")

        seen = {}

        def look(req):
            seen["reminders"] = [b for m in req.messages for b in m.content if isinstance(b, ReminderBlock)]
            return say("看到了崩溃事件")

        backend = ScriptedBackend([crash_now, look])
        s = rt.create_session(proj, backend=backend, board_id="usb-aabbccddeeff")

        orig = backend.stream

        async def delayed(req, cancel):  # 模型"思考"时设备崩溃：给串口线程和解析留一点时间
            async for ev in orig(req, cancel=cancel):
                yield ev
            await asyncio.sleep(0.5)

        backend.stream = lambda req, cancel: delayed(req, cancel)
        r = await asyncio.wait_for(s.prompt("看看代码"), 20)
        assert r.text == "看到了崩溃事件"
        assert any("LoadProhibited" in b.text and b.source == "device_event" for b in seen["reminders"])
    finally:
        await rt.stop()


async def test_idle_crash_notifies_ui(tmp_path):
    rt, proj, serials = await setup(tmp_path)
    notes = []
    rt.on_notify = notes.append
    try:
        rt.create_session(proj, backend=ScriptedBackend([]), board_id="usb-aabbccddeeff")
        serials["COM7"].push(S3_BOOT + S3_PANIC)
        for _ in range(100):
            if notes:
                break
            await asyncio.sleep(0.05)
        assert notes and notes[0].kind == "panic"  # I04：空闲 + notify → 通知界面，不自动处理
    finally:
        await rt.stop()


def _event_id(req) -> str:
    import re

    for m in reversed(req.messages):
        for b in m.content:
            text = b.content[0].text if isinstance(b, ToolResultBlock) else getattr(b, "text", "")
            found = re.search(r'event_id="(\w+)"|事件 (\w{12})', text or "")
            if found:
                return found.group(1) or found.group(2)
    raise AssertionError("没有找到 event_id")


def _trace(s):
    from firmwright.trace import read_trace

    return read_trace(s.trace.path)
