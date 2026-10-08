"""编译资源限制：跨进程编译锁、并行数、直接调 ninja 时的 stale build 检测。"""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from firmwright.config import BuildConfig
from firmwright.model.types import CancelToken
from firmwright.osal.filelock import file_lock


async def test_lock_serializes_and_reports_waiting(tmp_path):
    lock = tmp_path / "build.lock"
    order: list[str] = []
    waited: list[str] = []

    async def job(name: str, hold: float) -> None:
        async def on_wait() -> None:
            waited.append(name)

        async with file_lock(lock, on_wait=on_wait, poll=0.05):
            order.append(f"{name}+")
            await asyncio.sleep(hold)
            order.append(f"{name}-")

    await asyncio.gather(job("a", 0.3), job("b", 0.0))
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])
    assert len(waited) == 1  # 后来的那个排了队


async def test_lock_is_cross_process_and_cancellable(tmp_path):
    lock = tmp_path / "build.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import msvcrt,os,time,sys;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT);"
         "msvcrt.locking(fd,msvcrt.LK_NBLCK,1);print('locked',flush=True);time.sleep(30)", str(lock)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        cancel = CancelToken()
        asyncio.get_running_loop().call_later(0.4, cancel.cancel, "用户取消")
        with pytest.raises(asyncio.CancelledError):
            async with file_lock(lock, cancel=cancel, poll=0.05):
                pass  # 另一个进程拿着锁，进不来
    finally:
        holder.kill()


def test_jobs_default_is_half_the_cores(monkeypatch):
    monkeypatch.setattr("os.cpu_count", lambda: 16)
    assert BuildConfig().resolved_jobs() == 8
    assert BuildConfig(jobs=3).resolved_jobs() == 3
    monkeypatch.setattr("os.cpu_count", lambda: 2)
    assert BuildConfig().resolved_jobs() == 2


def test_stale_build_dir_detected(tmp_path):
    from firmwright.platform.esp_idf.adapter import EspIdfAdapter
    from firmwright.platform.esp_idf.parse import parse_build_output

    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "CMakeCache.txt").write_text("CMAKE_HOME_DIRECTORY:INTERNAL=C:/somewhere/else\n")
    a = EspIdfAdapter.__new__(EspIdfAdapter)
    msg = a._stale_build_dir(tmp_path)
    assert msg and parse_build_output(msg)[1] == "stale_build_dir"
    (tmp_path / "build" / "CMakeCache.txt").write_text(f"CMAKE_HOME_DIRECTORY:INTERNAL={tmp_path.as_posix()}\n")
    assert a._stale_build_dir(tmp_path) is None


PROJ = Path("C:/fwr/wt/w1demo")


@pytest.mark.skipif(not (PROJ / "build" / "build.ninja").exists(), reason="没有编译过的 IDF 工程")
async def test_real_incremental_build_uses_ninja_with_jobs(tmp_path, monkeypatch):
    """真实的增量编译（已经编译过，几乎不产生新进程）：确认走 ninja -j，并且 size 的 JSON 能读出来。"""
    import firmwright.platform.esp_idf.adapter as mod
    from firmwright.platform.esp_idf.adapter import EspIdfAdapter
    from firmwright.platform.esp_idf.env import IdfEnv, load_eim
    from firmwright.tools.base import ToolContext
    from firmwright.trace import Trace

    seen: list[list[str]] = []
    real = mod.run_process

    async def spy(argv, **kw):
        seen.append(list(argv))
        return await real(argv, **kw)

    monkeypatch.setattr(mod, "run_process", spy)
    a = EspIdfAdapter(IdfEnv(load_eim(Path("C:/Espressif/tools/eim_idf.json")), tmp_path / "cache"),
                      build=BuildConfig(jobs=4), lock_path=tmp_path / "build.lock")
    ctx = ToolContext(session_id="t", cwd=PROJ, cancel=CancelToken(), trace=Trace(None, "t"))
    res = await a.build(ctx)
    assert res.ok, res.for_model()
    ninja_calls = [c for c in seen if Path(c[0]).stem.lower() == "ninja"]
    assert ninja_calls and ninja_calls[0][-3:] == ["-j", "4", "all"]
    rep = await a.size(ctx)
    assert rep and rep.dram_total and rep.app_bin_size
