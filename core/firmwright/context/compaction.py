"""上下文压缩（§6.1 第 2 步，§6.2）。参照 grok：

- 触发：上下文用量超过窗口的 80% 时，在下一步调用模型之前压缩（grok compaction.rs 的 auto-compact 阈值）；
  用户也可以手动压缩；模型报"上下文超长"时压缩后重试一次。
- 方式：整体摘要替换原历史（grok full-replace）。摘要提示词照 grok 的 9 节结构
  （xai-grok-compaction/code_compaction/templates/full_replace_summary_prompt.txt），
  另外加了一节"设备与固件状态"：板子、烧录过的固件、没解决的崩溃、checkpoint 回退，这些是嵌入式调试里
  丢了就会重复踩坑的事实（新提）。
- 原文分段存档（grok compaction_mode Segments）：每次压缩前把这一段对话写成干净的 Markdown
  compaction\\segment_NNN.md，并更新 INDEX.md；摘要末尾告诉模型去哪里找原文。
- 压缩后的历史（grok 的 [SP, UP', AGENTS_MD?, UQ_last?, …, summary, reminder?]）：一条用户消息，依次是
  项目规则、skill 清单、记忆索引（重新注入）、摘要、当前状态、用户最近一次的原话。系统提示不变（前缀缓存）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..model.types import (
    Message,
    ModelBackend,
    ModelError,
    ModelRequest,
    ReasoningBlock,
    ReminderBlock,
    TextBlock,
    TextDelta,
    ToolCallBlock,
    ToolResultBlock,
    ToolSpec,
    Usage,
)

THRESHOLD = 0.80  # 用量超过窗口的 80% 时压缩
LOSSY_FRACTION = 0.70  # 摘要请求本身放不下时，把旧的工具结果截短到窗口的 70% 以内（grok 的 lossy 阶梯）
TOOL_RESULT_KEEP = 1500  # 截短时每个工具结果保留的字符数

SUMMARY_PROMPT = """Your task is to write a faithful, concise summary of the conversation so far, so that an assistant taking over can continue seamlessly after the earlier conversation is dropped. That assistant sees only this summary and the user's latest message. Keep everything needed to continue: what the user explicitly asked for, what you did recently, key technical details, file paths, commands, configuration and design decisions. But be tight: compact wording and short quotes, no long verbatim copies, no padding. A concise summary that fits is far more useful than an exhaustive one that gets truncated; stay within a few thousand words.
{user_context}
Important: if the conversation already contains a compaction summary (inside <conversation_summary> tags), treat it as the authoritative record of early history and carry what is still useful into the new summary, so nothing is lost across repeated compactions.

Review the conversation in your head first; do not output the analysis separately. Put the final summary in one <summary>...</summary> block with the numbered sections below. Write every section heading; if a section is empty, write "none":

1. User requests and intent: everything the user explicitly asked for and why, with details, constraints, scope limits and preferences.
2. Key technical concepts: technologies, frameworks, libraries, tools and chip features involved.
3. Files and code: every file read, created or changed. Give the path, why it matters and the relevant code; for code you wrote or changed, include the full snippet (complete for the most recent changes), not just a description.
4. Errors and fixes: every error hit (build failures, flash failures, command errors), the root cause, and how it was fixed. Quote the user's corrections verbatim.
5. Device and firmware state: the bound board (alias / chip / port), the latest build and flash results (firmware hash), crashes that happened (event id, exception type, decoded location) and whether they are resolved, what was verified on the device (expected lines / failure lines / PASS or not), which checkpoints were restored, and manual steps requested from the user with their outcome. Mark conclusions not verified on the device as "not verified on device".
6. Problem solving: problems already solved, plus ongoing diagnosis, hypotheses still being checked, and directions already ruled out (information like "tried this change, still crashes" must be kept).
7. All user messages: list every user message in order (excluding tool results); this is key to understanding how intent changed. Do not count this compaction instruction itself.
8. Pending tasks: things the user explicitly asked for that are not done yet. Don't invent tasks the user did not ask for.
9. Current work and next step: what was being done right before compaction (latest files, code, commands, state), specific enough to pick up; the next step is only the one that directly continues the current work and matches the user's latest request. If the last task was finished, write "confirm with the user before continuing".

Do not call any tools. Output only the <summary>...</summary> block and nothing after the closing tag."""

SEGMENT_HINT = ("\n\nThe full conversation before compaction is archived in segments at {loc}\\segment_*.md, "
                "with a table of contents in {loc}\\INDEX.md. When the summary is not enough, use read_file or grep to "
                "recover exact details (code, paths, tool output, raw serial lines). Do not modify these files.")


# ---------------------------------------------------------------------- 估算


_CJK = re.compile(r"[^\x00-\x7f]")


def estimate_text(s: str) -> int:
    """粗略的 token 估算：非 ASCII 字符约 1 个 token，ASCII 约 4 个字符 1 个 token。只用来决定要不要压缩。"""
    non_ascii = len(_CJK.findall(s))
    return non_ascii + (len(s) - non_ascii) // 4 + 1


def estimate_messages(msgs: list[Message]) -> int:
    total = 0
    for m in msgs:
        for b in m.content:
            if isinstance(b, TextBlock | ReasoningBlock | ReminderBlock):
                total += estimate_text(b.text)
            elif isinstance(b, ToolCallBlock):
                total += estimate_text(b.name + json.dumps(b.arguments, ensure_ascii=False))
            elif isinstance(b, ToolResultBlock):
                total += sum(estimate_text(c.text) if isinstance(c, TextBlock) else 1000 for c in b.content)
            else:  # 图片
                total += 1000
        total += 4
    return total


def estimate_tools(tools: list[ToolSpec]) -> int:
    return sum(estimate_text(t.name + t.description + json.dumps(t.parameters, ensure_ascii=False)) for t in tools)


class UsageTracker:
    """上下文用量：上一次请求的真实 prompt tokens + 之后追加的消息的估算。"""

    def __init__(self) -> None:
        self.last_input = 0  # 上一次请求服务端报告的 input tokens
        self.at_len = 0  # 那次请求时历史有多少条

    def observe(self, usage: Usage | None, history_len: int) -> None:
        if usage and usage.input_tokens:
            self.last_input = usage.input_tokens
            self.at_len = history_len

    def reset(self) -> None:
        self.last_input = 0
        self.at_len = 0

    def current(self, history: list[Message], system: str, tools: list[ToolSpec]) -> int:
        if self.last_input and self.at_len <= len(history):
            return self.last_input + estimate_messages(history[self.at_len:])
        return estimate_text(system) + estimate_tools(tools) + estimate_messages(history)


def should_compact(used: int, window: int, threshold: float = THRESHOLD) -> bool:
    return window > 0 and used >= window * threshold


def is_context_overflow(err: ModelError) -> bool:
    m = err.message.lower()
    return err.status in (400, 413) and any(k in m for k in (
        "context length", "context_length", "maximum context", "too many tokens", "prompt is too long",
        "reduce the length", "input length", "exceeds the model", "上下文"))


# ---------------------------------------------------------------------- 分段存档


def render_markdown(msgs: list[Message]) -> str:
    """把一段对话写成给人（和之后的模型）读的 Markdown：保留原文，不保留图片数据。"""
    out: list[str] = []
    for m in msgs:
        for b in m.content:
            if isinstance(b, TextBlock) and b.text.strip():
                who = {"user": "User", "assistant": "Assistant", "tool": "Tool"}[m.role]
                out.append(f"### {who}\n\n{b.text.strip()}\n")
            elif isinstance(b, ReminderBlock):
                out.append(f"### system-reminder ({b.source})\n\n{b.text.strip()}\n")
            elif isinstance(b, ReasoningBlock) and b.text.strip():
                out.append(f"<details><summary>Reasoning</summary>\n\n{b.text.strip()}\n\n</details>\n")
            elif isinstance(b, ToolCallBlock):
                args = json.dumps(b.arguments, ensure_ascii=False, indent=1) if b.arguments else (b.raw_arguments or "")
                out.append(f"### Call `{b.name}` ({b.id})\n\n```json\n{args}\n```\n")
            elif isinstance(b, ToolResultBlock):
                text = "\n".join(c.text if isinstance(c, TextBlock) else f"[image {c.alt}]" for c in b.content)
                tag = " (error)" if b.is_error else ""
                out.append(f"### Result `{b.name}`{tag} ({b.call_id})\n\n```\n{text}\n```\n")
            elif not isinstance(b, TextBlock):
                out.append(f"[image {getattr(b, 'alt', '')}]\n")
    return "\n".join(out)


def write_segment(folder: Path, msgs: list[Message], *, title: str, turn: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    n = len(list(folder.glob("segment_*.md"))) + 1
    path = folder / f"segment_{n:03d}.md"
    now = datetime.now(UTC).isoformat(timespec="seconds")
    path.write_text(f"# Segment {n} (compacted at {now}, turn {turn})\n\n" + render_markdown(msgs), "utf-8")
    users = [m.text().strip() for m in msgs if m.role == "user" and m.text().strip()]
    tools = sorted({b.name for m in msgs for b in m.content if isinstance(b, ToolCallBlock)})
    files = sorted({str(b.arguments.get("path")) for m in msgs for b in m.content
                    if isinstance(b, ToolCallBlock) and b.name in ("write_file", "edit_file") and b.arguments.get("path")})
    index = folder / "INDEX.md"
    head = "" if index.exists() else (f"# {title} · compaction archive\n\n"
                                       "Each segment is the full conversation before one compaction.\n\n")
    line = (f"- **{path.name}** ({now}, turn {turn}, {len(msgs)} messages)"
            f" user messages: {'; '.join(u[:60].replace(chr(10), ' ') for u in users[:3]) or 'none'}"
            f"{'…' if len(users) > 3 else ''}"
            f" · files changed: {', '.join(files[:8]) or 'none'} · tools used: {', '.join(tools)}\n")
    with index.open("a", encoding="utf-8") as f:
        f.write(head + line)
    return path


# ---------------------------------------------------------------------- 摘要


def prune_for_summary(msgs: list[Message], budget: int) -> list[Message]:
    """摘要请求本身放不下时：从旧到新把长工具结果截短，直到估算值低于预算（原文在分段存档里）。"""
    out = [m.model_copy(deep=True) for m in msgs]
    for m in out:
        if estimate_messages(out) <= budget:
            break
        if m.role != "tool":
            continue
        for b in m.content:
            if isinstance(b, ToolResultBlock):
                for i, c in enumerate(b.content):
                    if isinstance(c, TextBlock) and len(c.text) > TOOL_RESULT_KEEP:
                        b.content[i] = TextBlock(text=c.text[:TOOL_RESULT_KEEP] + "\n… (truncated for compaction; full text in the segment archive)")
    return out


def extract_summary(text: str) -> str:
    m = re.search(r"<summary>(.*?)(?:</summary>|$)", text, re.S)
    return (m.group(1) if m else text).strip()


async def summarize(backend: ModelBackend, *, model: str, system: str, history: list[Message],
                    tools: list[ToolSpec], window: int, cancel: Any, user_context: str = "") -> tuple[str, Usage | None]:
    """调一次模型写摘要。tools 照常带上（和正常请求同一个前缀，能命中缓存），提示词要求不调用工具。"""
    prompt = SUMMARY_PROMPT.format(
        user_context=f"\nAdditional instructions from the user for this compaction: {user_context}\n" if user_context else "")
    budget = int(window * LOSSY_FRACTION) - estimate_tools(tools) - estimate_text(system) - 4000
    msgs = history if estimate_messages(history) <= budget else prune_for_summary(history, budget)
    msgs = [*msgs, Message(role="user", content=[TextBlock(text=prompt)])]
    last_err: str | None = None
    for attempt in range(2):
        req = ModelRequest(model=model, system=system, messages=msgs, tools=tools if attempt == 0 else [])
        parts: list[str] = []
        usage: Usage | None = None
        err: ModelError | None = None
        async for ev in backend.stream(req, cancel=cancel):
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
            elif isinstance(ev, Usage):
                usage = ev
            elif isinstance(ev, ModelError):
                err = ev
                break
        text = "".join(parts).strip()
        if err is None and text:
            return extract_summary(text), usage
        last_err = err.message if err else "the model produced no summary (it may have only called tools)"
    raise RuntimeError(f"Compaction failed: {last_err}")


LAST_USER_TAG = "(the user's latest message before compaction, verbatim)"


def build_compacted(*, summary: str, segment_loc: str | None, preface: list[ReminderBlock],
                    state: str, last_user: str) -> list[Message]:
    """压缩后的新历史：一条用户消息。"""
    blocks: list[Any] = list(preface)
    text = (f"The earlier part of this session was compacted. Here is a summary of the conversation before "
            f"compaction:\n\n<conversation_summary>\n{summary}\n"
            "</conversation_summary>")
    if segment_loc:
        text += SEGMENT_HINT.format(loc=segment_loc)
    blocks.append(ReminderBlock(source="compaction", text=text))
    if state:
        blocks.append(ReminderBlock(source="state", text=state))
    blocks.append(TextBlock(text=f"{LAST_USER_TAG}\n{last_user}" if last_user else
                            "(continue the previous work)"))
    return [Message(role="user", content=blocks)]


def last_user_text(history: list[Message]) -> str:
    for m in reversed(history):
        if m.role == "user":
            t = m.text().strip()
            tagged = t.startswith((LAST_USER_TAG, "（压缩前用户最近一次的消息"))  # 旧会话里是中文
            if t and not tagged:
                return t
            if tagged:
                return t.split("\n", 1)[1] if "\n" in t else ""
    return ""


StateFn = Callable[[], str]
