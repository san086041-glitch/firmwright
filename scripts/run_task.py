"""不经过界面、直接驱动核心跑一个任务（I02：评测阶段的无界面批量运行入口）。

用法：
    core\\.venv\\Scripts\\python scripts\\run_task.py <工程目录> "<任务>" [--model deepseek-flash]
        [--board <板子 id 或 COM 口>] [--mode accept_edits] [--yes] [--human-auto]

--yes         所有"询问"级权限自动批准（禁止级仍然拒绝）
--human-auto  人工操作卡片在终端里提示，回车表示完成
输出：流式打印 agent 的回复和工具调用；结束后打印 trace.jsonl 的位置。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
# 开发期和桌面端共用仓库里的 dev-home 作为数据目录（见 desktop/src/main/sidecar.ts 的说明）
os.environ.setdefault("FIRMWRIGHT_HOME", str(Path(__file__).resolve().parents[1] / "dev-home"))

from firmwright.runtime import Runtime  # noqa: E402
from firmwright.tools.base import HumanAction, HumanReply  # noqa: E402

DIM, RESET, CYAN, YELLOW, RED, GREEN = "\033[2m", "\033[0m", "\033[36m", "\033[33m", "\033[31m", "\033[32m"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project", type=Path)
    ap.add_argument("task")
    ap.add_argument("--model")
    ap.add_argument("--board")
    ap.add_argument("--mode", default="accept_edits")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--human-auto", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    rt = Runtime()
    board_id = None
    if a.board:
        devices = await rt.start_devices()
        if devices is None:
            print("ESP-IDF is unavailable; boards cannot be used")
            return 2
        await asyncio.sleep(1.0)
        board_id = next((b.id for b in devices.boards.values() if a.board in (b.id, b.port)), None)
        if board_id is None:
            print(f"Board {a.board} not found; available: {[(b.id, b.port) for b in devices.boards.values()]}")
            return 2

    async def emit(u: dict) -> None:
        if a.quiet:
            return
        kind = u.get("sessionUpdate")
        if kind == "agent_message_chunk":
            print(u["content"]["text"], end="", flush=True)
        elif kind == "agent_thought_chunk":
            print(f"{DIM}{u['content']['text']}{RESET}", end="", flush=True)
        elif kind == "tool_call":
            print(f"\n{CYAN}▸ {u['title']}{RESET}", flush=True)
        elif kind == "tool_call_update" and u.get("status") in ("completed", "failed"):
            text = u["content"][0]["content"]["text"] if u.get("content") else ""
            color = GREEN if u["status"] == "completed" else RED
            first = "\n".join(text.splitlines()[:8])
            print(f"{color}  {first}{RESET}", flush=True)
        elif kind == "tool_call_update" and u.get("content"):
            print(f"{DIM}  … {u['content'][0]['content']['text']}{RESET}", flush=True)
        elif kind == "_fwr/injected":
            print(f"\n{YELLOW}⚡ injected: {u['text'][:300]}{RESET}", flush=True)

    async def ask_permission(req):
        if a.yes:
            print(f"{YELLOW}  [auto-approved] {req.title}: {req.reason}{RESET}")
            return "allow_once"
        try:
            ans = await asyncio.to_thread(input, f"{YELLOW}Allow {req.title}? ({req.reason}) [y/N/a=allow for this session] {RESET}")
        except EOFError:  # 非交互运行（没有 stdin）：一律拒绝
            return "reject"
        return {"y": "allow_once", "a": "allow_always"}.get(ans.strip().lower(), "reject")

    async def ask_human(action: HumanAction) -> HumanReply:
        if not a.human_auto:
            return HumanReply(done=False, note="running headless; manual steps are not possible")
        try:
            ans = await asyncio.to_thread(
                input, f"\n{YELLOW}🖐 {action.title}\n{action.instructions}\nPress Enter when done (type n if you cannot do it){RESET}")
        except EOFError:
            return HumanReply(done=False, note="no stdin")
        return HumanReply(done=ans.strip().lower() != "n", note=ans.strip())

    session = rt.create_session(a.project, model_id=a.model, board_id=board_id, mode=a.mode, emit=emit,
                                ask_permission=ask_permission, ask_human=ask_human)
    try:
        result = await session.prompt(a.task)
    finally:
        await rt.stop()
    print(f"\n\n{DIM}— finished: {result.stop_reason} · {result.steps} steps · tokens {json.dumps(result.usage)}"
          + (f" · error {result.error}" if result.error else "") + f"\n  trace: {session.trace.path}{RESET}")
    return 0 if result.stop_reason == "end_turn" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
