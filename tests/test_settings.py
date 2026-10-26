"""BotSettings：默认值、覆盖、保存 / 重载、环境与目录覆盖。"""

from pathlib import Path

from config.environment import EnvKind
from config.settings import BotSettings


def test_defaults_and_overrides_are_merged(tmp_path):
    settings = BotSettings(
        root=tmp_path,
        overrides={
            "agent": {
                "enable": False,
                "models": [{"id": "m1", "model": "a"}],
                "active_model": "m1",
            }
        },
        auto_create=False,
    )

    assert settings.agent_enabled is False
    assert settings.get("agent.models")[0]["id"] == "m1"
    assert settings.agent_model_routing["active_model"] == "m1"
    # 未覆盖的键仍然来自默认配置
    assert settings.get("heartbeat.min_minutes") == 10
    assert "group_context" in settings.section("prompts")


def test_save_and_reload_roundtrip(tmp_path):
    settings = BotSettings(root=tmp_path, auto_create=False)
    settings.update({"agent": {"base_url": "http://x/v1", "model": "test-model"}})
    saved = settings.save()

    assert Path(saved) == tmp_path / "config" / "bot_config.json"
    assert Path(saved).is_file()

    other = BotSettings(root=tmp_path, auto_create=False)
    assert other.agent_llm_config["base_url"] == "http://x/v1"
    assert other.agent_llm_config["model"] == "test-model"

    # 模拟面板写盘后热加载
    other.data["agent"]["model"] = "changed"
    other.save()
    settings.reload()
    assert settings.agent_llm_config["model"] == "changed"


def test_environment_overrides_paths_and_bridge(tmp_path):
    settings = BotSettings(
        root=tmp_path,
        env_overrides={
            "bot_env": "windows",
            "napcat_env": "wsl",
            "imgs_dir": str(tmp_path / "custom_imgs"),
        },
        auto_create=False,
    )

    assert settings.env.bot_env is EnvKind.WINDOWS
    assert settings.env.napcat_env is EnvKind.WSL
    assert settings.paths.images_dir == (tmp_path / "custom_imgs").resolve()
    assert settings.bridge.to_napcat(tmp_path / "a.png").startswith("/mnt/")


def test_invalid_env_value_falls_back_to_detected(tmp_path):
    # 未知环境名不应该让配置加载直接崩掉，沿用默认探测结果。
    settings = BotSettings(
        root=tmp_path, env_overrides={"bot_env": "not-a-real-env"}, auto_create=False
    )
    assert settings.env.bot_env in (EnvKind.WINDOWS, EnvKind.WSL, EnvKind.LINUX)
