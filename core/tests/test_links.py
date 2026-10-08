"""2026-10-04 事故的回归测试：worktree 里有指向外面的目录联接时，checkpoint 快照 / 回退 / 丢弃都不能碰到外面。"""

import sys
from pathlib import Path

import pytest
from test_workspace import make_repo

from firmwright.workspace import worktree
from firmwright.workspace.checkpoint import Checkpointer
from firmwright.workspace.links import find_links

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")


def junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


async def test_checkpoint_and_discard_never_follow_junctions(tmp_path):
    proj = make_repo(tmp_path / "repo", {"main.c": "int a;\n"})
    real = tmp_path / "real_project"  # 用户真实的工程（2026-10-04 事故里 D 盘上的那个工程）
    (real / "src").mkdir(parents=True)
    (real / "src" / "app.c").write_text("precious\n")
    info = await worktree.create(proj, "abc123", root=tmp_path / "wt")
    wt = Path(info.path)
    store = tmp_path / "store"
    store.mkdir()
    ck = Checkpointer(store, wt, "abc123")
    await ck.ensure_base()

    junction(wt / "Proj", real)  # agent 建的联接
    (wt / "main.c").write_text("int a = 1;\n")
    e1 = await ck.snapshot(turn=1)
    assert e1.files == ["main.c"]  # 联接下面的文件没有进 checkpoint
    assert find_links(wt) == [wt / "Proj"]

    preview = await ck.preview(0)
    assert preview["links"] == ["Proj"] and preview["modify"] == 1 and preview["delete"] == 0

    await ck.restore(0, turn=2)
    assert ck.unlinked == ["Proj"] and not (wt / "Proj").exists()
    assert (real / "src" / "app.c").read_text() == "precious\n"  # 事故里这里被删了
    assert (wt / "main.c").read_text() == "int a;\n"

    # 丢弃会话：worktree 里再有联接，删 worktree 也不能删到外面
    junction(wt / "Again", real)
    problems = await worktree.remove(Path(info.repo_root), wt, info.branch)
    assert problems == [] and not wt.exists()
    assert (real / "src" / "app.c").read_text() == "precious\n"
