"""找本机的 ESP-IDF 安装（首次启动向导，2026-10-06）。

原来只认 C:\\Espressif\\tools\\eim_idf.json 一个位置；发布后别人的 IDF 可能装在别的盘、用旧版离线安装器装、
或者自己 git clone 后跑 install.bat。三种来源：
- eim   ：EIM（ESP-IDF Installation Manager）写的 eim_idf.json，列出装好的版本、激活脚本、IDF 自己的 Python
- legacy：旧版安装器 / install.bat 的布局：IDF 根目录里有 export.ps1，工具和 Python 虚拟环境在
          IDF_TOOLS_PATH（缺省 %USERPROFILE%\\.espressif）的 python_env\\idf<主>.<次>_py*_env 下；
          idf-env.json 里记着装过的 IDF 路径
- folder：用户在向导里手动选的文件夹，按 legacy 的方式解析（选到 EIM 装的 IDF 时换成对应的 eim 条目）

只读文件，不运行任何脚本；真正激活环境在 env.IdfEnv 里（第一次编译时）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import string
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .env import IdfInstall

TESTED = (5, 5)  # 支持范围：ESP-IDF v5.5（CLAUDE.md）


class IdfCandidate(BaseModel):
    source: Literal["eim", "legacy", "folder"]
    id: str  # eim：安装 id（v5.5）；其他：由路径生成
    path: str  # IDF_PATH
    version: str | None = None  # "5.5.0"
    python: str | None = None
    activation_script: str | None = None
    tools_path: str | None = None
    eim_json: str | None = None  # 只有 eim
    problem: str | None = None  # 不能用的原因（英文，给界面）
    warning: str | None = None  # 能用但要提醒的（版本不是 v5.5）

    @property
    def usable(self) -> bool:
        return self.problem is None

    def install(self) -> IdfInstall:
        if self.problem or not (self.python and self.activation_script and self.tools_path):
            raise RuntimeError(self.problem or f"Incomplete ESP-IDF installation at {self.path}")
        return IdfInstall(id=self.id, path=self.path, python=self.python, activation_script=self.activation_script,
                          tools_path=self.tools_path, kind="eim" if self.source == "eim" else "export")


def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def read_version(idf_path: str | Path) -> str | None:
    """tools/cmake/version.cmake 里的 IDF_VERSION_MAJOR / MINOR / PATCH。"""
    f = Path(idf_path) / "tools" / "cmake" / "version.cmake"
    try:
        text = f.read_text("utf-8", errors="replace")
    except OSError:
        return None
    parts = [re.search(rf"IDF_VERSION_{k}\s+(\d+)", text) for k in ("MAJOR", "MINOR", "PATCH")]
    if not all(parts):
        return None
    return ".".join(m.group(1) for m in parts if m)


def _version_warning(version: str | None) -> str | None:
    if version is None:
        return "Could not read the ESP-IDF version"
    major, minor = (int(x) for x in version.split(".")[:2])
    if (major, minor) != TESTED:
        return f"Firmwright is tested with ESP-IDF v{TESTED[0]}.{TESTED[1]}; v{version} may behave differently"
    return None


def is_idf_root(p: Path) -> bool:
    return (p / "tools" / "idf.py").is_file()


# ------------------------------------------------------------------ EIM

def eim_candidates(eim_json: Path) -> list[IdfCandidate]:
    try:
        data = json.loads(eim_json.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for inst in data.get("idfInstalled", []) or []:
        try:
            c = IdfCandidate(source="eim", id=inst["id"], path=inst["path"], python=inst.get("python"),
                             activation_script=inst.get("activationScript"), tools_path=inst.get("idfToolsPath"),
                             eim_json=str(eim_json))
        except (KeyError, TypeError):
            continue
        c.version = read_version(c.path)
        c.problem = _missing(c)
        c.warning = None if c.problem else _version_warning(c.version)
        out.append(c)
    # 选中的那个排前面
    sel = data.get("idfSelectedId")
    out.sort(key=lambda c: c.id != sel)
    return out


def _missing(c: IdfCandidate) -> str | None:
    if not is_idf_root(Path(c.path)):
        return f"ESP-IDF not found at {c.path}"
    if not c.activation_script or not Path(c.activation_script).is_file():
        return "The activation script is missing; repair the installation"
    if not c.python or not Path(c.python).is_file():
        return "ESP-IDF's Python environment is missing; run the installer (install.bat) again"
    return None


def eim_json_paths() -> list[Path]:
    """各个盘的 <盘>:\\Espressif\\tools\\eim_idf.json（EIM 的默认位置），加上 IDF_TOOLS_PATH 下的。"""
    found: list[Path] = []
    if tp := os.environ.get("IDF_TOOLS_PATH"):
        found.append(Path(tp) / "eim_idf.json")
    for d in fixed_drives():
        found.append(Path(f"{d}:\\") / "Espressif" / "tools" / "eim_idf.json")
    return [p for p in dict.fromkeys(found) if p.is_file()]


def fixed_drives() -> list[str]:
    """本地硬盘的盘符。只看固定磁盘：断开的网络盘 / 光驱访问一次可能卡十几秒。"""
    if os.name != "nt":
        return []
    import ctypes

    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    return [d for i, d in enumerate(string.ascii_uppercase)
            if mask >> i & 1 and k32.GetDriveTypeW(f"{d}:\\") == 3]  # 3 = DRIVE_FIXED


# ------------------------------------------------------------------ install.bat / 旧版安装器

def default_tools_path() -> Path:
    return Path(os.environ.get("IDF_TOOLS_PATH") or Path.home() / ".espressif")


def find_python(tools_path: Path, version: str | None) -> Path | None:
    """<IDF_TOOLS_PATH>\\python_env\\idf5.5_py3.11_env\\Scripts\\python.exe；有多个 Python 版本时取最新的。"""
    if not version:
        return None
    mm = ".".join(version.split(".")[:2])
    envs = sorted((tools_path / "python_env").glob(f"idf{mm}_py*_env"), reverse=True)
    for e in envs:
        exe = e / "Scripts" / "python.exe"
        if exe.is_file():
            return exe
    return None


def folder_candidate(idf_path: Path, tools_path: Path | None = None, *, source: str = "folder") -> IdfCandidate:
    tools = tools_path or default_tools_path()
    version = read_version(idf_path)
    py = find_python(tools, version)
    export = idf_path / "export.ps1"
    c = IdfCandidate(source=source, id="idf-" + hashlib.sha1(_norm(idf_path).encode()).hexdigest()[:8],  # type: ignore[arg-type]
                     path=str(idf_path), version=version, python=str(py) if py else None,
                     activation_script=str(export) if export.is_file() else None, tools_path=str(tools))
    if not is_idf_root(idf_path):
        c.problem = f"Not an ESP-IDF folder (no tools\\idf.py in {idf_path})"
    elif c.activation_script is None:
        c.problem = "export.ps1 is missing from this ESP-IDF folder"
    elif py is None:
        c.problem = (f"ESP-IDF's tools are not installed in {tools}; run install.bat in {idf_path} first"
                     if version else "Could not read the ESP-IDF version")
    c.warning = None if c.problem else _version_warning(version)
    return c


def legacy_candidates() -> list[IdfCandidate]:
    out = []
    tools = default_tools_path()
    paths: list[str | None] = []
    try:
        data = json.loads((tools / "idf-env.json").read_text("utf-8"))
        paths += [v.get("path") for v in (data.get("idfInstalled") or {}).values() if isinstance(v, dict)]
    except (OSError, ValueError, AttributeError):
        pass
    if env := os.environ.get("IDF_PATH"):
        paths.append(env)
    for p in dict.fromkeys(x for x in paths if x):
        if is_idf_root(Path(p)):
            out.append(folder_candidate(Path(p), tools, source="legacy"))
    return out


# ------------------------------------------------------------------ 汇总

def discover(extra_eim: Path | None = None) -> list[IdfCandidate]:
    """本机所有能找到的 IDF；同一个 IDF 路径只留一条（EIM 优先：它的激活脚本和 Python 是确定的）。
    能用的排前面，同为能用时 v5.5 排前面。"""
    eims = [extra_eim] if extra_eim and extra_eim.is_file() else []
    seen: dict[str, IdfCandidate] = {}
    for j in dict.fromkeys([*eims, *eim_json_paths()]):
        for c in eim_candidates(j):
            seen.setdefault(_norm(c.path), c)
    for c in legacy_candidates():
        seen.setdefault(_norm(c.path), c)
    return sorted(seen.values(), key=lambda c: (not c.usable, c.warning is not None))


def inspect_folder(folder: Path, known: list[IdfCandidate] | None = None) -> list[IdfCandidate]:
    """向导里"浏览…"选的文件夹：可以是 IDF 根目录、它的上一级（C:\\Espressif\\v5.5）、
    或者 EIM 的根目录 / tools 目录（里面有 eim_idf.json）。"""
    known = known if known is not None else discover()
    by_path = {_norm(c.path): c for c in known if c.source == "eim"}
    for j in (folder / "eim_idf.json", folder / "tools" / "eim_idf.json"):
        if j.is_file():
            return eim_candidates(j)
    roots = [folder] if is_idf_root(folder) else []
    if not roots:
        try:
            roots = [d for d in sorted(folder.iterdir()) if d.is_dir() and is_idf_root(d)][:8]
        except OSError:
            roots = []
    if not roots:
        return [IdfCandidate(source="folder", id="", path=str(folder),
                             problem="No ESP-IDF found in this folder. Pick the esp-idf folder (the one with install.bat).")]
    return [by_path.get(_norm(r)) or folder_candidate(r) for r in roots]
