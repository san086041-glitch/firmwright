"""首次启动向导（2026-10-06）：找本机的 ESP-IDF、手动选文件夹、选了之后立刻生效并记进 settings.json。
全部用临时目录里造的假 IDF，不读本机真实的安装。"""

import json
from pathlib import Path

import pytest

from firmwright.config import Config, IdfConfig
from firmwright.platform.esp_idf import discover as d
from firmwright.runtime import Runtime
from firmwright.ui_settings import IdfChoice


def fake_idf(root: Path, version: str = "5.5.0") -> Path:
    major, minor, patch = version.split(".")
    (root / "tools" / "cmake").mkdir(parents=True)
    (root / "tools" / "idf.py").write_text("", "utf-8")
    (root / "tools" / "cmake" / "version.cmake").write_text(
        f"set(IDF_VERSION_MAJOR {major})\nset(IDF_VERSION_MINOR {minor})\nset(IDF_VERSION_PATCH {patch})\n", "utf-8")
    (root / "export.ps1").write_text("", "utf-8")
    return root


def fake_tools(tools: Path, mm: str = "5.5") -> Path:
    exe = tools / "python_env" / f"idf{mm}_py3.11_env" / "Scripts" / "python.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", "utf-8")
    return exe


def fake_eim(tmp: Path) -> Path:
    """EIM 布局：<根>\\v5.5\\esp-idf + <根>\\tools\\eim_idf.json（激活脚本和 Python 在 tools 下）。"""
    idf = fake_idf(tmp / "Espressif" / "v5.5" / "esp-idf")
    tools = tmp / "Espressif" / "tools"
    py = tools / "python" / "v5.5" / "venv" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", "utf-8")
    act = tools / "Microsoft.v5.5.PowerShell_profile.ps1"
    act.write_text("", "utf-8")
    j = tools / "eim_idf.json"
    j.write_text(json.dumps({"idfSelectedId": "v5.5", "idfInstalled": [
        {"id": "v5.5", "path": str(idf), "python": str(py), "activationScript": str(act), "idfToolsPath": str(tools)},
        {"id": "v5.4", "path": str(tmp / "missing"), "python": str(py), "activationScript": str(act),
         "idfToolsPath": str(tools)},
    ]}), "utf-8")
    return j


def test_eim_json_lists_usable_and_broken_installs(tmp_path):
    cands = d.eim_candidates(fake_eim(tmp_path))
    assert [c.id for c in cands] == ["v5.5", "v5.4"]
    ok, broken = cands
    assert ok.usable and ok.version == "5.5.0" and ok.warning is None
    assert ok.install().kind == "eim"
    assert not broken.usable and "not found" in (broken.problem or "")


def test_install_bat_layout_needs_its_python_env(tmp_path):
    idf = fake_idf(tmp_path / "esp-idf")
    tools = tmp_path / ".espressif"
    c = d.folder_candidate(idf, tools)
    assert not c.usable and "install.bat" in (c.problem or "")  # 克隆了还没跑 install.bat
    exe = fake_tools(tools)
    c = d.folder_candidate(idf, tools)
    assert c.usable and c.python == str(exe) and c.activation_script == str(idf / "export.ps1")
    inst = c.install()
    assert inst.kind == "export" and inst.tools_path == str(tools)


def test_other_versions_are_usable_with_a_warning(tmp_path):
    idf = fake_idf(tmp_path / "esp-idf", "5.3.2")
    fake_tools(tmp_path / ".espressif", "5.3")
    c = d.folder_candidate(idf, tmp_path / ".espressif")
    assert c.usable and "v5.5" in (c.warning or "")


def test_inspect_folder_accepts_root_parent_or_eim_dir(tmp_path):
    j = fake_eim(tmp_path)
    known = d.eim_candidates(j)
    # EIM 装的 IDF：上一级文件夹 → 换成 EIM 的条目（激活脚本、Python 都是确定的）
    got = d.inspect_folder(tmp_path / "Espressif" / "v5.5", known)
    assert [(c.source, c.id) for c in got] == [("eim", "v5.5")]
    # EIM 根目录 → 读它的 eim_idf.json
    assert [c.id for c in d.inspect_folder(tmp_path / "Espressif", [])] == ["v5.5", "v5.4"]
    # 什么都没有
    empty = tmp_path / "empty"
    empty.mkdir()
    (res,) = d.inspect_folder(empty, [])
    assert not res.usable and "esp-idf folder" in (res.problem or "")


@pytest.fixture
def no_idf_here(monkeypatch, tmp_path):
    """本机自动查找返回空，config.toml 指向一个不存在的 eim_idf.json。"""
    monkeypatch.setattr(d, "discover", lambda extra=None: [])
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(f'[idf]\neim_json = "{(tmp_path / "nope.json").as_posix()}"\n', "utf-8")
    return home


def test_missing_idf_is_reported_then_a_choice_takes_effect_and_persists(tmp_path, no_idf_here):
    rt = Runtime(home=no_idf_here)
    assert rt.platform is None
    st = rt.idf_status()
    assert st["active"] is None and "not found" in st["error"]

    idf = fake_idf(tmp_path / "esp-idf")
    fake_tools(tmp_path / ".espressif")
    choice = IdfChoice(source="folder", path=str(idf), tools_path=str(tmp_path / ".espressif"))
    assert rt.select_idf(choice) is True  # 原来没有平台：立刻生效
    assert rt.platform is not None and rt.idf_status()["active"]["path"] == str(idf)
    assert "esp-idf" in (no_idf_here / "settings.json").read_text("utf-8")

    rt2 = Runtime(home=no_idf_here)  # 重启后还是这个
    assert rt2.platform is not None and rt2.idf is not None and rt2.idf.source == "folder"


def test_default_config_finds_idf_elsewhere_but_explicit_path_is_respected(tmp_path, monkeypatch):
    """eim_json 用缺省值时自动找（IDF 装在别的盘）；配置里明确写了的路径不存在就报错，不擅自换。"""
    other = fake_eim(tmp_path / "D")
    monkeypatch.setattr(d, "discover", lambda extra=None: d.eim_candidates(other))
    home = tmp_path / "home"
    home.mkdir()
    # 缺省位置（C:\Espressif\tools\eim_idf.json）在这台机器上是存在的：换成一个不存在的"缺省值"（不算用户写的）
    idf = IdfConfig.model_construct(_fields_set=set(), eim_json=tmp_path / "C" / "eim_idf.json", idf_id=None,
                                    path=None, tools_path=None)
    rt = Runtime(Config(idf=idf), home=home)
    assert rt.idf is not None and rt.idf.eim_json == str(other) and rt.platform is not None

    (home / "config.toml").write_text(f'[idf]\neim_json = "{(tmp_path / "nope.json").as_posix()}"\n', "utf-8")
    rt = Runtime(home=home)
    assert rt.platform is None and "nope.json" in (rt.idf_error or "")


def test_broken_choice_is_rejected_and_not_saved(tmp_path, no_idf_here):
    rt = Runtime(home=no_idf_here)
    idf = fake_idf(tmp_path / "esp-idf")  # 没有 python_env
    with pytest.raises(RuntimeError, match="install.bat"):
        rt.select_idf(IdfChoice(source="folder", path=str(idf), tools_path=str(tmp_path / ".espressif")))
    assert not (no_idf_here / "settings.json").exists()


def test_switching_idf_while_one_is_active_needs_a_restart(tmp_path, no_idf_here):
    j = fake_eim(tmp_path)
    rt = Runtime(home=no_idf_here)
    assert rt.select_idf(IdfChoice(source="eim", path="", id="v5.5", eim_json=str(j))) is True
    other = fake_idf(tmp_path / "other" / "esp-idf")
    fake_tools(tmp_path / ".espressif")
    assert rt.select_idf(IdfChoice(source="folder", path=str(other), tools_path=str(tmp_path / ".espressif"))) is False
    assert rt.idf is not None and rt.idf.source == "eim"  # 这次运行还用原来的
    assert Runtime(home=no_idf_here).idf.path == str(other)  # type: ignore[union-attr]
