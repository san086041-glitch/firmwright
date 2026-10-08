"""权限（I13 + D15 + D16）。

规则语法、deny 优先、分析不了就不放行，这几条照搬 grok（xai-grok-permission-rules）：
    Tool            匹配这个工具的所有调用
    Tool(pattern)   pattern 匹配 permission_subject；shell 命令支持 "前缀:*"，路径支持 glob
硬件分级是新提的：工具（或平台适配器的 RiskRule）给出 risk；
  forbidden → 一律拒绝，任何规则和模式都放宽不了
  dangerous → 询问；只有显式 allow 规则能放宽
模式参照 grok：default / accept_edits / plan / always_approve。

判定顺序：forbidden → 受保护路径 → deny 规则 → plan 模式 → ask 规则 → 工作目录以外的写 / 链接（任何模式都问）
→ dangerous → shell 无法分析（Approve all 下原文没有外部路径就放行）→ 工作目录以外的读取 → allow 规则 → 模式默认。
路径见 paths.py（W7；写的部分 2026-10-05 真机实测事故后加）。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..config import PermissionConfig
from ..tools.base import Risk, Tool
from .paths import (
    PathCheck,
    PathPolicy,
    expand_vars,
    link_targets,
    raw_paths,
    shell_command_paths,
    shell_write_paths,
)
from .ps_parse import PsAnalysis

# 参数里带路径的文件工具（permission_subject 就是路径）
FILE_TOOLS = {"read_file", "list_dir", "grep", "write_file", "edit_file"}

Action = Literal["allow", "deny", "ask"]
Mode = Literal["default", "accept_edits", "plan", "always_approve"]

# 规则里的工具名 → 实际工具名。大小写不敏感；兼容 grok / Claude Code 的叫法
ALIASES: dict[str, list[str]] = {
    "bash": ["shell"], "shell": ["shell"], "powershell": ["shell"],
    "read": ["read_file", "list_dir", "grep"], "edit": ["edit_file", "write_file"],
    "write": ["write_file"], "grep": ["grep"],
    "build": ["build"], "flash": ["flash"], "settarget": ["set_target"], "set_target": ["set_target"],
    "clean": ["clean"], "monitor": ["await_marker", "read_log"], "reset": ["reset"],
    "efuse": ["efuse"],
}

# 内置的只读 shell 命令（default 模式下直接放行）
READ_ONLY_SHELL = [
    "get-childitem", "ls", "dir", "gci", "get-content", "cat", "type", "gc", "select-string", "sls",
    "findstr", "get-location", "pwd", "gl", "echo", "write-output", "write-host", "test-path",
    "resolve-path", "get-item", "gi", "measure-object", "select-object", "select", "sort-object",
    "sort", "where-object", "where", "?", "format-table", "ft", "format-list", "fl", "out-string",
    "get-command", "gcm", "get-date", "split-path", "join-path", "get-filehash", "tree",
    "git status", "git log", "git diff", "git show", "git branch", "git rev-parse", "git ls-files",
    "git blame", "where.exe", "start-sleep", "sleep",
]


@dataclass
class Rule:
    tools: list[str]  # 匹配的工具名；["*"] 表示全部
    pattern: str | None
    raw: str

    @classmethod
    def parse(cls, text: str) -> Rule:
        m = re.fullmatch(r"\s*([A-Za-z_*][\w*]*)\s*(?:\((.*)\))?\s*", text)
        if not m:
            raise ValueError(f"Invalid permission rule: {text!r} (expected Tool or Tool(pattern))")
        name, pattern = m.group(1), m.group(2)
        tools = ["*"] if name == "*" else ALIASES.get(name.lower(), [name.lower()])
        return cls(tools=tools, pattern=pattern, raw=text)

    def matches_tool(self, tool: str) -> bool:
        return "*" in self.tools or tool in self.tools


def match_pattern(pattern: str | None, subject: str, *, is_command: bool) -> bool:
    if pattern is None or pattern in ("*", ""):
        return True
    if is_command:
        p = pattern.strip().lower()
        s = subject.strip().lower()
        if p.endswith(":*"):
            prefix = p[:-2].strip()
            return s == prefix or s.startswith(prefix + " ")
        return fnmatch.fnmatchcase(s, p)
    s = subject.replace("\\", "/")
    p = pattern.replace("\\", "/")
    return fnmatch.fnmatch(s, p) or fnmatch.fnmatch(s.lower(), p.lower())


@dataclass
class RiskRule:
    """平台适配器提供的危险规则（I06）。match 对象是工具名 + subject（shell 命令按子命令逐条匹配）。"""

    tool: str  # 工具名，"shell" 匹配 shell 子命令
    regex: str
    risk: Risk
    reason: str
    field: Literal["subject", "args"] = "subject"  # args：匹配整个参数的 JSON（例如编辑内容里的 sdkconfig 项）

    def hit(self, tool: str, subject: str) -> bool:
        return tool == self.tool and re.search(self.regex, subject, re.IGNORECASE | re.MULTILINE) is not None


@dataclass
class Decision:
    action: Action
    reason: str
    risk: Risk = "normal"
    rule: str | None = None
    details: dict = field(default_factory=dict)


class PermissionEngine:
    def __init__(self, cfg: PermissionConfig | None = None, risk_rules: list[RiskRule] | None = None,
                 paths: PathPolicy | None = None) -> None:
        cfg = cfg or PermissionConfig()
        self.allow = [Rule.parse(r) for r in cfg.allow]
        self.ask = [Rule.parse(r) for r in cfg.ask]
        self.deny = [Rule.parse(r) for r in cfg.deny]
        self.risk_rules = risk_rules or []
        self.paths = paths  # W7：受保护路径 + 工作目录以外的读取（None = 不检查，旧行为）
        self.session_allow: list[Rule] = []  # 用户在审批卡片上点"本会话都允许"后加入
        self.session_exact: set[str] = set()  # 无法分析的命令：只按完整原文放行（2026-10-05）

    def add_session_allow(self, rule: str) -> None:
        self.session_allow.append(Rule.parse(rule))

    # ------------------------------------------------------------------

    def _subjects(self, tool: Tool, args, ps: PsAnalysis | None) -> list[str]:
        if tool.name == "shell" and ps is not None:
            return [c.normalized for c in ps.commands] or [tool.permission_subject(args)]
        return [tool.permission_subject(args)]

    def _rule_hits(self, rules: list[Rule], tool: Tool, subjects: list[str], *, every: bool) -> Rule | None:
        """every=True：每个子命令都要被某条规则覆盖（allow 用）；False：任一命中即可（deny / ask 用）。"""
        is_cmd = tool.name == "shell"
        cand = [r for r in rules if r.matches_tool(tool.name)]
        if not cand:
            return None
        if every:
            hit = None
            for s in subjects:
                r = next((r for r in cand if match_pattern(r.pattern, s, is_command=is_cmd)), None)
                if r is None:
                    return None
                hit = hit or r
            return hit
        for s in subjects:
            for r in cand:
                if match_pattern(r.pattern, s, is_command=is_cmd):
                    return r
        return None

    def _risk(self, tool: Tool, args, subjects: list[str]) -> tuple[Risk, str]:
        order = {"safe": 0, "normal": 1, "dangerous": 2, "forbidden": 3}
        risk: Risk = tool.risk_for(args)
        reason = tool.risk_reason(args) if hasattr(tool, "risk_reason") else ""
        args_json = json.dumps(args.model_dump(), ensure_ascii=False) if hasattr(args, "model_dump") else ""
        for rr in self.risk_rules:
            targets = [args_json] if rr.field == "args" else subjects
            if any(rr.hit(tool.name, t) for t in targets) and order[rr.risk] > order[risk]:
                risk, reason = rr.risk, rr.reason
        return risk, reason

    def evaluate(
        self, tool: Tool, args, *, mode: Mode, cwd: Path, ps: PsAnalysis | None = None
    ) -> Decision:
        subjects = self._subjects(tool, args, ps)
        risk, risk_reason = self._risk(tool, args, subjects)
        details: dict[str, Any] = {"subjects": subjects}

        if risk == "forbidden":
            return Decision("deny", f"Forbidden operation: {risk_reason or 'irreversible; it could permanently brick the board'}", risk, details=details)
        pc = self._path_check(tool, args, cwd, ps, subjects)
        if pc and pc.protected:
            return Decision("deny", "Protected path (Firmwright app data, key files, .env, .ssh); it cannot be read or written: "
                            + ", ".join(pc.protected[:3]), risk, details=details)
        if r := self._rule_hits(self.deny, tool, subjects, every=False):
            return Decision("deny", f"Matched deny rule {r.raw}", risk, r.raw, details)
        if mode == "plan" and not tool.caps.read_only:
            return Decision("deny", "Plan mode allows only read-only tools", risk, details=details)
        if r := self._rule_hits(self.ask, tool, subjects, every=False):
            return Decision("ask", f"Matched ask rule {r.raw}", risk, r.raw, details)

        # 2026-10-05（真机实测事故）：工作目录以外的写、指向工作目录以外的链接，**任何模式都询问**，
        # allow 规则也放宽不了；只有审批时"本会话都允许"把那个目录加进本会话的可写区域
        if (d := self._outside_write(tool, args, cwd, ps, subjects, risk, details)) is not None:
            return d
        # 2026-10-05（决定）：递归扫描盘符根目录 / 用户目录，任何模式都询问（Approve all 也问）。
        # 真机实测：agent 整盘搜索一个配置名，一条命令跑了 10 分钟。"本会话都允许"只放行完全相同的命令
        if (broad := self._broad_scan(tool, args, cwd, ps)) and getattr(args, "command", None) not in self.session_exact:
            details["unanalyzable"] = getattr(args, "command", "")  # 复用"按原文放行"
            return Decision("ask", "Recursively scans a whole drive or the user folder (this can take many minutes): "
                            + ", ".join(broad[:3]), risk, details=details)

        allow_rule = self._rule_hits(self.allow + self.session_allow, tool, subjects, every=True)
        if risk == "dangerous":
            if allow_rule:
                return Decision("allow", f"Dangerous operation, allowed by rule {allow_rule.raw}", risk, allow_rule.raw, details)
            return Decision("ask", f"Dangerous: {risk_reason or 'recoverable, but data will be lost'}", risk, details=details)

        if tool.name == "shell" and ps is not None and not ps.analyzable:
            command = getattr(args, "command", "")
            if command in self.session_exact:
                return Decision("allow", "Allowed for this session (same command)", risk, details=details)
            if mode == "always_approve" and self.paths is not None:
                # Approve all 放行分析不了的命令；只有原文里出现工作目录以外的路径时才问（2026-10-05 决定 1）
                outside = self.paths.outside_writes(raw_paths(command), cwd)
                if not outside:
                    return Decision("allow", "always_approve mode (command not statically analyzable, no paths outside the working directory)",
                                    risk, details=details)
                details["unanalyzable"] = command
                return Decision("ask", "The command cannot be analyzed statically and mentions paths outside the working directory: "
                                + ", ".join(outside[:3]), risk, details=details)
            why = "; ".join((ps.errors + ps.unsafe)[:3])
            details["unanalyzable"] = command
            return Decision("ask", f"The command cannot be analyzed statically ({why})", risk, details=details)

        # 工作目录以外的读取：放在 allow 规则之前，"Read" / "shell(get-content:*)" 这类按工具放行的规则
        # 不覆盖它；要长期放行某个目录，用配置的 [permissions] read_roots 或审批时的"本会话都允许"
        if pc and (pc.outside or pc.unresolved) and mode != "always_approve" and self._reads_only(tool, ps, subjects):
            shown = (pc.outside + pc.unresolved)[:3]
            details["outside"] = pc.outside
            return Decision("ask", "Reads outside the working directory: " + ", ".join(shown), risk, details=details)

        if allow_rule:
            return Decision("allow", f"Matched allow rule {allow_rule.raw}", risk, allow_rule.raw, details)

        # ---- 模式默认
        if self.paths is None:  # 没有路径策略时的旧检查（有路径策略时 _outside_write 已经处理）
            outside = self._outside_cwd(tool, args, cwd)
            if outside and not tool.caps.read_only:
                return Decision("ask", f"Modifies a file outside the working directory: {outside}", risk, details=details)
        if mode == "always_approve":
            return Decision("allow", "always_approve mode", risk, details=details)
        if tool.caps.read_only or risk == "safe":
            return Decision("allow", "read-only or safe operation", risk, details=details)
        if tool.caps.edits_files and mode == "accept_edits":
            return Decision("allow", "accept_edits mode auto-accepts edits", risk, details=details)
        if tool.name == "shell" and ps is not None:
            if not ps.redirects and all(_is_read_only_cmd(s) for s in subjects):
                return Decision("allow", "read-only command", risk, details=details)
            return Decision("ask", "This command is not read-only (it may modify files or start programs)", risk, details=details)
        if getattr(tool, "default_allow", False):
            return Decision("allow", "routine hardware operation (§5.5 allow level)", risk, details=details)
        if getattr(tool, "ask_reason", ""):
            return Decision("ask", tool.ask_reason, risk, details=details)
        if tool.caps.edits_files and mode == "default":
            return Decision("ask", "In the default mode, file edits need your approval (switch to \"Auto-accept edits\" to skip this)", risk, details=details)
        return Decision("ask", getattr(tool, "ask_reason", "") or "This operation needs your approval", risk, details=details)

    def _outside_write(self, tool: Tool, args, cwd: Path, ps: PsAnalysis | None, subjects: list[str], risk: Risk,
                       details: dict) -> Decision | None:
        if self.paths is None:
            return None
        if tool.caps.edits_files and tool.name in FILE_TOOLS:
            targets = [tool.permission_subject(args) or ""]
        elif tool.name == "shell" and ps is not None:
            for c in ps.commands:
                links = link_targets(c)
                if links is None:
                    continue
                # 链接目标只认工作目录（临时目录也不行）：checkpoint 和丢弃会话不该被引到外面去
                bad = [t for t in links if _out_of(cwd, t)]
                if bad:
                    details["outside_write"] = []  # 链接不提供按目录放行
                    return Decision("ask", "Creates a link pointing outside the working directory: " + ", ".join(bad[:3]),
                                    risk, details=details)
            if not ps.analyzable:
                return None  # 分析不了的命令在后面按原文判断
            sure, maybe = shell_write_paths(ps, _is_read_only_cmd)
            outside = self.paths.outside_writes(sure, cwd) + [
                p for p in self.paths.outside_writes(maybe, cwd)
                if "$" in p or not self.paths.is_readable(Path(p), cwd)]  # 工具链目录里的脚本当成读
            if not outside:
                return None
            details["outside_write"] = outside
            return Decision("ask", "May write outside the working directory: " + ", ".join(outside[:3]), risk, details=details)
        else:
            return None
        outside = self.paths.outside_writes(targets, cwd)
        if not outside:
            return None
        details["outside_write"] = outside
        return Decision("ask", "Writes outside the working directory: " + ", ".join(outside[:3]), risk, details=details)

    @staticmethod
    def _broad_scan(tool: Tool, args, cwd: Path, ps: PsAnalysis | None) -> list[str]:
        """递归扫描的目标里有盘符根目录、用户目录（或它的上级）时返回这些目标。"""
        if tool.name == "grep":
            paths = [tool.permission_subject(args) or "."]
        elif tool.name == "shell" and ps is not None:
            text = ps.source or getattr(args, "command", "")
            if not RECURSIVE.search(text):
                return []
            paths = shell_command_paths(ps) + raw_paths(text)
        else:
            return []
        home = Path.home().resolve()
        out = []
        for raw in dict.fromkeys(paths):
            s = expand_vars(raw.strip("'\""))
            if "$" in s or not s:
                continue
            p = Path(os.path.expanduser(s))
            p = (p if p.is_absolute() else cwd / p)
            try:
                p = p.resolve()
            except OSError:
                continue
            if p == Path(p.anchor) or p == home or p == home.parent or p in home.parents:
                out.append(str(p))
        return out

    def _path_check(self, tool: Tool, args, cwd: Path, ps: PsAnalysis | None,
                    subjects: list[str]) -> PathCheck | None:
        if self.paths is None:
            return None
        if tool.name in FILE_TOOLS:
            return self.paths.check_paths([tool.permission_subject(args) or "."], cwd, write=tool.caps.edits_files)
        if tool.name == "shell" and ps is not None:
            write = ps.redirects or not all(_is_read_only_cmd(s) for s in subjects)
            # 参数里的路径 + 原文里出现的盘符路径（2026-10-05：路径放在变量里——$roots=@('D:\','C:\Users')——
            # 原来认不出来，整盘递归搜索在默认模式下也没有询问）
            paths = list(dict.fromkeys(shell_command_paths(ps) + raw_paths(ps.source)))
            return self.paths.check_paths(paths, cwd, write=write)
        return None

    @staticmethod
    def _reads_only(tool: Tool, ps: PsAnalysis | None, subjects: list[str]) -> bool:
        """这次调用会被当成只读放行吗（不是只读的本来就要问，不用再加一道）。"""
        if tool.name == "shell":
            return ps is not None and not ps.redirects and all(_is_read_only_cmd(s) for s in subjects)
        return tool.caps.read_only

    @staticmethod
    def _outside_cwd(tool: Tool, args, cwd: Path) -> str | None:
        if not tool.caps.edits_files:
            return None
        subj = tool.permission_subject(args)
        if not subj:
            return None
        p = Path(subj)
        if not p.is_absolute():
            p = cwd / p
        try:
            p.resolve().relative_to(cwd.resolve())
            return None
        except ValueError:
            return str(p)


# 递归的写法：PowerShell -Recurse（及缩写）、cmd 的 dir /s、findstr /s、tree、rg / grep -r
RECURSIVE = re.compile(r"(?i)(?:^|\s)-r(?:e(?:c(?:u(?:r(?:s(?:e)?)?)?)?)?)?\b|(?:^|\s)/s\b|\btree\b|\brg\b|\bgrep\s+(?:-\w*r)")


def _out_of(cwd: Path, raw: str) -> bool:
    """路径（相对工作目录）在工作目录以外；含变量、解析不了的也算以外。"""
    s = expand_vars(raw)
    if "$" in s or "%" in s or s.startswith("("):
        return True
    p = Path(s)
    p = (p if p.is_absolute() else cwd / p).resolve()
    try:
        p.relative_to(cwd.resolve())
        return False
    except ValueError:
        return True


def _is_read_only_cmd(normalized: str) -> bool:
    return any(normalized == c or normalized.startswith(c + " ") for c in READ_ONLY_SHELL)
