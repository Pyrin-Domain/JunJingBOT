"""后端档案（NapCat / SnowLuma）与连接配置解析。"""

from config.backends import (
    BACKENDS,
    DEFAULT_BACKEND,
    NAPCAT,
    SNOWLUMA,
    backend_label,
    backend_options,
    get_backend,
    normalize_backend,
)
from config.connection import ConnectionSettings
from config.defaults import default_connection, default_sender_connection


def test_registry_contains_napcat_and_snowluma():
    assert set(BACKENDS) == {"napcat", "snowluma"}
    assert DEFAULT_BACKEND == "napcat"
    assert NAPCAT.history_anchor == "message_seq"
    assert SNOWLUMA.history_anchor == "message_id"


def test_normalize_backend_accepts_aliases():
    assert normalize_backend("napcat") == "napcat"
    assert normalize_backend("NapCatQQ") == "napcat"
    assert normalize_backend("snowluma") == "snowluma"
    # 用户常见的拼写错误 / 简写
    assert normalize_backend("snowluna") == "snowluma"
    assert normalize_backend("snow-luma") == "snowluma"
    assert normalize_backend("snow_luma") == "snowluma"
    assert normalize_backend("snowluma-onebot") == "snowluma"


def test_normalize_backend_unknown_falls_back_or_returns_empty():
    assert normalize_backend("nope") == "napcat"
    assert normalize_backend("nope", default="") == ""
    assert normalize_backend(None) == "napcat"
    assert normalize_backend(None, default="") == ""


def test_backend_profile_helpers():
    assert get_backend("snowluma") is SNOWLUMA
    assert get_backend("unknown") is NAPCAT
    assert backend_label("snowluna") == "SnowLuma"
    options = backend_options()
    assert [item["id"] for item in options] == ["napcat", "snowluma"]
    snow = options[1]
    assert snow["history_anchor"] == "message_id"
    assert snow["defaults"]["ws_port"] == 3001
    assert snow["defaults"]["sender_port"] == 3000


def test_connection_settings_reads_backend_aliases():
    for key in ("backend", "protocol", "adapter"):
        config = ConnectionSettings({key: "snowluna", "type": "ws_re"})
        assert config.backend == "snowluma"
        assert config.backend_label == "SnowLuma"
        assert config.history_anchor == "message_id"
        assert config.to_dict()["backend"] == "snowluma"


def test_connection_settings_inherits_default_backend():
    config = ConnectionSettings({"type": "ws_re"}, default_backend="snowluma")
    assert config.backend == "snowluma"
    assert config.history_anchor == "message_id"

    # 显式写 napcat 时不会被默认值覆盖
    explicit = ConnectionSettings(
        {"type": "ws_re", "backend": "napcat"}, default_backend="snowluma"
    )
    assert explicit.backend == "napcat"


def test_default_connection_uses_backend_profile():
    napcat = default_connection()
    assert napcat["backend"] == "napcat"
    assert napcat["ws_port"] == 3001
    assert napcat["ws_reverse_port"] == 8085

    snow = default_connection("snowluma")
    assert snow["backend"] == "snowluma"
    assert snow["ws_port"] == 3001
    assert snow["ws_reverse_port"] == 8085

    sender = default_sender_connection("snowluma")
    assert sender["backend"] == "snowluma"
    assert sender["ws_port"] == 3000
