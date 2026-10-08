"""D16：用 PowerShell 自带的语法解析器拆分命令，分析不了就询问。

grok 用 tree-sitter-bash 拆分命令，但 Windows 默认 shell 是 PowerShell，语法不同，
所以这里调用 System.Management.Automation.Language.Parser，把 AST 里的每个
CommandAst 拿出来，逐个做权限匹配。

以下情况标记为"无法静态分析"（unanalyzable），权限层一律询问：
  - 命令名不是字面量（& $x、& (Get-X)）
  - Invoke-Expression / iex、点号引入（. script.ps1）、Start-Process 之类间接执行
  - .NET 方法调用（[IO.File]::Delete(...)、$obj.Method()）
  - 语法错误
另外记录是否有重定向到文件（> / >>），用于判断是否只读。
"""

from __future__ import annotations

import asyncio
import base64
import json
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

from ..osal import powershell_exe

_SCRIPT = r"""
$src = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__B64__'))
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$tokens, [ref]$errors)
$L = 'System.Management.Automation.Language.'
$res = @{ errors = @($errors | ForEach-Object { $_.Message }); commands = @(); unsafe = @(); redirects = $false; redirect_targets = @() }
$cmds = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)
foreach ($c in $cmds) {
  $els = @()
  foreach ($e in $c.CommandElements) {
    if ($e -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $els += $e.Value }
    else { $els += $e.Extent.Text }
  }
  $first = $c.CommandElements[0]
  $dyn = -not ($first -is [System.Management.Automation.Language.StringConstantExpressionAst])
  if ($c.InvocationOperator -eq 'Dot') { $res.unsafe += ('dot-source: ' + $c.Extent.Text) }
  if ($dyn) { $res.unsafe += ('dynamic command: ' + $c.Extent.Text) }
  $res.commands += ,@{ name = $(if ($dyn) { $first.Extent.Text } else { $first.Value }); args = @($els | Select-Object -Skip 1); text = $c.Extent.Text }
  foreach ($r in $c.Redirections) { if ($r -is [System.Management.Automation.Language.FileRedirectionAst]) {
    $loc = $r.Location.Extent.Text
    if ($loc -notmatch '^(\$null|nul)$') { $res.redirects = $true; $res.redirect_targets += ($loc -replace '^[''"]|[''"]$', '') } } }
}
$inv = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)
foreach ($i in $inv) { $res.unsafe += ('method call: ' + $i.Extent.Text) }
$res | ConvertTo-Json -Depth 6 -Compress
"""

# 间接执行任意代码的命令：出现就视为无法分析
INDIRECT = {
    "invoke-expression", "iex", "start-process", "saps", "start", "invoke-command", "icm",
    "start-job", "sajb", "invoke-item", "ii", "add-type", "set-alias", "new-alias",
    "powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe", "bash", "bash.exe",
    "wsl", "wsl.exe",
}


@dataclass
class PsCommand:
    name: str
    args: list[str]
    text: str

    @property
    def normalized(self) -> str:
        """用于规则匹配的形式：小写命令名（去掉 .exe）+ 空格分隔的参数。"""
        name = self.name.lower()
        if name.endswith(".exe"):
            name = name[:-4]
        return " ".join([name, *self.args]).strip()


@dataclass
class PsAnalysis:
    commands: list[PsCommand] = field(default_factory=list)
    unsafe: list[str] = field(default_factory=list)  # 无法静态分析的原因
    errors: list[str] = field(default_factory=list)
    redirects: bool = False
    redirect_targets: list[str] = field(default_factory=list)  # > / >> 写到的文件
    source: str = ""  # 命令原文（无法分析时按原文里的绝对路径判断）

    @property
    def analyzable(self) -> bool:
        return not self.unsafe and not self.errors


def _analyze_sync(command: str) -> PsAnalysis:
    b64 = base64.b64encode(command.encode("utf-8")).decode()
    script = _SCRIPT.replace("__B64__", b64)
    try:
        proc = subprocess.run(
            [powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        return PsAnalysis(errors=[f"parser unavailable: {e}"], source=command)

    def as_list(v) -> list:
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    res = PsAnalysis(
        errors=[str(x) for x in as_list(data.get("errors"))],
        unsafe=[str(x) for x in as_list(data.get("unsafe"))],
        redirects=bool(data.get("redirects")),
        redirect_targets=[str(x) for x in as_list(data.get("redirect_targets"))],
        source=command,
    )
    for c in as_list(data.get("commands")):
        cmd = PsCommand(name=str(c.get("name", "")), args=[str(a) for a in as_list(c.get("args"))],
                        text=str(c.get("text", "")))
        res.commands.append(cmd)
        if cmd.name.lower() in INDIRECT:
            res.unsafe.append(f"indirect execution: {cmd.text}")
    return res


@lru_cache(maxsize=512)
def analyze_cached(command: str) -> PsAnalysis:
    return _analyze_sync(command)


async def analyze(command: str) -> PsAnalysis:
    return await asyncio.to_thread(analyze_cached, command)
