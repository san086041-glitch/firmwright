"""2026-10-05 真机实测事故之后的权限改动：

- 工作目录以外的写、指向工作目录以外的链接：任何模式都询问（事故里 agent 在 Approve all 下建了指向真实工程的联接）
- Approve all 放行无法静态分析的命令，原文里有工作目录以外的路径时才问
- "本会话都允许"：外部写按目录放行；无法分析的命令按完整原文放行
"""

from test_paths import setup, sh

from firmwright.permissions.engine import PermissionEngine
from firmwright.permissions.paths import link_targets, raw_paths
from firmwright.permissions.ps_parse import analyze_cached
from firmwright.tools.fs import WriteFile


def scratch(tmp_path, policy):
    """pytest 的 tmp_path 本身在系统临时目录下；测试里把"临时目录"换成一个单独的目录。"""
    d = tmp_path / "systemp"
    d.mkdir()
    policy.write_roots = [d.resolve()]
    return d


def test_shell_writes_outside_ask_in_every_mode(tmp_path, monkeypatch):
    cwd, home, session, other, idf, elsewhere, secret, policy = setup(tmp_path)
    tmp = scratch(tmp_path, policy)
    monkeypatch.setenv("TEMP", str(tmp))
    e = PermissionEngine(paths=policy)
    real = tmp_path / "real_project"
    real.mkdir()
    cmds = [
        f"Copy-Item main.c '{real}\\main.c'",                     # 复制到外面
        f"git -C '{real}' checkout abc123 -- src",                 # 对真实仓库的 git 写操作
        f"tar -xf x.tar -C '{real}'",
        f"Get-Content main.c > '{real}\\out.txt'",                 # 重定向
        f"New-Item -ItemType Junction -Path proj -Target '{real}'",  # 事故里的联接
    ]
    for cmd in cmds:
        for mode in ("default", "accept_edits", "always_approve"):
            d = sh(e, cwd, cmd, mode)
            assert d.action == "ask", (cmd, mode, d.reason)
    # 工作目录里、临时目录里写不受影响
    assert sh(e, cwd, "Copy-Item main.c main2.c", "always_approve").action == "allow"
    assert sh(e, cwd, f"Copy-Item main.c '{tmp}\\fwr-x.c'", "always_approve").action == "allow"
    assert sh(e, cwd, "Set-Content $env:TEMP\\fwr-y.txt hi", "always_approve").action == "allow"
    # 从外面复制进来是读，不算外部写
    assert sh(e, cwd, f"Copy-Item '{idf}\\esp_err.h' .", "always_approve").action == "allow"
    # 指向工作目录里面的链接可以
    assert sh(e, cwd, "New-Item -ItemType SymbolicLink -Path l -Target main.c", "always_approve").action == "allow"


def test_file_tools_outside_ask_even_with_allow_rule(tmp_path):
    cwd, *_, policy = setup(tmp_path)
    scratch(tmp_path, policy)
    from firmwright.config import PermissionConfig

    e = PermissionEngine(PermissionConfig(allow=["Write"]), paths=policy)
    d = e.evaluate(WriteFile(), WriteFile.Args(path=str(tmp_path / "x.c"), content=""), mode="always_approve", cwd=cwd)
    assert d.action == "ask" and "outside" in d.reason
    # 本会话允许这个目录后不再问
    policy.add_session_write_root(tmp_path)
    d = e.evaluate(WriteFile(), WriteFile.Args(path=str(tmp_path / "y.c"), content=""), mode="always_approve", cwd=cwd)
    assert d.action == "allow"


def test_approve_all_allows_unanalyzable_without_outside_paths(tmp_path):
    cwd, *_, policy = setup(tmp_path)
    scratch(tmp_path, policy)
    e = PermissionEngine(paths=policy)
    inside = '$b = [System.IO.File]::ReadAllBytes("main\\main.c"); $b[-5..-1] -join ","'
    assert sh(e, cwd, inside, "always_approve").action == "allow"
    assert sh(e, cwd, inside, "default").action == "ask"
    out = f'[System.IO.File]::WriteAllText("{tmp_path}\\elsewhere\\x.txt", "hi")'
    d = sh(e, cwd, out, "always_approve")
    assert d.action == "ask" and "outside the working directory" in d.reason
    # "本会话都允许"按原文放行：同一条命令不再问，换一条还问
    e.session_exact.add(inside)
    assert sh(e, cwd, inside, "default").action == "allow"
    assert sh(e, cwd, inside + "; 1", "default").action == "ask"


def test_helpers():
    assert raw_paths(r'Foo("D:\firmwright\x.c"); Bar ..\up\y') == [r"D:\firmwright\x.c", r"..\up\y"]
    c = analyze_cached(r"New-Item -ItemType:Junction -Path p -Target:D:\real").commands[0]
    assert link_targets(c) == [r"D:\real"]
    assert link_targets(analyze_cached("New-Item -ItemType Directory x").commands[0]) is None
