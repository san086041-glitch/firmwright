"""ESP-IDF 环境缓存（I11 / I15）。

思路同 ESP-IDF 官方 MCP 的 `eim run`：由 EIM 安装的激活脚本建立环境。我们在一个干净的
PowerShell 里执行激活脚本，把它改动过的环境变量存成快照
（%LOCALAPPDATA%\\Firmwright\\idf-env-cache\\<版本>.json），之后每个子进程直接带上这份环境，
不用每条命令都重新激活（grok 的 shell_state 只在 unix 上可用）。

agent 自己的 Python 和 ESP-IDF 的 Python 严格分开：调用 idf.py 时用 IDF 自己的 venv。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from pydantic import BaseModel

from ...config import app_home
from ...osal import powershell_exe, run_process

# Git Bash / MSYS 带进来的变量会让 idf.py 直接退出（"MSys/Mingw is no longer supported"）
MSYS_VARS = ("MSYSTEM", "MSYSTEM_PREFIX", "MSYSTEM_CHOST", "MSYSTEM_CARCH", "MINGW_PREFIX", "MINGW_CHOST",
             "MINGW_PACKAGE_PREFIX")


class IdfInstall(BaseModel):
    id: str
    path: str  # IDF_PATH
    python: str  # IDF 自己的 venv python
    activation_script: str
    tools_path: str
    # eim：EIM 生成的激活脚本，自己设好全部变量；export：IDF 自带的 export.ps1（install.bat / 旧版安装器的布局），
    # 要先告诉它 IDF_PATH 和 IDF_TOOLS_PATH（discover.py）
    kind: str = "eim"


def load_eim(eim_json: Path, idf_id: str | None = None) -> IdfInstall:
    data = json.loads(eim_json.read_text("utf-8"))
    want = idf_id or data.get("idfSelectedId")
    for inst in data.get("idfInstalled", []):
        if inst.get("id") == want:
            return IdfInstall(id=inst["id"], path=inst["path"], python=inst["python"],
                              activation_script=inst["activationScript"], tools_path=inst["idfToolsPath"])
    raise RuntimeError(f"No ESP-IDF installation with id={want} in {eim_json}")


def clean_base_env() -> dict[str, str]:
    env = dict(os.environ)
    for k in MSYS_VARS:
        env.pop(k, None)
    return env


_DUMP = r"""
[Console]::OutputEncoding=[Text.Encoding]::UTF8
$before=@{}; Get-ChildItem env: | ForEach-Object { $before[$_.Name]=$_.Value }
. '__SCRIPT__' *> $null
$d=@{}; Get-ChildItem env: | ForEach-Object { if ($before[$_.Name] -ne $_.Value) { $d[$_.Name]=$_.Value } }
$d | ConvertTo-Json -Compress
"""


class IdfEnv:
    """按需加载、带文件缓存的 IDF 环境。缓存以激活脚本的修改时间作为失效依据。"""

    def __init__(self, install: IdfInstall, cache_dir: Path | None = None) -> None:
        self.install = install
        self.cache_dir = cache_dir or app_home() / "idf-env-cache"
        self._env: dict[str, str] | None = None
        self._lock = asyncio.Lock()

    @property
    def cache_file(self) -> Path:
        return self.cache_dir / f"{self.install.id}.json"

    def _stamp(self) -> float:
        return Path(self.install.activation_script).stat().st_mtime

    async def delta(self) -> dict[str, str]:
        """激活脚本带来的环境变量增量（已去掉 MSYS 变量）。"""
        async with self._lock:
            if self._env is not None:
                return self._env
            cf = self.cache_file
            if cf.exists():
                try:
                    cached = json.loads(cf.read_text("utf-8"))
                    if cached.get("stamp") == self._stamp():
                        env: dict[str, str] = cached["env"]
                        self._env = env
                        return env
                except (ValueError, KeyError, OSError):
                    pass
            script = _DUMP.replace("__SCRIPT__", self.install.activation_script.replace("'", "''"))
            base = clean_base_env()
            if self.install.kind == "export":
                base["IDF_PATH"], base["IDF_TOOLS_PATH"] = self.install.path, self.install.tools_path
            res = await run_process([powershell_exe(), "-NoLogo", "-NoProfile", "-NonInteractive",
                                     "-ExecutionPolicy", "Bypass", "-Command", script],
                                    env=base, timeout=180)
            try:
                env = json.loads(res.stdout.strip().lstrip("﻿"))
            except ValueError as e:
                raise RuntimeError(f"Reading the environment from the ESP-IDF activation script failed: {res.output[-1000:]}") from e
            if "IDF_PATH" not in env:
                env["IDF_PATH"] = self.install.path
            if self.install.kind == "export":  # 事先设好的变量不在增量里，补上
                env.setdefault("IDF_TOOLS_PATH", self.install.tools_path)
            for k in MSYS_VARS:
                env.pop(k, None)
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            cf.write_text(json.dumps({"stamp": self._stamp(), "env": env}, ensure_ascii=False, indent=1), "utf-8")
            self._env = env
            return env

    async def full(self) -> dict[str, str]:
        """完整的子进程环境 = 当前环境（去掉 MSYS）+ IDF 增量。"""
        env = clean_base_env()
        env.update(await self.delta())
        env.setdefault("IDF_CCACHE_ENABLE", "1")  # 每个 worktree 的 build 目录独立，ccache 能省掉大部分重编
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def idf_py(self) -> list[str]:
        return [self.install.python, str(Path(self.install.path) / "tools" / "idf.py")]
