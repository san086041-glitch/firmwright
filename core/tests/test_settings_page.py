"""设置页补全（2026-10-06）：编译并行数、worktree 根目录、默认权限模式、权限规则、MCP 服务器、重启核心。"""

import json

import pytest

from firmwright.acp.server import AcpServer, RpcError
from firmwright.config import Config, IdfConfig
from firmwright.model.fake import ScriptedBackend, say
from firmwright.runtime import Runtime

TOML = """
[permissions]
allow = ["read_file"]

[mcp.servers.toml-srv]
command = "node"
args = ["server.js"]
enabled = false
"""


def make(tmp_path) -> Runtime:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(TOML, "utf-8")
    # 不让测试碰本机的 ESP-IDF：明写一个不存在的 eim_idf.json
    cfg = Config.load(home / "config.toml")
    cfg.idf = IdfConfig(eim_json=tmp_path / "none.json")
    return Runtime(cfg, home=home)


def test_general_settings_apply_now_and_persist(tmp_path):
    rt = make(tmp_path)
    root = tmp_path / "wt"
    rt.update_general(build_jobs=6, worktree_root=str(root), permission_mode="accept_edits")
    assert rt.config.build.jobs == 6 and rt.config.build.resolved_jobs() == 6
    assert rt.config.worktree.root == root and root.is_dir()  # 不存在就建
    data = json.loads((rt.home / "settings.json").read_text("utf-8"))
    assert data["build_jobs"] == 6 and data["permission_mode"] == "accept_edits"

    rt2 = Runtime(home=rt.home)  # 重启后还在（这次用 config.toml + settings.json）
    assert rt2.config.build.jobs == 6 and rt2.config.worktree.root == root
    assert rt2.config.defaults.permission_mode == "accept_edits"


@pytest.mark.parametrize("kw, msg", [
    ({"build_jobs": 99}, "0 \\(automatic\\) to 64"),
    ({"worktree_root": "relative\\wt"}, "absolute"),
    ({"worktree_root": "C:\\my work\\wt"}, "spaces"),
    ({"permission_mode": "yolo"}, "Unknown permission mode"),
])
def test_bad_general_settings_are_rejected(tmp_path, kw, msg):
    rt = make(tmp_path)
    with pytest.raises(ValueError, match=msg):
        rt.update_general(**kw)
    assert not (rt.home / "settings.json").exists()


def test_worktree_root_cannot_be_in_the_data_folder(tmp_path):
    rt = make(tmp_path)
    with pytest.raises(ValueError, match="data folder"):
        rt.update_general(worktree_root=str(rt.home / "wt"))


def test_rules_merge_with_config_and_reach_open_sessions(tmp_path):
    rt = make(tmp_path)
    proj = tmp_path / "proj"
    proj.mkdir()
    s = rt.create_session(proj, backend=ScriptedBackend([say("ok")]))
    s.permissions.add_session_allow("shell(git status)")

    with pytest.raises(ValueError, match="Invalid permission rule"):
        rt.set_rules(["shell(idf.py build"], [], [])
    rt.set_rules(["shell(idf.py build)", "read_file", "  "], [], ["shell(idf.py erase-flash)"])
    # config.toml 的在前、不重复；空的丢掉
    assert rt.config.permissions.allow == ["read_file", "shell(idf.py build)"]
    assert rt.ui.rules.allow == ["shell(idf.py build)"] and rt.ui.rules.deny == ["shell(idf.py erase-flash)"]
    # 已打开的会话立刻用上新规则；"本会话都允许"的保留
    assert [r.raw for r in s.permissions.allow] == ["read_file", "shell(idf.py build)"]
    assert [r.raw for r in s.permissions.deny] == ["shell(idf.py erase-flash)"]
    assert [r.raw for r in s.permissions.session_allow] == ["shell(git status)"]

    rt2 = Runtime(home=rt.home)
    assert rt2.config.permissions.allow == ["read_file", "shell(idf.py build)"]


def test_mcp_servers_from_the_ui(tmp_path):
    rt = make(tmp_path)
    with pytest.raises(ValueError, match="config.toml"):
        rt.save_mcp("toml-srv", {"command": "x"})
    with pytest.raises(ValueError, match="Command is required"):
        rt.save_mcp("docs", {"command": "  "})
    rt.save_mcp("docs", {"command": "uvx", "args": ["espressif-docs-mcp"], "env": {"A": "1"}})
    assert "docs" in rt.ui.mcp_servers and "docs" not in rt.config.mcp.servers  # 重启后才用
    rt2 = Runtime(home=rt.home)
    assert rt2.config.mcp.servers["docs"].args == ["espressif-docs-mcp"]
    with pytest.raises(ValueError, match="config.toml"):
        rt2.delete_mcp("toml-srv")
    rt2.delete_mcp("docs")
    assert "docs" not in Runtime(home=rt.home).config.mcp.servers


async def test_settings_get_lists_sources_and_pending_servers(tmp_path):
    rt = make(tmp_path)
    rt.set_rules(["shell(idf.py build)"], [], [])
    rt.save_mcp("docs", {"command": "uvx"})

    async def send(msg):
        pass

    st = await AcpServer(rt, send).settings_get({})
    assert st["rules"]["allow"] == {"config": ["read_file"], "settings": ["shell(idf.py build)"]}
    by = {m["name"]: m for m in st["mcp"]}
    assert by["toml-srv"]["source"] == "config.toml" and not by["toml-srv"]["enabled"]
    assert by["docs"]["source"] == "settings" and by["docs"]["pending"]
    assert st["buildJobsAuto"] == rt.config.build.resolved_jobs()


async def test_restart_refuses_while_a_session_works(tmp_path):
    rt = make(tmp_path)
    exits = []

    async def send(msg):
        pass

    server = AcpServer(rt, send)
    with pytest.raises(RpcError, match="not available"):
        await server.core_restart({})
    server.request_exit = lambda: exits.append(1)
    proj = tmp_path / "proj"
    proj.mkdir()
    s = rt.create_session(proj, backend=ScriptedBackend([say("ok")]), title="busy one")
    s.status = "running"
    with pytest.raises(RpcError, match="busy one"):
        await server.core_restart({})
    s.status = "idle"
    assert (await server.core_restart({}))["restarting"]
    import asyncio

    await asyncio.sleep(0.3)
    assert exits == [1]


def test_mcp_exit_reason_prefers_the_error_line():
    from firmwright.mcp.client import exit_reason

    node = ["node:internal/modules/cjs/loader:1386", "  throw err;", "Error: Cannot find module 'x.js'",
            "    at Module._resolveFilename", "", "Node.js v24.15.0"]
    assert exit_reason(node) == "Error: Cannot find module 'x.js'"
    assert exit_reason(["starting", "bye"]) == "bye"
    assert exit_reason([]) == ""


def test_flashed_size_comes_from_the_firmware_archive(tmp_path):
    """2026-10-06（界面改进第 12 项）：固件大小条显示"比板子上那份大了多少"，板子上那份从烧录存档里读。"""
    from types import SimpleNamespace

    from firmwright.workspace.checkpoint import FirmwareRecord

    arch = tmp_path / "fw" / "0001"
    arch.mkdir(parents=True)
    (arch / "flasher_args.json").write_text(json.dumps({"app": {"offset": "0x10000", "file": "blink.bin"}}), "utf-8")
    (arch / "blink.bin").write_bytes(b"\0" * 1234)
    rec = FirmwareRecord(seq=1, turn=3, at="2026-10-06T00:00:00Z", archive=str(arch))
    got = AcpServer._flashed_size(SimpleNamespace(_turn_firmware=rec, checkpoints=None))  # type: ignore[arg-type]
    assert got == {"appBinSize": 1234, "turn": 3, "at": "2026-10-06T00:00:00Z", "source": "agent"}
    # 存档被清理 / 还没烧过：不显示
    gone = rec.model_copy(update={"archive": str(tmp_path / "nope")})
    assert AcpServer._flashed_size(SimpleNamespace(_turn_firmware=gone, checkpoints=None)) is None  # type: ignore[arg-type]
    assert AcpServer._flashed_size(SimpleNamespace(_turn_firmware=None, checkpoints=None)) is None  # type: ignore[arg-type]
