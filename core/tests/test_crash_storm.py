"""2026-10-05 真机：agent 故意做了每 10 秒崩溃一次的固件。

- 任务中：反复崩溃（60 秒内 3 次或 reboot_loop）后，同类崩溃不再一条条注入，最多每分钟一条汇总；平静 30 秒后恢复
- 空闲：同一块板子 5 分钟内只通知一次，下次通知带上期间的次数
"""

from test_device import FakeSession

from firmwright.device.events import DeviceEvent
from firmwright.device.router import EventRouter

KINDS = ["panic", "panic", "assert", "abort", "stack_overflow", "panic", "abort", "wdt_reset", "panic", "assert", "panic", "abort"]


class B:
    owner_session = "s1"
    idle_policy = None


def make(t, sess, notified=None):
    return EventRouter(get_board=lambda _: B, get_session=lambda _: sess,
                       notify=(notified if notified is not None else []).append, clock=lambda: t[0])


def test_crash_loop_is_summarized_while_a_task_runs():
    t = [0.0]
    sess = FakeSession()
    r = make(t, sess)
    for kind in KINDS:  # 每 10 秒崩溃一次，共 120 秒
        t[0] += 10
        r.route(DeviceEvent.make("b", kind, f"{kind} happened"))
    texts = [rb.text for rb, _ in sess.injected]
    # 前 3 次照常（第 3 次附说明"进入崩溃循环"），之后每分钟一条汇总：12 次崩溃只注入 4 条
    assert len(texts) == 4
    assert "crash-looping" in texts[2]
    assert texts[3].startswith("[device event] still crash-looping") and "event_id=" in texts[3]
    # 汇总不打断正在等待的工具；前面的单条崩溃照常打断
    assert [i for _, i in sess.injected] == [True, True, True, False]

    # 平静 30 秒以上后，新的崩溃照常注入，并带上上一次循环里没汇报的
    t[0] += 45
    r.route(DeviceEvent.make("b", "panic", "fresh crash"))
    last = sess.injected[-1][0].text
    assert "fresh crash" in last and "not reported individually" in last


def test_idle_notifications_are_throttled_per_board():
    t = [0.0]
    sess = FakeSession(running=False)
    notified: list[DeviceEvent] = []
    r = make(t, sess, notified)
    for _ in range(10):  # 空闲时每 10 秒崩溃一次
        t[0] += 10
        r.route(DeviceEvent.make("b", "panic", "crash"))
    assert len(notified) == 1
    t[0] += 300
    r.route(DeviceEvent.make("b", "panic", "crash"))
    assert len(notified) == 2 and notified[-1].detail["crashes_since_last_notice"] == 9
