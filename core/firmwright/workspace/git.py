"""git 命令行的薄封装。只用 git 自带的命令，不引入 libgit2 / GitPython。

所有命令都带 core.quotepath=false（中文路径原样输出）和 core.longpaths=true（Windows 长路径），
并关掉交互提示：核心是后台进程，git 等用户输入就会一直卡住。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

from ..osal import IS_WINDOWS, ProcResult, run_process

# 换行符设置（core.autocrlf 等）沿用用户自己的配置：worktree 检出、合并回用户工作目录时和用户平时用 git 一样
GIT_FLAGS = ["-c", "core.quotepath=false", "-c", "core.longpaths=true"]
# checkpoint 提交的作者：固定写成 Firmwright，不依赖用户有没有配 user.name（影子引用不进用户的分支）
BOT_IDENTITY = {"GIT_AUTHOR_NAME": "Firmwright", "GIT_AUTHOR_EMAIL": "firmwright@localhost",
                "GIT_COMMITTER_NAME": "Firmwright", "GIT_COMMITTER_EMAIL": "firmwright@localhost"}


class GitError(RuntimeError):
    def __init__(self, args: list[str], res: ProcResult) -> None:
        self.args_ = args
        self.res = res
        msg = (res.stderr or res.stdout).strip() or f"exit code {res.code}"
        super().__init__(f"git {' '.join(args[:3])} failed: {msg[-800:]}")


def git_exe() -> str | None:
    return shutil.which("git")


def _env(extra: Mapping[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"  # 读操作（status）不去抢 index.lock，避免和用户自己的 git 打架
    env["LC_ALL"] = "C"  # 错误信息用英文，方便按文字判断（界面上另有中文说明）
    if extra:
        env.update(extra)
    return env


async def run(cwd: Path, *args: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None,
              timeout: float = 120) -> ProcResult:
    exe = git_exe() or "git"
    return await run_process([exe, *GIT_FLAGS, *args], cwd=cwd, env=_env(env), timeout=timeout, stdin=stdin)


async def out(cwd: Path, *args: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None,
              timeout: float = 120) -> str:
    """运行并返回 stdout（去掉首尾空白）；失败抛 GitError。"""
    res = await run(cwd, *args, env=env, stdin=stdin, timeout=timeout)
    if not res.ok:
        raise GitError(list(args), res)
    return res.stdout.strip()


async def raw(cwd: Path, *args: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None,
              timeout: float = 300) -> bytes:
    """原样拿 stdout 的字节（补丁要保留 CRLF，不能走按行处理的 run_process）；失败抛 GitError。"""
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = await asyncio.create_subprocess_exec(
        git_exe() or "git", *GIT_FLAGS, *args, cwd=str(cwd), env=_env(env),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
    try:
        so, se = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        proc.kill()
        raise
    if proc.returncode != 0:
        raise GitError(list(args), ProcResult(code=proc.returncode, stdout="",
                                              stderr=se.decode("utf-8", "replace"), duration_ms=0))
    return so


async def ok(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> bool:
    return (await run(cwd, *args, env=env)).ok
