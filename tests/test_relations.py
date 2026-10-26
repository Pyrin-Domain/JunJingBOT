"""关系档案：标签、画像、会过期的近期状态、长期记忆、自动整理与工具。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from agent.prompts import (
    FALLBACK_CORE,
    FALLBACK_PERSONA,
    build_persona_parts,
    build_system_prompt,
)
from agent.relations import RelationStore
from agent.tools.registry import _current_context_var
from agent.tools.relations import build_relation_tools
from core.jsonstore import read_json
from messaging.models import ChatContext, ChatMessage, Segment, Sender


class _Settings:
    """最小的 BotSettings 替身；路径与 section 的行为和真实配置一致。"""

    def __init__(self, root: Path, *, masters=("10001",), **relations):
        self.paths = SimpleNamespace(root=Path(root), data_dir=Path(root) / "data")
        self.master_qq = list(masters)
        self._sections = {"agent": {}, "relations": dict(relations)}

    def section(self, name: str) -> dict:
        return self._sections.get(name, {})


def _store(tmp_path, **kwargs) -> RelationStore:
    settings = _Settings(tmp_path, **kwargs)
    return RelationStore.from_settings(settings)


def _ctx(
    qq: str = "20001",
    name: str = "小明",
    text: str = "在吗",
    *,
    group_id: str = "20009",
    group_name: str = "测试群",
    self_id: str = "30000",
    masters=(),
    history=(),
    message_id: str = "m1",
) -> ChatContext:
    current = ChatMessage(
        message_id=message_id,
        sender=Sender(qq=qq, name=name),
        segments=[Segment(type="text", text=text)],
    )
    return ChatContext(
        chat_type="group",
        group_id=group_id,
        group_name=group_name,
        self_id=self_id,
        master_ids=list(masters),
        history=list(history),
        current=current,
        now=datetime.now(),
    )


def _labels(profile: dict) -> list[str]:
    return [item["text"] for item in profile.get("labels") or []]


# ── 主人 / 标签 ──────────────────────────────────────────────
def test_master_label_is_seeded_and_persisted(tmp_path):
    store = _store(tmp_path)
    store.observe_context(_ctx(qq="20001"))

    profile = store.user("10001")
    assert _labels(profile) == ["主人"]
    assert profile["labels"][0]["source"] == "seed"
    saved = read_json(tmp_path / "data" / "relations" / "users" / "10001.json")
    assert saved["labels"][0]["text"] == "主人"


def test_manual_json_with_only_description_is_still_loaded(tmp_path):
    root = tmp_path / "data" / "relations" / "users"
    root.mkdir(parents=True)
    (root / "20002.json").write_text(
        json.dumps({"qq": "20002", "description": "只会写描述的人"}, ensure_ascii=False),
        encoding="utf-8",
    )

    store = _store(tmp_path)
    profile = store.user("20002")
    assert profile["description"] == "只会写描述的人"
    assert "只会写描述的人" in store.render(_ctx(qq="20002"))


def test_manual_labels_are_protected_from_auto_removal(tmp_path):
    store = _store(tmp_path)
    store.update_person("20001", label_add="朋友", source="manual")
    store.update_person("20001", label_add="同学", source="auto")

    # 自动整理只能删掉自己加的标签
    store.update_person("20001", label_remove="朋友", source="auto")
    assert _labels(store.user("20001")) == ["朋友", "同学"]

    store.update_person("20001", label_remove="同学", source="auto")
    assert _labels(store.user("20001")) == ["朋友"]


def test_labels_are_capped(tmp_path):
    store = _store(tmp_path, max_labels=2)
    for label in ("甲", "乙", "丙"):
        store.update_person("20001", label_add=label)
    assert _labels(store.user("20001")) == ["乙", "丙"]


def test_long_or_multiline_labels_are_rejected(tmp_path):
    store = _store(tmp_path)
    store.update_person("20001", label_add="这是一整句不该成为标签的话")
    assert _labels(store.user("20001")) == []

    # 多行输入按“多个标签”处理，每一段仍然必须够短
    store.update_person("20001", label_add="换\n行")
    assert _labels(store.user("20001")) == ["换", "行"]


# ── 近期状态：必须过期 ──────────────────────────────────────
def test_state_renders_then_expires(tmp_path):
    store = _store(tmp_path)
    store.update_person("20001", state="最近很难过", state_hours=2)
    ctx = _ctx(qq="20001")

    rendered = store.render(ctx)
    assert "最近很难过" in rendered
    assert "近期状态" in rendered

    profile = store.user("20001")
    profile["states"][0]["expires_at"] = (
        datetime.now() - timedelta(minutes=1)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    rendered = store.render(ctx)
    assert "最近很难过" not in rendered
    assert "近期状态" not in rendered


def test_state_hours_cannot_exceed_max(tmp_path):
    store = _store(tmp_path, state_ttl_hours=72, state_max_hours=168)
    store.update_person("20001", state="考试周", state_hours=999)

    state = store.user("20001")["states"][0]
    at = datetime.strptime(state["at"], "%Y-%m-%dT%H:%M:%S")
    expires = datetime.strptime(state["expires_at"], "%Y-%m-%dT%H:%M:%S")
    assert expires - at <= timedelta(hours=168, seconds=1)


def test_new_state_with_same_text_replaces_old_one(tmp_path):
    store = _store(tmp_path)
    store.update_person("20001", state="在生气")
    store.update_person("20001", state="在生气")
    assert len(store.user("20001")["states"]) == 1


def test_self_status_is_rendered_and_expires(tmp_path):
    store = _store(tmp_path)
    store.record_self_status({"心情": "开心", "动作": "摇尾巴"}, "g1")

    rendered = store.render(_ctx())
    assert "我现在的状态" in rendered
    assert "心情：开心" in rendered
    assert (tmp_path / "data" / "relations" / "self.json").is_file()

    store._self_status["expires_at"] = (
        datetime.now() - timedelta(hours=1)
    ).strftime("%Y-%m-%dT%H:%M:%S")
    assert "心情：开心" not in store.render(_ctx())


# ── 长期记忆：按重要度 + 时间裁剪 ───────────────────────────
def test_memories_are_trimmed_by_importance(tmp_path):
    store = _store(tmp_path, max_memories=3)
    store.update_person("20001", memory="低优先级一", importance=1)
    store.update_person("20001", memory="很重要的雷区", importance=3)
    store.update_person("20001", memory="普通喜好", importance=2)
    store.update_person("20001", memory="低优先级二", importance=1)

    texts = [item["text"] for item in store.user("20001")["memories"]]
    assert len(texts) == 3
    assert "很重要的雷区" in texts
    assert "普通喜好" in texts
    # 两条 importance=1 只保留较新的一条
    assert "低优先级二" in texts
    assert "低优先级一" not in texts


def test_duplicate_memory_is_not_added_twice(tmp_path):
    store = _store(tmp_path)
    store.update_person("20001", memory="不喜欢被叫全名", importance=2)
    store.update_person("20001", memory="不喜欢被叫全名", importance=3)
    memories = store.user("20001")["memories"]
    assert len(memories) == 1
    assert memories[0]["importance"] == 3


# ── 自动整理 ────────────────────────────────────────────────
def test_apply_extraction_only_updates_known_people(tmp_path):
    store = _store(tmp_path)
    ctx = _ctx(qq="20001")
    store.observe_context(ctx)

    raw = json.dumps(
        {
            "people": [
                {
                    "qq": "20001",
                    "labels_add": ["同学"],
                    "state": "刚考完试",
                    "memory": "在准备考研",
                    "importance": 3,
                    "confidence": 0.9,
                },
                {"qq": "99999", "labels_add": ["陌生人"], "confidence": 0.99},
                {"qq": "20001", "labels_add": ["低置信"], "confidence": 0.2},
            ],
            "group": {"topics": ["考试", "复习"]},
        },
        ensure_ascii=False,
    )
    result = store.apply_extraction(raw, ctx)

    assert result == {"ok": True, "updated": ["20001"]}
    profile = store.user("20001")
    assert "同学" in _labels(profile)
    assert "陌生人" not in _labels(profile)
    assert "低置信" not in _labels(profile)
    assert profile["states"][0]["text"] == "刚考完试"
    assert store.group("20009")["topics"] == ["考试", "复习"]


def test_apply_extraction_ignores_broken_json(tmp_path):
    store = _store(tmp_path)
    assert store.apply_extraction("不是 JSON", _ctx()) == {
        "ok": False,
        "reason": "invalid_json",
    }


def test_apply_extraction_cannot_remove_master_label(tmp_path):
    store = _store(tmp_path)
    ctx = _ctx(qq="10001", name="主人")
    store.observe_context(ctx)

    raw = json.dumps(
        {
            "people": [
                {
                    "qq": "10001",
                    "labels_remove": ["主人"],
                    "confidence": 0.99,
                }
            ]
        },
        ensure_ascii=False,
    )
    store.apply_extraction(raw, ctx)
    assert "主人" in _labels(store.user("10001"))


def test_auto_extract_respects_counter_and_enable(tmp_path):
    store = _store(tmp_path, auto_extract=True, auto_every=2, auto_extract_min_seconds=0)
    assert store.should_extract("g1", direct=True) is False
    assert store.should_extract("g1", direct=True) is True
    # 刚跑过，计数清零，重新累计
    assert store.should_extract("g1", direct=True) is False

    off = _store(tmp_path, auto_extract=False)
    assert off.should_extract("g1", direct=True) is False


def test_build_extract_prompt_contains_recent_sender_and_reply(tmp_path):
    store = _store(tmp_path)
    ctx = _ctx(qq="20001", name="小明", text="我最近在准备考试")
    store.observe_context(ctx)

    prompt = store.build_extract_prompt(ctx, ["加油呀"])
    assert "小明" in prompt
    assert "我最近在准备考试" in prompt
    assert "加油呀" in prompt
    assert "只从上面的记录里" in prompt


# ── 渲染 ────────────────────────────────────────────────────
def test_render_puts_current_sender_first_and_shows_portrait(tmp_path):
    store = _store(tmp_path)
    store.update_person(
        "20001",
        name="小明",
        portrait="说话很直，但很照顾人",
        relation="认识很久的朋友",
        memory="下个月过生日",
        importance=2,
    )
    store.update_person("20002", name="小红")

    rendered = store.render(_ctx(qq="20001", name="小明"))
    assert rendered.startswith("<relations>")
    assert "小明(20001)" in rendered
    assert "画像：说话很直，但很照顾人" in rendered
    assert "关系：认识很久的朋友" in rendered
    assert "重要记忆" in rendered
    assert "下个月过生日" in rendered
    # 没在当前上下文出现的人不注入
    assert "小红" not in rendered


def test_render_respects_people_budget(tmp_path):
    store = _store(tmp_path, max_people=1)
    store.update_person("20001", name="小明", label_add="朋友")
    store.update_person("20002", name="小红", label_add="朋友")

    history = [
        ChatMessage(
            message_id="h1",
            sender=Sender(qq="20002", name="小红"),
            segments=[Segment(type="text", text="你好")],
        )
    ]
    rendered = store.render(_ctx(qq="20001", history=history))
    assert "小明(20001)" in rendered
    assert "小红(20002)" not in rendered


def test_disabled_store_is_a_noop(tmp_path):
    store = RelationStore(None, enabled=False)
    assert store.user("20001") == {}
    assert store.render(_ctx()) == ""
    assert store.update_person("20001", label_add="朋友")["ok"] is False


# ── 工具 ────────────────────────────────────────────────────
def test_update_person_tool_writes_profile(tmp_path):
    store = _store(tmp_path)
    agent = SimpleNamespace(relations=store)
    tools = {item.name: item for item in build_relation_tools(agent)}

    result = asyncio.run(
        tools["update_person"].ainvoke(
            {
                "qq": "20001",
                "label_add": "朋友",
                "state": "今天很开心",
                "memory": "喜欢猫",
                "importance": 3,
            }
        )
    )
    assert result["ok"] is True
    profile = store.user("20001")
    assert "朋友" in _labels(profile)
    assert profile["states"][0]["text"] == "今天很开心"
    assert profile["memories"][0]["text"] == "喜欢猫"


def test_update_group_tool_requires_context_and_writes_group(tmp_path):
    store = _store(tmp_path)
    agent = SimpleNamespace(relations=store)
    tools = {item.name: item for item in build_relation_tools(agent)}

    _current_context_var.set(None)
    assert asyncio.run(tools["update_group"].ainvoke({"topics": "考试"}))["ok"] is False

    _current_context_var.set(_ctx(group_id="20009"))
    result = asyncio.run(
        tools["update_group"].ainvoke({"topics": "考试、复习", "notes": "晚上比较活跃"})
    )
    assert result["ok"] is True
    group = store.group("20009")
    assert group["topics"] == ["考试", "复习"]
    assert group["notes"] == "晚上比较活跃"


def test_tool_builds_store_from_settings_when_missing(tmp_path):
    settings = _Settings(tmp_path)
    agent = SimpleNamespace(settings=settings)
    tools = {item.name: item for item in build_relation_tools(agent)}

    asyncio.run(tools["update_person"].ainvoke({"qq": "20001", "label_add": "朋友"}))
    assert agent.relations.enabled is True
    assert "朋友" in _labels(agent.relations.user("20001"))


# ── 提示词人设槽位 ──────────────────────────────────────────
def test_system_prompt_has_no_unresolved_slots(tmp_path):
    settings = _Settings(tmp_path)
    prompt = build_system_prompt(settings=settings)
    assert "{persona}" not in prompt
    assert "{core_behavior}" not in prompt
    assert "__MEME_CATALOG__" not in prompt
    assert FALLBACK_PERSONA.splitlines()[0] in prompt
    assert FALLBACK_CORE.splitlines()[0] in prompt


def test_persona_files_override_fallbacks(tmp_path):
    (tmp_path / "persona.md").write_text("## 测试角色\n\n我是测试角色。", encoding="utf-8")
    (tmp_path / "core.md").write_text("## 测试行为\n\n- 永远说真话。", encoding="utf-8")
    settings = _Settings(tmp_path)
    settings._sections["agent"] = {
        "persona_file": "persona.md",
        "persona_core_file": "core.md",
    }

    persona, core = build_persona_parts(settings)
    assert persona == "## 测试角色\n\n我是测试角色。"
    assert core == "## 测试行为\n\n- 永远说真话。"
    prompt = build_system_prompt(settings=settings)
    assert "我是测试角色。" in prompt
    assert "永远说真话。" in prompt
