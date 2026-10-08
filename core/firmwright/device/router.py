"""事件路由（I04，§4.4）：任务中 → 注入 agent 循环；空闲 → 按设置忽略或通知界面。

去抖和限流参照 grok monitor（200ms 合并、令牌桶：初始 10 个、每 2 秒补 1 个），
但超限时把多出来的事件合并成一条汇总，不像 grok 那样持续超限 30 秒就把监听停掉——
串口日志是设备的"生命体征"，不能因为刷屏就不看了。
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from ..model.types import ReminderBlock
from .events import DeviceEvent

CRASH_KINDS = {"panic", "abort", "assert", "stack_overflow", "stack_smash", "reboot_loop", "wdt_reset", "brownout"}


@dataclass
class Route:
    action: Literal["inject", "notify", "drop", "suppressed"]
    session_id: str | None = None
    reason: str = ""


@dataclass
class _Bucket:
    tokens: float = 10.0
    last: float = field(default_factory=time.monotonic)
    suppressed: Counter = field(default_factory=Counter)
    last_key: tuple | None = None
    last_at: float = 0.0


@dataclass
class _Storm:
    """一块板子的"崩溃风暴"状态（2026-10-05）：反复崩溃重启时，同类事件合并成定期汇总再交给 agent。
    真机：agent 故意做了每 10 秒崩溃一次的固件，110 秒里十几条几乎一样的崩溃事件都塞进了上下文。"""

    recent: deque = field(default_factory=deque)  # 最近的崩溃时刻
    active: bool = False
    last_crash: float = 0.0
    last_report: float = 0.0
    pending: Counter = field(default_factory=Counter)  # 还没汇报的崩溃（按类型计数）
    examples: dict = field(default_factory=dict)  # 类型 → (摘要, 事件 id)，汇总里每类给一个例子


class EventRouter:
    CAPACITY = 10.0
    REFILL_PER_S = 0.5  # 每 2 秒补 1 个
    MERGE_S = 0.2
    STORM_WINDOW_S = 60.0  # 60 秒内崩溃 3 次（或出现 reboot_loop）→ 进入风暴状态
    STORM_MIN = 3
    STORM_QUIET_S = 30.0  # 30 秒没有新的崩溃 → 风暴结束
    STORM_REPORT_S = 60.0  # 风暴中最多每分钟给 agent 一条汇总
    NOTIFY_COOLDOWN_S = 300.0  # 空闲时同一块板子 5 分钟内只通知一次

    def __init__(
        self,
        *,
        get_board: Callable[[str], object | None],
        get_session: Callable[[str], Any],  # 会话（有 running / inject）；测试里用替身
        notify: Callable[[DeviceEvent], None],
        global_policy: Callable[[], str] = lambda: "notify",
        clock: Callable[[], float] = time.monotonic,
        raw_log: Callable[[DeviceEvent], str] | None = None,
    ) -> None:
        self.raw_log = raw_log  # context.log_digest 关掉时给：事件 → 串口原文
        self.get_board = get_board
        self.get_session = get_session
        self.notify = notify
        self.global_policy = global_policy
        self.clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._storms: dict[str, _Storm] = {}
        self._last_notify: dict[str, float] = {}
        self._muted_notifies: Counter = Counter()  # 冷却期内没通知的崩溃次数（下次通知时带上）
        self.log: list[tuple[str, Route]] = []  # 最近的路由决定（调试 / trace）

    def _storm_step(self, ev: DeviceEvent) -> tuple[str, str]:
        """崩溃类事件在任务中的处理。返回 (动作, 文字)：
        ("deliver", 附加说明) 照常注入；("summary", 汇总文字) 注入汇总代替原文；("hold", "") 先攒着。"""
        st = self._storms.setdefault(ev.board_id, _Storm())
        now = self.clock()
        if st.active and now - st.last_crash > self.STORM_QUIET_S:
            st.active = False  # 平静了一段时间：这次崩溃按新的一次处理（攒着的计数在下一条汇总里带上）
        st.last_crash = now
        st.recent.append(now)
        while st.recent and now - st.recent[0] > self.STORM_WINDOW_S:
            st.recent.popleft()
        if not st.active:
            if st.pending:  # 上一次风暴里还没汇报的：顺带说一句，不丢
                earlier = ", ".join(f"{k}×{n}" for k, n in st.pending.most_common())
                st.pending.clear()
                st.examples.clear()
                note = f"(During the previous crash loop, these crashes were not reported individually: {earlier}.)"
                if ev.kind == "reboot_loop" or len(st.recent) >= self.STORM_MIN:
                    st.active, st.last_report = True, now
                return "deliver", note
            if ev.kind == "reboot_loop" or len(st.recent) >= self.STORM_MIN:
                st.active, st.last_report = True, now
                return "deliver", ("(The board is crash-looping. Further crashes are summarized at most once a minute "
                                   "instead of one reminder each; the device's event list and read_log have every one.)")
            return "deliver", ""
        st.pending[ev.kind] += 1
        st.examples[ev.kind] = (ev.summary, ev.id)
        if now - st.last_report < self.STORM_REPORT_S:
            return "hold", ""
        st.last_report = now
        n = sum(st.pending.values())
        lines = [f"[device event] still crash-looping · board {ev.board_id} · {n} more crash"
                 f"{'' if n == 1 else 'es'} since the last report:"]
        for kind, cnt in st.pending.most_common():
            summary, eid = st.examples[kind]
            lines.append(f"- {kind} ×{cnt}: {summary} (latest event_id={eid})")
        lines.append("Use diagnose_crash(event_id=...) for any of them. To stop the loop, flash firmware that does not crash.")
        st.pending.clear()
        st.examples.clear()
        return "summary", "\n".join(lines)

    def _admit(self, ev: DeviceEvent) -> tuple[bool, str | None]:
        """限流。返回 (是否放行, 要附带的汇总文字)。"""
        b = self._buckets.setdefault(ev.board_id, _Bucket(tokens=self.CAPACITY, last=self.clock()))
        now = self.clock()
        b.tokens = min(self.CAPACITY, b.tokens + (now - b.last) * self.REFILL_PER_S)
        b.last = now
        key = (ev.kind, ev.summary)
        if b.last_key == key and now - b.last_at < self.MERGE_S:
            b.suppressed[ev.kind] += 1
            return False, None
        b.last_key, b.last_at = key, now
        if b.tokens < 1:
            b.suppressed[ev.kind] += 1
            return False, None
        b.tokens -= 1
        summary = None
        if b.suppressed:
            parts = ", ".join(f"{k}×{n}" for k, n in b.suppressed.most_common())
            summary = (f"(earlier, {sum(b.suppressed.values())} events were merged because there were too many: "
                       f"{parts}; use read_log for details)")
            b.suppressed.clear()
        return True, summary

    def route(self, ev: DeviceEvent) -> Route:
        # info 级（启动、标记、重连）只给界面和 await_marker，不打扰 agent 循环
        if ev.severity == "info":
            return self._record(ev, Route("drop", reason="info events are only shown in the UI"))
        ok, summary = self._admit(ev)
        if not ok:
            return self._record(ev, Route("suppressed", reason="rate-limited / duplicate within 200 ms"))
        board = self.get_board(ev.board_id)
        owner = getattr(board, "owner_session", None) if board else None
        session = self.get_session(owner) if owner else None
        if session is not None and getattr(session, "running", False):
            action, extra = self._storm_step(ev) if ev.kind in CRASH_KINDS else ("deliver", "")
            if action == "hold":
                return self._record(ev, Route("suppressed", owner, "crash loop; merged into the next periodic summary"))
            if action == "summary":
                session.inject(ReminderBlock(source="device_event", text=extra + (f"\n{summary}" if summary else ""),
                                             event_id=ev.id), interrupt=False)
                return self._record(ev, Route("inject", owner, "crash loop; periodic summary injected"))
            text = ev.reminder_text(self.raw_log(ev) if self.raw_log else None) + (f"\n{extra}" if extra else "") + (f"\n{summary}" if summary else "")
            session.inject(ReminderBlock(source="device_event", text=text, event_id=ev.id),
                           interrupt=ev.severity == "critical")
            return self._record(ev, Route("inject", owner, "a task is running; injected into the agent loop"))
        if ev.kind not in CRASH_KINDS:
            return self._record(ev, Route("drop", reason="when idle, only crash-type events matter"))
        policy = getattr(board, "idle_policy", None) or self.global_policy()
        if policy == "notify":
            # 同一块板子 5 分钟内只通知一次（板子反复崩溃时系统通知会一直弹）；下次通知带上期间的次数
            now = self.clock()
            last = self._last_notify.get(ev.board_id)
            if last is not None and now - last < self.NOTIFY_COOLDOWN_S:
                self._muted_notifies[ev.board_id] += 1
                return self._record(ev, Route("suppressed", owner, "idle; already notified about this board recently"))
            self._last_notify[ev.board_id] = now
            if muted := self._muted_notifies.pop(ev.board_id, 0):
                ev.detail["crashes_since_last_notice"] = muted
            self.notify(ev)
            return self._record(ev, Route("notify", owner, "idle; notifying the user per settings (no automatic handling, I04)"))
        return self._record(ev, Route("drop", owner, "idle; ignored per settings"))

    def _record(self, ev: DeviceEvent, r: Route) -> Route:
        self.log.append((ev.id, r))
        if len(self.log) > 500:
            del self.log[:100]
        return r
