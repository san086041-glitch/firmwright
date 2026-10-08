"""真机实测（2026-10-04）第 3 / 4 / 6 条的修复：

- 板子名：没认出芯片前不用系统语言的设备描述；认出芯片后按芯片命名（用户改过的名字不动）
- 串口在出数据但状态是 Offline → 自动纠正
- 板子被别的会话占着：错误里说清楚是谁；空闲时可以移过来，原会话收到说明
- chip_info：经过设备管理器让出串口，认出芯片
"""

from fakes import S3_BOOT, FakeAdapter, FakeSerial, board_port
from test_device import make_manager, wait_for

from firmwright.device.manager import BoardBusy
from firmwright.model.types import CancelToken
from firmwright.platform.base import PortInfo
from firmwright.services import Services
from firmwright.tools.base import ToolContext
from firmwright.tools.hw import ChipInfo, Empty, Flash, FlashArgs
from firmwright.trace import Trace


async def test_alias_offline_selfheal_and_transfer(tmp_path):
    ports = [PortInfo(device="COM3", vid=0x303A, pid=0x1001, serial_number="288485564744", description="USB 串行设备 (COM3)")]
    serials: dict[str, FakeSerial] = {}
    m = await make_manager(tmp_path, ports, serials)
    try:
        b = m.boards["usb-288485564744"]
        assert b.alias == "USB-JTAG board COM3"  # 不是"USB 串行设备 COM3"
        serials["COM3"].push(S3_BOOT)
        await wait_for(lambda: b.chip == "esp32s3")
        assert b.alias == "ESP32-S3 COM3"
        m.update_board(b.id, alias="My S3")
        m.set_chip(b, "esp32s3")
        assert b.alias == "My S3"  # 用户改过的名字不动

        # 不管怎么进入的 disconnected，只要串口还在出数据就纠正
        b.state = "disconnected"
        serials["COM3"].push("I (100) app: hello\n")
        await wait_for(lambda: b.state == "running")

        # 归属：错误里写清楚被哪个会话占着
        m.owner_label = {"s1": "test", "s2": "other project"}.get
        m.acquire(b.id, "s1")
        try:
            m.acquire(b.id, "s2")
            raise AssertionError("expected BoardBusy")
        except BoardBusy as e:
            assert 'session "test"' in str(e)
        try:
            m.transfer(b.id, "s2", from_busy=lambda sid: "is running")
            raise AssertionError("expected BoardBusy")
        except BoardBusy as e:
            assert "is running" in str(e)
        m.transfer(b.id, "s2", from_busy=lambda sid: None)
        assert b.owner_session == "s2"
    finally:
        await m.stop()


def test_reset_pulse_rewrites_dtr_after_each_rts_change():
    """Windows usbser.sys 只在 DTR 变化时发控制请求：每次改 RTS 后要再写一次 DTR，否则复位根本没发到板子上。"""
    from firmwright.device.manager import _pulse_reset

    class Rec:
        def __init__(self):
            self.log: list[tuple[str, bool]] = []
            self._dtr = self._rts = False

        @property
        def dtr(self):
            return self._dtr

        @dtr.setter
        def dtr(self, v):
            self._dtr = v
            self.log.append(("dtr", v))

        @property
        def rts(self):
            return self._rts

        @rts.setter
        def rts(self, v):
            self._rts = v
            self.log.append(("rts", v))

    s = Rec()
    _pulse_reset(s, usb_jtag=True)
    assert s.log == [("dtr", False), ("rts", True), ("dtr", False), ("rts", False), ("dtr", False)]


async def test_reset_retries_access_denied_and_post_flash_reset_never_raises(tmp_path):
    """回退重烧后复位打开 COM3 报"拒绝访问"（USB-JTAG 重新枚举中），整个回退被判失败（2026-10-05 真机）。"""
    from firmwright.device.manager import Board, DeviceManager

    opened: list[int] = []

    class Ser:
        dtr = rts = False

        def close(self):
            pass

    def opener(port, baud):
        opened.append(1)
        if len(opened) < 3:
            raise PermissionError(13, "Access is denied")
        return Ser()

    m = DeviceManager(FakeAdapter(), home=tmp_path, lister=lambda: [], opener=opener)
    m.boards["b1"] = Board(id="b1", alias="S3", port="COM3", usb_jtag=True)
    await m.reset("b1")  # 前两次拒绝访问，第三次成功
    assert len(opened) == 3

    def always_denied(port, baud):
        raise PermissionError(13, "Access is denied")

    m.opener = always_denied
    m.reset = lambda bid, wait=0.0: DeviceManager.reset(m, bid, wait=0.0)  # type: ignore[method-assign]
    why = await m.reset_after_flash("b1")
    assert why and "PermissionError" in why


async def test_session_rebinds_its_board_when_it_comes_back(tmp_path):
    """重启桌面端时板子正好不在（掉线 / 崩溃重启）：会话恢复后等板子连上自动绑回去（2026-10-05 真机）。"""
    import asyncio

    from firmwright.config import Config, WorktreeConfig
    from firmwright.model.fake import ScriptedBackend
    from firmwright.runtime import Runtime

    ports: list = []
    rt = Runtime(Config(worktree=WorktreeConfig(enabled=False)), home=tmp_path / "home")
    rt.platform = FakeAdapter()
    await rt.start_devices(lister=lambda: list(ports), opener=lambda p, b: FakeSerial(p, b), scan_interval=0.05)
    try:
        proj = tmp_path / "proj"
        proj.mkdir()
        s = rt.create_session(proj, backend=ScriptedBackend([]), session_id="abc123")
        # 界面（ACP 服务）是在运行时之后注册的监听者：它最后收到的状态必须是"已绑定"
        ui_owner: list = []
        rt.devices.listen(lambda kind, p: ui_owner.append(p.owner_session) if kind == "state" else None)
        rt.want_board("abc123", "usb-aabbccddeeff")
        assert s.board_id is None
        ports.append(board_port())  # 板子回来了
        await wait_for(lambda: s.board_id == "usb-aabbccddeeff")
        assert rt.devices.boards["usb-aabbccddeeff"].owner_session == "abc123"
        await asyncio.sleep(0.05)
        assert ui_owner[-1] == "abc123", ui_owner
        # 用户自己改了绑定：不再自动绑
        rt.bind_board("abc123", None)
        rt.want_board("abc123", "usb-aabbccddeeff")
        rt.bind_board("abc123", None)
        ports.clear()
        await asyncio.sleep(0.2)
        ports.append(board_port())
        await asyncio.sleep(0.3)
        assert s.board_id is None
    finally:
        await rt.stop()


class ChipAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.ports_seen: list[str] = []

    async def chip_info(self, ctx, port):
        self.ports_seen.append(port)
        return {"ok": True, "chip": "esp32s3", "description": "ESP32-S3 (QFN56) (revision v0.2)", "features": "WiFi",
                "mac": "28:84:85:56:47:44", "flash_size": "16MB", "crystal": "40MHz"}


async def test_chip_info_tool_and_no_board_message(tmp_path):
    ports = [board_port()]
    serials: dict[str, FakeSerial] = {}
    adapter = ChipAdapter()
    m = await make_manager(tmp_path, ports, serials, adapter)
    try:
        bid = "usb-aabbccddeeff"
        m.owner_label = lambda sid: "test"
        services = Services(platform=adapter, devices=m)

        def ctx(board_id):
            return ToolContext(session_id="s2", cwd=tmp_path, cancel=CancelToken(), trace=Trace(None, "s2"),
                               services=services, board_id=board_id)

        res = await ChipInfo().run(ctx(bid), Empty())
        assert not res.is_error and "MAC: 28:84:85:56:47:44" in res.text_content()
        assert adapter.ports_seen == ["COM7"] and m.boards[bid].chip == "esp32s3"
        assert bid in m.monitors  # 用完把串口接回来了

        # 没绑定板子：告诉 agent 板子在哪个会话手里
        m.acquire(bid, "s1")
        res = await Flash().run(ctx(None), FlashArgs())
        assert res.is_error and 'bound to session "test"' in res.text_content()
    finally:
        await m.stop()
