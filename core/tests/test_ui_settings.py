"""设置页（2026-10-05）：界面添加的模型存 settings.json、密钥存凭据管理器；config.toml 不改、它的模型只读。"""

import json

import keyring
import pytest

from firmwright import ui_settings
from firmwright.model.fake import ScriptedBackend, call, say
from firmwright.runtime import Runtime

TOML = """
[models.toml-model]
base_url = "https://api.example.com/v1"
key_ref = "env:NOPE"

[defaults]
model = "toml-model"
"""

FIELDS = {"model": "deepseek-chat", "base_url": "https://api.deepseek.com", "context_window": 64000,
          "reasoning": True, "effort_style": "thinking_type"}


@pytest.fixture
def vault(monkeypatch):
    """假的凭据管理器：不碰真实的 Windows 凭据。"""
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda s, u, p: store.__setitem__((s, u), p))
    monkeypatch.setattr(keyring, "get_password", lambda s, u: store.get((s, u)))

    def delete(s, u):
        from keyring.errors import PasswordDeleteError

        if (s, u) not in store:
            raise PasswordDeleteError("missing")
        del store[(s, u)]

    monkeypatch.setattr(keyring, "delete_password", delete)
    return store


def make(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(TOML, "utf-8")
    return Runtime(home=home)


def test_saved_model_persists_key_goes_to_vault_and_toml_is_untouched(tmp_path, vault):
    rt = make(tmp_path)
    info = rt.save_model("ds", FIELDS, "sk-secret")
    assert info["source"] == "settings" and info["hasKey"] and info["keyRef"] == "keyring:firmwright/ds"
    assert vault[("firmwright", "ds")] == "sk-secret"
    saved = (rt.home / "settings.json").read_text("utf-8")
    assert "sk-secret" not in saved  # 密钥不落盘
    assert (rt.home / "config.toml").read_text("utf-8") == TOML

    rt2 = make(tmp_path)  # 重启后还在
    models = {m["id"]: m for m in rt2.list_models()}
    assert models["ds"]["model"] == "deepseek-chat" and models["ds"]["source"] == "settings"
    assert models["toml-model"]["source"] == "config.toml" and not models["toml-model"]["hasKey"]
    assert rt2.config.defaults.model == "toml-model"  # config.toml 定了默认，界面没改就不动

    # 改模型不重新输入密钥：保留原来的
    rt2.save_model("ds", {**FIELDS, "context_window": 128000})
    assert rt2.config.models["ds"].context_window == 128000 and vault[("firmwright", "ds")] == "sk-secret"


def test_rules_toml_models_are_read_only_new_models_need_a_key(tmp_path, vault):
    rt = make(tmp_path)
    with pytest.raises(ValueError, match="config.toml"):
        rt.save_model("toml-model", FIELDS, "k")
    with pytest.raises(ValueError, match="API key is required"):
        rt.save_model("new", FIELDS)
    with pytest.raises(ValueError, match="Base URL"):
        rt.save_model("new", {**FIELDS, "base_url": "api.deepseek.com"}, "k")
    with pytest.raises(ValueError, match="Model name"):
        rt.save_model("bad name!", FIELDS, "k")
    with pytest.raises(ValueError, match="config.toml"):
        rt.delete_model("toml-model")


def test_defaults_persist_and_delete_cleans_up(tmp_path, vault):
    rt = make(tmp_path)
    rt.save_model("ds", FIELDS, "sk")
    rt.set_defaults(model="ds", idle_policy="ignore")
    data = json.loads((rt.home / "settings.json").read_text("utf-8"))
    assert data["default_model"] == "ds" and data["idle_policy"] == "ignore"
    rt2 = make(tmp_path)
    assert rt2.config.defaults.model == "ds" and rt2.config.defaults.idle_policy == "ignore"
    rt2.delete_model("ds")
    assert "ds" not in rt2.config.models and ("firmwright", "ds") not in vault
    assert rt2.config.defaults.model == "toml-model"  # 默认模型被删了：退回剩下的


def test_broken_settings_file_does_not_stop_the_core(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text("{not json", "utf-8")
    rt = Runtime(home=home)
    assert rt.ui.models == {}


async def test_connection_test_checks_tool_calling(tmp_path, monkeypatch):
    cfg = ui_settings.ModelConfig(base_url="https://x", key_ref="inline")
    monkeypatch.setattr(ui_settings, "make_backend", lambda c, n, api_key=None: (ScriptedBackend([call("ping", {"ok": True})]), n))
    res = await ui_settings.test_model(cfg, "m", api_key="k")
    assert res["ok"] and res["toolCalls"] and res["warning"] is None
    monkeypatch.setattr(ui_settings, "make_backend", lambda c, n, api_key=None: (ScriptedBackend([say("pong")]), n))
    res = await ui_settings.test_model(cfg, "m", api_key="k")
    assert res["ok"] and not res["toolCalls"] and "function calling" in res["warning"]
