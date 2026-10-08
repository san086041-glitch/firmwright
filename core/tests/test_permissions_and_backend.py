import json
from pathlib import Path

import httpx

from firmwright.config import PermissionConfig
from firmwright.model.openai_compat import OpenAICompatBackend, to_openai_messages
from firmwright.model.types import (
    CancelToken,
    ImageBlock,
    Message,
    ModelCaps,
    ModelRequest,
    ReminderBlock,
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
)
from firmwright.permissions.engine import PermissionEngine, RiskRule
from firmwright.permissions.ps_parse import analyze_cached
from firmwright.tools.fs import EditFile, ReadFile
from firmwright.tools.shell import Shell, ShellArgs

CWD = Path("D:/proj")


def shell_decision(cmd: str, engine: PermissionEngine | None = None, mode="default"):
    engine = engine or PermissionEngine()
    return engine.evaluate(Shell(), ShellArgs(command=cmd), mode=mode, cwd=CWD, ps=analyze_cached(cmd))


def test_shell_read_only_allowed_and_writes_ask():
    assert shell_decision("git status; Get-ChildItem *.c | Select-String foo").action == "allow"
    assert shell_decision("Remove-Item -Recurse build").action == "ask"
    assert shell_decision("echo hi > out.txt").action == "ask"  # 重定向写文件
    assert shell_decision("iex $x").action == "ask"  # 无法分析


def test_shell_prefix_rules():
    e = PermissionEngine(PermissionConfig(allow=["Bash(git commit:*)"], deny=["Bash(git push:*)"]))
    assert shell_decision("git commit -m x", e).action == "allow"
    assert shell_decision("git status; git push origin main", e).action == "deny"
    # 每个子命令都要被 allow 覆盖
    assert shell_decision("git commit -m x; Remove-Item a", e).action == "ask"


def test_forbidden_cannot_be_loosened():
    rr = [RiskRule("shell", r"\bespefuse", "forbidden", "eFuse 不可逆")]
    e = PermissionEngine(PermissionConfig(allow=["Bash"]), risk_rules=rr)
    d = shell_decision("espefuse.py burn_efuse X", e, mode="always_approve")
    assert d.action == "deny" and d.risk == "forbidden"


def test_dangerous_asks_even_in_always_approve():
    rr = [RiskRule("shell", r"erase[_-]flash", "dangerous", "整片擦除")]
    e = PermissionEngine(risk_rules=rr)
    assert shell_decision("idf.py erase-flash", e, mode="always_approve").action == "ask"
    e2 = PermissionEngine(PermissionConfig(allow=["Bash(idf.py erase-flash)"]), risk_rules=rr)
    assert shell_decision("idf.py erase-flash", e2).action == "allow"


def test_edit_modes_and_outside_cwd():
    e = PermissionEngine()
    a = EditFile.Args(path="main.c", old_string="a", new_string="b")
    assert e.evaluate(EditFile(), a, mode="default", cwd=CWD).action == "ask"
    assert e.evaluate(EditFile(), a, mode="accept_edits", cwd=CWD).action == "allow"
    out = EditFile.Args(path="C:/Windows/x.c", old_string="a", new_string="b")
    assert e.evaluate(EditFile(), out, mode="always_approve", cwd=CWD).action == "ask"
    assert e.evaluate(ReadFile(), ReadFile.Args(path="C:/x"), mode="default", cwd=CWD).action == "allow"


def test_message_conversion():
    req = ModelRequest(model="m", system="sys", messages=[
        Message(role="user", content=[ReminderBlock(source="rules", text="R"), TextBlock(text="hi")]),
        Message(role="assistant", content=[TextBlock(text="t"), ToolCallBlock(id="c1", name="read_file",
                                                                                arguments={"path": "a"})]),
        Message(role="tool", content=[ToolResultBlock(call_id="c1", name="read_file", content=[
            TextBlock(text="img:"), ImageBlock(media_type="image/png", data="AAA", alt="a.png")])]),
    ])
    msgs = to_openai_messages(req, ModelCaps(vision=False))
    assert "<system-reminder" in msgs[1]["content"] and msgs[1]["content"].endswith("hi")
    assert msgs[2]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
    assert msgs[3]["role"] == "tool" and msgs[3]["tool_call_id"] == "c1"
    assert "no vision" in msgs[3]["content"] and len(msgs) == 4  # 图片降级为文字（I13）
    msgs_v = to_openai_messages(req, ModelCaps(vision=True))
    assert msgs_v[4]["content"][0]["type"] == "image_url"


def _sse(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


async def test_sse_stream_tool_calls_and_retry():
    hits = {"n": 0}
    body = _sse([
        {"choices": [{"delta": {"content": "我来读"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "read_file",
                                                                                     "arguments": "{\"pa"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"a.c\"}"}}]},
                      "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 9,
                                  "prompt_tokens_details": {"cached_tokens": 64}}},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, text="slow down")
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    be = OpenAICompatBackend("http://x/v1", "k", transport=httpx.MockTransport(handler))
    req = ModelRequest(model="m", system="s", messages=[Message(role="user", content=[TextBlock(text="hi")])])
    evs = [e async for e in be.stream(req, cancel=CancelToken())]
    kinds = [e.kind for e in evs]
    assert hits["n"] == 2
    done = next(e for e in evs if e.kind == "tool_call_done")
    assert done.call.arguments == {"path": "a.c"}
    usage = next(e for e in evs if e.kind == "usage")
    assert usage.cached_tokens == 64
    assert kinds[-1] == "stop" and evs[-1].reason == "tool_calls"
