"""路径映射端点：GET / POST / preview，以及热加载后 PathBridge 复用。"""

import asyncio
import json

from config.settings import BotSettings
from web.routes import WEB_SERVER_KEY
from web.routes.paths import (
    handle_get_paths,
    handle_preview_paths,
    handle_save_paths,
)
from web.validation import validate_paths_config


class _Server:
    def __init__(self, settings):
        self.settings = settings


class _Request:
    def __init__(self, server, payload=None):
        self.app = {WEB_SERVER_KEY: server}
        self._payload = payload

    async def json(self):
        return self._payload


def _body(response):
    return json.loads(response.body)


def _settings(tmp_path):
    return BotSettings(
        root=tmp_path,
        env_overrides={
            "bot_env": "windows",
            "backend_env": "wsl",
            "path_profile": "windows_to_wsl",
            "wsl_mount_root": "/mnt",
        },
        auto_create=False,
    )


def test_get_paths_exposes_env_profiles_and_resolved_dirs(tmp_path):
    settings = _settings(tmp_path)
    response = asyncio.run(handle_get_paths(_Request(_Server(settings))))
    body = _body(response)

    assert response.status == 200
    assert body["ok"] is True
    assert body["data"]["bot_env"] == "windows"
    assert body["data"]["backend_env"] == "wsl"
    assert body["data"]["path_profile"] == "windows_to_wsl"
    assert [item["id"] for item in body["profiles"]] == [
        "same",
        "windows_to_wsl",
        "wsl_to_windows",
        "custom",
    ]
    assert {item["id"] for item in body["env_options"]} == {"windows", "wsl", "linux"}
    assert body["resolved"]["root"] == str(tmp_path.resolve())
    assert body["resolved"]["imgs_dir"].endswith("imgs")
    assert body["meta"]["env_path"].endswith("environment.json")


def test_save_paths_writes_canonical_keys_and_hot_reloads_bridge(tmp_path):
    env_path = tmp_path / "config" / "environment.json"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(
        json.dumps(
            {
                "bot_env": "windows",
                "backend_env": "wsl",
                "path_profile": "windows_to_wsl",
                "imgs_dir": "custom_imgs",
                "path_map": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = BotSettings(root=tmp_path, auto_create=False)
    bridge = settings.bridge
    assert bridge.to_backend("D:\\share\\a.png") == "/mnt/d/share/a.png"

    payload = {
        "data": {
            "bot_env": "windows",
            "backend_env": "wsl",
            "path_profile": "windows_to_wsl",
            "wsl_mount_root": "/mnt",
            "path_map": [{"from": "D:\\share", "to": "/data/share", "note": "NAS"}],
        }
    }
    response = asyncio.run(handle_save_paths(_Request(_Server(settings), payload)))
    body = _body(response)

    assert response.status == 200, body
    assert body["ok"] is True
    assert body["restart_required"] is False
    assert body["saved_to"].endswith("environment.json")

    saved = json.loads(env_path.read_text(encoding="utf-8"))
    assert saved["backend_env"] == "wsl"
    assert saved["path_profile"] == "windows_to_wsl"
    assert saved["path_map"] == [{"from": "D:\\share", "to": "/data/share", "note": "NAS"}]
    # 路径字段之外的目录配置必须原样保留
    assert saved["imgs_dir"] == "custom_imgs"
    # 旧别名不应再写回文件
    assert "napcat_env" not in saved
    assert "onebot_env" not in saved

    # ImageResolver 持有的是同一个 PathBridge，热加载必须原地生效
    assert settings.bridge is bridge
    assert bridge.to_backend("D:\\share\\a.png") == "/data/share/a.png"
    assert bridge.to_local("/data/share/a.png").as_posix() == "D:/share/a.png"


def test_save_paths_rejects_invalid_profile_and_empty_rules(tmp_path):
    settings = _settings(tmp_path)

    response = asyncio.run(
        handle_save_paths(
            _Request(_Server(settings), {"data": {"path_profile": "nope"}})
        )
    )
    body = _body(response)
    assert response.status == 400
    assert any("path_profile" in item for item in body["errors"])

    response = asyncio.run(
        handle_save_paths(
            _Request(
                _Server(settings),
                {"data": {"path_profile": "custom", "path_map": [{"from": "D:\\x"}]}},
            )
        )
    )
    body = _body(response)
    assert response.status == 400
    assert any("path_map" in item for item in body["errors"])


def test_preview_paths_uses_current_config_and_draft_rules(tmp_path):
    settings = _settings(tmp_path)
    server = _Server(settings)

    response = asyncio.run(
        handle_preview_paths(_Request(server, {"value": "D:\\Code\\a.png"}))
    )
    body = _body(response)
    assert response.status == 200, body
    assert body["to_backend"]["output"] == "/mnt/d/Code/a.png"
    assert body["to_local"]["output"] == "D:\\Code\\a.png"

    response = asyncio.run(
        handle_preview_paths(
            _Request(
                server,
                {
                    "value": "D:\\share\\a.png",
                    "data": {
                        "path_map": [{"from": "D:\\share", "to": "/data/share"}],
                    },
                },
            )
        )
    )
    body = _body(response)
    assert body["to_backend"]["output"] == "/data/share/a.png"
    assert body["to_backend"]["rule"]["from"] == "D:\\share"
    assert body["to_local"]["output"] == "D:\\share\\a.png"


def test_preview_paths_handles_special_values_and_empty_input(tmp_path):
    server = _Server(_settings(tmp_path))

    response = asyncio.run(
        handle_preview_paths(
            _Request(server, {"value": "https://example.com/a.png"})
        )
    )
    body = _body(response)
    assert body["to_backend"]["special"] is True
    assert body["to_backend"]["output"] == "https://example.com/a.png"

    response = asyncio.run(handle_preview_paths(_Request(server, {"value": ""})))
    assert response.status == 400


def test_validate_paths_config_normalizes_and_warns():
    errors, warnings, cleaned = validate_paths_config(
        {
            "bot_env": "windows",
            "onebot_env": "wsl",
            "path_profile": "windows_to_wsl",
            "wsl_mount_root": "/mnt/",
            "path_map": [{"from": "D:\\share", "to": "/data/share"}],
        }
    )
    assert errors == []
    assert cleaned["backend_env"] == "wsl"
    assert cleaned["path_profile"] == "windows_to_wsl"
    assert cleaned["wsl_mount_root"] == "/mnt"
    assert cleaned["path_map"] == [{"from": "D:\\share", "to": "/data/share"}]
    assert warnings == []

    errors, warnings, _ = validate_paths_config(
        {
            "bot_env": "windows",
            "backend_env": "linux",
            "path_profile": "same",
            "path_map": [{"from": "relative", "to": "also-relative"}],
        }
    )
    assert errors == []
    assert any("path_profile=same" in item for item in warnings)
    assert any("不是绝对路径" in item for item in warnings)
