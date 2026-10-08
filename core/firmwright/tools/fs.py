"""基础文件工具：读、写、改、列目录、搜索。参照 grok 的 read_file / write / edit / grep。"""

from __future__ import annotations

import base64
import difflib
import fnmatch
import os
import re
from pathlib import Path

from pydantic import BaseModel, Field

from ..model.types import ImageBlock, TextBlock
from .base import Tool, ToolCaps, ToolContext, ToolResult

SKIP_DIRS = {".git", "build", "node_modules", ".venv", "__pycache__", "managed_components", ".firmwright-cache"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".bmp": "image/bmp"}
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000


def _rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.cwd.resolve())).replace("\\", "/")
    except ValueError:
        return str(p)


def unified_diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}", n=3
        )
    )


def diff_stat(diff: str) -> tuple[int, int]:
    add = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    rem = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
    return add, rem


def read_text(path: Path) -> str:
    data = path.read_bytes()
    for enc in ("utf-8", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path, relative to the working directory or absolute")
    offset: int = Field(1, description="First line to read (1-based)")
    limit: int = Field(MAX_READ_LINES, description="Maximum number of lines")
    pages: str | None = Field(None, description="PDF only: pages to read, e.g. \"12\", \"120-125\", \"3,7-9\"")
    query: str | None = Field(None, description="PDF only: find pages by keywords (space-separated, all must appear)")


class ReadFile(Tool):
    name = "read_file"
    description = (
        "Read a file with line numbers. Also reads images (shown directly when the model supports vision). "
        "Read large files in chunks with offset/limit. "
        "PDF (technical reference manuals, datasheets, errata): call without arguments first to get the page count and "
        "the bookmark outline, then read specific pages with pages, or find pages by keyword with query."
    )
    Args = ReadFileArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def permission_subject(self, args: ReadFileArgs) -> str:
        return args.path

    async def run(self, ctx: ToolContext, args: ReadFileArgs) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.exists():
            return ToolResult.error(f"File not found: {args.path}")
        if path.is_dir():
            return ToolResult.error(f"{args.path} is a directory; use list_dir")
        suffix = path.suffix.lower()
        if suffix in IMAGE_TYPES:
            data = base64.b64encode(path.read_bytes()).decode()
            return ToolResult(
                content=[
                    TextBlock(text=f"Image {_rel(ctx, path)} ({path.stat().st_size} bytes)"),
                    ImageBlock(media_type=IMAGE_TYPES[suffix], data=data, alt=_rel(ctx, path)),
                ]
            )
        if suffix == ".pdf":
            return await _read_pdf(ctx, path, args)
        text = read_text(path)
        lines = text.splitlines()
        start = max(args.offset, 1)
        chunk = lines[start - 1 : start - 1 + max(1, min(args.limit, MAX_READ_LINES))]
        out = []
        for i, line in enumerate(chunk, start):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS] + "…(truncated)"
            out.append(f"{i:>6}\t{line}")
        tail = ""
        end = start - 1 + len(chunk)
        if end < len(lines):
            tail = f"\n… ({len(lines)} lines total, showing {start}–{end}; continue with offset={end + 1})"
        if not lines:
            return ToolResult.text("(empty file)")
        return ToolResult.text("\n".join(out) + tail)


async def _read_pdf(ctx: ToolContext, path: Path, args: ReadFileArgs) -> ToolResult:
    import asyncio

    from ..config import app_home
    from ..context import pdf

    try:
        if args.query:
            return ToolResult.text(await pdf.search(path, args.query, app_home() / "cache" / "pdf", ctx.progress))
        if args.pages:
            return ToolResult.text(await asyncio.to_thread(pdf.read_pages, path, args.pages))
        return ToolResult.text(await asyncio.to_thread(pdf.summary, path))
    except ValueError as e:
        return ToolResult.error(f"Invalid pages argument: {e}")
    except Exception as e:  # 加密、损坏的 PDF
        return ToolResult.error(f"Failed to read the PDF: {type(e).__name__}: {e}")


class WriteFileArgs(BaseModel):
    path: str = Field(description="File path")
    content: str = Field(description="The complete new content")


class WriteFile(Tool):
    name = "write_file"
    description = "Create a file or overwrite it entirely. To change part of an existing file, use edit_file."
    Args = WriteFileArgs
    caps = ToolCaps(lock="path", edits_files=True)

    def lock_keys(self, args: WriteFileArgs, ctx: ToolContext) -> list[str]:
        return [str(ctx.resolve(args.path)).lower()]

    def permission_subject(self, args: WriteFileArgs) -> str:
        return args.path

    async def run(self, ctx: ToolContext, args: WriteFileArgs) -> ToolResult:
        path = ctx.resolve(args.path)
        old = read_text(path) if path.exists() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.content, "utf-8", newline="")
        rel = _rel(ctx, path)
        diff = unified_diff(rel, old, args.content)
        add, rem = diff_stat(diff)
        verb = "Overwrote" if old else "Created"
        return ToolResult.text(f"{verb} {rel} (+{add} −{rem})", diff=diff, path=rel, added=add, removed=rem)


class EditFileArgs(BaseModel):
    path: str = Field(description="File path")
    old_string: str = Field(description="Text to replace; must match the file exactly (including indentation)")
    new_string: str = Field(description="Replacement text")
    replace_all: bool = Field(False, description="Replace every occurrence; by default old_string must be unique in the file")


class EditFile(Tool):
    name = "edit_file"
    description = "Replace an exact span of text in a file. old_string must be unique by default; include more context lines if it is not."
    Args = EditFileArgs
    caps = ToolCaps(lock="path", edits_files=True)

    def lock_keys(self, args: EditFileArgs, ctx: ToolContext) -> list[str]:
        return [str(ctx.resolve(args.path)).lower()]

    def permission_subject(self, args: EditFileArgs) -> str:
        return args.path

    async def run(self, ctx: ToolContext, args: EditFileArgs) -> ToolResult:
        path = ctx.resolve(args.path)
        if not path.exists():
            return ToolResult.error(f"File not found: {args.path}")
        if args.old_string == args.new_string:
            return ToolResult.error("old_string and new_string are identical; nothing to change")
        old = read_text(path)
        # 文件可能是 CRLF；模型给的通常是 LF
        crlf = "\r\n" in old
        text = old.replace("\r\n", "\n") if crlf else old
        needle = args.old_string.replace("\r\n", "\n")
        count = text.count(needle)
        if count == 0:
            return ToolResult.error("old_string was not found in the file. read_file first to confirm the exact text "
                                     "(watch indentation and spaces).")
        if count > 1 and not args.replace_all:
            return ToolResult.error(f"old_string occurs {count} times; add context to make it unique, or set replace_all=true")
        new_text = text.replace(needle, args.new_string.replace("\r\n", "\n"))
        if crlf:
            new_text = new_text.replace("\n", "\r\n")
        path.write_text(new_text, "utf-8", newline="")
        rel = _rel(ctx, path)
        diff = unified_diff(rel, old, new_text)
        add, rem = diff_stat(diff)
        return ToolResult.text(f"Edited {rel} (+{add} −{rem})", diff=diff, path=rel, added=add, removed=rem)


class ListDirArgs(BaseModel):
    path: str = Field(".", description="Directory")
    pattern: str | None = Field(None, description="Optional glob, e.g. **/*.c")


class ListDir(Tool):
    name = "list_dir"
    description = "List a directory; with pattern, match recursively by glob (skips build, .git and similar)."
    Args = ListDirArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def permission_subject(self, args: ListDirArgs) -> str:
        return args.path

    async def run(self, ctx: ToolContext, args: ListDirArgs) -> ToolResult:
        root = ctx.resolve(args.path)
        if not root.is_dir():
            return ToolResult.error(f"Not a directory: {args.path}")
        out: list[str] = []
        if args.pattern:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
                    if fnmatch.fnmatch(rel, args.pattern) or fnmatch.fnmatch(fn, args.pattern):
                        out.append(rel)
                if len(out) > 500:
                    break
        else:
            for p in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                out.append(p.name + ("/" if p.is_dir() else ""))
        if not out:
            return ToolResult.text("(no matches)")
        more = "\n… (more than 500 entries, truncated)" if len(out) > 500 else ""
        return ToolResult.text("\n".join(out[:500]) + more)


class GrepArgs(BaseModel):
    pattern: str = Field(description="Regular expression (Python syntax)")
    path: str = Field(".", description="Directory or file to search")
    glob: str | None = Field(None, description="Only search files whose name matches this glob, e.g. *.c")
    ignore_case: bool = False
    max_results: int = 200


class Grep(Tool):
    name = "grep"
    description = "Search file contents by regex; returns file:line: text. Skips build, .git and similar."
    Args = GrepArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def permission_subject(self, args: GrepArgs) -> str:
        return args.path

    async def run(self, ctx: ToolContext, args: GrepArgs) -> ToolResult:
        try:
            rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
        except re.error as e:
            return ToolResult.error(f"Invalid regex: {e}")
        root = ctx.resolve(args.path)
        files: list[Path]
        if root.is_file():
            files = [root]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    if args.glob and not fnmatch.fnmatch(fn, args.glob):
                        continue
                    files.append(Path(dirpath) / fn)
        hits: list[str] = []
        policy = ctx.services.paths if ctx.services else None
        for f in files:
            if policy and policy.is_protected(f, write=False):
                continue  # 受保护的文件（.env、应用数据）不搜内容
            try:
                if f.stat().st_size > 2_000_000:
                    continue
                text = read_text(f)
            except OSError:
                continue
            if "\x00" in text[:1000]:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{_rel(ctx, f)}:{i}: {line.strip()[:300]}")
                    if len(hits) >= args.max_results:
                        break
            if len(hits) >= args.max_results:
                break
        if not hits:
            return ToolResult.text("(no matches)")
        more = f"\n… (reached the limit of {args.max_results} results)" if len(hits) >= args.max_results else ""
        return ToolResult.text("\n".join(hits) + more)
