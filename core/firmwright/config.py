"""配置与磁盘布局（方案 §3.1 / §3.3，D01 D14）。

%LOCALAPPDATA%\\Firmwright\\config.toml，写法参照 grok 的 [model.<name>]。
测试和脚本可以用 FIRMWRIGHT_HOME 覆盖应用数据目录。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .mcp.client import McpServerConfig
from .model.types import ModelCaps


def app_home() -> Path:
    if env := os.environ.get("FIRMWRIGHT_HOME"):
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Firmwright"  # D01


class ModelConfig(BaseModel):
    backend: Literal["openai_compat"] = "openai_compat"  # I07：以后加 anthropic_messages 等
    model: str | None = None  # 发给 API 的模型名；缺省用配置里的键名
    base_url: str
    key_ref: str  # D14："keyring:<service>/<user>"；也可 "env:<变量名>" 或开发期的 "dotenv:<路径>#<变量名>"
    context_window: int = 128_000
    vision: bool = False
    reasoning: bool = False
    echo_reasoning: bool = False
    parallel_tool_calls: bool = True
    # 评测用：请求的实际输入超过 context_window 时当作 API 拒绝（"上下文超长"），模拟真实的小窗口模型
    enforce_window: bool = False
    temperature: float | None = None
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # 思考程度（2026-10-05）：各家参数不同，按 effort_style 翻译；用户没选（"Default"）时什么都不发
    #   reasoning_effort：OpenAI 标准 "reasoning_effort": low / medium / high
    #   enable_thinking ：Qwen / SiliconFlow 的 "enable_thinking" + "thinking_budget"，可以关
    #   thinking_budget ：同上但不提供"关"（SiliconFlow 上的 GLM 关不掉；Kimi 关了会把思考写进回答里，2026-10-05 实测）
    #   thinking_type   ：GLM / Kimi / DeepSeek 一类的 "thinking": {"type": "enabled" | "disabled"}，只有开 / 关
    effort_style: Literal["reasoning_effort", "enable_thinking", "thinking_budget", "thinking_type"] = "reasoning_effort"

    def caps(self) -> ModelCaps:
        return ModelCaps(
            context_window=self.context_window,
            vision=self.vision,
            reasoning=self.reasoning,
            echo_reasoning=self.echo_reasoning,
            parallel_tool_calls=self.parallel_tool_calls,
            enforce_window=self.enforce_window,
        )


class Defaults(BaseModel):
    model: str | None = None
    idle_policy: Literal["ignore", "notify"] = "notify"  # I04 + D11
    permission_mode: Literal["default", "accept_edits", "plan", "always_approve"] = "default"
    max_steps: int = 60  # 一轮里最多调用模型的次数


class PermissionConfig(BaseModel):
    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    read_roots: list[Path] = Field(default_factory=list)  # W7：工作目录以外不用询问就能读的目录


class Features(BaseModel):
    """§6.5 功能开关，为以后的对照实验准备。只做配置项，不做界面。"""

    events_inject: bool = True  # events.inject
    events_interrupt: bool = True  # events.interrupt
    context_compaction: bool = True  # context.compaction（W6）：用量超过 80% 自动压缩
    context_skills: bool = True  # context.skills（W6）：skill 清单 + skill 工具
    context_memory: bool = True  # context.memory（W6）：跨会话记忆的注入和工具
    subagents: bool = True  # subagents（W7）：spawn_subagent 工具
    context_log_digest: bool = True  # context.log_digest：串口只给事件摘要 + log_ref
    verify_device_oracle: bool = True  # verify.device_oracle（W7）
    verify_progress_judge: bool = True  # verify.progress_judge（2026-10-05）：goal 连续几轮没有进展就暂停
    checkpoint_enabled: bool = True  # checkpoint.enabled（W5）


class IdfConfig(BaseModel):
    """用哪个 ESP-IDF。三种写法（2026-10-06 首次启动向导）：
    - path 有值：直接用这个 IDF 根目录（install.bat / 旧版安装器的布局，tools_path 缺省 %USERPROFILE%\\.espressif）
    - eim_json 指向的文件存在：用 EIM 登记的安装（idf_id 缺省取它选中的那个）
    - 都没有：自动找本机的 IDF（platform/esp_idf/discover.py），取第一个能用的"""

    eim_json: Path = Path(r"C:\Espressif\tools\eim_idf.json")
    idf_id: str | None = None  # 缺省用 eim_idf.json 的 idfSelectedId
    path: Path | None = None
    tools_path: Path | None = None


class BuildConfig(BaseModel):
    """agent 调用编译时的资源限制（只影响 Firmwright 自己发起的编译，不影响手动运行的 idf.py）。

    ESP-IDF 默认按 逻辑核数+2 并行，全量编译一两分钟里创建上千个进程；多份同时编译在开发机上触发过蓝屏。
    """

    jobs: int = 0  # Ninja 并行数；0 = 自动（逻辑核数的一半，至少 2）
    exclusive: bool = True  # 全局同一时间只跑一个编译类操作（跨进程文件锁）

    def resolved_jobs(self) -> int:
        if self.jobs > 0:
            return self.jobs
        return max(2, (os.cpu_count() or 4) // 2)


class SkillsConfig(BaseModel):
    """W6：skill（SKILL.md）。工程的 .firmwright/skills 等目录和内置 skills/ 之外，可以再加目录。"""

    paths: list[Path] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)  # 不使用的 skill 名


class McpConfig(BaseModel):
    """W6：MCP 客户端。[mcp.servers.<名字>] command / args / env / cwd / enabled / timeout / read_only"""

    servers: dict[str, McpServerConfig] = Field(default_factory=dict)
    search_threshold: int = 30  # MCP 工具超过这个数量时，改用 search_tool + use_tool（grok 的做法）


class WorktreeConfig(BaseModel):
    """W5：每个会话一个 git worktree（I09）。"""

    enabled: bool = True  # 关掉后新会话直接在工程目录里工作（checkpoint 仍然可用，只要工程是 git 仓库）
    root: Path = Path(r"C:\fwr\wt")  # D02：短路径
    prebuild: bool = True  # 新建会话后在后台先编译一次（§5.2），受 [build] 的排队限制


class Config(BaseModel):
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    defaults: Defaults = Field(default_factory=Defaults)
    permissions: PermissionConfig = Field(default_factory=PermissionConfig)
    features: Features = Field(default_factory=Features)
    idf: IdfConfig = Field(default_factory=IdfConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    worktree: WorktreeConfig = Field(default_factory=WorktreeConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or app_home() / "config.toml"
        if not path.exists():
            return cls()
        data = tomllib.loads(path.read_text("utf-8"))
        # [features] 里允许写成 "events.inject = true" 的点号形式
        feats = data.get("features")
        if isinstance(feats, dict):
            data["features"] = _flatten_features(feats)
        return cls.model_validate(data)


def _flatten_features(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}_{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_features(v, key))
        else:
            out[key.replace(".", "_")] = v
    return out


class SecretError(RuntimeError):
    pass


def resolve_key(key_ref: str) -> str:
    """D14：API Key 存 Windows 凭据管理器（keyring），也允许 env: 引用（脚本、CI）。"""
    if key_ref.startswith("env:"):
        val = os.environ.get(key_ref[4:], "")
        if not val:
            raise SecretError(f"Environment variable {key_ref[4:]} is empty")
        return val
    if key_ref.startswith("keyring:"):
        import keyring

        service, _, user = key_ref[8:].partition("/")
        val = keyring.get_password(service, user or "default")
        if not val:
            raise SecretError(f"No {service}/{user} entry in the Windows Credential Manager")
        return val
    if key_ref.startswith("dotenv:"):
        # 开发期用：直接引用另一个 .env 文件里的变量，不把密钥复制进本仓库或配置
        path, _, var = key_ref[7:].rpartition("#")
        for line in Path(path).read_text("utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == var:
                return v.strip().strip('"').strip("'")
        raise SecretError(f"{var} not found in {path}")
    raise SecretError(f"Unrecognized key_ref: {key_ref} (expected keyring:… / env:… / dotenv:…#VAR)")


EFFORTS = {"reasoning_effort": ["low", "medium", "high"], "enable_thinking": ["off", "low", "medium", "high"],
           "thinking_budget": ["low", "medium", "high"],
           "thinking_type": ["off", "on"]}


def effort_options(cfg: ModelConfig) -> list[str]:
    """界面上可选的思考程度（不含 "default" = 不发参数）。不支持推理的模型没有选项。"""
    return EFFORTS[cfg.effort_style] if cfg.reasoning else []


def make_backend(cfg: ModelConfig, name: str, *, api_key: str | None = None):
    """按配置构造模型后端。返回 (backend, 发给 API 的模型名)。
    api_key：设置页"测试连接"时表单里还没保存的密钥，直接传进来（不经过环境变量，免得 agent 的 shell 看到）。"""
    if cfg.backend == "openai_compat":
        from .model.openai_compat import OpenAICompatBackend

        return (
            OpenAICompatBackend(cfg.base_url, api_key or resolve_key(cfg.key_ref), cfg.caps(), extra_body=cfg.extra_body,
                                effort_style=cfg.effort_style),
            cfg.model or name,
        )
    raise ValueError(f"Backend not implemented: {cfg.backend}")
