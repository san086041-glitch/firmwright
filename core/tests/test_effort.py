"""思考程度（2026-10-05）：按模型配置的 effort_style 翻译成各家的参数；没选时什么都不发。"""

from firmwright.config import ModelConfig, effort_options
from firmwright.model.openai_compat import OpenAICompatBackend
from firmwright.model.types import ModelRequest


def body(style: str, effort: str | None) -> dict:
    b = OpenAICompatBackend("http://x", "k", effort_style=style)
    return b.build_body(ModelRequest(model="m", system="s", messages=[], effort=effort))


def test_effort_params_per_style():
    assert "reasoning_effort" not in body("reasoning_effort", None)  # 默认：不发
    assert body("reasoning_effort", "high")["reasoning_effort"] == "high"
    b = body("enable_thinking", "low")
    assert b["enable_thinking"] is True and b["thinking_budget"] == 1024
    assert body("enable_thinking", "off")["enable_thinking"] is False
    assert body("thinking_type", "on")["thinking"] == {"type": "enabled"}
    assert body("thinking_type", "off")["thinking"] == {"type": "disabled"}


def test_options_only_for_reasoning_models():
    cfg = ModelConfig(base_url="http://x", key_ref="env:X")
    assert effort_options(cfg) == []
    cfg.reasoning = True
    assert effort_options(cfg) == ["low", "medium", "high"]
    cfg.effort_style = "enable_thinking"
    assert effort_options(cfg) == ["off", "low", "medium", "high"]
    cfg.effort_style = "thinking_budget"
    assert effort_options(cfg) == ["low", "medium", "high"]
