"""板级事实卡（D18）：<工程>\\.firmwright\\facts.toml。

验证者和 await_marker 都以它为准（参照 stm32-skill 每个工程一张 debug-loop.md）。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator


class Facts(BaseModel):
    chip: str | None = None
    baud: int = 115200
    boot_banner: str | None = None  # 出现它才算启动成功（正则）
    pass_marker: list[str] = Field(default_factory=list)
    fail_marker: list[str] = Field(default_factory=list)
    boot_window_s: float = 8.0
    settle_s: float = 2.0  # 期望行出现后再观察多久：这段时间里崩溃或出现失败行，仍然算失败
    port: str | None = None  # 可选：固定的 COM 口（没有 USB 序列号的转串口芯片用）

    @field_validator("pass_marker", "fail_marker", mode="before")
    @classmethod
    def _listify(cls, v):
        if v is None:
            return []
        return [v] if isinstance(v, str) else list(v)

    def compiled(self) -> tuple[re.Pattern | None, list[re.Pattern], list[re.Pattern]]:
        b = re.compile(self.boot_banner) if self.boot_banner else None
        return b, [re.compile(p) for p in self.pass_marker], [re.compile(p) for p in self.fail_marker]

    @classmethod
    def load(cls, root: Path) -> Facts | None:
        p = root / ".firmwright" / "facts.toml"
        if not p.is_file():
            return None
        return cls.model_validate(tomllib.loads(p.read_text("utf-8")))
