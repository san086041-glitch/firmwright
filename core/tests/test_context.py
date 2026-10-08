"""W6 上下文工程：压缩、skill、记忆、PDF、MCP。"""

import sys
from pathlib import Path

from firmwright.config import McpConfig
from firmwright.context import compaction, pdf
from firmwright.context.memory import MemoryStore
from firmwright.context.skills import SkillCatalog, discover, parse_frontmatter
from firmwright.mcp.client import McpServerConfig
from firmwright.mcp.tools import Bm25, McpManager, tokenize
from firmwright.model.fake import ScriptedBackend, call, say
from firmwright.model.types import Message, ModelCaps, ModelError, ReminderBlock, TextBlock, ToolResultBlock
from firmwright.session.agent import Session
from firmwright.session.store import SessionStore
from firmwright.tools.base import ToolRegistry
from firmwright.tools.fs import ReadFile

HERE = Path(__file__).parent
REPO = HERE.parents[1]


def text_of(msg: Message) -> str:
    return "\n".join(b.text for b in msg.content if isinstance(b, TextBlock | ReminderBlock))


# ---------------------------------------------------------------------- 压缩


def test_estimate_and_threshold():
    assert compaction.estimate_text("a" * 400) == 101
    assert compaction.estimate_text("中文" * 50) == 101  # 非 ASCII 每个字约 1 token
    assert compaction.should_compact(80, 100) and not compaction.should_compact(79, 100)
    assert compaction.is_context_overflow(ModelError(message="HTTP 400: This model's maximum context length is 128k",
                                                     status=400))
    assert not compaction.is_context_overflow(ModelError(message="HTTP 500: oops", status=500))


async def test_auto_compaction_mid_turn(tmp_path):
    """读了一个大文件后用量超过 80%：下一步之前压缩，原文进分段存档，模型拿着摘要继续。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "big.c").write_text("int x;\n" * 3000)  # 估算约 5000 token
    (proj / "AGENTS.md").write_text("规则：只改 main.c", "utf-8")
    steps = [
        call("read_file", {"path": "big.c"}),
        say("<summary>1. 用户的请求和意图：看 big.c\n9. 当前工作和下一步：读完了 big.c</summary>"),  # 压缩请求
        say("看完了，big.c 全是 int x;"),
    ]
    backend = ScriptedBackend(steps, caps=ModelCaps(context_window=6000))
    store = SessionStore(tmp_path / "s")
    s = Session(id="t1", cwd=proj, backend=backend, model="m", registry=ToolRegistry([ReadFile()]), store=store)
    s.state_fn = lambda: "当前状态：没有绑定板子"
    r = await s.prompt("看一下 big.c 里是什么")
    assert r.stop_reason == "end_turn" and "全是" in r.text
    summary_req = backend.requests[1]
    assert "<summary>" in summary_req.messages[-1].content[0].text  # 压缩请求的最后一条是摘要提示词
    assert summary_req.tools  # 带着工具（同一前缀，命中缓存）
    after = backend.requests[2].messages
    assert after[0].role == "user"
    body = text_of(after[0])
    assert "<conversation_summary>" in body and "读完了 big.c" in body
    assert "规则：只改 main.c" in body  # 项目规则重新注入
    assert "当前状态：没有绑定板子" in body
    assert "看一下 big.c 里是什么" in body  # 用户最近一次的原话
    assert "segment_*.md" in body
    seg = tmp_path / "s" / "compaction" / "segment_001.md"
    assert seg.exists() and "int x;" in seg.read_text("utf-8")
    assert "segment_001.md" in (tmp_path / "s" / "compaction" / "INDEX.md").read_text("utf-8")
    assert store.load_history()[0].content == s.history[0].content  # history.jsonl 已重写
    rows = [line for line in (tmp_path / "s" / "trace.jsonl").read_text("utf-8").splitlines() if '"compaction"' in line]
    assert rows and '"reason": "auto"' in rows[0]


async def test_manual_compaction_and_feature_off(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    backend = ScriptedBackend([say("第一轮"), say("<summary>摘要内容</summary>")])
    s = Session(id="t2", cwd=proj, backend=backend, model="m", registry=ToolRegistry([]),
                store=SessionStore(tmp_path / "s"))
    await s.prompt("你好")
    info = await s.compact("重点保留问候")
    assert info["ok"] and info["reason"] == "manual" and len(s.history) == 1
    assert "重点保留问候" in backend.requests[-1].messages[-1].content[0].text

    # 关掉 context.compaction：用量再高也不压缩（对照实验）
    from firmwright.config import Features

    big = tmp_path / "p" / "big.c"
    big.write_text("int x;\n" * 3000)
    b2 = ScriptedBackend([call("read_file", {"path": "big.c"}), say("好")], caps=ModelCaps(context_window=6000))
    s2 = Session(id="t3", cwd=proj, backend=b2, model="m", registry=ToolRegistry([ReadFile()]),
                 features=Features(context_compaction=False))
    await s2.prompt("读")
    assert len(b2.requests) == 2 and len(s2.history) == 4


async def test_overflow_error_triggers_compaction_and_retry(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    from firmwright.model.types import Stop

    overflow = [ModelError(message="HTTP 400: maximum context length exceeded", status=400), Stop(reason="error")]
    backend = ScriptedBackend([say("第一轮"), overflow, say("<summary>短摘要</summary>"), say("重试成功")])
    s = Session(id="t4", cwd=proj, backend=backend, model="m", registry=ToolRegistry([]))
    await s.prompt("一")
    r = await s.prompt("二")
    assert r.stop_reason == "end_turn" and r.text == "重试成功"
    assert "短摘要" in text_of(s.history[0])


def test_prune_for_summary_shrinks_old_tool_results():
    msgs = [Message(role="user", content=[TextBlock(text="q")]),
            Message(role="tool", content=[ToolResultBlock(call_id="1", name="read_file",
                                                          content=[TextBlock(text="x" * 40000)])])]
    out = compaction.prune_for_summary(msgs, budget=2000)
    assert compaction.estimate_messages(out) < 2000
    assert len(msgs[1].content[0].content[0].text) == 40000  # 原消息没被改


# ---------------------------------------------------------------------- skill


def test_builtin_skills_and_project_override(tmp_path):
    skills = {s.name: s for s in discover(None, None)}
    assert {"esp32-s3", "esp32-p4"} <= set(skills)
    assert skills["esp32-s3"].chips == ["esp32s3"] and skills["esp32-s3"].scope == "builtin"
    proj = tmp_path / "proj"
    (proj / ".firmwright" / "skills" / "esp32-s3").mkdir(parents=True)
    (proj / ".firmwright" / "skills" / "esp32-s3" / "SKILL.md").write_text(
        "---\nname: esp32-s3\ndescription: 本工程自己的 S3 约定\nchips: [esp32s3]\n---\n正文", "utf-8")
    over = {s.name: s for s in discover(proj, None)}
    assert over["esp32-s3"].scope == "project" and over["esp32-s3"].description == "本工程自己的 S3 约定"
    cat = SkillCatalog(list(over.values()))
    listing = cat.listing("esp32s3").text
    assert listing.index("esp32-s3") < listing.index("esp32-p4") and "matches the bound board's chip" in listing
    meta, body = parse_frontmatter("---\nname: x\nchips: [a, 'b']\n---\nhello")
    assert meta == {"name": "x", "chips": ["a", "b"]} and body == "hello"


async def test_skill_tool_loads_body(tmp_path):
    from firmwright.context.skills import SkillTool
    from firmwright.model.types import CancelToken
    from firmwright.tools.base import ToolContext
    from firmwright.trace import Trace

    cat = SkillCatalog(discover(None, None))
    tool = SkillTool(cat)
    ctx = ToolContext(session_id="x", cwd=tmp_path, cancel=CancelToken(), trace=Trace(None, "x"))
    res = await tool.run(ctx, tool.parse({"name": "ESP32-S3"}))
    assert '<skill name="esp32-s3"' in res.text_content() and "Known failure modes" in res.text_content()
    assert "esp32-s3" in cat.loaded and "already loaded in this session" in cat.listing().text
    bad = await tool.run(ctx, tool.parse({"name": "nope"}))
    assert bad.is_error and "esp32-p4" in bad.text_content()


# ---------------------------------------------------------------------- 记忆


def test_memory_remember_search_index(tmp_path):
    m = MemoryStore(tmp_path / "memory", "C-proj", "proj")
    p = m.remember("workspace", "烧录问题", "这块板子烧录前要先按住 BOOT 键")
    m.remember("global", "用户偏好", "回复用中文")
    m.remember("workspace", "LED 驱动", "LED 接在 GPIO48，高电平点亮")
    assert p.name == "烧录问题.md" and "- 这块板子烧录前要先按住 BOOT 键" in p.read_text("utf-8")
    assert "topics/烧录问题.md" in (tmp_path / "memory" / "workspaces" / "C-proj" / "MEMORY.md").read_text("utf-8")
    hits = m.search("GPIO48")
    assert hits and hits[0].title == "LED 驱动" and hits[0].scope == "workspace"
    assert m.search("BOOT 键")[0].title == "烧录问题"  # "键"只有 1 个字，走 LIKE
    assert [h.scope for h in m.search("中文", scope="global")] == ["global"]
    idx = m.index_text()
    assert "Project memory" in idx and "Global memory" in idx
    pre = m.preface("LED 不亮，GPIO48 好像没输出").text
    assert "may be relevant to this task" in pre and "GPIO48" in pre
    # 改文件后增量重建索引
    p.write_text("# 烧录问题\n\n- 换了一根数据线就好了\n", "utf-8")
    assert m.search("数据线") and not m.search("先按住")
    assert m.resolve("../outside.txt") is None
    # 另一个工程看不到这个工程的记忆
    other = MemoryStore(tmp_path / "memory", "C-other", "other")
    assert not other.search("GPIO48") and other.search("中文")


# ---------------------------------------------------------------------- PDF


def make_pdf(path: Path, pages: list[str], outline: list[tuple[str, int]]) -> None:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    for text in pages:
        page = w.add_blank_page(612, 792)
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = w._add_object(stream)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    for title, n in outline:
        w.add_outline_item(title, n - 1)
    with path.open("wb") as f:
        w.write(f)


async def test_pdf_outline_pages_and_search(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRMWRIGHT_HOME", str(tmp_path / "home"))
    doc = tmp_path / "trm.pdf"
    make_pdf(doc, ["Chapter 1 System and Memory", "Chapter 2 IO MUX and GPIO Matrix", "GPIO matrix routing details",
                   "Chapter 3 Watchdog Timers"], [("System and Memory", 1), ("IO MUX and GPIO Matrix", 2),
                                                  ("Watchdog Timers", 4)])
    assert pdf.parse_pages("2-3,9", 4) == [2, 3]
    s = pdf.summary(doc)
    assert "4 pages" in s and "IO MUX and GPIO Matrix (p. 2)" in s
    assert "GPIO matrix routing details" in pdf.read_pages(doc, "3")
    found = await pdf.search(doc, "gpio matrix", tmp_path / "cache")
    assert "page 2 " in found and "page 3 " in found and "page 4 " not in found
    assert list((tmp_path / "cache").glob("*.json"))  # 全文缓存
    # 通过 read_file 工具
    from firmwright.model.types import CancelToken
    from firmwright.tools.base import ToolContext
    from firmwright.trace import Trace

    rf = ReadFile()
    ctx = ToolContext(session_id="x", cwd=tmp_path, cancel=CancelToken(), trace=Trace(None, "x"))
    assert "Outline:" in (await rf.run(ctx, rf.parse({"path": "trm.pdf"}))).text_content()
    assert "Watchdog" in (await rf.run(ctx, rf.parse({"path": "trm.pdf", "pages": "4"}))).text_content()
    assert "page 4 " in (await rf.run(ctx, rf.parse({"path": "trm.pdf", "query": "watchdog"}))).text_content()


# ---------------------------------------------------------------------- MCP


def test_bm25_ranks_relevant_tool():
    docs = ["esp-idf build project firmware", "flash firmware to the board serial port", "read the sdkconfig value"]
    s = Bm25(docs).scores("flash the board")
    assert s.index(max(s)) == 1
    assert "build" in tokenize("idf_build_Project") and "project" in tokenize("idf_build_Project")


async def test_mcp_client_tools_and_permissions(tmp_path):
    from firmwright.model.types import CancelToken
    from firmwright.permissions.engine import PermissionEngine
    from firmwright.tools.base import ToolContext
    from firmwright.trace import Trace

    cfg = McpConfig(servers={"fake": McpServerConfig(command=sys.executable, args=[str(HERE / "fake_mcp_server.py")])})
    mgr = McpManager(cfg.servers, cfg.search_threshold)
    try:
        await mgr.start()
        assert mgr.status()[0]["alive"] and mgr.status()[0]["tools"] == 2
        tools = {t.name: t for t in mgr.registry_tools()}
        assert set(tools) == {"mcp__fake__echo", "mcp__fake__add"}
        ctx = ToolContext(session_id="x", cwd=tmp_path, cancel=CancelToken(), trace=Trace(None, "x"))
        echo, add = tools["mcp__fake__echo"], tools["mcp__fake__add"]
        assert (await echo.run(ctx, echo.parse({"text": "你好"}))).text_content() == "你好"
        assert (await add.run(ctx, add.parse({"a": 2, "b": 3}))).text_content() == "5"
        assert echo.spec().parameters["required"] == ["text"]
        eng = PermissionEngine()
        assert eng.evaluate(echo, echo.parse({"text": "x"}), mode="default", cwd=tmp_path).action == "allow"
        assert eng.evaluate(add, add.parse({"a": 1, "b": 1}), mode="default", cwd=tmp_path).action == "ask"
        assert mgr.listing() is None  # 工具少：不用 search_tool
    finally:
        await mgr.stop()


async def test_mcp_many_tools_use_search(tmp_path):
    from firmwright.model.types import CancelToken
    from firmwright.tools.base import ToolContext
    from firmwright.trace import Trace

    servers = {"big": McpServerConfig(command=sys.executable, args=[str(HERE / "fake_mcp_server.py"), "40"])}
    mgr = McpManager(servers, search_threshold=30)
    try:
        await mgr.start()
        tools = {t.name: t for t in mgr.registry_tools()}
        assert set(tools) == {"search_tool", "use_tool"}
        assert "big: 40 tools" in mgr.listing().text
        ctx = ToolContext(session_id="x", cwd=tmp_path, cancel=CancelToken(), trace=Trace(None, "x"))
        found = (await tools["search_tool"].run(ctx, tools["search_tool"].parse({"query": "add two integers"})))
        assert found.text_content().splitlines()[1].startswith("- mcp__big__add")
        use = tools["use_tool"]
        res = await use.run(ctx, use.parse({"name": "mcp__big__add", "arguments": {"a": 40, "b": 2}}))
        assert res.text_content() == "42"
        assert use.risk_for(use.parse({"name": "mcp__big__echo"})) == "safe"
        missing = await use.run(ctx, use.parse({"name": "mcp__big__nope"}))
        assert missing.is_error
    finally:
        await mgr.stop()


def test_mcp_server_that_fails_to_start():
    import asyncio

    async def go():
        mgr = McpManager({"bad": McpServerConfig(command=sys.executable, args=["-c", "import sys; sys.exit(3)"])})
        await mgr.start()
        st = mgr.status()[0]
        assert not st["alive"] and st["error"]
        assert mgr.registry_tools() == []
        await mgr.stop()

    asyncio.run(go())


# ---------------------------------------------------------------------- 经过 ACP：注入、上下文信息、手动压缩


async def test_acp_preface_context_info_and_compact(tmp_path):
    from test_acp import Client

    proj = tmp_path / "proj"
    proj.mkdir()
    c = Client(tmp_path, [say("好的"), say("<summary>摘要</summary>")])
    MemoryStore(c.rt.home / "memory", None).remember("global", "用户偏好", "回复要简短")
    await c.call("initialize", {"protocolVersion": 1})
    sid = (await c.call("session/new", {"cwd": str(proj)}))["sessionId"]
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "回复要怎样"}]})
    first = c.backend.requests[0]
    sources = [b.source for b in first.messages[0].content if isinstance(b, ReminderBlock)]
    assert sources == ["skills", "memory"]
    assert "remember" in [t.name for t in first.tools] and "skill" in [t.name for t in first.tools]
    assert "## Memory" in first.system
    info = await c.call("_fwr/context/info", {"sessionId": sid})
    assert {s["name"] for s in info["skills"]} >= {"esp32-s3", "esp32-p4"} and info["memory"]["index"]
    assert info["window"] > 0 and info["used"] > 0
    res = await c.call("_fwr/session/compact", {"sessionId": sid})
    assert res["ok"] and res["reason"] == "manual"
    kinds = [u["sessionUpdate"] for u in c.updates(sid)]
    assert "_fwr/compacting" in kinds and "_fwr/compacted" in kinds and "_fwr/context" in kinds


def test_memory_related_recall_for_chinese_sentence(tmp_path):
    """首轮召回：整句中文没有空格，按关键词重叠打分（英文词 + 中文两字组合）。"""
    m = MemoryStore(tmp_path / "memory", "k", "proj")
    m.remember("workspace", "sdkconfig 关键配置", "task watchdog 超时 5 秒，不 panic，检查 CPU0/CPU1 的空闲任务")
    m.remember("workspace", "接线", "LED 接在 GPIO48")
    hits = m.related("如果循环里忙等 10 秒不让出 CPU，看门狗超时会复位吗？")
    assert [h.title for h in hits] == ["sdkconfig 关键配置"]
    assert m.related("今天天气怎么样") == []
    assert "may be relevant to this task" in m.preface("LED 接的是哪个 GPIO 口").text
