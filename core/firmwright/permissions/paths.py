"""路径策略（W7 修复：只读工具原来对路径没有任何限制）。

演示里执行者用 read_file 翻了 Firmwright 应用数据目录里别的会话历史；同样的路径能读到模型密钥所在的 .env。
这里给文件工具和 shell 加两层：

受保护路径 → 一律拒绝（和 forbidden 一样，规则和模式都放宽不了）
    - 应用数据目录（会话历史、配置、记忆库、串口日志……）。例外：本会话自己的目录（压缩存档要用
      read_file 找回细节）和应用数据里的 skills\\ 可以读，不能写
    - key_ref 指向的密钥文件（dotenv:<路径>#VAR）
    - 名字是 .env / .env.* 的文件（.env.example 之类的模板除外）、用户目录下的 .ssh\\
  参照 grok sandbox 的 deny 列表（user-guide/18-sandbox.md：`deny = ["**/.env", ...]`，读写都拒绝）。
  grok 靠内核沙箱强制；Windows 上没有对应机制，这里在权限层按路径判断。

工作目录以外的读取 → 询问
    grok 的 workspace 档位"哪里都能读"，strict 档位只读 CWD + 系统路径 + ~/.grok。这里取中间：
    工作目录 + 可读区域（平台适配器给的工具链目录，如 ESP-IDF 源码和工具；skill 目录；配置里的
    [permissions] read_roots）不问，其余询问；用户点"本会话都允许"时把那个目录加进本会话的可读区域。

shell 命令按 PowerShell 解析出的参数逐个判断，看起来像路径的参数才算；含变量的路径（$env:X\\…）
解析不了，按"工作目录以外"处理（只读命令也要询问）。
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath

from .ps_parse import PsAnalysis, PsCommand

ENV_TEMPLATES = {".env.example", ".env.sample", ".env.template", ".env.dist"}

# 这些参数的值不是路径（Select-String -Pattern 'TEST:' 之类）
NON_PATH_PARAMS = {
    "pattern", "simplematch", "first", "last", "skip", "skiplast", "encoding", "property", "expandproperty",
    "context", "delimiter", "format", "depth", "tail", "totalcount", "head", "index", "value", "inputobject",
    "message", "object", "n", "max-count", "since", "until", "author", "grep", "pretty",
}
# 值是路径的参数（值不算位置参数）
PATH_PARAMS = {"path", "literalpath", "lp", "pspath", "filepath", "destination", "file", "directory", "workingdirectory"}
# 第一个位置参数是搜索模式而不是路径的命令
PATTERN_FIRST = {"select-string", "sls", "findstr", "where-object", "where", "?", "echo", "write-output",
                 "write-host", "select-object", "select", "sort-object", "sort", "format-table", "ft",
                 "format-list", "fl", "measure-object", "get-command", "gcm", "get-date", "where.exe"}
# 参数全都不是路径的命令
NO_PATH_CMDS = {"echo", "write-output", "write-host", "get-date", "get-command", "gcm", "where-object", "where",
                "?", "select-object", "select", "sort-object", "sort", "format-table", "ft", "format-list", "fl",
                "measure-object", "out-string", "where.exe"}


def _norm(p: Path) -> str:
    return os.path.normcase(str(p))


def _under(p: Path, root: Path) -> bool:
    a, b = _norm(p), _norm(root).rstrip("\\/")
    return a == b or a.startswith(b + os.sep)


def _resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except (OSError, RuntimeError, ValueError):
        return Path(os.path.abspath(p))


def dotenv_files(key_refs: list[str]) -> list[Path]:
    out = []
    for ref in key_refs:
        if ref.startswith("dotenv:"):
            path, _, _ = ref[7:].rpartition("#")
            if path:
                out.append(Path(path))
    return out


@dataclass
class PathCheck:
    protected: list[str] = field(default_factory=list)  # 命中受保护路径的
    outside: list[str] = field(default_factory=list)  # 在工作目录和可读区域之外的（已解析成绝对路径）
    unresolved: list[str] = field(default_factory=list)  # 含变量等、静态解析不了的路径参数


@dataclass
class PathPolicy:
    protected: list[Path] = field(default_factory=list)  # 整个目录 / 文件受保护
    readable_inside_protected: list[Path] = field(default_factory=list)  # 受保护目录里允许只读的部分
    read_roots: list[Path] = field(default_factory=list)  # 工作目录之外不用问就能读的地方
    session_read_roots: list[Path] = field(default_factory=list)  # 用户在审批时"本会话都允许"的目录
    # 2026-10-05：工作目录之外可以直接写的地方（系统临时目录）；其余的写一律询问，任何模式都一样
    write_roots: list[Path] = field(default_factory=lambda: [Path(tempfile.gettempdir())])
    session_write_roots: list[Path] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.protected = [_resolve(p) for p in self.protected]
        self.readable_inside_protected = [_resolve(p) for p in self.readable_inside_protected]
        self.read_roots = [_resolve(p) for p in self.read_roots]
        self.write_roots = [_resolve(p) for p in self.write_roots]

    def add_session_write_root(self, path: str | Path) -> None:
        p = _resolve(Path(path))
        if not any(_under(p, r) for r in self.session_write_roots):
            self.session_write_roots.append(p)

    def is_writable(self, p: Path, cwd: Path) -> bool:
        return _under(p, _resolve(cwd)) or any(_under(p, r) for r in (*self.write_roots, *self.session_write_roots))

    def outside_writes(self, paths: list[str], cwd: Path) -> list[str]:
        """写到工作目录（和临时目录）以外的路径。环境变量先展开；展开后仍含变量的按"以外"处理（原样返回）。"""
        out: list[str] = []
        for raw in paths:
            if not raw:
                continue
            s = expand_vars(raw)
            if "$" in s or "%" in s:
                out.append(raw)
                continue
            p = Path(os.path.expanduser(s))
            if not p.is_absolute():
                p = cwd / p
            p = _resolve(p)
            if not self.is_writable(p, cwd):
                out.append(str(p))
        return out

    def add_session_root(self, path: str | Path) -> None:
        p = _resolve(Path(path))
        if not any(_under(p, r) for r in self.session_read_roots):
            self.session_read_roots.append(p)

    # ------------------------------------------------------------------

    def is_protected(self, p: Path, *, write: bool) -> bool:
        name = p.name.lower()
        if (name == ".env" or name.startswith(".env.")) and name not in ENV_TEMPLATES:
            return True
        if any(part.lower() == ".ssh" for part in p.parts) and _under(p, Path.home()):
            return True
        for root in self.protected:
            if _under(p, root):
                if not write and any(_under(p, r) for r in self.readable_inside_protected):
                    return False
                return True
        return False

    def is_readable(self, p: Path, cwd: Path) -> bool:
        if _under(p, _resolve(cwd)):
            return True
        return any(_under(p, r) for r in (*self.read_roots, *self.readable_inside_protected, *self.session_read_roots))

    def check_paths(self, paths: list[str], cwd: Path, *, write: bool) -> PathCheck:
        res = PathCheck()
        for raw in paths:
            if not raw:
                continue
            if "$" in raw or "%" in raw:
                res.unresolved.append(raw)
                continue
            p = Path(os.path.expanduser(raw))
            if not p.is_absolute():
                p = cwd / p
            p = _resolve(p)
            if self.is_protected(p, write=write):
                res.protected.append(raw)
            elif not self.is_readable(p, cwd):
                res.outside.append(str(p))
        return res


# ---------------------------------------------------------------------- shell 参数里的路径


def _looks_like_path(arg: str) -> bool:
    if not arg or arg[0] in "{(@[":
        return False  # 脚本块、子表达式、哈希表、类型字面量
    if re.match(r"^[a-z][a-z0-9+.-]*://", arg, re.I):
        return False  # URL
    if re.fullmatch(r"-?\d+(\.\d+)?", arg):
        return False
    if "$" in arg:
        # 只有明显是路径的变量表达式才算（$env:LOCALAPPDATA\…、$HOME/…）；$_.Name 之类不算
        return bool(re.search(r"[\\/]", arg)) or arg.lower().startswith(("$env:", "$home", "${env:"))
    return True


def shell_paths(cmd: PsCommand) -> list[str]:
    """一个命令里看起来是路径的参数（去掉引号）。"""
    name = cmd.name.lower()
    if name in NO_PATH_CMDS:
        return []
    out: list[str] = []
    skip_next = path_next = False
    positional = 0
    args = cmd.args
    if name == "git":
        args = _git_path_args(args)
    for a in args:
        a = a.strip()
        if len(a) >= 2 and a[0] == a[-1] and a[0] in "'\"":
            a = a[1:-1]
        if skip_next:
            skip_next = False
            continue
        if path_next:
            path_next = False
            if _looks_like_path(a):
                out.append(a)
            continue
        if a.startswith("-") and len(a) > 1 and not a[1:2].isdigit():
            pname, sep, val = a.lstrip("-").partition(":")
            pname = pname.lower()
            if sep and val and pname not in NON_PATH_PARAMS:
                if _looks_like_path(val):
                    out.append(val)
            elif not sep and pname in NON_PATH_PARAMS:
                skip_next = True
                if pname == "pattern":
                    positional += 1  # 模式已经按名字给了，后面的位置参数都是路径
            elif not sep and pname in PATH_PARAMS:
                path_next = True
            continue
        if a.startswith("/") and len(a) <= 3 and name in ("findstr", "dir", "tree"):
            continue  # cmd 风格的开关 /s /i
        positional += 1
        if positional == 1 and name in PATTERN_FIRST:
            continue
        if _looks_like_path(a):
            out.append(a)
    return out


def _git_path_args(args: list[str]) -> list[str]:
    """git：只看 -C <目录>、--git-dir / --work-tree，以及 "--" 之后的路径。提交号、分支名不算路径。"""
    out: list[str] = []
    it = iter(range(len(args)))
    after_dd = False
    for i in it:
        a = args[i]
        if after_dd:
            out.append(a)
        elif a == "--":
            after_dd = True
        elif a == "-C" and i + 1 < len(args):
            out.append(args[i + 1])
            next(it, None)
        elif a.startswith(("--git-dir=", "--work-tree=")):
            out.append(a.split("=", 1)[1])
        elif re.match(r"^[\w.-]+:(?![\\/])", a):
            continue  # HEAD:path、rev:path（仓库内对象，不是磁盘路径）
        elif PureWindowsPath(a).is_absolute() or a.startswith(("..", "~")):
            out.append(a)
    return out


def shell_command_paths(ps: PsAnalysis) -> list[str]:
    paths: list[str] = []
    for c in ps.commands:
        paths += shell_paths(c)
    return paths


# ---------------------------------------------------------------------- 写到哪里（2026-10-05）

COPY_CMDS = {"copy-item", "cp", "copy", "cpi", "move-item", "mv", "move", "mi", "robocopy", "xcopy"}
LINK_TYPES = {"junction", "symboliclink", "hardlink"}


def expand_vars(s: str) -> str:
    """$env:NAME / ${env:NAME} / %NAME% / $HOME 展开成实际值（展开不了的原样留着）。"""
    s = re.sub(r"\$\{env:(\w+)\}|\$env:(\w+)", lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)), s, flags=re.I)
    s = re.sub(r"%(\w+)%", lambda m: os.environ.get(m.group(1), m.group(0)), s)
    return re.sub(r"^\$home\b", lambda m: str(Path.home()), s, flags=re.I)


def link_targets(cmd: PsCommand) -> list[str] | None:
    """New-Item -ItemType Junction/SymbolicLink/HardLink 的链接目标；不是建链接的命令返回 None。"""
    if cmd.name.lower() not in ("new-item", "ni"):
        return None
    args = [a.strip().strip("'\"") for a in cmd.args]
    lowered = [a.lower() for a in args]
    kind = None
    for i, a in enumerate(lowered):
        if a.startswith("-itemtype") or a in ("-type", "-it"):
            kind = a.split(":", 1)[1] if ":" in a else (lowered[i + 1] if i + 1 < len(lowered) else "")
    if kind not in LINK_TYPES:
        return None
    out = []
    for i, a in enumerate(lowered):
        m = re.match(r"-(target|value)(:.*)?$", a)
        if m:
            out.append(args[i].split(":", 1)[1] if m.group(2) else (args[i + 1] if i + 1 < len(args) else ""))
    return [t.strip("'\"") for t in out if t] or ["(unknown target)"]


def shell_write_paths(ps: PsAnalysis, is_read_only) -> tuple[list[str], list[str]]:
    """一条 shell 命令可能写到的路径，分两类返回 (sure, maybe)：
    sure  = 重定向的文件、复制 / 移动的目标——一定是写
    maybe = 其他不是只读的命令的路径参数（python <工具链里的脚本> 这类多半是读），可读区域里的不算
    """
    sure = list(ps.redirect_targets)
    maybe: list[str] = []
    for c in ps.commands:
        if is_read_only(c.normalized):
            continue
        paths = shell_paths(c)
        name = c.name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        if name in COPY_CMDS and paths:
            dest = _named_value(c.args, "destination")
            sure += [dest] if dest else paths[-1:]
        else:
            maybe += paths
    return sure, maybe


def _named_value(args: list[str], pname: str) -> str | None:
    for i, a in enumerate(args):
        low = a.lower()
        if low.startswith(f"-{pname}:"):
            return a.split(":", 1)[1].strip("'\"")
        if low == f"-{pname}" and i + 1 < len(args):
            return args[i + 1].strip("'\"")
    return None


# 盘符路径；\\server\share（服务器名和共享名都得是普通字符，正则里的 '\\build\\' 不算）；..\ 开头的相对路径
_ABS = re.compile(r"""(?<![\w$\\])(?:[A-Za-z]:[\\/][^\s'"`;|,)(}]*|\\\\[\w.-]+\\[\w$.-]+[^\s'"`;|,)(}]*|\.\.[\\/][^\s'"`;|,)(}]*)""")


def raw_paths(text: str) -> list[str]:
    """命令原文里的绝对路径（C:\\…、\\\\server\\…）和 ..\\ 开头的相对路径：给无法静态分析的命令用。"""
    return [m.group(0).rstrip(".") for m in _ABS.finditer(expand_vars(text))]
