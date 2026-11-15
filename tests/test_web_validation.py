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
    data["web_panel"] = {
        "port": True,
        "host": "0.0.0.0",
        "token": "",
        "cors_origins": ["https://panel.example.com/with-a-path"],
    }
    errors, _warnings = validate_config(data)
    assert any("web_panel.port" in item for item in errors)
    assert any("token" in item for item in errors)
    assert any("cors_origins" in item for item in errors)

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


def test_agent_catchphrase_config_validation():
    data = _base(catchphrase_file="agent/catchphrases.json", catchphrase_limit=10)
    errors, _ = validate_config(data)
    assert not any("catchphrase" in item for item in errors)

    data = _base(catchphrase_file=123, catchphrase_limit=-1)
    errors, _ = validate_config(data)
    assert any("catchphrase_file" in item for item in errors)
    assert any("catchphrase_limit" in item for item in errors)


def test_auto_forward_sender_filter_validation():
    data = _base()
    data["auto_forward"] = {
        "filter": [
            {"group_id": 20002, "user_id": [10003, "10004"]},
        ]
    }
    errors, _ = validate_config(data)
    assert not any("auto_forward.filter" in item for item in errors)

    data["auto_forward"] = {
        "filter": [
            {"user_id": [10003]},
            "bad",
            {"group_id": 20002, "user_id": {"not": "a list"}},
        ]
    }
    errors, warnings = validate_config(data)
    assert any("group_id" in item for item in errors)
    assert any("[1] 必须是对象" in item for item in errors)
    assert any("user_id 必须是数组" in item for item in errors)


def test_forward_image_dedup_validation():
    data = _base()
    data["forward_image_dedup"] = {
        "db_path": "forward_image.db",
        "keep_days": 30,
        "image_keep_days": 2,
    }
    errors, _ = validate_config(data)
    assert not any("forward_image_dedup" in item for item in errors)

    data["forward_image_dedup"] = {
        "enable": "yes",
        "db_path": 5,
        "keep_days": -1,
        "image_keep_days": True,
    }
    errors, _ = validate_config(data)
    assert any("forward_image_dedup.enable" in item for item in errors)
    assert any("forward_image_dedup.db_path" in item for item in errors)
    assert any("forward_image_dedup.keep_days" in item for item in errors)
    assert any("forward_image_dedup.image_keep_days" in item for item in errors)


def test_relations_section_is_known_and_validated():
    data = _base()
    data["relations"] = {
        "enable": "yes",
        "data_dir": 5,
        "max_people": 0,
        "auto_extract_min_seconds": -1,
    }
    errors, warnings = validate_config(data)

    assert any("relations 必须是对象" in item or "relations.enable" in item for item in errors)
    assert any("relations.data_dir" in item for item in errors)
    assert any("relations.max_people" in item for item in errors)
    assert any("relations.auto_extract_min_seconds" in item for item in errors)
    assert not any("未知参数段 'relations'" in item for item in warnings)


def test_persona_file_must_be_a_string():
    data = _base()
    data["agent"]["persona_file"] = 123
    errors, _ = validate_config(data)
    assert any("agent.persona_file" in item for item in errors)


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


def test_export_section_is_known_and_validated():
    """``export.auto_target_group_ids``：已知段不报警告，类型错要报错。"""
    data = _base()
    data["export"] = {"auto_target_group_ids": [20003, 20002]}
    errors, warnings = validate_config(data)
    assert errors == []
    assert not any("未知参数段 'export'" in item for item in warnings)

    broken = _base()
    broken["export"] = {"auto_target_group_ids": 20003}
    errors, _ = validate_config(broken)
    assert any("export.auto_target_group_ids 必须是数组" in item for item in errors)
