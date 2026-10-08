"""Skills（§6.2，I10）。参照 grok（xai-grok-tools/src/implementations/skills/、types/skill_discovery_tracker/）：

- 一个 skill 是一个目录，里面有 SKILL.md：YAML frontmatter（name、description、when-to-use）+ Markdown 正文。
- 发现顺序（同名时前面的覆盖后面的）：工程的 .firmwright/skills、.agents/skills、.claude/skills
  → 用户的 <应用数据>/skills → 内置的 skills/（仓库根目录：通用的 esp-idf、生成的 esp32-chips 芯片卡片、
  手写的 esp32-s3 / esp32-p4）。
- 清单（名字 + 描述）以 system-reminder 注入第一条用户消息，**不改系统提示**（grok："The system prompt is never
  mutated for skills"），前缀缓存不受影响；压缩后重新注入。
- 正文由模型调用 skill 工具按需加载，格式照 grok 的 <skill name=… description=… path=…>…</skill>。
- 多出来的一个字段 chips（新提）：适用的芯片。绑定了板子时，清单里把对得上的 skill 排在前面并标出来。
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from ..model.types import ReminderBlock
from ..tools.base import Tool, ToolCaps, ToolContext, ToolResult

BODY_CAP = 40_000  # 正文最多给多少字符（grok 按 read_file 的上限截断）
PROJECT_DIRS = [".firmwright/skills", ".agents/skills", ".claude/skills"]


def builtin_dir() -> Path:
    env = os.environ.get("FIRMWRIGHT_SKILLS_DIR")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):  # PyInstaller 打包后：skills 作为数据打进包里（packaging/firmwright-core.spec）
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "skills"
    return Path(__file__).resolve().parents[3] / "skills"  # 仓库根目录的 skills/


class SkillInfo(BaseModel):
    name: str
    description: str
    when_to_use: str = ""
    chips: list[str] = Field(default_factory=list)
    path: str
    scope: str  # project | user | builtin


_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.S)


def parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """只支持 skill 需要的简单 YAML：key: value、引号字符串、[a, b] 列表。"""
    m = _FM.match(text)
    if not m:
        return {}, text
    meta: dict[str, object] = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line or line.startswith((" ", "\t")):
            continue
        key, val = line.split(":", 1)
        v = val.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[key.strip()] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[key.strip()] = v.strip("'\"")
    return meta, text[m.end():]


def parse_skill(path: Path, scope: str) -> SkillInfo | None:
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return None
    meta, body = parse_frontmatter(text)
    name = str(meta.get("name") or path.parent.name).strip().lower().replace(" ", "-").replace("_", "-")
    desc = str(meta.get("description") or "").strip()
    if not desc:  # grok：没有 description 时用正文第一段
        desc = next((p.strip() for p in body.split("\n\n") if p.strip() and not p.lstrip().startswith("#")), "")[:300]
    chips = meta.get("chips") or []
    return SkillInfo(name=name, description=desc, when_to_use=str(meta.get("when-to-use") or ""),
                     chips=[str(c).lower() for c in chips] if isinstance(chips, list) else [str(chips).lower()],
                     path=str(path), scope=scope)


def discover(project: Path | None, user_dir: Path | None, builtin: Path | None = None,
             disabled: list[str] | None = None) -> list[SkillInfo]:
    roots: list[tuple[Path, str]] = []
    if project is not None:
        roots += [(project / d, "project") for d in PROJECT_DIRS]
    if user_dir is not None:
        roots.append((user_dir, "user"))
    roots.append((builtin or builtin_dir(), "builtin"))
    seen: dict[str, SkillInfo] = {}
    for root, scope in roots:
        if not root.is_dir():
            continue
        for md in sorted(root.glob("*/SKILL.md")):
            info = parse_skill(md, scope)
            if info and info.name not in seen and info.name not in (disabled or []):
                seen[info.name] = info
    return list(seen.values())


class SkillCatalog:
    """一个会话能用的 skill。清单在会话开始时发现一次；skill 工具和清单都从这里取。"""

    def __init__(self, skills: list[SkillInfo]) -> None:
        self.skills = {s.name: s for s in skills}
        self.loaded: set[str] = set()  # 本会话加载过的（压缩后的清单里标出来）

    def listing(self, chip: str | None = None) -> ReminderBlock | None:
        if not self.skills:
            return None
        items = sorted(self.skills.values(), key=lambda s: (not (chip and chip in s.chips), s.name))
        lines = []
        for s in items:
            mark = " (matches the bound board's chip)" if chip and chip in s.chips else ""
            when = f" When to use: {s.when_to_use}" if s.when_to_use else ""
            loaded = " [already loaded in this session]" if s.name in self.loaded else ""
            lines.append(f"- {s.name}{mark}: {s.description}{when}{loaded}")
        return ReminderBlock(source="skills", text=(
            "Available skills (domain knowledge and practices). When one is relevant to the task, load its body with the "
            "skill tool before acting; don't load irrelevant ones.\n" + "\n".join(lines)))


class SkillArgs(BaseModel):
    name: str = Field(description="Skill name, from the skill list at the start of the conversation")


class SkillTool(Tool):
    name = "skill"
    description = ("Load a skill's body (domain knowledge, initialization constraints, known failure modes, pointers "
                   "to manual chapters). Available skills are listed in the system-reminder at the start of the "
                   "conversation.")
    Args = SkillArgs
    caps = ToolCaps(read_only=True, risk="safe")

    def __init__(self, catalog: SkillCatalog) -> None:
        self.catalog = catalog

    def permission_subject(self, args: SkillArgs) -> str:
        return args.name

    async def run(self, ctx: ToolContext, args: SkillArgs) -> ToolResult:
        key = args.name.strip().lower()
        info = self.catalog.skills.get(key)
        if info is None:
            names = ", ".join(sorted(self.catalog.skills)) or "(none)"
            return ToolResult.error(f"No skill named {args.name}. Available: {names}")
        _meta, body = parse_frontmatter(Path(info.path).read_text("utf-8", errors="replace"))
        body = body.strip()
        if len(body) > BODY_CAP:
            body = body[:BODY_CAP] + f"\n\n[body truncated at {BODY_CAP} characters; read the rest with read_file {info.path}]"
        self.catalog.loaded.add(info.name)
        ctx.trace.record("skill_loaded", name=info.name, scope=info.scope, chars=len(body))
        folder = str(Path(info.path).parent)
        return ToolResult.text(
            f'<skill name="{info.name}" description="{info.description}" path="{info.path}">\n{body}\n</skill>\n'
            f"(If the skill folder {folder} has references or other attached files, read them with read_file.)",
            skill=info.name)
