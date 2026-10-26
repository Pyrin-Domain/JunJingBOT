"""config.path_profiles：预设规则生成、别名、自定义规则叠加。"""

from core.pathmap import PathTranslator

from config.path_profiles import (
    PROFILES,
    get_profile,
    normalize_profile,
    path_profile_options,
)


def test_profile_options_are_stable_and_documented():
    options = path_profile_options()
    assert [item["id"] for item in options] == [
        "same",
        "windows_to_wsl",
        "wsl_to_windows",
        "custom",
    ]
    for item in options:
        assert item["label"]
        assert item["description"]


def test_same_profile_has_no_rules():
    profile = get_profile("same")
    assert profile.rules() == []
    translator = PathTranslator(profile.rules())
    assert translator.to_backend("D:\\Code\\a.png") == "D:\\Code\\a.png"


def test_windows_to_wsl_generates_drive_rules():
    profile = get_profile("windows_to_wsl")
    translator = PathTranslator(profile.rules(mount_root="/mnt"))
    assert translator.to_backend("C:\\a.png") == "/mnt/c/a.png"
    assert translator.to_backend("d:/a.png") == "/mnt/d/a.png"
    assert translator.to_local("/mnt/e/a.png") == "E:\\a.png"


def test_windows_to_wsl_honors_custom_mount_root():
    translator = PathTranslator(get_profile("windows_to_wsl").rules(mount_root="/windrive"))
    assert translator.to_backend("D:\\a.png") == "/windrive/d/a.png"


def test_wsl_to_windows_generates_reverse_drive_rules():
    translator = PathTranslator(get_profile("wsl_to_windows").rules())
    assert translator.to_backend("/mnt/d/a.png") == "D:\\a.png"
    assert translator.to_local("D:\\a.png") == "/mnt/d/a.png"


def test_custom_profile_only_uses_extra_rules():
    profile = get_profile("custom")
    assert profile.rules() == []
    translator = PathTranslator(
        profile.rules(extra=[{"from": "/app/data", "to": "/napcat/data"}])
    )
    assert translator.to_backend("/app/data/a.png") == "/napcat/data/a.png"
    assert translator.to_backend("/app/other/a.png") == "/app/other/a.png"


def test_extra_rules_override_preset_by_longest_prefix():
    profile = get_profile("windows_to_wsl")
    translator = PathTranslator(
        profile.rules(
            mount_root="/mnt",
            extra=[{"from": "D:\\share", "to": "/data/share", "note": "NAS"}],
        )
    )
    assert translator.to_backend("D:\\share\\a.png") == "/data/share/a.png"
    assert translator.to_backend("D:\\Code\\a.png") == "/mnt/d/Code/a.png"
    # 反向也要优先命中更长的自定义规则
    assert translator.to_local("/data/share/a.png") == "D:\\share\\a.png"


def test_profile_aliases_and_unknown_values():
    assert normalize_profile("manual") == "custom"
    assert normalize_profile("WINDOWS-WSL") == "windows_to_wsl"
    assert normalize_profile("wsl-windows") == "wsl_to_windows"
    assert normalize_profile("identity") == "same"
    assert normalize_profile("nope", default="") == ""
    assert get_profile("nope").id == "same"
    assert PROFILES["custom"].id == "custom"
