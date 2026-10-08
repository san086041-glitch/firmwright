"""W7 修复：只读工具对路径没有限制（演示里执行者读了应用数据目录里别的会话历史）。"""

import asyncio
from pathlib import Path

from firmwright.config import Config, IdfConfig, ModelConfig, PermissionConfig
from firmwright.model.types import CancelToken
from firmwright.permissions.engine import PermissionEngine
from firmwright.permissions.paths import PathPolicy, shell_paths
from firmwright.permissions.ps_parse import PsCommand, analyze_cached
from firmwright.runtime import Runtime
from firmwright.services import Services
from firmwright.tools.base import ToolContext
from firmwright.tools.fs import EditFile, Grep, ListDir, ReadFile
from firmwright.tools.shell import Shell, ShellArgs
from firmwright.trace import Trace


def setup(tmp_path: Path):
    cwd = tmp_path / "wt" / "abc123"
    home = tmp_path / "home"
    session = home / "sessions" / "proj" / "abc123"
    other = home / "sessions" / "proj" / "zzz999"
    idf = tmp_path / "Espressif" / "frameworks"
    elsewhere = tmp_path / "elsewhere"
    secret = tmp_path / "otherproj" / "keys.env"
    for d in (cwd, session / "compaction", other, idf, elsewhere, secret.parent, home / "skills" / "s1"):
        d.mkdir(parents=True, exist_ok=True)
    (cwd / "main.c").write_text("int x;", "utf-8")
    (cwd / ".env").write_text("API_KEY=sk-123", "utf-8")
    (cwd / ".env.example").write_text("API_KEY=", "utf-8")
    (session / "compaction" / "1.md").write_text("archive", "utf-8")
    (other / "history.jsonl").write_text("{}", "utf-8")
    (idf / "esp_err.h").write_text("#define ESP_OK 0", "utf-8")
    (elsewhere / "notes.txt").write_text("hi", "utf-8")
    secret.write_text("KEY=1", "utf-8")
    policy = PathPolicy(protected=[home, secret], readable_inside_protected=[session, home / "skills"],
                        read_roots=[idf])
    return cwd, home, session, other, idf, elsewhere, secret, policy


def read(engine, cwd, path, mode="default"):
    return engine.evaluate(ReadFile(), ReadFile.Args(path=str(path)), mode=mode, cwd=cwd)


def sh(engine, cwd, cmd, mode="default"):
    return engine.evaluate(Shell(), ShellArgs(command=cmd), mode=mode, cwd=cwd, ps=analyze_cached(cmd))


def test_read_file_paths(tmp_path):
    cwd, home, session, other, idf, elsewhere, secret, policy = setup(tmp_path)
    e = PermissionEngine(paths=policy)
    assert read(e, cwd, "main.c").action == "allow"
    assert read(e, cwd, idf / "esp_err.h").action == "allow"  # 平台给的可读区域
    assert read(e, cwd, session / "compaction" / "1.md").action == "allow"  # 自己的压缩存档
    assert read(e, cwd, home / "skills" / "s1").action == "allow"
    # 演示里的越界读：别的会话的历史 → 拒绝，任何模式都放宽不了
    for mode in ("default", "always_approve", "plan"):
        d = read(e, cwd, other / "history.jsonl", mode)
        assert d.action == "deny" and "Protected" in d.reason
    assert read(e, cwd, home / "config.toml", "always_approve").action == "deny"
    assert read(e, cwd, secret, "always_approve").action == "deny"  # key_ref 指向的文件
    assert read(e, cwd, ".env").action == "deny"
    assert read(e, cwd, ".env.example").action == "allow"
    assert read(e, cwd, "../../wt/abc123/../../home/sessions/proj/zzz999/history.jsonl").action == "deny"
    # 工作目录以外的普通位置 → 询问；always_approve 放行
    d = read(e, cwd, elsewhere / "notes.txt")
    assert d.action == "ask" and d.details["outside"]
    assert read(e, cwd, elsewhere / "notes.txt", "always_approve").action == "allow"
    # 按工具整体放行的规则不覆盖"工作目录以外"
    e2 = PermissionEngine(PermissionConfig(allow=["Read"]), paths=policy)
    assert read(e2, cwd, elsewhere / "notes.txt").action == "ask"
    assert read(e2, cwd, other / "history.jsonl").action == "deny"
    # 配置里的 read_roots / 本会话加的目录
    policy.add_session_root(elsewhere)
    assert read(e, cwd, elsewhere / "notes.txt").action == "allow"


def test_session_dir_readable_but_not_writable(tmp_path):
    cwd, home, session, *_rest, policy = setup(tmp_path)
    e = PermissionEngine(paths=policy)
    a = EditFile.Args(path=str(session / "compaction" / "1.md"), old_string="a", new_string="b")
    assert e.evaluate(EditFile(), a, mode="always_approve", cwd=cwd).action == "deny"


def test_list_dir_and_grep_paths(tmp_path):
    cwd, home, session, other, idf, elsewhere, secret, policy = setup(tmp_path)
    e = PermissionEngine(paths=policy)
    assert e.evaluate(ListDir(), ListDir.Args(path=str(home)), mode="default", cwd=cwd).action == "deny"
    assert e.evaluate(Grep(), Grep.Args(pattern="x"), mode="default", cwd=cwd).action == "allow"
    assert e.evaluate(Grep(), Grep.Args(pattern="x", path=str(tmp_path)), mode="default", cwd=cwd).action == "ask"


def test_grep_skips_protected_files(tmp_path):
    cwd, *_rest, policy = setup(tmp_path)
    ctx = ToolContext(session_id="s", cwd=cwd, cancel=CancelToken(), trace=Trace(None, "s"),
                      services=Services(paths=policy))
    res = asyncio.run(Grep().run(ctx, Grep.Args(pattern="API_KEY")))
    text = res.content[0].text
    assert ".env.example" in text and "sk-123" not in text


def test_shell_paths(tmp_path):
    cwd, home, session, other, idf, elsewhere, secret, policy = setup(tmp_path)
    e = PermissionEngine(paths=policy)
    assert sh(e, cwd, "Get-Content main.c; git log --oneline -5").action == "allow"
    assert sh(e, cwd, "Get-ChildItem *.c | Select-String -Pattern 'TEST:'").action == "allow"
    assert sh(e, cwd, "Select-String 'API_KEY' main.c").action == "allow"  # 第一个位置参数是模式
    assert sh(e, cwd, "Get-ChildItem | Where-Object { $_.Length -gt 10 }").action == "allow"
    assert sh(e, cwd, f"Get-Content {idf / 'esp_err.h'}").action == "allow"
    assert sh(e, cwd, f"Get-Content '{other / 'history.jsonl'}'", "always_approve").action == "deny"
    assert sh(e, cwd, f"Get-ChildItem -Path:{home}").action == "deny"
    assert sh(e, cwd, "type .env").action == "deny"
    assert sh(e, cwd, f"Copy-Item {secret} x.txt", "always_approve").action == "deny"  # 非只读命令也拦
    d = sh(e, cwd, f"Get-Content {elsewhere / 'notes.txt'}")
    assert d.action == "ask" and "outside the working directory" in d.reason
    assert sh(e, cwd, "Get-ChildItem ..\\..").action == "ask"
    # 含变量的路径解析不了 → 只读命令也要问
    assert sh(e, cwd, "Get-ChildItem $env:LOCALAPPDATA\\Firmwright").action == "ask"
    assert sh(e, cwd, f"git -C {elsewhere} status").action == "ask"
    assert sh(e, cwd, "git show HEAD:main.c").action == "allow"


def test_shell_paths_extraction():
    c = PsCommand(name="Select-String", args=["-Pattern", "TEST:", "-Path", "a.c", "b.c"], text="")
    assert shell_paths(c) == ["a.c", "b.c"]
    c = PsCommand(name="Get-ChildItem", args=["-Recurse", "-Filter", "*.c", "https://x.y/z"], text="")
    assert shell_paths(c) == ["*.c"]
    assert shell_paths(PsCommand(name="git", args=["diff", "HEAD~1", "--", "main/x.c"], text="")) == ["main/x.c"]


def test_runtime_policy(tmp_path):
    secret = tmp_path / "keys" / ".env.local"
    cfg = Config(idf=IdfConfig(eim_json=tmp_path / "none.json"),
                 models={"m": ModelConfig(base_url="http://x", key_ref=f"dotenv:{secret}#K")},
                 permissions=PermissionConfig(read_roots=[tmp_path / "docs"]))
    rt = Runtime(cfg, home=tmp_path / "home")
    sdir = tmp_path / "home" / "sessions" / "p" / "s1"
    p = rt.path_policy(sdir)
    cwd = tmp_path / "wt"
    assert p.is_protected(tmp_path / "home" / "config.toml", write=False)
    assert not p.is_protected(sdir / "history.jsonl", write=False)
    assert p.is_protected(sdir / "history.jsonl", write=True)
    assert p.is_protected(secret.resolve(), write=False)
    assert p.is_readable((tmp_path / "docs" / "a.pdf").resolve(), cwd)
