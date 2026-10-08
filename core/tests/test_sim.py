"""模拟板：设备面板这条链路在没有真板子时的替身（device/sim.py）。"""

import asyncio

from fakes import FakeAdapter

from firmwright.device.manager import DeviceManager
from firmwright.device.sim import SIM_PORT, SimFirmware, SimHub


async def wait_for(cond, timeout=5.0):
    t0 = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t0 > timeout:
            raise AssertionError("等待超时")
        await asyncio.sleep(0.05)


async def test_sim_board_boots_and_crashes(tmp_path):
    hub = SimHub(FakeAdapter())
    m = DeviceManager(hub.wrap(), home=tmp_path, lister=hub.lister(lambda: []),
                      opener=hub.opener(lambda p, b: None), scan_interval=0.05)
    events = []
    m.listen(lambda k, p: events.append(p) if k == "event" else None)
    await m.start()
    try:
        bid = next(iter(m.boards))
        assert m.boards[bid].port == SIM_PORT and m.boards[bid].usb_jtag
        hub.board.fw = SimFirmware()
        hub.board.fw.flashed = True
        hub.board.fw.markers = ["TEST:x:PASS"]
        await m.reset(bid)
        await wait_for(lambda: any(e.kind == "boot" for e in events))
        await wait_for(lambda: m.boards[bid].chip == "esp32s3")
        hub.board.crash_now()
        await wait_for(lambda: any(e.kind == "panic" for e in events))
        panic = next(e for e in events if e.kind == "panic")
        assert panic.detail["exception"] == "LoadProhibited" and len(panic.backtrace.frames) == 3
        # 拔掉 → 断开事件
        hub.board.present = False
        await wait_for(lambda: m.boards[bid].state == "disconnected")
    finally:
        await m.stop()


def test_sim_firmware_reads_project(tmp_path):
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "main.c").write_text('printf("TEST:half:%s\\n", ok ? "PASS" : "FAIL"); // SIM_CRASH\n')
    fw = SimFirmware.from_project(tmp_path, None)
    assert fw.crash and fw.markers == ["TEST:half:PASS"]


SRC = r"""
#define PERIOD_MS 400
#define EXPECTED_HALF_MS 200
uint32_t half_of(uint32_t p) { return p / 2; }
void app_main(void) {
    printf("TEST:boot:PASS\n");
    uint32_t half = half_of(PERIOD_MS);
    printf("TEST:half_period:%s\n", half == EXPECTED_HALF_MS ? "PASS" : "FAIL");
    for (;;) { vTaskDelay(pdMS_TO_TICKS(half)); }
}
"""


def test_sim_firmware_follows_source(tmp_path):
    """W7：LED 间隔和自检标记按源码算（原来写死 500 ms、一律 PASS，时序目标在模拟板上验证不了）。"""
    (tmp_path / "main").mkdir()
    main = tmp_path / "main" / "main.c"
    main.write_text(SRC, "utf-8")
    fw = SimFirmware.from_project(tmp_path, None)
    assert fw.markers == ["TEST:boot:PASS", "TEST:half_period:PASS"] and fw.led_half_ms == 200
    main.write_text(SRC.replace("PERIOD_MS 400", "PERIOD_MS 1000"), "utf-8")
    fw = SimFirmware.from_project(tmp_path, None)
    assert fw.markers[1] == "TEST:half_period:FAIL" and fw.led_half_ms == 500
    main.write_text(SRC.replace("PERIOD_MS 400", "PERIOD_MS 415"), "utf-8")  # 207 ms → 按 10 ms tick 取整
    assert SimFirmware.from_project(tmp_path, None).led_half_ms == 200
    # 算不出来 → 旧行为；sim.toml 可以指定
    main.write_text(r'void f(void){ printf("TEST:x:%s\n", check() ? "PASS" : "FAIL"); vTaskDelay(get()); }', "utf-8")
    fw = SimFirmware.from_project(tmp_path, None)
    assert fw.markers == ["TEST:x:PASS"] and fw.led_half_ms == 500
    (tmp_path / ".firmwright").mkdir()
    (tmp_path / ".firmwright" / "sim.toml").write_text('led_half_ms = 120\n[markers]\nx = "FAIL"\n', "utf-8")
    fw = SimFirmware.from_project(tmp_path, None)
    assert fw.markers == ["TEST:x:FAIL"] and fw.led_half_ms == 120


async def test_sim_board_knows_its_chip_before_any_boot(tmp_path):
    """2026-10-06：设备栏原来显示 "unknown chip"，要等一次启动日志或点 Identify。"""
    hub = SimHub(FakeAdapter())
    m = DeviceManager(hub.wrap(), home=tmp_path, lister=hub.lister(lambda: []),
                      opener=hub.opener(lambda p, b: None), scan_interval=0.05)
    await m.start()
    try:
        b = next(iter(m.boards.values()))
        assert b.chip == hub.board.chip.id and "S3" in b.alias.upper()
    finally:
        await m.stop()


def test_dev_boards_are_off_in_release_builds(monkeypatch):
    import sys

    from firmwright import runtime

    monkeypatch.setenv("FIRMWRIGHT_SIM_BOARD", "1")
    assert runtime.dev_boards_requested() and runtime.dev_boards_allowed()
    monkeypatch.setattr(sys, "frozen", True, raising=False)  # PyInstaller 打包后的样子
    assert not runtime.dev_boards_allowed()


async def test_reset_from_the_serial_panel(tmp_path):
    """2026-10-06：串口面板空着时的"Reset board"。会话正在执行时拒绝；复位后能看到启动日志。"""
    import pytest

    from firmwright.acp.server import AcpServer, RpcError
    from firmwright.config import Config, IdfConfig
    from firmwright.model.fake import ScriptedBackend, say
    from firmwright.runtime import Runtime

    rt = Runtime(Config(idf=IdfConfig(eim_json=tmp_path / "none.json")), home=tmp_path / "home")
    hub = SimHub(FakeAdapter())
    m = DeviceManager(hub.wrap(), home=tmp_path, lister=hub.lister(lambda: []),
                      opener=hub.opener(lambda p, b: None), scan_interval=0.05)
    events = []
    m.listen(lambda k, p: events.append(p) if k == "event" else None)
    await m.start()
    rt.devices = m
    m.owner_label = rt.session_title  # start_devices 里接的那条线

    async def send(msg):
        pass

    server = AcpServer(rt, send)
    try:
        bid = next(iter(m.boards))
        with pytest.raises(RpcError, match="not connected"):
            await server.boards_reset({"boardId": "nope"})
        proj = tmp_path / "proj"
        proj.mkdir()
        s = rt.create_session(proj, backend=ScriptedBackend([say("ok")]), title="busy one")
        m.boards[bid].owner_session = s.id
        s.status = "running"
        with pytest.raises(RpcError, match="busy one"):
            await server.boards_reset({"boardId": bid})
        s.status = "idle"
        hub.board.fw = SimFirmware()
        hub.board.fw.flashed = True
        assert (await server.boards_reset({"boardId": bid}))["ok"]
        await wait_for(lambda: any(e.kind == "boot" for e in events))
    finally:
        await m.stop()
