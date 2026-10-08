"""跨进程的文件锁：同一时间只允许一个编译类操作（编译 / 烧录前的增量编译 / 读大小 / set-target）。

ESP-IDF 全量编译会在一两分钟里创建上千个进程；几份同时编译时进程创建的频率成倍增加，
在这台开发机上触发过蓝屏（创建子进程 → 应用兼容性缓存 → NtUnmapViewOfSection）。
桌面端、开发桥、scripts/run_task.py 可能同时运行，所以锁要跨进程，用 %LOCALAPPDATA%\\Firmwright\\build.lock。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

from ..model.types import CancelToken


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


@contextlib.asynccontextmanager
async def file_lock(path: Path, *, cancel: CancelToken | None = None,
                    on_wait: Callable[[], Awaitable[None]] | None = None,
                    poll: float = 0.5) -> AsyncIterator[None]:
    """拿到锁才进入；等待期间可以被取消（抛 asyncio.CancelledError），第一次需要等待时回调 on_wait。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    try:
        waited = False
        while not _try_lock(fd):
            if not waited and on_wait:
                await on_wait()
            waited = True
            if cancel and cancel.cancelled:
                raise asyncio.CancelledError(cancel.reason)
            await asyncio.sleep(poll)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)
