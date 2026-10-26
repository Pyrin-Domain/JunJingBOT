"""关系档案维护工具：``update_person`` / ``update_group``。

工具只做「把模型明确说出来的东西写进去」，判断和过期策略都在
:mod:`agent.relations` 里：

* ``label_add`` / ``label_remove``：关系标签（主人、朋友、群主……），可变；
* ``state`` + ``state_hours``：近期状态，默认 72 小时自动过期；
* ``memory`` / ``importance``：重要记忆，长期保留，按重要度裁剪；
* ``portrait`` / ``relation``：人格画像 / 和君景的关系，变化很慢。

模型的猜测、玩笑、别人的隐私都不应该写进来；提示词里已经写死这条边界，
这里再做一次长度和数量限制，避免把整段聊天塞进档案。
"""

from __future__ import annotations

import logging
from typing import Any

from .registry import (
    current_context,
    record_tool_call,
    settings_of,
    tool,
)

logger = logging.getLogger("QQBotAgent.Relations")


def _store(agent: Any) -> Any:
    store = getattr(agent, "relations", None)
    if store is not None:
        return store
    # 兼容只设置了 settings 的旧测试 / 外部构造方式
    settings = settings_of(agent)
    if settings is None:
        return None
    try:
        from agent.relations import RelationStore

        store = RelationStore.from_settings(settings)
        agent.relations = store
        return store
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[关系] 初始化失败: {exc}")
        return None


def _qq_of(value: Any) -> str:
    text = str(value or "").strip()
    return text


def build_relation_tools(agent: Any) -> list:
    """关系档案工具；``agent.relations`` 不存在时工具仍可构建，只是返回不可用。"""

    @tool
    async def update_person(
        qq,
        label_add="",
        label_remove="",
        state="",
        state_hours=72,
        memory="",
        importance=2,
        portrait="",
        relation="",
    ) -> dict:
        """维护某个人的关系档案（标签 / 人格画像 / 近期状态 / 重要记忆）。
        参数 qq:str 对方的 QQ 号（从上下文每行的括号里取）。
        label_add:str 要新增的关系标签，多个用顿号分隔；label_remove:str 要删掉的标签。
        state:str 最近的状态/情绪（只写这一轮明确表现出来的）；state_hours:int 状态保留多少小时，默认 72。
        memory:str 值得长期记住的事（称呼、雷区、长期喜好、重要经历）；importance:int 1~3，默认 2。
        portrait:str 补充人格画像（长期性格）；relation:str 和君景的关系描述。
        只写对方明确说过的稳定信息，不要记猜测、玩笑或隐私；没有要记的就不要调用。
        """
        qq_text = _qq_of(qq)
        logger.info(
            f"[工具调用] update_person(qq={qq_text!r}, label_add={label_add!r}, "
            f"state={str(state)[:30]!r}, memory={str(memory)[:30]!r})"
        )
        store = _store(agent)
        if store is None:
            record_tool_call("update_person", {"qq": qq_text}, error="关系档案不可用")
            return {"ok": False, "reason": "关系档案不可用"}
        result = store.update_person(
            qq_text,
            label_add=label_add,
            label_remove=label_remove,
            state=state,
            state_hours=state_hours,
            memory=memory,
            importance=importance,
            portrait=portrait,
            relation=relation,
            source="manual",
        )
        logger.info(f"[工具结果] update_person -> {result}")
        record_tool_call(
            "update_person",
            {"qq": qq_text, "label_add": label_add, "memory": memory, "state": state},
            result=result,
        )
        return result

    @tool
    async def update_group(topics="", lore="", notes="", description="") -> dict:
        """维护当前群的长期资料。
        topics:str 常聊话题；lore:str 群梗 / 群内称呼习惯；notes:str 群规、雷区、插话边界；
        description:str 群的气氛和主题。多个条目用顿号或换行分隔。
        只写这个群里确实长期成立的东西；临时话题、别的群的梗不要写。
        """
        context = current_context()
        group_id = _qq_of(getattr(context, "group_id", ""))
        if not group_id:
            record_tool_call("update_group", {}, error="当前不是群聊")
            return {"ok": False, "reason": "当前不是群聊，无法更新群资料"}
        logger.info(
            f"[工具调用] update_group(group={group_id}, topics={str(topics)[:30]!r}, "
            f"notes={str(notes)[:30]!r})"
        )
        store = _store(agent)
        if store is None:
            record_tool_call("update_group", {"group_id": group_id}, error="关系档案不可用")
            return {"ok": False, "reason": "关系档案不可用"}
        result = store.update_group(
            group_id,
            topics=topics,
            lore=lore,
            notes=notes,
            description=description,
            source="manual",
        )
        logger.info(f"[工具结果] update_group -> {result}")
        record_tool_call(
            "update_group",
            {"group_id": group_id, "topics": topics, "notes": notes},
            result=result,
        )
        return result

    return [update_person, update_group]


__all__ = ["build_relation_tools"]
