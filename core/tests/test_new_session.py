"""新建会话的改进（2026-10-05，真机实测之后）：

- 工程里未提交的改动默认带进 worktree（可以取消），收尾时"应用到工程文件夹"
- 初始化 git 时文件夹是空的就报错，不再做一个只有 .gitignore 的空提交
- 选好文件夹后的工程检查：不存在 / 不是 git / 子目录里才有工程
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_workspace import EDIT, git, make_repo, new_session, wt_client

from firmwright.model.fake import call, say
from firmwright.platform.base import ProjectInfo


def dirty_repo(tmp_path: Path) -> Path:
    proj = make_repo(tmp_path / "proj", {"main.c": "int a;\nint b;\n", "other.c": "int x;\n"})
    (proj / "other.c").write_text("int x = 1;\n")  # 改了没提交
    (proj / "notes.txt").write_text("todo\n")  # 新文件，没被跟踪
    return proj


async def test_uncommitted_changes_are_carried_and_applied_back(tmp_path):
    proj = dirty_repo(tmp_path)
    c = wt_client(tmp_path, [EDIT, say("ok")])
    await c.call("initialize", {"protocolVersion": 1})
    new = await c.call("session/new", {"cwd": str(proj)})
    sid, meta = new["sessionId"], new["_meta"]["fwr"]
    wt = Path(meta["worktree"])
    assert (wt / "other.c").read_text() == "int x = 1;\n" and (wt / "notes.txt").exists()
    assert sorted(meta["carried"]) == ["notes.txt", "other.c"] and meta["dirtyCount"] == 2
    assert git(proj, "status", "--porcelain") == "M other.c\n?? notes.txt"  # 用户的 index 没动（首个空格被 strip）

    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "edit b"}]})
    diff = await c.call("_fwr/session/diff", {"sessionId": sid})
    assert diff["files"] == ["main.c"]  # 带进来的改动在 checkpoint #0 里，不算 agent 的改动

    res = await c.call("_fwr/session/apply", {"sessionId": sid})
    assert res["ok"] and res["files"] == ["main.c"] and res["_meta"]["fwr"]["state"] == "applied"
    assert (proj / "main.c").read_text() == "int a;\nint b = 2;\n"
    assert (proj / "other.c").read_text() == "int x = 1;\n" and (proj / "notes.txt").exists()
    assert git(proj, "log", "--oneline").count("\n") == 0  # 不提交
    assert not wt.exists()


async def test_carry_can_be_turned_off(tmp_path):
    proj = dirty_repo(tmp_path)
    c = wt_client(tmp_path, [say("ok")])
    await c.call("initialize", {"protocolVersion": 1})
    new = await c.call("session/new", {"cwd": str(proj), "_meta": {"fwr": {"carryDirty": False}}})
    wt = Path(new["_meta"]["fwr"]["worktree"])
    assert (wt / "other.c").read_text() == "int x;\n" and not (wt / "notes.txt").exists()
    assert new["_meta"]["fwr"]["carried"] == []


async def test_merge_refuses_when_agent_edits_a_carried_file(tmp_path):
    proj = dirty_repo(tmp_path)
    steps = [call("edit_file", {"path": "other.c", "old_string": "int x = 1;", "new_string": "int x = 2;"}), say("ok")]
    c = wt_client(tmp_path, steps)
    sid = await new_session(c, proj)
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "edit x"}]})
    with pytest.raises(RuntimeError) as ei:
        await c.call("_fwr/session/merge", {"sessionId": sid})
    assert ei.value.args[0]["data"]["kind"] == "carried" and ei.value.args[0]["data"]["conflicts"] == ["other.c"]
    assert c.rt.metas[sid].state == "active"
    await c.call("_fwr/session/apply", {"sessionId": sid})
    assert (proj / "other.c").read_text() == "int x = 2;\n"


async def test_apply_refuses_when_project_changed_meanwhile(tmp_path):
    proj = dirty_repo(tmp_path)
    c = wt_client(tmp_path, [EDIT, say("ok")])
    sid = await new_session(c, proj)
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "edit b"}]})
    (proj / "main.c").write_text("int a;\nint b = 9;\n")  # 用户在编辑器里改了同一行
    with pytest.raises(RuntimeError) as ei:
        await c.call("_fwr/session/apply", {"sessionId": sid})
    assert ei.value.args[0]["data"]["kind"] == "conflict" and ei.value.args[0]["data"]["conflicts"] == ["main.c"]
    assert (proj / "main.c").read_text() == "int a;\nint b = 9;\n" and c.rt.metas[sid].state == "active"


async def test_init_git_refuses_empty_folder(tmp_path):
    proj = tmp_path / "empty"
    proj.mkdir()
    c = wt_client(tmp_path, [])
    await c.call("initialize", {"protocolVersion": 1})
    with pytest.raises(RuntimeError) as ei:
        await c.call("_fwr/project/init_git", {"cwd": str(proj)})
    assert ei.value.args[0]["code"] == -32027
    res = await c.call("_fwr/project/inspect", {"cwd": str(proj)})
    assert res["repo"]["is_git"] and res["repo"]["head"] is None  # 没有做空提交


async def test_new_project_in_an_empty_folder(tmp_path):
    """用户明确选了"在这里新建工程"：允许空的初始提交；agent 在副本里建工程，收尾时 Apply 写回空文件夹。"""
    proj = tmp_path / "newproj"
    proj.mkdir()
    steps = [call("write_file", {"path": "main/main.c", "content": "void app_main(void) {}\n"}), say("created")]
    c = wt_client(tmp_path, steps)
    await c.call("initialize", {"protocolVersion": 1})
    await c.call("_fwr/project/init_git", {"cwd": str(proj), "newProject": True})
    git(proj, "config", "user.name", "Tester")
    git(proj, "config", "user.email", "tester@example.com")
    new = await c.call("session/new", {"cwd": str(proj), "_meta": {"fwr": {"mode": "accept_edits"}}})
    sid = new["sessionId"]
    await c.call("session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": "make a blink project"}]})
    # agent 第一步就被告知这是新工程
    first = c.rt.sessions[sid].backend.requests[0].messages
    assert any("started a new project here" in getattr(b, "text", "") for m in first for b in m.content)
    res = await c.call("_fwr/session/apply", {"sessionId": sid})
    assert res["files"] == ["main/main.c"] and (proj / "main" / "main.c").read_text() == "void app_main(void) {}\n"


class _Idf:
    """只认 CMakeLists.txt 里有 project.cmake 的目录。"""

    def detect(self, root: Path):
        cm = root / "CMakeLists.txt"
        return ProjectInfo(root=str(root)) if cm.is_file() and "project.cmake" in cm.read_text() else None


async def test_inspect_reports_folder_state_and_nested_project(tmp_path):
    parent = tmp_path / "firmwright"
    sub = parent / "ESP32S3_Demo"
    sub.mkdir(parents=True)
    (sub / "CMakeLists.txt").write_text("include($ENV{IDF_PATH}/tools/cmake/project.cmake)\n")
    (sub / "main.c").write_text("int a;\n")
    (sub / "build").mkdir()
    (sub / "build" / "x.o").write_text("obj")
    c = wt_client(tmp_path, [])
    await c.call("initialize", {"protocolVersion": 1})
    c.rt.platform = _Idf()  # 初始化之后再换，免得设备管理器去用它

    assert (await c.call("_fwr/project/inspect", {"cwd": str(tmp_path / "missing")})) == {"exists": False}
    res = await c.call("_fwr/project/inspect", {"cwd": str(parent)})
    assert res["project"] is None and res["candidates"] == [str(sub)]
    assert not res["repo"]["is_git"] and res["scan"]["files"] == 2  # build 不算
    res = await c.call("_fwr/project/inspect", {"cwd": str(sub)})
    assert res["project"]["root"] == str(sub) and res["candidates"] == []
    assert res["spaces"] == {"worktree": None, "inPlace": None}


async def test_inspect_flags_spaces_in_the_build_path(tmp_path):
    # 仓库在 repo\，工程在 "my proj\fw"：副本里的路径（worktree 根\<id>\my proj\fw）和原地路径都带空格
    proj = make_repo(tmp_path / "repo", {"main.c": "int a;\n"}, sub="my proj/fw")
    c = wt_client(tmp_path, [])
    await c.call("initialize", {"protocolVersion": 1})
    res = await c.call("_fwr/project/inspect", {"cwd": str(proj)})
    assert res["spaces"] == {"worktree": "my proj", "inPlace": "my proj"}
    # 空格在仓库根目录以上：副本没问题，原地工作才有问题
    proj2 = make_repo(tmp_path / "with space" / "repo2", {"main.c": "int a;\n"})
    res = await c.call("_fwr/project/inspect", {"cwd": str(proj2)})
    assert res["spaces"] == {"worktree": None, "inPlace": "with space"}
