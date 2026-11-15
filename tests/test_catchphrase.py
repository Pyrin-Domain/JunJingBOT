import json
from types import SimpleNamespace

from agent.catchphrase import (
    Catchphrase,
    catchphrase_hint,
    catalog_lines,
    load_catchphrases,
    parse_catchphrases,
    render_catalog,
)
from agent.prompts import build_system_prompt


class _Settings:
    def __init__(self, root, agent=None):
        self.paths = SimpleNamespace(root=root)
        self._agent = agent or {}

    def section(self, name):
        return self._agent if name == "agent" else {}


def test_parse_catchphrases_context_is_text_describe_is_usage():
    entries = parse_catchphrases(
        [
            {
                "context": "那当然啦~",
                "describe": "被夸可爱、被夸厉害时说；表达开心又有点得意",
            },
            {"phrase": "你猜", "description": "被问是不是机器人时用"},
            "只写正文也可以",
            {"context": "那当然啦~", "describe": "被夸可爱、被夸厉害时说；表达开心又有点得意"},
            {"context": "", "describe": "只有说明、没有正文"},
            {"context": "没有说明", "describe": ""},
            123,
        ]
    )
    assert [entry.content for entry in entries] == [
        "那当然啦~",
        "你猜",
        "只写正文也可以",
        "只有说明、没有正文",
        "没有说明",
    ]
    assert entries[0].describe.startswith("被夸可爱")
    assert entries[1].describe == "被问是不是机器人时用"
    assert entries[-1].describe == ""


def test_parse_catchphrases_accepts_wrapper_and_mapping():
    wrapped = parse_catchphrases(
        {"catchphrases": [{"context": "好耶", "describe": "开心时"}]}
    )
    assert wrapped == [Catchphrase("好耶", "开心时")]

    mapped = parse_catchphrases({"好耶": "开心时"})
    assert mapped == [Catchphrase("好耶", "开心时")]


def test_catalog_lines_and_limit():
    entries = [Catchphrase("A", "a"), Catchphrase("B", "b")]
    assert catalog_lines(entries) == [
        "- 口头禅：「A」｜说明：a",
        "- 口头禅：「B」｜说明：b",
    ]
    assert catalog_lines(entries, 1) == ["- 口头禅：「A」｜说明：a"]
    assert "暂无配置" in render_catalog([])


def test_catchphrase_triggers_come_from_quoted_examples():
    entry = Catchphrase(
        "喵？",
        "不知道回什么但还想听下去时用。比如对方问「在吗？」或者只是喊你「君景！」",
    )
    assert entry.triggers == ("在吗？", "君景！")
    assert Catchphrase("怪哦", "遇到奇怪的东西时用").triggers == ()


def test_catchphrase_hint_matches_quoted_trigger_and_ignores_cq_code():
    entries = [
        Catchphrase(
            "喵？",
            "不知道回什么但还想听下去时用。比如对方问「在吗？」或者只是喊你「君景！」",
        )
    ]
    hint = catchphrase_hint("[CQ:at,qq=3122793026] 君景！", entries)
    assert "喵？" in hint
    assert "君景！" in hint
    assert catchphrase_hint("今天吃什么", entries) == ""


def test_load_catchphrases_missing_or_bad_json(tmp_path):
    assert load_catchphrases(tmp_path / "missing.json") == []

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_catchphrases(bad) == []


def test_build_system_prompt_injects_catchphrase_catalog(tmp_path):
    root = tmp_path
    (root / "agent").mkdir()
    (root / "agent" / "catchphrases.json").write_text(
        json.dumps(
            [
                {
                    "context": "那当然啦~",
                    "describe": "被夸可爱、被夸厉害时说；表达开心又有点得意",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = _Settings(
        root, {"catchphrase_file": "agent/catchphrases.json"}
    )

    prompt = build_system_prompt(settings=settings)

    assert "口头禅：「那当然啦~」｜说明：被夸可爱、被夸厉害时说" in prompt
    assert "视为命中" in prompt
    assert "口头禅可以更短" in prompt
    assert "__CATCHPHRASE_CATALOG__" not in prompt


def test_build_system_prompt_can_disable_catchphrases(tmp_path):
    settings = _Settings(tmp_path, {"catchphrase_file": ""})
    prompt = build_system_prompt(settings=settings)
    assert "暂无配置" in prompt
    assert "__CATCHPHRASE_CATALOG__" not in prompt
