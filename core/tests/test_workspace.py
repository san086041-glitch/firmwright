"""W5：git worktree 隔离、checkpoint 时间线与回退、会话收尾（合并 / 导出补丁 / 丢弃）。

全部用真的 git，在临时目录里建仓库；硬件部分用假串口和假平台适配器。
"""

import asyncio
import subprocess
from pathlib import Path

import pytest
from fakes import FakeAdapter, FakeSerial, board_port
from test_acp import Client

from firmwright.config import Config, WorktreeConfig
from firmwright.model.fake import ScriptedBackend, call, say
from firmwright.platform.base import FlashResult
from firmwright.runtime import Runtime
from firmwright.workspace import worktree
from firmwright.workspace.checkpoint import Checkpointer


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "core.autocrlf=false", *args], cwd=cwd, check=True, capture_output=True, encoding="utf-8",
                          text=True).stdout.strip()


def make_repo(root: Path, files: dict[str, str | bytes], *, sub: str = "") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Tester")
    git(root, "config", "user.email", "tester@example.com")
    git(root, "config", "core.autocrlf", "false")  # 不受本机系统配置影响（Git for Windows 默认 true）
    proj = root / sub if sub else root
    for rel, text in files.items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text if isinstance(text, bytes) else text.encode())
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return proj


def test_default_worktree_root_is_short_path():
    assert WorktreeConfig().root == Path("C:/fwr/wt")  # D02（曾被 shell 的 heredoc 把 \f 吃成换页符）


# ---------------------------------------------------------------------- worktree + checkpoint（不经过 ACP）


async def test_worktree_seed_checkpoint_and_restore(tmp_path):
    proj = make_repo(tmp_path / "repo", {"main.c": b"int a;\r\nint b;\r\n", ".gitignore": "build/\n"}, sub="fw")
    (proj / "sdkconfig").write_text('CONFIG_IDF_TARGET="esp32s3"\n')  # 没被 git 跟踪
    info = await worktree.create(proj, "abc123", root=tmp_path / "wt", seeds=["sdkconfig", ".firmwright"])
    cwd = Path(info.cwd)
    assert cwd == tmp_path / "wt" / "abc123" / "fw" and info.subdir == "fw" and info.target_branch == "main"
    assert (cwd / "sdkconfig").read_text().startswith("CONFIG_IDF_TARGET") and info.seeded == ["sdkconfig"]
    assert git(proj, "branch", "--list", "fwr/abc123")

    ck = Checkpointer(tmp_path / "store", Path(info.path), "abc123", excludes=["**/build/**"])
    (tmp_path / "store").mkdir()
    base = await ck.ensure_base()
    assert base.seq == 0 and not base.changed

    (cwd / "main.c").write_bytes(b"int a;\r\nint b = 2;\r\n")
    (cwd / "new.c").write_text("void f(void) {}\n")
    (cwd / "build").mkdir()
    (cwd / "build" / "x.o").write_text("obj")
    e1 = await ck.snapshot(turn=1, prompt="改一下")
    assert e1.changed and sorted(e1.files) == ["fw/main.c", "fw/new.c"]  # build 不进 checkpoint
    same = await ck.snapshot(turn=2)
    assert not same.changed and same.commit == e1.commit
    assert b"\r\n" in await ck.patch_bytes(base.commit, e1.commit)  # 补丁保留 CRLF

    # 回退到起点：改动撤销、新文件删除，build 不动；用户的分支 / index 都没动
    r = await ck.restore(0, turn=2)
    assert r.kind == "restore" and r.restored_to == 0
    assert (cwd / "main.c").read_bytes() == b"int a;\r\nint b;\r\n"
    assert not (cwd / "new.c").exists() and (cwd / "build" / "x.o").exists()
    assert git(Path(info.path), "status", "--porcelain") == "?? fw/sdkconfig"  # 只剩拷进来的未跟踪文件
    # 回退本身也能撤销
    await ck.restore(e1.seq, turn=2)
    assert (cwd / "main.c").read_bytes() == b"int a;\r\nint b = 2;\r\n" and (cwd / "new.c").exists()
    # 用户自己的工程完全没变
    assert (proj / "main.c").read_bytes() == b"int a;\r\nint b;\r\n"

    # 回退前没记录的改动会先记一个 manual 点
    (cwd / "main.c").write_text("手改\n")
    await ck.restore(0, turn=3)
    assert [e.kind for e in ck.entries[-2:]] == ["manual", "restore"]
    reloaded = Checkpointer(tmp_path / "store", Path(info.path), "abc123")
    assert len(reloaded.entries) == len(ck.entries)

    problems = await worktree.remove(Path(info.repo_root), Path(info.path), info.branch)
    assert problems == [] and not Path(info.path).exists()
    assert git(proj, "branch", "--list", "fwr/abc123") == ""


# ---------------------------------------------------------------------- ACP 全流程


def wt_client(tmp_path, steps, **kw) -> Client:
    return Client(tmp_path, steps, approve="allow_once", worktree=WorktreeConfig(root=tmp_path / "wt", prebuild=False),
                  **kw)


async def new_session(c: Client, proj: Path) -> str:
    await c.call("initialize", {"protocolVersion": 1})
    return (await c.call("session/new", {"cwd": str(proj), "mcpServers": []}))["sessionId"]


EDIT = call("edit_file", {"path": "main.c", "old_string": "int b;", "new_string": "int b = 2;"})


async def test_not_git_then_init_then_isolated_session_and_merge(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "main.c").write_text("int a;\nint b;\n")
    c = wt_client(tmp_path, [EDIT, say("改好了")])
    await c.call("initialize", {"protocolVersion": 1})
    with pytest.raises(RuntimeError) as ei:
        await c.call("session/new", {"cwd": str(proj)})
    err = ei.value.args[0]
    assert err["code"] == -32020 and err["data"]["reason"] == "not_git"

    # 用户确认初始化
    init = await c.call("_fwr/project/init_git", {"cwd": str(proj)})
    assert init["repo"]["head"] and git(proj, "ls-files") == "main.c"  # 没有平台适配器时不写 .gitignore
    git(proj, "config", "user.name", "Tester")
    git(proj, "config", "user.email", "tester@example.com")
    new = await c.call("session/new", {"cwd": str(proj)})
    sid = new["sessionId"]
    meta = new["_meta"]["fwr"]
    assert meta["isolation"] == "worktree" and meta["branch"] == f"fwr/{sid}"
    wt = Path(meta["worktree"])
    assert wt == tmp_path / "wt" / sid and (wt / "main.c").exists()

    res = await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "改 b"}]})
    assert res["stopReason"] == "end_turn"
    assert (wt / "main.c").read_text() == "int a;\nint b = 2;\n"
    assert (proj / "main.c").read_text() == "int a;\nint b;\n"  # 隔离：用户的工程没变
    cps = [u for u in c.updates(sid) if u["sessionUpdate"] == "_fwr/checkpoint"]
    assert cps and cps[-1]["entry"]["changed"] and cps[-1]["entry"]["files"] == ["main.c"]
    lst = await c.call("_fwr/checkpoints/list", {"sessionId": sid})
    assert [e["kind"] for e in lst["entries"]] == ["base", "turn"]

    diff = await c.call("_fwr/session/diff", {"sessionId": sid})
    assert diff["files"] == ["main.c"] and "+int b = 2;" in diff["patch"]

    merged = await c.call("_fwr/session/merge", {"sessionId": sid, "message": "给 b 赋初值"})
    assert merged["ok"] and merged["files"] == ["main.c"] and merged["checkedOutAt"]
    assert (proj / "main.c").read_text() == "int a;\nint b = 2;\n"  # 合并进了用户检出的 main
    assert git(proj, "log", "-1", "--format=%s %an") == "给 b 赋初值 Tester"  # 快进，作者是用户
    assert not wt.exists() and git(proj, "branch", "--list", f"fwr/{sid}") == ""
    assert git(proj, "for-each-ref", f"refs/fwr/ckpt/{sid}/") == ""
    assert merged["_meta"]["fwr"]["state"] == "merged"
    with pytest.raises(RuntimeError) as ei:
        await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "再改"}]})
    assert ei.value.args[0]["code"] == -32023
    # 列表里能看到已合并的会话
    lst = await c.call("_fwr/sessions/list")
    assert lst["sessions"][0]["state"] == "merged"


async def test_merge_conflict_leaves_everything_as_is(tmp_path):
    proj = make_repo(tmp_path / "proj", {"main.c": "int a;\nint b;\n"})
    c = wt_client(tmp_path, [EDIT, say("好")])
    sid = await new_session(c, proj)
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "改"}]})
    (proj / "main.c").write_text("int a;\nint b = 3;\n")  # 用户在主分支上改了同一行并提交
    git(proj, "commit", "-qam", "user change")
    head = git(proj, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError) as ei:
        await c.call("_fwr/session/merge", {"sessionId": sid})
    err = ei.value.args[0]
    assert err["code"] == -32026 and err["data"]["kind"] == "conflict" and err["data"]["conflicts"] == ["main.c"]
    assert git(proj, "rev-parse", "HEAD") == head and git(proj, "status", "--porcelain") == ""
    assert (tmp_path / "wt" / sid).exists() and c.rt.metas[sid].state == "active"


async def test_merge_into_branch_not_checked_out(tmp_path):
    proj = make_repo(tmp_path / "proj", {"main.c": "int a;\nint b;\n"})
    git(proj, "branch", "dev")
    git(proj, "switch", "-q", "dev")
    (proj / "other.txt").write_text("dev 上的提交\n", "utf-8")
    git(proj, "add", "-A")
    git(proj, "commit", "-qm", "dev work")
    git(proj, "switch", "-q", "main")
    c = wt_client(tmp_path, [EDIT, say("好")])
    sid = await new_session(c, proj)
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "改"}]})
    res = await c.call("_fwr/session/merge", {"sessionId": sid, "targetBranch": "dev"})
    assert res["ok"] and res["checkedOutAt"] is None
    assert git(proj, "show", "dev:main.c") == "int a;\nint b = 2;" and git(proj, "show", "dev:other.txt")
    assert len(git(proj, "log", "-1", "--format=%P", "dev").split()) == 2  # 分叉了，是合并提交
    assert (proj / "main.c").read_text() == "int a;\nint b;\n"  # 检出的 main 没动


async def test_export_patch_and_discard(tmp_path):
    proj = make_repo(tmp_path / "proj", {"main.c": b"int a;\r\nint b;\r\n"})
    steps = [call("edit_file", {"path": "main.c", "old_string": "int b;", "new_string": "int b = 2;"}), say("好")]
    c = wt_client(tmp_path, steps)
    sid = await new_session(c, proj)
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "改"}]})
    out = await c.call("_fwr/session/export_patch", {"sessionId": sid})
    patch = Path(out["path"])
    assert patch.exists() and out["files"] == ["main.c"]
    git(proj, "apply", "--check", str(patch))  # 能直接套到用户的工程上（CRLF 也对）
    res = await c.call("_fwr/session/discard", {"sessionId": sid})
    assert res["cleanup"] == [] and res["_meta"]["fwr"]["state"] == "discarded"
    assert not (tmp_path / "wt" / sid).exists() and git(proj, "branch", "--list", f"fwr/{sid}") == ""
    assert (proj / "main.c").read_bytes() == b"int a;\r\nint b;\r\n"


async def test_in_place_session_still_has_checkpoints(tmp_path):
    proj = make_repo(tmp_path / "proj", {"main.c": "int a;\nint b;\n"})
    c = wt_client(tmp_path, [EDIT, say("好")])
    await c.call("initialize", {"protocolVersion": 1})
    new = await c.call("session/new", {"cwd": str(proj), "_meta": {"fwr": {"isolation": "in_place"}}})
    sid = new["sessionId"]
    assert new["_meta"]["fwr"]["isolation"] == "in_place" and new["_meta"]["fwr"]["cwd"] == str(proj.resolve())
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "改"}]})
    assert (proj / "main.c").read_text() == "int a;\nint b = 2;\n"
    await c.call("_fwr/checkpoints/restore", {"sessionId": sid, "seq": 0})
    assert (proj / "main.c").read_text() == "int a;\nint b;\n"
    assert git(proj, "status", "--porcelain") == ""  # 用户的 index 没被动过
    with pytest.raises(RuntimeError):
        await c.call("_fwr/session/discard", {"sessionId": sid})


# ---------------------------------------------------------------------- 固件存档与回退重烧


async def test_restore_reflashes_firmware_of_that_point(tmp_path):
    proj = make_repo(tmp_path / "proj", {"main.c": "v0\n"})
    adapter = FakeAdapter()
    cfg = Config(worktree=WorktreeConfig(root=tmp_path / "wt", prebuild=False))
    rt = Runtime(cfg, home=tmp_path / "home")
    rt.platform = adapter
    await rt.start_devices(lister=lambda: [board_port()], opener=lambda p, b: FakeSerial(p, b), scan_interval=0.05)
    try:
        await asyncio.sleep(0.2)
        steps = [call("write_file", {"path": "main.c", "content": "v1\n"}), call("flash", {}), say("v1 烧好了"),
                 call("write_file", {"path": "main.c", "content": "v2\n"}), call("flash", {}), say("v2 烧好了")]
        sid = rt.new_session_id()
        ws, repo = await rt.prepare_workspace(proj, sid)
        s = rt.create_session(proj, backend=ScriptedBackend(steps), board_id="usb-aabbccddeeff", mode="accept_edits",
                              session_id=sid, workspace=ws, repo=repo)
        await s.checkpoints.ensure_base()
        assert (await s.prompt("烧 v1")).stop_reason == "end_turn"
        assert (await s.prompt("烧 v2")).stop_reason == "end_turn"
        turns = [e for e in s.checkpoints.entries if e.kind == "turn"]
        assert [e.firmware.seq for e in turns] == [1, 2] and turns[0].firmware.archive
        res = await rt.restore_checkpoint(sid, turns[0].seq, reflash=True)
        assert (s.cwd / "main.c").read_text() == "v1\n"
        assert adapter.reflashed == ["v1\n"] and res["flash"]["ok"]
        assert res["entry"]["firmware"]["source"] == "restore"
        assert any(r.source == "checkpoint" and "#1" in r.text for r in s._pending)  # 下一轮告诉模型
        # 起点之前没烧过固件：要求重烧时如实报告
        res0 = await rt.restore_checkpoint(sid, 0, reflash=True)
        assert not res0["flash"]["ok"] and "No firmware was flashed" in res0["flash"]["summary"]
        # 两轮之间回退了两次：只交给 agent 最后一次（2026-10-05 真机：三条都交了）
        cps = [r for r in s._pending if r.source == "checkpoint"]
        assert len(cps) == 1 and "#0" in cps[0].text and "2 times" in cps[0].text

        # 重烧那一刻板子在重启、串口没了：等板子回来再试一次（2026-10-05 真机：直接报 serial port not found）
        calls = []

        async def flaky(ctx, port, archive, scope="app"):
            calls.append(port)
            if len(calls) == 1:
                return FlashResult(op="flash", ok=False, port=port, scope=scope, error_class="port_not_found",
                                   summary="serial port not found")
            return FlashResult(op="flash", ok=True, port=port, scope=scope, image_sha256="archived")

        adapter.flash_image = flaky
        res2 = await rt.restore_checkpoint(sid, turns[1].seq, reflash=True)
        assert res2["flash"]["ok"] and len(calls) == 2
    finally:
        await rt.stop()
