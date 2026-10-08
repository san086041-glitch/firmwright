"""run_process 读输出（2026-10-05）：esptool 用 \\r 原地刷新进度、整个读 flash 过程不换行，
readline() 遇到超过 64 KB 的"一行"抛 ValueError，真机 read_flash 因此失败。"""

import sys

from firmwright.osal import run_process

CHILD = r"""
import sys
for i in range(0, 101):
    sys.stdout.write(f"Writing ({i} %)\r"); sys.stdout.flush()
sys.stdout.write("\nDone\r\n\nA" + "x" * 200000 + "\nend")
"""


async def test_carriage_return_progress_and_huge_lines():
    seen: list[str] = []
    res = await run_process([sys.executable, "-c", CHILD], on_line=lambda _n, line: seen.append(line))
    assert res.code == 0
    lines = res.stdout.split("\n")
    # \r 刷新的进度在收集的输出里只留最后一段；空行保留；超长行完整
    assert lines[0] == "Writing (100 %)" and lines[1] == "Done" and lines[2] == ""
    assert len(lines[3]) == 200001 and lines[4] == "end"
    # 回调收到每一次进度（界面能实时显示百分比）
    assert sum(1 for s in seen if s.startswith("Writing (")) == 101
