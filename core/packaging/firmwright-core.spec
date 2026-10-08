# PyInstaller 配置：发布版的 Python 核心（I15：onedir，不用 onefile，减少杀毒误报、启动快）。
# 用法（在 core 目录）：.venv\Scripts\python -m PyInstaller packaging\firmwright-core.spec --noconfirm
# 产物：core\dist\firmwright-core\firmwright-core.exe（+ _internal\），桌面端打包时整个目录放进 resources\core。
# 内置 skill 作为数据打进 _internal\skills（context/skills.py 的 builtin_dir 在打包后从这里读）。
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parents[1]  # noqa: F821 — SPECPATH 由 PyInstaller 注入

hidden = (
    # 评测代码（firmwright.eval）不开源、也不随应用发布
    [m for m in collect_submodules("firmwright") if not m.startswith("firmwright.eval")]
    + collect_submodules("keyring.backends")  # 凭据管理器后端按入口点加载，静态分析看不到
    + ["win32ctypes.core", "serial.tools.list_ports_windows"]
)

# 本机的 Python 是 miniconda 的：_sqlite3 / _ssl 等扩展依赖的 DLL 在 <base>\Library\bin，PyInstaller 找不到
# （打包后报 "DLL load failed while importing _sqlite3"）。存在就显式带上；官方 Python 没有这个目录，跳过
_conda_bin = Path(sys.base_prefix) / "Library" / "bin"
_dlls = ["sqlite3.dll", "libssl-3-x64.dll", "libcrypto-3-x64.dll", "ffi.dll", "ffi-8.dll", "ffi-7.dll",
         "libbz2.dll", "liblzma.dll", "libexpat.dll", "expat.dll", "zlib.dll"]
binaries = [(str(_conda_bin / d), ".") for d in _dlls if (_conda_bin / d).is_file()]

a = Analysis(  # noqa: F821
    [str(Path(SPECPATH) / "firmwright_core.py")],  # noqa: F821
    pathex=[str(ROOT / "core")],
    binaries=binaries,
    datas=[(str(ROOT / "skills"), "skills")],
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "numpy", "firmwright.eval"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="firmwright-core",
    console=True,  # 核心走 stdio（ACP）；桌面端启动它时 windowsHide，不会弹黑窗口
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="firmwright-core", upx=False)  # noqa: F821
