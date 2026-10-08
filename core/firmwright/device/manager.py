"""设备管理器（I05，§4.4）。

- 发现板子：每秒扫描串口，用平台适配器按 USB 序列号认板子（D03），COM 口变了也认得出
- 串口监听独立于会话生命周期（I04 推论）：一直在后台读，写原始日志 + 偏移，喂给日志解析器
- 设备归属：同一时间一块板子最多属于一个会话
- 独占操作：烧录前暂停监听、释放串口，烧录后等 USB 重新枚举再恢复（§10 风险表）
- 事件分发：DeviceEvent 交给事件路由；串口内容每 100ms 合并一次推给界面
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel

from ..config import app_home
from ..facts import Facts
from ..platform.base import BoardIdentity, PortInfo
from .events import DeviceEvent, LogRef

BoardState = Literal["disconnected", "idle", "flashing", "running", "crashed", "busy"]


class Board(BaseModel):
    id: str  # D03：USB 序列号，稳定
    chip: str | None = None
    alias: str
    port: str | None = None  # 当前的 COM 口，会变
    state: BoardState = "idle"
    owner_session: str | None = None  # I05：同一时间最多属于一个会话
    idle_policy: Literal["ignore", "notify"] | None = None  # I04；None 表示跟随全局设置（D11）
    usb_jtag: bool = False
    # 2026-10-06 全系列：连接方式（usb_serial_jtag / usb_otg / uart_bridge / unknown）；芯片自带 USB 的烧录后会重新枚举
    link: str = "unknown"
    native_usb: bool = False
    mac: str | None = None  # chip_info 读到的 MAC（没有 USB 序列号的 UART 桥板子靠它认回来）
    stable_id: bool = True
    description: str = ""
    baud: int = 115200
    alias_custom: bool = False  # 用户自己改过名字；没改过的，认出芯片后自动改成按芯片命名


class BoardBusy(Exception):
    pass


class SerialLike(Protocol):
    in_waiting: int
    dtr: bool
    rts: bool

    def read(self, n: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...
    def close(self) -> None: ...


def open_serial(port: str, baud: int) -> SerialLike:
    import serial

    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = 0.05
    # 打开串口前先把 DTR / RTS 置为无效，否则打开时会把芯片复位（idf monitor 同样处理）
    s.dtr = False
    s.rts = False
    s.open()
    return cast("SerialLike", s)  # pyserial 没有类型标注


def list_ports() -> list[PortInfo]:
    from serial.tools import list_ports as lp

    out = []
    for p in lp.comports():
        out.append(PortInfo(device=p.device, vid=p.vid, pid=p.pid, serial_number=p.serial_number,
                            description=p.description or "", location=p.location))
    return out


Listener = Callable[[str, Any], None]  # (kind, payload)：kind = event / state / serial


class SerialMonitor:
    """一块板子的后台串口监听。读线程只搬字节，解析在事件循环里做（避免和别的状态竞争）。"""

    def __init__(self, mgr: DeviceManager, board: Board, parser, log_path: Path,
                 opener: Callable[[str, int], SerialLike]) -> None:
        self.mgr = mgr
        self.board = board
        self.parser = parser
        self.log_path = log_path
        self.opener = opener
        self.ser: SerialLike | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._buf = b""
        self._buf_since = 0.0
        self._chunk: list[str] = []
        self.loop = asyncio.get_running_loop()
        self.lines: deque[tuple[float, str]] = deque(maxlen=2000)  # 最近的行，给 await_marker 用

    def start(self) -> None:
        self.ser = self.opener(self.board.port or "", self.board.baud)
        self._thread = threading.Thread(target=self._run, name=f"serial-{self.board.id}", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread:
            await asyncio.to_thread(self._thread.join, 2)
        if self.ser:
            with contextlib.suppress(Exception):
                self.ser.close()
        self.ser = None
        self._flush_partial(force=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ser = self.ser
                if ser is None:
                    break
                n = getattr(ser, "in_waiting", 0) or 1
                data = ser.read(n)
            except Exception as e:  # 拔线：SerialException / OSError
                self.loop.call_soon_threadsafe(self.mgr._monitor_died, self.board.id, repr(e))
                return
            if data:
                self.loop.call_soon_threadsafe(self._on_bytes, data)

    # ---- 以下在事件循环线程里执行

    def _on_bytes(self, data: bytes) -> None:
        if not self._buf:
            self._buf_since = time.monotonic()
        self._buf += data
        while True:
            i = self._buf.find(b"\n")
            if i < 0:
                break
            raw, self._buf = self._buf[: i + 1], self._buf[i + 1 :]
            self._line(raw)
            self._buf_since = time.monotonic()

    def _flush_partial(self, force: bool = False) -> None:
        if self._buf and (force or time.monotonic() - self._buf_since > 0.5):
            raw, self._buf = self._buf, b""
            self._line(raw)

    def _line(self, raw: bytes) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as f:
            start = f.tell()
            f.write(raw)
            end = f.tell()
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        self.lines.append((time.monotonic(), text))
        self._chunk.append(text)
        self.mgr._on_line(self.board.id, text, LogRef(board_id=self.board.id, file=str(self.log_path),
                                                       start=start, end=end))
        for ev in self.parser.feed(text, start, end):
            self.mgr._on_event(ev)

    def tick(self) -> None:
        self._flush_partial()
        for ev in self.parser.tick():
            self.mgr._on_event(ev)
        if self._chunk:
            text = "\n".join(self._chunk)
            self._chunk = []
            self.mgr._notify("serial", {"board_id": self.board.id, "text": text})


class DeviceManager:
    def __init__(
        self,
        adapter,
        *,
        home: Path | None = None,
        lister: Callable[[], list[PortInfo]] = list_ports,
        opener: Callable[[str, int], SerialLike] = open_serial,
        scan_interval: float = 1.0,
        facts_for: Callable[[Board], Facts | None] | None = None,
    ) -> None:
        self.adapter = adapter
        self.home = home or app_home()
        self.lister = lister
        self.opener = opener
        self.scan_interval = scan_interval
        self.facts_for = facts_for or (lambda b: None)
        self.boards: dict[str, Board] = {}
        self.monitors: dict[str, SerialMonitor] = {}
        self.events: dict[str, DeviceEvent] = {}  # id → 事件（diagnose_crash 用）
        self.recent: dict[str, deque[DeviceEvent]] = {}
        self._listeners: list[Listener] = []
        self._paused: set[str] = set()
        self._excl: dict[str, asyncio.Lock] = {}
        self._tasks: list[asyncio.Task] = []
        self._line_waiters: list[Callable[[str, str, LogRef], None]] = []
        self.marks: dict[str, float] = {}  # 板子 → 最近一次烧录 / 复位的 monotonic 时间（await_marker 从这里开始看）
        self._known = self._load_known()
        # 扫描不能并发：两次扫描交错时（一个刚停掉监听、另一个又启动了），板子会停在 Offline 但串口还在出数据
        # （2026-10-04 真机实测第 3 条）
        self._scan_lock = asyncio.Lock()
        self.owner_label: Callable[[str], str] = lambda sid: sid  # 会话 id → 界面上的标题（runtime 设置）

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        await self.scan()
        self._tasks.append(asyncio.create_task(self._scan_loop()))
        self._tasks.append(asyncio.create_task(self._tick_loop()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for m in list(self.monitors.values()):
            await m.stop()
        self.monitors.clear()

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(self.scan_interval)
            try:
                await self.scan()
            except Exception as e:  # 扫描出错不能让管理器死掉
                self._notify("error", {"where": "scan", "message": repr(e)})

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(0.1)  # 串口内容每 100ms 合并发一次（_fwr/serial/chunk）
            for m in list(self.monitors.values()):
                m.tick()

    # ------------------------------------------------------------------ 订阅

    def listen(self, fn: Listener) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    def _notify(self, kind: str, payload: Any) -> None:
        for fn in list(self._listeners):
            try:
                fn(kind, payload)
            except Exception:
                pass

    def _on_event(self, ev: DeviceEvent) -> None:
        self.events[ev.id] = ev
        if len(self.events) > 5000:
            for k in list(self.events)[:1000]:
                self.events.pop(k, None)
        self.recent.setdefault(ev.board_id, deque(maxlen=200)).append(ev)
        b = self.boards.get(ev.board_id)
        if b:
            if ev.kind == "boot" and ev.detail.get("chip") and not b.chip:
                self.set_chip(b, ev.detail["chip"])
            new_state = b.state
            if ev.severity == "critical" and ev.kind not in ("wdt_reset",) or ev.kind == "reboot_loop":
                new_state = "crashed"
            elif ev.kind == "marker" and ev.detail.get("marker") == "boot_banner":
                new_state = "running"
            elif ev.kind == "download_mode":
                new_state = "busy"
            if new_state != b.state and b.state not in ("flashing", "disconnected"):
                b.state = new_state
                self._notify("state", b.model_copy())
        self._notify("event", ev)

    def _on_line(self, board_id: str, text: str, ref: LogRef) -> None:
        b = self.boards.get(board_id)
        if b and b.state == "disconnected" and board_id in self.monitors:
            # 串口在出数据，板子显然连着：状态纠正回来（不管是怎么进入 disconnected 的）
            b.state = "running"
            self._notify("state", b.model_copy())
        for w in list(self._line_waiters):
            w(board_id, text, ref)

    @contextlib.contextmanager
    def watch_lines(self, fn: Callable[[str, str, LogRef], None]):
        self._line_waiters.append(fn)
        try:
            yield
        finally:
            self._line_waiters.remove(fn)

    # ------------------------------------------------------------------ 发现

    def _load_known(self) -> dict[str, dict]:
        p = self.home / "boards.json"
        if p.is_file():
            try:
                return json.loads(p.read_text("utf-8"))
            except ValueError:
                return {}
        return {}

    def _remember(self, b: Board) -> None:
        self._known[b.id] = {"alias": b.alias, "chip": b.chip, "last_port": b.port, "idle_policy": b.idle_policy,
                             "alias_custom": b.alias_custom, "mac": b.mac}
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "boards.json").write_text(json.dumps(self._known, ensure_ascii=False, indent=1), "utf-8")

    def update_board(self, board_id: str, **fields) -> Board:
        b = self.boards[board_id]
        for k, v in fields.items():
            setattr(b, k, v)
        if "alias" in fields:
            b.alias_custom = True
        self._remember(b)
        self._notify("state", b.model_copy())
        return b

    def set_chip(self, b: Board, chip: str) -> None:
        """认出芯片（启动日志 / chip_info）：记下来；名字没被用户改过的，改成按芯片命名。"""
        b.chip = chip
        if not b.alias_custom:
            b.alias = _chip_alias(chip, b.port)
        self._remember(b)
        self._notify("state", b.model_copy())

    def set_mac(self, b: Board, mac: str) -> None:
        """chip_info 读到 MAC。没有可靠 USB 序列号的板子（CH340 等，id 是 port-comX）换了 USB 口就会变成"新板子"：
        这里按 MAC 找回以前记下的同一块板子，继承用户起的名字、芯片和空闲策略。"""
        mac = mac.lower()
        b.mac = mac
        if not b.stable_id:
            old = next((v for k, v in self._known.items() if k != b.id and (v.get("mac") or "").lower() == mac), None)
            if old:
                if old.get("alias_custom") and not b.alias_custom:
                    b.alias, b.alias_custom = old["alias"], True
                b.chip = b.chip or old.get("chip")
                b.idle_policy = b.idle_policy or old.get("idle_policy")
        self._remember(b)
        self._notify("state", b.model_copy())

    async def scan(self) -> None:
        async with self._scan_lock:
            await self._scan()

    async def _scan(self) -> None:
        ports = await asyncio.to_thread(self.lister)
        seen: dict[str, tuple[PortInfo, BoardIdentity]] = {}
        for p in ports:
            ident = self.adapter.identify(p)
            if ident:
                seen[ident.id] = (p, ident)
        for bid, (p, ident) in seen.items():
            b = self.boards.get(bid)
            if b is None:
                known = self._known.get(bid, {})
                chip = known.get("chip") or ident.chip_hint
                custom = bool(known.get("alias_custom"))
                alias = known.get("alias") if custom else _chip_alias(chip, p.device) if chip else _alias(p, ident)
                b = Board(id=bid, alias=alias or _alias(p, ident), chip=chip, port=p.device, usb_jtag=ident.usb_jtag,
                          link=ident.link, native_usb=ident.native_usb or ident.usb_jtag, mac=known.get("mac"),
                          stable_id=ident.stable, description=p.description, idle_policy=known.get("idle_policy"),
                          alias_custom=custom)
                self.boards[bid] = b
                self._remember(b)
                self._notify("state", b.model_copy())
            elif b.state == "disconnected" or b.port != p.device:
                old = b.port
                b.port = p.device
                if b.state == "disconnected":
                    b.state = "idle"
                    if bid not in self._paused:
                        self._on_event(DeviceEvent.make(bid, "reconnect", f"Board reconnected ({old} → {p.device})"))
                self._remember(b)
                self._notify("state", b.model_copy())
            if bid not in self.monitors and bid not in self._paused and b.state != "disconnected":
                self._start_monitor(b)
        for bid, b in self.boards.items():
            if bid not in seen and b.state != "disconnected" and bid not in self._paused:
                await self._stop_monitor(bid)
                b.state = "disconnected"
                self._notify("state", b.model_copy())
                self._on_event(DeviceEvent.make(bid, "disconnect", f"Board disconnected ({b.port})"))

    def _log_path(self, board_id: str) -> Path:
        return self.home / "serial" / board_id / f"{datetime.now():%Y%m%d}.log"

    def _start_monitor(self, b: Board) -> None:
        facts = self.facts_for(b)
        if facts and facts.baud:
            b.baud = facts.baud
        log = self._log_path(b.id)
        parser = self.adapter.log_parser(b.id, b.chip, facts, str(log))
        mon = SerialMonitor(self, b, parser, log, self.opener)
        try:
            mon.start()
        except Exception as e:  # 被别的程序占用
            b.state = "busy"
            self._notify("state", b.model_copy())
            self._notify("error", {"where": "open", "board_id": b.id, "message": repr(e)})
            return
        self.monitors[b.id] = mon

    async def _stop_monitor(self, board_id: str) -> None:
        m = self.monitors.pop(board_id, None)
        if m:
            await m.stop()

    def _monitor_died(self, board_id: str, why: str) -> None:
        m = self.monitors.pop(board_id, None)
        if m:
            asyncio.ensure_future(m.stop())
        # 下一次扫描会判断是断开还是重新枚举

    def refresh_parser(self, board_id: str) -> None:
        """facts.toml 变了 / 板子换了归属后，重建解析器（标记规则来自工程的事实卡）。"""
        m = self.monitors.get(board_id)
        b = self.boards.get(board_id)
        if m and b:
            m.parser = self.adapter.log_parser(b.id, b.chip, self.facts_for(b), str(m.log_path))

    # ------------------------------------------------------------------ 归属

    def acquire(self, board_id: str, session_id: str) -> Board:
        b = self.boards.get(board_id)
        if b is None:
            raise KeyError(f"No such board: {board_id}")
        if b.owner_session and b.owner_session != session_id:
            raise BoardBusy(f"Board {b.alias} is in use by {self.owner_text(b)}")
        b.owner_session = session_id
        self.refresh_parser(board_id)
        self._notify("state", b.model_copy())
        return b

    def owner_text(self, b: Board) -> str:
        return f'session "{self.owner_label(b.owner_session)}"' if b.owner_session else "no session"

    def transfer(self, board_id: str, to_session: str, *, from_busy: Callable[[str], str | None]) -> Board:
        """把板子从别的会话移到 to_session（2026-10-05 决定 3）。条件：原会话没有在执行、板子没有在烧录。
        from_busy(会话 id) 返回原会话不能放手的原因（None = 可以）。"""
        b = self.boards.get(board_id)
        if b is None:
            raise KeyError(f"No such board: {board_id}")
        if b.state == "flashing" or (self._excl.get(board_id) and self._excl[board_id].locked()):
            raise BoardBusy(f"Board {b.alias} is being flashed; try again when it finishes")
        if b.owner_session and b.owner_session != to_session:
            why = from_busy(b.owner_session)
            if why:
                raise BoardBusy(f"Board {b.alias} is in use by {self.owner_text(b)}, which {why}; stop it first")
        b.owner_session = to_session
        self.refresh_parser(board_id)
        self._notify("state", b.model_copy())
        return b

    def release(self, board_id: str, session_id: str) -> None:
        b = self.boards.get(board_id)
        if b and b.owner_session == session_id:
            b.owner_session = None
            self._notify("state", b.model_copy())

    @contextlib.asynccontextmanager
    async def exclusive(self, board_id: str, *, state: BoardState = "flashing") -> AsyncIterator[Board]:
        """独占串口：暂停监听 → 交出串口 → 恢复监听。S3 原生 USB 烧录后会重新枚举，等它回来。"""
        lock = self._excl.setdefault(board_id, asyncio.Lock())
        async with lock:
            b = self.boards[board_id]
            self._paused.add(board_id)
            await self._stop_monitor(board_id)
            prev = b.state
            b.state = state
            self._notify("state", b.model_copy())
            try:
                yield b
            finally:
                deadline = time.monotonic() + (8 if b.usb_jtag or b.native_usb else 2)
                while time.monotonic() < deadline:
                    await self.scan()
                    if any(self.adapter.identify(p) and self.adapter.identify(p).id == board_id
                           for p in await asyncio.to_thread(self.lister)):
                        break
                    await asyncio.sleep(0.3)
                self._paused.discard(board_id)
                b.state = "idle" if b.state in (state,) else prev
                await self.scan()
                if board_id not in self.monitors and b.state != "disconnected":
                    self._start_monitor(b)
                self._notify("state", b.model_copy())

    # ------------------------------------------------------------------ 操作

    async def reset(self, board_id: str, *, wait: float = 3.0) -> None:
        """RTS 拉低 100ms 复位芯片（EN 引脚）；DTR 保持高，不进下载模式。
        烧录刚结束时 USB-JTAG 在重新枚举、或监听线程刚死还没放手，打开串口会"拒绝访问"
        （2026-10-05 真机：回退重烧后复位报 PermissionError，整个回退被判失败）：wait 秒内重试。"""
        b = self.boards[board_id]
        deadline = time.monotonic() + wait
        while True:
            try:
                m = self.monitors.get(board_id)
                if m and m.ser:
                    await asyncio.to_thread(_pulse_reset, m.ser, b.usb_jtag or b.native_usb)
                    return
                ser = await asyncio.to_thread(self.opener, b.port or "", b.baud)
                try:
                    await asyncio.to_thread(_pulse_reset, ser, b.usb_jtag or b.native_usb)
                finally:
                    ser.close()
                return
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.3)

    async def reset_after_flash(self, board_id: str) -> str | None:
        """烧录成功后补一次复位（抓完整的启动日志）。只是锦上添花：失败不影响烧录结果，返回失败原因。"""
        try:
            await self.reset(board_id)
            return None
        except Exception as e:
            self._notify("error", {"where": "reset", "board_id": board_id, "message": repr(e)})
            return f"{type(e).__name__}: {e}"

    def read_log(self, board_id: str, ref: LogRef | None = None, *, grep: str | None = None,
                 tail: int = 200, max_bytes: int = 256_000) -> str:
        if ref:
            p = Path(ref.file)
            if not p.is_file():
                return "(the log file does not exist)"
            with p.open("rb") as f:
                f.seek(max(0, ref.start))
                data = f.read(min(ref.end - ref.start, max_bytes))
            return data.decode("utf-8", "replace")
        p = self._log_path(board_id)
        if not p.is_file():
            return "(no serial log yet)"
        size = p.stat().st_size
        with p.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            text = f.read().decode("utf-8", "replace")
        lines = text.splitlines()
        if grep:
            rx = re.compile(grep)
            lines = [line for line in lines if rx.search(line)]
        return "\n".join(lines[-tail:])

    def event(self, event_id: str) -> DeviceEvent | None:
        return self.events.get(event_id)

    def latest_crash(self, board_id: str) -> DeviceEvent | None:
        for ev in reversed(self.recent.get(board_id, ())):
            if ev.kind in ("panic", "abort", "assert", "stack_overflow", "stack_smash"):
                return ev
        return None


def _pulse_reset(ser: SerialLike, usb_jtag: bool = False) -> None:
    """拉 RTS（EN）复位，DTR 保持无效（不进下载模式）。和 esptool 的 HardReset 一样：

    - Windows 的 usbser.sys（ESP32-S3 / P4 的 USB-Serial-JTAG、CDC 类转接芯片）只在 DTR 变化时才把
      SET_CONTROL_LINE_STATE 发给设备，单独改 RTS 根本不会送到板子上。所以每次改 RTS 后再写一次 DTR。
      真机实测（2026-10-05）：原来的写法复位不了，uptime 一直在涨。
    - USB-Serial-JTAG 的复位脉冲 esptool 用 200 ms，UART 转接用 100 ms。
    """
    def set_rts(v: bool) -> None:
        ser.rts = v
        ser.dtr = ser.dtr  # usbser.sys：写一次 DTR，让新的 RTS 跟着控制请求一起发出去

    ser.dtr = False
    set_rts(True)
    time.sleep(0.2 if usb_jtag else 0.1)
    set_rts(False)
    if usb_jtag:
        time.sleep(0.2)


def _alias(p: PortInfo, ident: BoardIdentity | None = None) -> str:
    """认出芯片之前的名字。Windows 的设备描述是系统语言的（"USB 串行设备"），USB-Serial-JTAG 直接叫 USB-JTAG。"""
    if ident and ident.usb_jtag:
        return f"USB-JTAG board {p.device}"
    desc = p.description.split("(")[0].strip()
    if not desc or not desc.isascii():
        desc = "Serial board"
    return f"{desc} {p.device}"


def chip_display_name(chip: str) -> str:
    """esp32 → ESP32，esp32c61 → ESP32-C61（所有 ESP32 系列同一个规则，不用逐个列）。"""
    c = chip.lower()
    if c.startswith("esp32") and len(c) > 5:
        return "ESP32-" + c[5:].upper()
    return chip.upper()


def _chip_alias(chip: str, port: str | None) -> str:
    name = chip_display_name(chip)
    return f"{name} {port}" if port else name
