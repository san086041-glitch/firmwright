import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from fakes import FACTS, S3_BOOT, S3_PANIC, S3_REBOOT, FakeAdapter, FakeSerial, board_port

from firmwright.device.events import Backtrace, DeviceEvent, Frame
from firmwright.device.manager import DeviceManager
from firmwright.device.router import EventRouter
from firmwright.model.types import CancelToken
from firmwright.platform.esp_idf.decode import fault_evidence, is_internal
from firmwright.platform.esp_idf.logparse import EspLogParser
from firmwright.platform.esp_idf.parse import classify_flash, parse_build_output
from firmwright.services import Services
from firmwright.tools.base import HumanReply, ToolContext
from firmwright.tools.hw import AwaitMarker, AwaitMarkerArgs, DiagnoseArgs, DiagnoseCrash, Flash, FlashArgs
from firmwright.trace import Trace


def feed_all(p: EspLogParser, text: str):
    evs = []
    off = 0
    for line in text.splitlines():
        evs += p.feed(line, off, off + len(line) + 1)
        off += len(line) + 1
    return evs


# ---------------------------------------------------------------- 日志解析


def test_parser_boot_banner_and_panic():
    p = EspLogParser("b1", "x.log", FACTS)
    evs = feed_all(p, S3_BOOT + S3_PANIC)
    kinds = [e.kind for e in evs]
    assert kinds == ["boot", "marker", "panic"]
    assert evs[0].detail["chip"] == "esp32s3" and evs[0].detail["reset_reason"] == "POWERON"
    panic = evs[2]
    assert panic.severity == "critical"
    assert panic.detail["exception"] == "LoadProhibited"
    assert panic.detail["registers"]["EXCVADDR"] == "0x00000000"
    assert "NULL pointer" in panic.summary
    assert [f.pc for f in panic.backtrace.frames] == ["0x42008c1a", "0x42008c39", "0x4201a3e3", "0x4037a1fd"]
    assert panic.log_ref.start < panic.log_ref.end


def test_parser_block_closed_by_idle_timeout_and_next_boot():
    t = [0.0]
    p = EspLogParser("b1", clock=lambda: t[0])
    assert feed_all(p, "abort() was called at PC 0x42008d3f on core 0\nBacktrace: 0x42008d3c:0x3fc99e40") == []
    t[0] = 5.0
    evs = p.tick()
    assert [e.kind for e in evs] == ["abort"] and evs[0].detail["pc"] == "0x42008d3f"
    # 被下一次启动打断的块
    evs = feed_all(p, "assert failed: app_main main.c:12 (x == 1)\n" + S3_REBOOT)
    assert [e.kind for e in evs] == ["assert", "boot"]
    assert "x == 1" in evs[0].summary


def test_parser_reboot_loop_and_wdt_and_download():
    t = [0.0]
    p = EspLogParser("b1", facts=FACTS, clock=lambda: t[0])
    kinds = []
    for i in range(3):
        t[0] = i * 2.0
        kinds += [e.kind for e in feed_all(p, "rst:0x7 (TG0WDT_SYS_RST),boot:0x8 (SPI_FAST_FLASH_BOOT)")]
    assert kinds.count("wdt_reset") == 3 and kinds.count("reboot_loop") == 1
    evs = feed_all(p, "rst:0x1 (POWERON),boot:0x0 (DOWNLOAD(USB/UART0))\nwaiting for download")
    assert [e.kind for e in evs].count("download_mode") == 2


def test_fault_evidence_explains_null_pointer():
    p = EspLogParser("b1")
    ev = feed_all(p, S3_PANIC)[0]
    fe = fault_evidence(ev)
    assert fe.arch == "xtensa" and fe.exception == "LoadProhibited"
    assert "NULL pointer" in fe.explanation


def test_frame_folding():
    assert is_internal("C:/Espressif/v5.5/esp-idf/components/freertos/port.c", "vPortTask", "C:/Espressif/v5.5/esp-idf",
                       Path("C:/fwr/wt/abc"))
    assert not is_internal("C:/fwr/wt/abc/main/main.c", "app_main", "C:/Espressif/v5.5/esp-idf", Path("C:/fwr/wt/abc"))
    assert is_internal(None, None, None, None)


def test_build_and_flash_output_parsing():
    out = """[5/10] Building C object esp-idf/main/CMakeFiles/__idf_main.dir/main.c.obj
FAILED: esp-idf/main/CMakeFiles/__idf_main.dir/main.c.obj
C:/fwr/wt/abc/main/main.c:17:5: error: 'hlaf' undeclared (first use in this function); did you mean 'half'?
C:/fwr/wt/abc/main/main.c:20:9: warning: unused variable 'x' [-Wunused-variable]
ninja: build stopped: subcommand failed."""
    diags, cls = parse_build_output(out, Path("C:/fwr/wt/abc"))
    assert cls == "build_error"
    assert diags[0].file == "main/main.c" and diags[0].line == 17 and diags[0].col == 5
    assert diags[1].severity == "warning"
    ld = "C:/x/ld.exe: esp-idf/main/libmain.a(main.c.obj):(.literal.app_main+0x8): undefined reference to `blink_init'"
    diags, cls = parse_build_output(ld)
    assert cls == "link_error" and "blink_init" in diags[0].message

    cls, actions, _ = classify_flash("A fatal error occurred: Failed to connect to ESP32-S3: No serial data received.")
    assert cls == "sync_failed" and actions[0].human and actions[0].kind == "enter_download_mode"
    cls, _, _ = classify_flash("could not open port 'COM7': PermissionError(13, 'Access is denied.', None, 5)")
    assert cls == "port_busy"
    cls, actions, _ = classify_flash("A fatal error occurred: This chip is ESP32-P4, not ESP32-S3. Wrong --chip argument?")
    assert cls == "chip_mismatch" and actions[0].kind == "set_target"


# ---------------------------------------------------------------- 设备管理器 + 路由


async def make_manager(tmp_path, ports, serials, adapter=None):
    def opener(port, baud):
        s = FakeSerial(port, baud)
        serials[port] = s
        return s

    adapter = adapter or FakeAdapter()
    m = DeviceManager(adapter, home=tmp_path, lister=lambda: list(ports), opener=opener, scan_interval=0.05,
                      facts_for=lambda b: FACTS)
    await m.start()
    return m


async def wait_for(cond, timeout=3.0):
    t0 = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.02)


async def test_manager_discovery_events_and_reconnect(tmp_path):
    ports = [board_port()]
    serials: dict[str, FakeSerial] = {}
    m = await make_manager(tmp_path, ports, serials)
    got = []
    m.listen(lambda k, p: got.append((k, p)))
    try:
        assert "usb-aabbccddeeff" in m.boards
        serials["COM7"].push(S3_BOOT + S3_PANIC)
        await wait_for(lambda: any(k == "event" and p.kind == "panic" for k, p in got))
        b = m.boards["usb-aabbccddeeff"]
        assert b.chip == "esp32s3" and b.state == "crashed"
        await wait_for(lambda: any(k == "serial" for k, _ in got))
        assert "Guru Meditation" in m.read_log(b.id, grep="Guru")
        panic = m.latest_crash(b.id)
        assert "LoadProhibited" in m.read_log(b.id, panic.log_ref)
        # 拔掉 → 断开；换个 COM 口插回来 → 认出是同一块板子（D03）
        ports.clear()
        await wait_for(lambda: m.boards[b.id].state == "disconnected")
        ports.append(board_port(device="COM9"))
        await wait_for(lambda: m.boards[b.id].port == "COM9" and b.id in m.monitors)
        assert any(k == "event" and p.kind == "reconnect" for k, p in got)
    finally:
        await m.stop()


class FakeSession:
    def __init__(self, running=True):
        self.running = running
        self.injected = []

    def inject(self, r, interrupt=False):
        self.injected.append((r, interrupt))


def test_log_digest_off_injects_raw_serial_output():
    """功能开关 context.log_digest 关掉（消融基线）：注入串口原文，不给摘要、调用栈和 diagnose_crash 提示。"""
    from firmwright.device.events import Backtrace, Frame
    from firmwright.session.prompt import RAW_LINE, system_prompt

    class B:
        owner_session = "s1"
        idle_policy = None

    sess = FakeSession()
    raw = "Guru Meditation Error: Core  0 panic'ed (LoadProhibited)\nBacktrace: 0x42008c20:0x3fc9"
    r = EventRouter(get_board=lambda _: B, get_session=lambda _: sess, notify=lambda e: None, raw_log=lambda ev: raw)
    ev = DeviceEvent.make("b", "panic", "CPU exception LoadProhibited", backtrace=Backtrace(
        frames=[Frame(pc="0x42008c20", function="led_set", file="blink.c", line=42)], decoded=True))
    assert r.route(ev).action == "inject"
    text = sess.injected[0][0].text
    assert raw in text and "led_set" not in text and "CPU exception" not in text and "diagnose_crash" not in text
    assert RAW_LINE in system_prompt(log_digest=False) and RAW_LINE not in system_prompt()


def test_router_inject_notify_drop_and_rate_limit():
    class B:
        owner_session = "s1"
        idle_policy = None

    sess = FakeSession()
    notified = []
    t = [0.0]
    r = EventRouter(get_board=lambda _: B, get_session=lambda _: sess, notify=notified.append, clock=lambda: t[0])
    ev = DeviceEvent.make("b", "panic", "LoadProhibited")
    assert r.route(ev).action == "inject" and sess.injected[0][1] is True
    assert sess.injected[0][0].event_id == ev.id
    assert r.route(DeviceEvent.make("b", "boot", "boot")).action == "drop"
    # 200ms 内重复 → 合并
    assert r.route(DeviceEvent.make("b", "panic", "LoadProhibited")).action == "suppressed"
    # 令牌桶：一口气来很多事件，超出的被合并，补满后带汇总
    for i in range(20):
        t[0] += 0.3
        r.route(DeviceEvent.make("b", "abort", f"a{i}"))
    t[0] += 30
    r.route(DeviceEvent.make("b", "abort", "late"))
    assert any("were merged" in r.text for r, _ in sess.injected)
    assert len(sess.injected) < 15
    # 空闲：按设置通知 / 忽略
    sess.running = False
    t[0] += 30
    assert r.route(DeviceEvent.make("b", "panic", "x")).action == "notify" and notified
    B.idle_policy = "ignore"
    t[0] += 30
    assert r.route(DeviceEvent.make("b", "panic", "y")).action == "drop"


# ---------------------------------------------------------------- 硬件工具


def ctx_for(m, tmp_path, adapter, human=None) -> ToolContext:
    async def ask(action):
        return human(action) if human else HumanReply(done=False)

    return ToolContext(session_id="s1", cwd=tmp_path, cancel=CancelToken(), trace=Trace(None, "s1"),
                       services=Services(platform=adapter, devices=m, facts=FACTS), board_id="usb-aabbccddeeff",
                       ask_human_cb=ask)


async def test_flash_then_await_marker_pass_and_crash(tmp_path):
    ports = [board_port()]
    serials: dict[str, FakeSerial] = {}
    adapter = FakeAdapter()
    m = await make_manager(tmp_path, ports, serials, adapter)
    try:
        ctx = ctx_for(m, tmp_path, adapter)
        res = await Flash().run(ctx, FlashArgs())
        assert not res.is_error and adapter.flash_calls == [("COM7", "app")]
        # 烧录后监听恢复，并且主动复位了一次
        await wait_for(lambda: "COM7" in serials and not serials["COM7"].closed)

        async def device_prints():
            await asyncio.sleep(0.2)
            serials["COM7"].push(S3_BOOT + "TEST:half_period:PASS\n")

        asyncio.create_task(device_prints())
        res = await AwaitMarker().run(ctx, AwaitMarkerArgs())
        assert not res.is_error and res.meta["status"] == "pass"

        from firmwright.tools.hw import mark
        mark(ctx, "usb-aabbccddeeff")

        async def crash():
            await asyncio.sleep(0.2)
            serials["COM7"].push(S3_BOOT + S3_PANIC)

        asyncio.create_task(crash())
        res = await AwaitMarker().run(ctx, AwaitMarkerArgs(timeout_s=5))
        assert res.is_error and res.meta["status"] == "crash"
        assert "LoadProhibited" in res.text_content()
        ev_id = res.meta["event"]["id"]
        assert ev_id in ctx.extra["delivered_events"]
        diag = await DiagnoseCrash().run(ctx, DiagnoseArgs(event_id=ev_id))
        assert "NULL pointer" in diag.text_content()

        # 超时：返回最后几行
        mark(ctx, "usb-aabbccddeeff")
        res = await AwaitMarker().run(ctx, AwaitMarkerArgs(expect="never", timeout_s=0.3, since="now"))
        assert res.meta["status"] == "timeout"
    finally:
        await m.stop()


async def test_flash_sync_failure_asks_human_then_retries(tmp_path):
    ports = [board_port()]
    serials: dict[str, FakeSerial] = {}
    adapter = FakeAdapter(flash_ok=False)
    m = await make_manager(tmp_path, ports, serials, adapter)
    asked = []

    def human(action):
        asked.append(action)
        adapter.flash_ok = True  # 用户按了 BOOT + RESET
        return HumanReply(done=True)

    try:
        ctx = ctx_for(m, tmp_path, adapter, human)
        res = await Flash().run(ctx, FlashArgs())
        assert asked and asked[0].kind == "enter_download_mode"
        assert not res.is_error and len(adapter.flash_calls) == 2
    finally:
        await m.stop()


# ---------------------------------------------------------------- 真实 ELF 的解码（有编译产物时才跑）

ELF = Path("C:/fwr/wt/w1test/build/blink_demo.elf")


@pytest.mark.skipif(not ELF.exists() or not Path("C:/Espressif/tools/eim_idf.json").exists(), reason="没有 IDF 编译产物")
async def test_real_addr2line_decode(tmp_path):
    from firmwright.platform.esp_idf.adapter import EspIdfAdapter
    from firmwright.platform.esp_idf.env import IdfEnv, load_eim

    adapter = EspIdfAdapter(IdfEnv(load_eim(Path("C:/Espressif/tools/eim_idf.json")), tmp_path / "cache"))
    env = await adapter.idf.full()
    nm = shutil.which("xtensa-esp32s3-elf-nm", path=env["PATH"])
    syms = subprocess.run([nm, str(ELF)], capture_output=True, text=True).stdout
    addr = {line.split()[2]: line.split()[0] for line in syms.splitlines() if len(line.split()) == 3}
    pcs = [f"0x{int(addr['app_main'], 16) + 8:08x}", f"0x{int(addr['main_task'], 16) + 8:08x}"]
    ev = DeviceEvent.make("b", "panic", "x", backtrace=Backtrace(frames=[Frame(pc=pc) for pc in pcs]))
    bt = await adapter.unwind(ev, ELF, Path("C:/fwr/wt/w1test"))
    assert bt.decoded
    assert bt.frames[0].function == "app_main" and bt.frames[0].file.endswith("main.c") and not bt.frames[0].internal
    assert bt.frames[-1].function == "main_task" and bt.frames[-1].internal


async def test_await_marker_catches_crash_right_after_pass(tmp_path):
    """固件先打印 PASS、紧接着崩溃（W4 演示里遇到的）：不能报通过。"""
    ports = [board_port()]
    serials: dict[str, FakeSerial] = {}
    m = await make_manager(tmp_path, ports, serials)
    try:
        ctx = ctx_for(m, tmp_path, FakeAdapter())
        from firmwright.tools.hw import mark

        mark(ctx, "usb-aabbccddeeff")

        async def device():
            await asyncio.sleep(0.2)
            serials["COM7"].push(S3_BOOT + "TEST:half_period:PASS\n")
            await asyncio.sleep(0.3)
            serials["COM7"].push(S3_PANIC)

        asyncio.create_task(device())
        res = await AwaitMarker().run(ctx, AwaitMarkerArgs(timeout_s=5))
        assert res.is_error and res.meta["status"] == "crash"
        assert "Expected line" in res.text_content() or "LoadProhibited" in res.text_content()

        # 稳定窗口内一切正常 → 仍然是通过
        mark(ctx, "usb-aabbccddeeff")

        async def healthy():
            await asyncio.sleep(0.2)
            serials["COM7"].push(S3_BOOT + "TEST:half_period:PASS\n")

        asyncio.create_task(healthy())
        res = await AwaitMarker().run(ctx, AwaitMarkerArgs(timeout_s=5, since="now"))
        assert not res.is_error and res.meta["status"] == "pass"
    finally:
        await m.stop()
