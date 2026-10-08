"""工作目录里的目录联接 / 符号链接（2026-10-04 真机实测事故之后加）。

事故经过：agent 在 worktree 里建了一个指向用户真实工程（D 盘）的目录联接；checkpoint 快照经过联接把
真实工程存了进去，回退时 `read-tree -m -u` 又经过联接把真实工程的文件删掉了。

这里的做法不依赖权限层（权限层的静态分析总有漏网的写法）：
- 快照：链接本身和它下面的内容都不进 checkpoint（pathspec 排除）
- 回退前：先删掉链接本身（只删链接，不碰链接指向的目录），再改文件，回退就到不了外面
- 丢弃会话删 worktree 前：同样先删掉所有链接
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def _is_link(p: Path) -> bool:
    try:
        return p.is_symlink() or p.is_junction()
    except OSError:
        return False


def find_links(root: Path, *, skip: set[str] | frozenset[str] = frozenset({".git"})) -> list[Path]:
    """root 下所有指向别处的目录链接（联接、目录符号链接）。不进入链接内部，也不进入 skip 里的目录名。"""
    out: list[Path] = []
    for dirpath, dirnames, _files in os.walk(root):
        keep = []
        for d in dirnames:
            if d in skip:
                continue
            p = Path(dirpath) / d
            if _is_link(p):
                out.append(p)
            else:
                keep.append(d)
        dirnames[:] = keep  # 不往链接里面走
    return out


def unlink(p: Path) -> None:
    """只删链接本身。目录联接和目录符号链接在 Windows 上都用 rmdir 删（不会删到目标里的内容）。"""
    try:
        os.unlink(p)
    except OSError:
        os.rmdir(p)


def skip_names(excludes: list[str]) -> set[str]:
    """从 checkpoint 的排除规则（**/build/** 这种）里取出目录名，扫描时跳过（build 目录很大）。"""
    names = {".git"}
    for pat in excludes:
        m = re.fullmatch(r"(?:\*\*/)?([^/*?\[]+)/\*\*", pat)
        if m:
            names.add(m.group(1))
    return names
