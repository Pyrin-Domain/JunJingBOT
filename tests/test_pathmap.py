"""core.pathmap：最长前缀、边界、大小写、特殊值、双向翻译。"""

import pytest

from core.pathmap import PathRule, PathTranslator, is_special, looks_windows, match_prefix


def _translator():
    return PathTranslator([
        PathRule("D:\\", "/mnt/d"),
        PathRule("D:\\share", "/data/share", "NAS"),
    ])


def test_longest_prefix_wins():
    translator = _translator()
    assert translator.to_backend("D:\\Code\\a.png") == "/mnt/d/Code/a.png"
    assert translator.to_backend("D:\\share\\a.png") == "/data/share/a.png"


def test_prefix_match_is_boundary_aware():
    translator = PathTranslator([PathRule("/mnt/d", "/data")])
    assert translator.to_backend("/mnt/d/a.png") == "/data/a.png"
    assert translator.to_backend("/mnt/d") == "/data"
    # 不能把 /mnt/database 当成 /mnt/d 下的路径
    assert translator.to_backend("/mnt/database") == "/mnt/database"


def test_windows_source_is_case_insensitive_but_rest_keeps_case():
    translator = PathTranslator([PathRule("D:\\Code", "/mnt/d/Code")])
    assert (
        translator.to_backend("d:\\code\\MiXeD\\A.PNG")
        == "/mnt/d/Code/MiXeD/A.PNG"
    )


def test_forward_and_back_slashes_are_equivalent():
    translator = _translator()
    assert translator.to_backend("D:/Code/a.png") == "/mnt/d/Code/a.png"
    assert translator.to_backend("D:\\Code/a.png") == "/mnt/d/Code/a.png"


def test_special_values_pass_through():
    translator = _translator()
    for value in (
        "https://example.com/a.png",
        "http://example.com/a.png",
        "base64://AAAA",
        "file:///tmp/a.png",
        "data:image/png;base64,AAAA",
        "//server/share/a.png",
    ):
        assert is_special(value)
        assert translator.to_backend(value) == value
        assert translator.explain(value)["special"] is True


def test_reverse_translation_and_roundtrip():
    translator = _translator()
    assert translator.to_local("/mnt/d/Code/a.png") == "D:\\Code\\a.png"
    assert translator.to_local("/data/share/a.png") == "D:\\share\\a.png"

    for value in ("D:\\Code\\a.png", "D:\\share\\a.png"):
        assert translator.to_local(translator.to_backend(value)) == value


def test_explain_reports_rule_and_output():
    result = _translator().explain("D:\\share\\a.png")
    assert result["matched"] is True
    assert result["output"] == "/data/share/a.png"
    assert result["rule"]["from"] == "D:\\share"
    assert result["rule"]["to"] == "/data/share"
    assert result["rule"]["note"] == "NAS"

    missing = _translator().explain("E:\\other\\a.png")
    assert missing["matched"] is False
    assert missing["output"] == "E:\\other\\a.png"


def test_root_prefix_and_unc():
    root = PathTranslator([PathRule("/", "/data")])
    assert root.to_backend("/a/b") == "/data/a/b"

    unc = PathTranslator([PathRule("\\\\server\\share", "/data/share")])
    assert unc.to_backend("\\\\server\\share\\a.png") == "/data/share/a.png"


def test_drive_root_and_mount_root():
    translator = PathTranslator([PathRule("D:\\", "/mnt/d")])
    assert translator.to_backend("D:\\") == "/mnt/d"
    assert translator.to_backend("D:") == "/mnt/d"
    assert translator.to_local("/mnt/d") == "D:\\"


def test_style_only_affects_unmatched_paths():
    assert PathTranslator(style="posix").to_backend("D:\\x") == "D:/x"
    assert PathTranslator(style="windows").to_backend("/a/b") == "\\a\\b"
    assert PathTranslator(style="auto").to_backend("/a/b") == "/a/b"


def test_match_prefix_and_looks_windows():
    assert match_prefix("D:\\Code\\a", "D:\\") == (True, 3)
    assert match_prefix("D:\\Code\\a", "D:\\code") == (True, 8)
    assert match_prefix("D:\\Code\\a", "D:\\nope") == (False, 0)
    assert looks_windows("D:\\a")
    assert looks_windows("\\\\server\\share")
    assert not looks_windows("/mnt/d")


def test_invalid_rules_are_rejected():
    with pytest.raises(ValueError):
        PathRule("", "/mnt/d")
    with pytest.raises(ValueError):
        PathRule("D:\\", "")
    # from_mapping 对缺字段返回 None，方便过滤面板里的半截输入
    assert PathRule.from_mapping({"from": "", "to": "/x"}) is None
    assert PathRule.from_mapping({"from": "D:\\x", "to": "/x"}).to_dict() == {
        "from": "D:\\x",
        "to": "/x",
    }
