"""web.validation：关键校验规则与旧面板保持一致。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from config.defaults import default_bot_config
from web.routes.ws_settings import handle_get_ws, handle_save_ws
from web.routes import WEB_SERVER_KEY
from web.validation import validate_config, validate_ws_config, ws_section


def _base(**agent):
    return {
        "master_qq": ["10001"],
        "heartbeat": {"min_minutes": 10, "max_minutes": 20},
        "agent": {"enable": True, **agent},
    }


def test_default_config_is_valid():
    errors, warnings = validate_config(default_bot_config())
    assert errors == []
    assert isinstance(warnings, list)


def test_minimal_config_only_warns_about_missing_keyword_actions():
    errors, warnings = validate_config(_base())
    assert errors == []
    assert any("keyword_actions" in item for item in warnings)


def test_bad_core_sections_are_errors():
    errors, _ = validate_config(
        {
            "master_qq": [],
            "heartbeat": {"min_minutes": True, "max_minutes": 20},
            "agent": "not-an-object",
            "message": {"extraconfig": {"tokenizer": "yes"}},
        }
    )
    assert any("master_qq" in item for item in errors)
    assert any("heartbeat.min_minutes" in item for item in errors)
    assert any("agent 必须是对象" in item for item in errors)
    assert any("tokenizer" in item for item in errors)


def test_keyword_action_regex_and_unknown_action():
    data = _base()
    data["keyword_actions"] = [
        {"regex": "(", "action": "unknown"},
        {"regex": "ok", "action": "send_img", "params": {}},
    ]
    errors, warnings = validate_config(data)

    assert any("不是合法正则" in item for item in errors)
    assert any("img_addr" in item for item in errors)
    assert any("未注册" in item for item in warnings)


def test_known_actions_can_be_extended():
    data = _base()
    data["keyword_actions"] = [{"regex": "x", "action": "custom", "params": {}}]
    _, warnings = validate_config(data, known_actions={"custom"})
    assert not any("custom" in item for item in warnings)


def test_invalid_web_panel_and_ws_config():
    data = _base()
    data["web_panel"] = {"port": True, "host": "0.0.0.0", "token": ""}
    errors, warnings = validate_config(data)
    assert any("web_panel.port" in item for item in errors)
    assert any("token" in item for item in warnings)

    ws_errors, cleaned = validate_ws_config(
        {
            "type": "ws_re",
            "ws_host": " 127.0.0.1 ",
            "ws_reverse_port": "3001",
            "ws_reverse_strict_token": 1,
        }
    )
    assert ws_errors == []
    assert cleaned["ws_host"] == "127.0.0.1"
    assert cleaned["ws_reverse_port"] == 3001
    assert cleaned["ws_reverse_strict_token"] is True

    ws_errors, _ = validate_ws_config({"type": "nope", "ws_port": 70000})
    assert any("type" in item for item in ws_errors)
    assert any("ws_port" in item for item in ws_errors)

    assert set(ws_section({"type": "ws", "extra": 1})) == {"type"}


def test_ws_config_accepts_backend_aliases():
    errors, cleaned = validate_ws_config({"type": "ws_re", "backend": "snowluna"})
    assert errors == []
    assert cleaned["backend"] == "snowluma"

    errors, cleaned = validate_ws_config({"backend": ""})
    assert errors == []
    assert cleaned["backend"] == "napcat"

    errors, _ = validate_ws_config({"backend": "nope"})
    assert any("backend" in item for item in errors)

    section = ws_section({"type": "ws", "backend": "snowluma", "extra": 1})
    assert section == {"type": "ws", "backend": "snowluma"}


class _FakePaths:
    def __init__(self, root: Path):
        self.root = root

    def config_file(self, name: str) -> Path:
        return self.root / name


class _FakeSettings:
    def __init__(self, root: Path, backend: str = "snowluma"):
        self.paths = _FakePaths(root)
        self.connection = SimpleNamespace(backend=backend)

    def reload(self):
        return self


class _FakeServer:
    def __init__(self, root: Path, backend: str = "snowluma"):
        self.settings = _FakeSettings(root, backend)

    def connection_info(self):
        return {"backend": self.settings.connection.backend}


class _Request:
    def __init__(self, server, payload=None):
        self.app = {WEB_SERVER_KEY: server}
        self._payload = payload

    async def json(self):
        return self._payload


def _body(response):
    return json.loads(response.body)


def test_ws_settings_get_exposes_backends_and_inherits_sender_backend(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"type": "ws_re", "backend": "snowluma"}), encoding="utf-8"
    )
    (tmp_path / "sender_config.json").write_text(
        json.dumps({"type": "ws", "ws_port": 3000}), encoding="utf-8"
    )
    server = _FakeServer(tmp_path)

    response = asyncio.run(handle_get_ws(_Request(server)))
    body = _body(response)

    assert response.status == 200
    assert [item["id"] for item in body["backends"]] == ["napcat", "snowluma"]
    assert body["default_backend"] == "napcat"
    assert body["data"]["backend"] == "snowluma"
    # sender_config 没写 backend 时显示继承后的值
    assert body["sender"]["backend"] == "snowluma"


def test_ws_settings_save_normalizes_backend_and_syncs_sender(tmp_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"type": "ws_re", "backend": "napcat"}), encoding="utf-8"
    )
    server = _FakeServer(tmp_path, backend="snowluma")
    payload = {
        "data": {"type": "ws_re", "backend": "snowluna"},
        "sender": {"type": "ws", "ws_port": 3000},
    }

    response = asyncio.run(handle_save_ws(_Request(server, payload)))
    body = _body(response)

    assert response.status == 200, body
    assert body["ok"] is True
    assert "SnowLuma" in body["hint"]

    config = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    sender = json.loads((tmp_path / "sender_config.json").read_text(encoding="utf-8"))
    assert config["backend"] == "snowluma"
    assert sender["backend"] == "snowluma"
    assert sender["ws_port"] == 3000
