"""read_file 读 PDF（§6.2：芯片手册按页读）。参照 grok read_file/pdf；Embedder 的思路是"回答以手册为依据"，
方案 I10 不建向量索引，所以给模型三种用法：

1. 不带参数：页数 + 书签目录（章节 → 页码），先看目录再决定读哪几页；
2. pages="120-125"：按页取文字（一次最多 20 页 / 6 万字符）；
3. query="GPIO matrix"：在全文里找含有这些词的页（第一次搜索时把每页文字缓存到 <应用数据>\\cache\\pdf\\，
   技术参考手册上千页，第一次要几十秒，之后很快）。
扫描版 PDF 抽不出文字，会如实说明。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

MAX_PAGES = 20
MAX_CHARS = 60_000
MAX_OUTLINE = 400


def _reader(path: Path):
    from pypdf import PdfReader

    return PdfReader(str(path))


def parse_pages(spec: str, total: int) -> list[int]:
    """'3' / '3-7' / '3,5,9-10' → 1 起的页码列表（裁到 1..total）。"""
    out: list[int] = []
    for part in re.split(r"[,，\s]+", spec.strip()):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a or 1), int(b or total)
            out += list(range(max(lo, 1), min(hi, total) + 1))
        else:
            n = int(part)
            if 1 <= n <= total:
                out.append(n)
    return sorted(dict.fromkeys(out))


def outline(path: Path) -> tuple[int, list[tuple[int, str, int]]]:
    """(页数, [(层级, 标题, 页码)])"""
    r = _reader(path)
    items: list[tuple[int, str, int]] = []

    def walk(nodes, level: int) -> None:
        for n in nodes:
            if isinstance(n, list):
                walk(n, level + 1)
                continue
            try:
                num = r.get_destination_page_number(n)
                page = num + 1 if num is not None else 0
            except Exception:
                page = 0
            items.append((level, str(getattr(n, "title", "")).strip(), page))

    try:
        walk(r.outline, 0)
    except Exception:
        pass
    return len(r.pages), items


def read_pages(path: Path, spec: str) -> str:
    r = _reader(path)
    total = len(r.pages)
    pages = parse_pages(spec, total)
    if not pages:
        return f"Pages {spec!r} are outside 1–{total}"
    out, used, shown = [], 0, []
    for n in pages[:MAX_PAGES]:
        text = (r.pages[n - 1].extract_text() or "").strip()
        block = f"===== page {n} =====\n{text or '(no text could be extracted; it may be an image or a scan)'}"
        if used + len(block) > MAX_CHARS and shown:
            break
        out.append(block)
        used += len(block)
        shown.append(n)
    tail = ""
    if len(shown) < len(pages):
        tail = (f"\n\n… (showing pages {shown[0]}–{shown[-1]} only; at most {MAX_PAGES} pages / {MAX_CHARS} "
                f"characters per call, read the rest in further calls)")
    return f"{path.name} ({total} pages)\n\n" + "\n\n".join(out) + tail


async def search(path: Path, query: str, cache_dir: Path,
                 progress: Callable[[str], Awaitable[None]] | None = None, limit: int = 12) -> str:
    import asyncio

    st = path.stat()
    key = hashlib.sha1(f"{path.resolve()}|{st.st_size}|{st.st_mtime}".encode()).hexdigest()[:16]
    cache = cache_dir / f"{key}.json"
    if cache.exists():
        pages = json.loads(cache.read_text("utf-8"))
    else:
        if progress:
            await progress(f"First search in {path.name}: extracting the full text (cached afterwards)…")

        def extract() -> list[str]:
            r = _reader(path)
            return [(p.extract_text() or "") for p in r.pages]

        pages = await asyncio.to_thread(extract)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(pages, ensure_ascii=False), "utf-8")
    terms = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
    if not terms:
        return "query is empty"
    scored = []
    for i, text in enumerate(pages, 1):
        low = text.lower()
        counts = [low.count(t) for t in terms]
        if all(counts):
            scored.append((sum(counts), i, low.find(terms[0]), text))
    if not scored:
        empty = sum(1 for p in pages if not p.strip())
        note = f" ({empty} of them have no extractable text)" if empty else ""
        return f"{path.name} has {len(pages)} pages{note}; none contains all of {terms}. Try other words, or check the outline first."
    scored.sort(key=lambda x: (-x[0], x[1]))
    lines = []
    for n, page, pos, text in scored[:limit]:
        snip = " ".join(text[max(0, pos - 80): pos + 160].split())
        lines.append(f"- page {page} ({n} hits): …{snip}…")
    more = f"\n({len(scored)} pages matched; showing the first {limit})" if len(scored) > limit else ""
    return f"{path.name} has {len(pages)} pages; matching pages (by number of hits):\n" + "\n".join(lines) + more + \
        "\nRead specific pages with the pages argument."


def summary(path: Path) -> str:
    total, items = outline(path)
    head = f"{path.name}: PDF, {total} pages. "
    if not items:
        return head + "No bookmark outline. Find pages by keyword with query, or read the table of contents in the first pages with pages."
    lines = [f"{'  ' * lvl}- {title} (p. {page})" for lvl, title, page in items[:MAX_OUTLINE]]
    more = f"\n… ({len(items)} outline entries; showing the first {MAX_OUTLINE})" if len(items) > MAX_OUTLINE else ""
    return head + "Outline:\n" + "\n".join(lines) + more + \
        "\n\nRead specific pages with pages (e.g. pages=\"120-123\"), or find pages by keyword with query."
