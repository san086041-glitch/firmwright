"""跨会话记忆（§6.2，I10）。参照 grok xai-grok-memory（v2 的文件布局 + v1 的检索）：

    <应用数据>\\memory\\
    ├─ global\\topics\\*.md            全局：跨工程的偏好、经验（例如"这台电脑高并发编译不稳定"）
    ├─ workspaces\\<工程>\\topics\\*.md 工程：这个工程的约定、踩过的坑、板子的特性
    │                               同一个仓库的 worktree 共用一个工程记忆（grok 同样如此）
    ├─ …\\MEMORY.md                  每个范围一份生成的索引（只读），第一轮注入
    └─ index.sqlite                  FTS5 全文索引（trigram 分词，中文也能搜），按文件修改时间增量更新

- 不做 grok 的 dream 自动整理、不做向量检索（方案 §8：第一版不做）。
- 工具：memory_search（检索）、memory_get（读一个主题文件）、remember（往主题文件追加一条）。
  删改用 edit_file 直接改主题文件（在工作目录以外，按权限规则会询问）。
- 注入：第一轮把两个范围的索引和"和第一句话相关的命中"作为 system-reminder 注入；压缩后重新注入索引。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field

from ..model.types import ReminderBlock
from ..tools.base import Tool, ToolCaps, ToolContext, ToolResult

INDEX_BUDGET = 3000  # 注入的索引最多多少字符
SCOPES = ("workspace", "global")


class Hit(BaseModel):
    scope: str
    path: str  # 相对记忆根目录，用 /
    title: str
    snippet: str
    score: float


def slugify(topic: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", topic.strip()).strip("-.").lower()
    return s[:60] or "notes"


class MemoryStore:
    def __init__(self, root: Path, workspace_key: str | None = None, workspace_label: str = "") -> None:
        self.root = root
        self.workspace_label = workspace_label
        self.dirs: dict[str, Path] = {"global": root / "global"}
        if workspace_key:
            self.dirs["workspace"] = root / "workspaces" / workspace_key
        self.db_path = root / "index.sqlite"

    # ------------------------------------------------------------------ 文件

    def topics(self, scope: str) -> list[Path]:
        d = self.dirs.get(scope)
        return sorted((d / "topics").glob("*.md")) if d and (d / "topics").is_dir() else []

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def resolve(self, rel: str) -> Path | None:
        """memory_get 只能读记忆目录里的文件。"""
        p = (self.root / rel).resolve()
        try:
            p.relative_to(self.root.resolve())
        except ValueError:
            return None
        return p if p.is_file() else None

    def remember(self, scope: str, topic: str, text: str) -> Path:
        if scope not in self.dirs:
            raise ValueError("This session has no project memory (use global)" if scope == "workspace"
                             else f"Unknown scope {scope}")
        topics = self.dirs[scope] / "topics"
        topics.mkdir(parents=True, exist_ok=True)
        path = topics / f"{slugify(topic)}.md"
        if not path.exists():
            path.write_text(f"# {topic.strip()}\n\n", "utf-8")
        line = " ".join(text.strip().splitlines())
        with path.open("a", encoding="utf-8") as f:
            f.write(f"- {line} ({date.today().isoformat()})\n")
        self.write_index(scope)
        return path

    def write_index(self, scope: str) -> None:
        d = self.dirs[scope]
        d.mkdir(parents=True, exist_ok=True)
        head = ("# Global memory index" if scope == "global" else f"# Project memory index: {self.workspace_label}") + \
            "\n\n(generated automatically; edit the topic files, not this index)\n\n"
        lines = [f"- topics/{p.name} — {_title(p)} ({_count(p)} entries)" for p in self.topics(scope)]
        (d / "MEMORY.md").write_text(head + "\n".join(lines) + "\n", "utf-8")

    def index_text(self, budget: int = INDEX_BUDGET) -> str:
        parts = []
        for scope in SCOPES:
            items = self.topics(scope)
            if not items:
                continue
            base = self.rel(self.dirs[scope])
            label = "Project memory" if scope == "workspace" else "Global memory"
            parts.append(f"{label} ({base}/):\n" + "\n".join(
                f"- {base}/topics/{p.name} — {_title(p)} ({_count(p)} entries)" for p in items))
        text = "\n\n".join(parts)
        return text if len(text) <= budget else text[:budget] + "\n… (index truncated; use memory_search)"

    # ------------------------------------------------------------------ 检索

    def _db(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path)
        con.execute("create virtual table if not exists chunks using fts5(scope unindexed, path unindexed, "
                    "title, body, tokenize='trigram')")
        con.execute("create table if not exists files(path text primary key, mtime real)")
        return con

    def sync(self) -> None:
        """增量更新索引：只重建修改时间变了的文件，删掉已经不存在的文件。"""
        with self._db() as con:
            known = dict(con.execute("select path, mtime from files").fetchall())
            present: set[str] = set()
            for scope in self.dirs:
                for p in self.topics(scope):
                    rel = self.rel(p)
                    present.add(rel)
                    mtime = p.stat().st_mtime
                    if known.get(rel) == mtime:
                        continue
                    con.execute("delete from chunks where path = ?", (rel,))
                    for title, body in _chunks(p):
                        con.execute("insert into chunks(scope, path, title, body) values (?, ?, ?, ?)",
                                    (scope, rel, title, body))
                    con.execute("insert or replace into files(path, mtime) values (?, ?)", (rel, mtime))
            mine = {self.rel(d) for d in self.dirs.values() if d.exists()}
            for rel in set(known) - present:
                # 只清理属于本会话两个范围的记录；别的工程的记录留着
                if any(rel.startswith(m + "/") for m in mine):
                    con.execute("delete from chunks where path = ?", (rel,))
                    con.execute("delete from files where path = ?", (rel,))

    def search(self, query: str, scope: str | None = None, limit: int = 8) -> list[Hit]:
        self.sync()
        terms = [t for t in re.split(r"[\s,，。；;、]+", query) if t]
        if not terms:
            return []
        scopes = [scope] if scope else list(self.dirs)
        prefixes = tuple(self.rel(self.dirs[s]) + "/" for s in scopes if s in self.dirs)
        if not prefixes:
            return []
        long = [t for t in terms if len(t) >= 3]
        short = [t for t in terms if len(t) < 3]  # trigram 搜不了不到 3 个字的词，用 LIKE
        # path 是 FTS5 里不建索引的列，在 SQL 里对它做 LIKE 过滤不可靠：多取一些，再在这里按范围过滤
        fetch = limit * 20
        rows: list[tuple] = []
        with self._db() as con:
            if long:
                match = " OR ".join('"' + t.replace('"', '""') + '"' for t in long)
                rows += con.execute(
                    "select scope, path, title, snippet(chunks, 3, '[', ']', '…', 24), bm25(chunks) "
                    "from chunks where chunks match ? order by bm25(chunks) limit ?", (match, fetch)).fetchall()
            for t in short:
                rows += con.execute(
                    "select scope, path, title, substr(body, max(1, instr(body, ?) - 40), 120), 0.0 "
                    "from chunks where body like ? or title like ? limit ?",
                    (t, f"%{t}%", f"%{t}%", fetch)).fetchall()
        rows = [r for r in rows if str(r[1]).startswith(prefixes)]
        seen: dict[tuple[str, str], Hit] = {}
        for sc, path, title, snip, rank in rows:
            key = (path, title)
            if key not in seen:
                seen[key] = Hit(scope=sc, path=path, title=title, snippet=" ".join(str(snip).split()),
                                score=round(-float(rank), 3))
        return sorted(seen.values(), key=lambda h: -h.score)[:limit]

    def related(self, text: str, limit: int = 3, min_score: float = 2.0) -> list[Hit]:
        """首轮召回用：用户的第一句话是一整句中文、没有空格，FTS 的整词匹配几乎命中不了。
        这里按"关键词重叠"打分：英文 / 数字词（≥3 个字符，权重 2）+ 中文的两字组合（权重 1）。
        记忆库很小（几百个小节），直接在 Python 里算。"""
        self.sync()
        words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text)} - _STOP
        grams = {run[i:i + 2] for run in re.findall(r"[一-鿿]+", text) for i in range(len(run) - 1)}
        if not words and not grams:
            return []
        prefixes = tuple(self.rel(d) + "/" for d in self.dirs.values())
        with self._db() as con:
            rows = con.execute("select scope, path, title, body from chunks").fetchall()
        hits: list[Hit] = []
        for sc, path, title, body in rows:
            if not str(path).startswith(prefixes):
                continue
            low = (title + "\n" + body).lower()
            got_w = [w for w in words if w in low]
            got_g = [g for g in grams if g in low]
            score = 2 * len(got_w) + len(got_g)
            if score < min_score:
                continue
            first = got_w[0] if got_w else got_g[0]
            pos = low.find(first)
            snip = " ".join(body[max(0, pos - 40): pos + 120].split())
            hits.append(Hit(scope=sc, path=path, title=title, snippet=snip, score=float(score)))
        return sorted(hits, key=lambda h: -h.score)[:limit]

    def preface(self, first_prompt: str, max_hits: int = 3) -> ReminderBlock | None:
        idx = self.index_text()
        if not idx:
            return None
        text = ("Cross-session memory index (things earlier sessions or the user asked you to remember). Read topic "
                "files with memory_get and search with memory_search when needed. Memory is history, not necessarily "
                "the present: verify paths, commands and board state with tools.\n\n" + idx)
        hits = self.related(first_prompt, limit=max_hits) if first_prompt.strip() else []
        if hits:
            text += "\n\nMemory that may be relevant to this task:\n" + "\n".join(
                f"- {h.path} ({h.title}): {h.snippet}" for h in hits)
        return ReminderBlock(source="memory", text=text)


_STOP = {"the", "and", "for", "with", "this", "that", "from", "you", "are", "not", "can", "how", "what"}


def _title(p: Path) -> str:
    for line in p.read_text("utf-8", errors="replace").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return p.stem


def _count(p: Path) -> int:
    return sum(1 for line in p.read_text("utf-8", errors="replace").splitlines() if line.lstrip().startswith("- "))


def _chunks(p: Path) -> list[tuple[str, str]]:
    """按 ## 小节切块；没有小节就整篇一块。"""
    text = p.read_text("utf-8", errors="replace")
    title = _title(p)
    parts = re.split(r"^(?=## )", text, flags=re.M)
    out = []
    for part in parts:
        if not part.strip():
            continue
        head = part.splitlines()[0]
        sub = head[3:].strip() if head.startswith("## ") else ""
        out.append((f"{title} / {sub}" if sub else title, part))
    return out


# ---------------------------------------------------------------------- 工具


class MemorySearchArgs(BaseModel):
    query: str = Field(description="Keywords, space-separated (any language)")
    scope: str | None = Field(None, description="workspace (this project) or global; omit to search both")


class MemorySearch(Tool):
    name = "memory_search"
    description = ("Search cross-session memory (conventions, pitfalls and board quirks recorded by earlier sessions). "
                   "Returns matching topic files and snippets.")
    Args = MemorySearchArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def permission_subject(self, args: MemorySearchArgs) -> str:
        return args.query

    async def run(self, ctx: ToolContext, args: MemorySearchArgs) -> ToolResult:
        hits = self.store.search(args.query, args.scope)
        if not hits:
            return ToolResult.text("No matches.")
        return ToolResult.text("\n".join(f"- [{h.scope}] {h.path} ({h.title}): {h.snippet}" for h in hits))


class MemoryGetArgs(BaseModel):
    path: str = Field(description="Memory file path, relative to the memory root (as given by memory_search / the index)")


class MemoryGet(Tool):
    name = "memory_get"
    description = "Read a whole memory file (a topic file or the MEMORY.md index)."
    Args = MemoryGetArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def permission_subject(self, args: MemoryGetArgs) -> str:
        return args.path

    async def run(self, ctx: ToolContext, args: MemoryGetArgs) -> ToolResult:
        p = self.store.resolve(args.path.replace("\\", "/"))
        if p is None:
            return ToolResult.error(f"No such memory file: {args.path}")
        return ToolResult.text(f"{p}\n\n" + p.read_text("utf-8", errors="replace"))


class RememberArgs(BaseModel):
    scope: str = Field("workspace", description="workspace: about this project only; global: applies across projects "
                                                   "(user preferences, PC environment)")
    topic: str = Field(description="Topic, e.g. 'LED driver', 'flashing issues', 'user preferences'; entries on "
                                   "the same topic go into the same file")
    text: str = Field(description="One fact to remember, as a self-contained sentence")


class Remember(Tool):
    name = "remember"
    description = (
        "Record a fact in cross-session memory. Use it when the user explicitly asks you to remember something, or when "
        "you find a fact that is stable, specific, likely to be reused and not findable in the repository or docs (e.g. a "
        "board quirk, a fix that was tried and did not work). Never record secrets, temporary task state or unverified "
        "guesses.")
    Args = RememberArgs
    caps = ToolCaps(edits_files=True, risk="normal")

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def permission_subject(self, args: RememberArgs) -> str:
        return f"{args.scope}:{args.topic}"

    async def run(self, ctx: ToolContext, args: RememberArgs) -> ToolResult:
        try:
            path = self.store.remember(args.scope, args.topic, args.text)
        except ValueError as e:
            return ToolResult.error(str(e))
        ctx.trace.record("remember", scope=args.scope, topic=args.topic, path=str(path))
        return ToolResult.text(f"Remembered ({args.scope} / {self.store.rel(path)}): {args.text}", path=str(path))


MEMORY_PROMPT = """
## Memory
- Cross-session memory has two scopes: project memory (only about this project) and global memory (across projects,
  e.g. user preferences, this PC's environment). The index is in a system-reminder at the start of the conversation.
- When the user asks you to "remember" something, or you discover a fact that is stable, specific, likely to be reused
  and not findable in the repository (a board quirk, a fix that was tried and did not work), record it with remember.
  Never record secrets, temporary state or unverified guesses.
- Memory is history, not necessarily the present. Verify anything that can change (paths, commands, board state) with
  tools before relying on it."""
