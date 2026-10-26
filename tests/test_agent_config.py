import threading

import asyncio
import json
from types import SimpleNamespace

import pytest

from pathlib import Path

import agent.campus as campus_module
import agent.config as llm_module
from agent.config import LLM_CONFIG, resolve_llm_config
from bot.app import BotApp
from config import BotSettings
from web.validation import validate_config


def BotConfig(*, path=None, overrides=None):
    """旧测试构造 BotConfig(path=..., overrides=...) 的兼容包装。

    新项目固定把配置放在 ``<root>/config/bot_config.json``，
    所以这里把旧参数里的文件名映射成 root。
    """
    root = Path(path).parent if path is not None else None
    return BotSettings(root=root, overrides=overrides, auto_create=False)


def _base(**agent):
    return {
        "master_qq": ["10001"],
        "heartbeat": {"min_minutes": 10, "max_minutes": 20},
        "agent": {"enable": True, **agent},
    }


# ── 配置合并 ────────────────────────────────────────────────
def test_resolve_llm_config_merges_and_normalizes():
    cfg = resolve_llm_config(
        {
            "base_url": "  http://127.0.0.1:8000/v1  ",
            "api_key": " sk-panel ",
            "model": " Qwen3.6 ",
            "temperature": "0.2",
            "max_tokens": "4096",
            "enable": True,          # 不属于 LLM 参数，忽略
            "unrelated": "x",
        }
    )
    assert cfg["base_url"] == "http://127.0.0.1:8000/v1"
    assert cfg["api_key"] == "sk-panel"
    assert cfg["model"] == "Qwen3.6"
    assert cfg["temperature"] == 0.2
    assert cfg["max_tokens"] == 4096
    assert "enable" not in cfg
    assert "unrelated" not in cfg


def test_resolve_llm_config_supports_qianfan_env(monkeypatch):
    monkeypatch.setenv("QIAN_FAN_API_KEY", "qianfan-secret")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    cfg = resolve_llm_config({
        "base_url": "https://qianfan.baidubce.com/v2",
        "api_key_env": "QIAN_FAN_API_KEY",
        "api_key": "",
        "model": "ernie-speed",
    })

    assert cfg["api_key"] == "qianfan-secret"
    assert cfg["api_key_env"] == "QIAN_FAN_API_KEY"
    assert cfg["model"] == "ernie-speed"


def test_resolve_llm_config_empty_values_keep_defaults():
    assert resolve_llm_config({"api_key": "", "max_tokens": None}) == dict(
        resolve_llm_config()
    )
    assert resolve_llm_config({"temperature": True, "max_tokens": "abc"}) == dict(
        resolve_llm_config()
    )


def test_resolve_llm_config_max_tokens_follows_env(monkeypatch):
    monkeypatch.setenv("LLM_MAX_TOKENS", "2048")
    assert resolve_llm_config({"max_tokens": None})["max_tokens"] == 2048


def test_resolve_llm_config_defaults_are_stable():
    cfg = resolve_llm_config()
    assert cfg["base_url"] == "https://api.deepseek.com/v1"
    assert cfg["model"] == "deepseek-v4-flash"
    assert cfg["temperature"] == 0.7
    assert cfg["max_tokens"] == 10240
    assert LLM_CONFIG["api_key"] is not None


# ── BotConfig ───────────────────────────────────────────────
def test_agent_llm_config_defaults(tmp_path):
    cfg = BotConfig(path=tmp_path / "bot_config.json")
    assert cfg.agent_llm_config == {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-v4-flash",
        "temperature": 0.7,
        "max_tokens": None,
    }


def test_agent_llm_config_excludes_non_llm_keys(tmp_path):
    cfg = BotConfig(
        path=tmp_path / "bot_config.json",
        overrides={"agent": {"enable": False, "base_url": "http://x/v1", "foo": 1}},
    )
    assert cfg.agent_enabled is False
    assert cfg.agent_llm_config["base_url"] == "http://x/v1"
    assert cfg.get("agent.models") == []
    assert cfg.get("agent.active_model") == ""
    assert "enable" not in cfg.agent_llm_config
    assert "models" not in cfg.agent_llm_config
    assert "foo" not in cfg.agent_llm_config


# ── 网页面板校验 ────────────────────────────────────────────
def _model(**entry):
    return {"id": "m1", "name": "", "base_url": "https://api.example.com/v1",
            "api_key": "", "api_key_env": "MY_API_KEY", "model": "my-model", **entry}


def test_validate_accepts_model_list():
    errors, _warnings = validate_config(
        _base(models=[_model(), _model(id="m2", name="备用", model="other-model")],
              active_model="m2")
    )
    assert errors == []


@pytest.mark.parametrize(
    "models",
    [
        "not-a-list",
        [123],
        [{"id": ""}],
        [{"id": "m1"}, {"id": "m1"}],
        [{"id": "m1", "name": 123}],
        [{"id": "m1", "base_url": 123}],
        [{"id": "m1", "base_url": "api.example.com/v1"}],
        [{"id": "m1", "api_key": 123}],
        [{"id": "m1", "api_key_env": "1 BAD NAME"}],
        [{"id": "m1", "model": 123}],
    ],
)
def test_validate_rejects_bad_model_list(models):
    errors, _warnings = validate_config(_base(models=models))
    assert errors, models


def test_validate_warns_unknown_active_model():
    errors, warnings = validate_config(_base(models=[_model()], active_model="m9"))
    assert errors == []
    assert any("active_model" in item for item in warnings)


def test_validate_accepts_agent_llm_fields():
    errors, _warnings = validate_config(
        _base(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-test",
            model="deepseek-v4-flash",
            temperature=0.5,
            max_tokens=4096,
        )
    )
    assert errors == []


def test_validate_accepts_qianfan_env_name():
    errors, _warnings = validate_config(
        _base(
            base_url="https://qianfan.baidubce.com/v2",
            api_key_env="QIAN_FAN_API_KEY",
            model="ernie-speed",
        )
    )
    assert errors == []


def test_validate_allows_empty_optional_agent_fields():
    errors, _warnings = validate_config(_base(api_key="", max_tokens=None))
    assert errors == []


@pytest.mark.parametrize(
    "override",
    [
        {"base_url": "api.deepseek.com/v1"},
        {"base_url": "   "},
        {"base_url": 123},
        {"model": ""},
        {"api_key": 123},
        {"temperature": 3},
        {"temperature": True},
        {"max_tokens": 0},
        {"max_tokens": 12.5},
    ],
)
def test_validate_rejects_bad_agent_llm_fields(override):
    errors, _warnings = validate_config(_base(**override))
    assert errors, override


def test_panel_save_persists_agent_fields(tmp_path):
    """走一遍真实的 POST /api/config：校验 → 合并 → 写盘 → 回读。"""
    from web.routes.config import handle_save_config
    from web.server import WebServer

    path = tmp_path / "bot_config.json"
    config = BotConfig(path=path)
    config.save()
    server = WebServer(settings=config, port=0, token=None)

    payload = config.to_dict()
    payload["agent"].update(
        {
            "base_url": "http://127.0.0.1:8000/v1",
            "api_key": "sk-panel",
            "model": "Qwen3.6",
            "temperature": 0.3,
            "max_tokens": 2048,
        }
    )

    class _Request:
        app = server.app

        async def json(self):
            return {"data": payload}

    response = asyncio.run(handle_save_config(_Request()))
    body = json.loads(response.body)

    assert response.status == 200, body
    assert body["ok"] is True
    assert BotConfig(path=path).agent_llm_config["base_url"] == "http://127.0.0.1:8000/v1"
    assert config.agent_llm_config["model"] == "Qwen3.6"
    assert config.agent_llm_config["max_tokens"] == 2048


# ── BOTMain 接线 / 热重载 ───────────────────────────────────
class _FakeLLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def _bare_bot(tmp_path, enable=True, **agent):
    bot = BotApp.__new__(BotApp)
    bot.settings = BotConfig(
        path=tmp_path / "bot_config.json", overrides={"agent": {"enable": enable, **agent}}
    )
    bot.api = bot.mp = bot.media = bot.images = None
    bot._agent = None
    bot.campus_assistant = None
    bot._agent_generation = 0
    bot._agent_ready = threading.Event()
    return bot


def test_init_agent_bg_passes_panel_config(tmp_path, monkeypatch):
    import agent

    bot = _bare_bot(
        tmp_path, base_url="http://127.0.0.1:8000/v1", api_key="sk", model="qwen3"
    )
    monkeypatch.setattr(agent, "QQBotAgent", _FakeLLM)
    monkeypatch.setattr(campus_module, "CampusAssistant", _FakeLLM)

    bot._init_agent_bg(generation=0)

    assert bot._agent.kwargs["llm_config"]["base_url"] == "http://127.0.0.1:8000/v1"
    assert bot.campus_assistant.kwargs["llm_config"]["model"] == "qwen3"
    assert bot._agent_ready.is_set()


def test_init_agent_bg_discards_stale_generation(tmp_path, monkeypatch):
    import agent

    bot = _bare_bot(tmp_path, base_url="http://new/v1")
    bot._agent_generation = 2  # 面板已经又保存过一次
    monkeypatch.setattr(agent, "QQBotAgent", _FakeLLM)
    monkeypatch.setattr(campus_module, "CampusAssistant", _FakeLLM)

    bot._init_agent_bg(generation=1)

    assert bot._agent is None
    assert bot.campus_assistant is None
    assert not bot._agent_ready.is_set()


def test_restart_agent_swaps_state(tmp_path):
    bot = _bare_bot(tmp_path, enable=False)
    old_ready = bot._agent_ready
    bot._agent = "old"

    bot._restart_agent()

    assert bot._agent_generation == 1
    assert bot._agent is None
    assert bot._agent_ready is not old_ready
    assert bot._agent_ready.wait(timeout=5)


def test_reload_config_hot_reloads_agent_section(tmp_path, monkeypatch):
    path = tmp_path / "bot_config.json"
    BotConfig(path=path, overrides={"agent": {"base_url": "http://old/v1"}}).save()

    bot = BotApp.__new__(BotApp)
    bot.settings = BotConfig(path=path)
    bot.mp = SimpleNamespace(extraconfig={})
    calls = []
    monkeypatch.setattr(
        bot, "_restart_agent", lambda: calls.append(bot.settings.agent_llm_config)
    )

    # 面板先把新配置写盘，再调 reload_config（真实流程就是这样）
    BotConfig(path=path, overrides={"agent": {"base_url": "http://new/v1"}}).save()
    result = bot.reload_config()

    assert len(calls) == 1
    assert calls[0]["base_url"] == "http://new/v1"
    assert result["agent_reloading"] is True
    assert "agent" in result["applied"]
    assert result["restart_required"] is False
