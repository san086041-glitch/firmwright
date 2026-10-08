"""会话可用的外部服务（平台适配器、设备管理器、板级事实卡）。

工具通过 ToolContext.services 拿到它们；没有硬件的会话（或测试）可以全部为 None。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .device.manager import DeviceManager
    from .facts import Facts
    from .permissions.paths import PathPolicy
    from .platform.base import PlatformAdapter


@dataclass
class Services:
    platform: PlatformAdapter | None = None
    devices: DeviceManager | None = None
    facts: Facts | None = None
    paths: PathPolicy | None = None  # W7：受保护路径（grep 遍历目录时跳过）
    log_digest: bool = True  # 功能开关 context.log_digest：False = 串口给原文，不给摘要和预先解码的调用栈（消融基线）
    extra: dict[str, Any] | None = None
