"""消息主流程。

这里只做“按顺序调用”，不实现具体业务：

    断点 → 插话计数 → 临时历史 → 搬运 → AI（@ / 敏感期）→ 关键词

顺序与旧 ``BOTMain.process_message`` 完全一致。改顺序前先读
``docs/MIGRATION.md`` 第 2.5 节：AI 和关键词互相调换会导致同一条消息双发。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable

from config.settings import BotSettings
from core.logging import get_logger
from messaging.attention import AMBIENT, Attention, detect_attention_of

log = get_logger("bot.pipeline")


class MessagePipeline:
    def __init__(
        self,
        *,
        mp,
        settings: BotSettings,
        forward,
        interject,
        commands=None,
        agent_getter: Callable[[], Awaitable[Any]] | None = None,
        keyword_builder: Callable[[], list[dict]] | None = None,
        campus_getter: Callable[[], Any] | None = None,
    ):
        self.mp = mp
        self.settings = settings
        self.forward = forward
        self.interject = interject
        self.commands = commands
        self._agent_getter = agent_getter
        self._keyword_builder = keyword_builder
        self._campus_getter = campus_getter
        self.emoji_list: list[dict] = []

    # ════════════════════ 入口 ════════════════════
    async def process_message(self, event: dict, is_recover: bool = False) -> None:
        """处理一条 NapCat 消息事件。"""
        if not event:
            return
        # 旧版由监听层保证只有 message 事件进来；这里不额外检查 post_type，
        # 断点回放 / 测试传入的事件可能没有该字段。
        if event.get("post_type") not in (None, "message"):
            return

        # 每条群消息立即记录断点；恢复的消息不参与插话计数
        attention = self._attention(event)
        if event.get("message_type") == "group":
            self.mp.check_point[str(event.get("group_id"))] = event
            if not is_recover:
                self.interject.note_message(event, attention=attention)

        # 临时会话历史：每条新消息都记一笔（Bot 发言后的敏感期 / 续聊靠它）
        if not is_recover:
            self.mp.remember_thread_message(event, master_ids=self.settings.master_qq)

        message_id = event["message_id"]
        user_id = event["user_id"]
        master_qq = self.settings.master_qq

        self._emoji_function(event)

        # 自动搬运：命中就结束，不再走 AI / 关键词
        if self.settings.auto_forward_enabled and await self.forward.auto_forward(
            event=event,
            is_recover=is_recover,
            **self.settings.auto_forward_kwargs,
        ):
            return

        ated_user = await self.mp.is_AT(event=event, user_id=[self.mp.user_id])

        hot_seconds = self._hot_seconds()
        replying_to_bot = (
            not ated_user
            and not is_recover
            and self.mp.thread_is_hot(
                self.mp.thread_id_of(event), hot_seconds=hot_seconds
            )
            and self.mp.replies_to_bot(event)
        )

        # 「喊名字但没 @」也是召唤：这类消息以前既不进 AI、插话器又当「没人叫我」
        # 让开，于是一整句话石沉大海。只对群聊生效，私聊入口保持原样。
        summoned = attention.is_summon and event.get("message_type") == "group"
        if summoned and not ated_user:
            log.info(
                "[召唤] 群 %s 直呼（%s），转 AI 回复",
                event.get("group_id"),
                attention.evidence,
            )

        if (ated_user or replying_to_bot or summoned) and not is_recover:
            await self._handle_ai(
                event=event,
                message_id=message_id,
                user_id=user_id,
                master_qq=master_qq,
                ated_user=ated_user,
                hot_seconds=hot_seconds,
                is_recover=is_recover,
            )
            return

        await self._match_keywords(event, is_recover)

    # ════════════════════ AI 分支 ════════════════════
    async def _handle_ai(
        self,
        *,
        event: dict,
        message_id,
        user_id,
        master_qq: list[str],
        ated_user,
        hot_seconds: float,
        is_recover: bool,
    ) -> None:
        clean_text = self.mp.get_clean_context(event, True)

        # 旧版“历史”命令：保留原有入口（旧实现调用的方法本身不存在，见 TODO）
        pattern = self.settings.prompt("history_command_regex")
        if pattern and re.match(pattern, clean_text):
            await self.history_solution(event)
            return

        thread_id = (
            f"group_{event['group_id']}"
            if event["message_type"] == "group"
            else f"private_{user_id}"
        )
        template_key = (
            "group_context"
            if event["message_type"] == "group"
            else "private_context"
        )
        history = [] if not (self.mp.extraconfig or {}).get("recent") else None

        context = await self.mp.build_chat_context(
            event=event,
            history=history,
            history_count=10,
            note=self.settings.prompt(template_key),
            note_values={
                "group_id": event.get("group_id", ""),
                "user_id": user_id,
                "message_id": message_id,
                "ated_user": (
                    "、".join(ated_user)
                    if isinstance(ated_user, (list, tuple))
                    else str(ated_user or "")
                ),
                "self_id": self.mp.user_id,
                "master_id": ", ".join(master_qq),
            },
            master_ids=master_qq,
            aliases=self._aliases(),
        )

        agent = await self._get_agent()
        if agent is None:
            # AI 被关掉 / 初始化失败 → 没有调用模型，退回关键词
            await self._match_keywords(event, is_recover)
            return

        reply = await agent.chat(event=event, context=context, thread_id=thread_id)
        log.debug("[AI] 回复: %s", reply)

        # Bot 刚在这个群参与过对话 → 插话计数归零并进入冷却
        if event["message_type"] == "group":
            self.interject.note_bot_spoke(event["group_id"])

        if isinstance(reply, str):
            reply = [reply] if reply else []
        if not reply:
            return

        if "__NETWORK__" in reply:
            await self.mp.send_message(
                event=event, message=self.settings.reply("network_upgrade")
            )
            await self._route_to_campus_assistant(
                event,
                clean_text,
                iclhistory=context.render_history_only() or None,
            )
            return

        if all(item == "__SILENT__" for item in reply):
            return

        for item in reply:
            if not item or "已成功发" in item:
                continue
            result = await self.mp.send_message(event, item)
            self.mp.remember_sent_message(
                event, item, result, hot_seconds=hot_seconds
            )

    async def _get_agent(self):
        if self._agent_getter is None:
            return None
        return await self._agent_getter()

    # ════════════════════ 关键词 ════════════════════
    async def _match_keywords(self, event: dict, is_recover: bool = False) -> bool:
        if is_recover:
            return False
        patterns = self.keyword_patterns()
        if await self.mp.pattern_match(event=event, pattern_config=patterns):
            log.info("[关键词] 匹配成功")
            return True
        return False

    def keyword_patterns(self) -> list[dict]:
        if self._keyword_builder is None:
            return []
        try:
            return self._keyword_builder()
        except Exception as exc:  # noqa: BLE001 - 配置写错不能阻断消息
            log.warning("[关键词] 构建规则失败: %s", exc)
            return []

    # ════════════════════ 历史命令（保留旧行为） ════════════════════
    async def history_solution(self, event: dict) -> None:
        message_id = event["message_id"]
        getter = getattr(self, "get_message_history", None)
        if getter is None:
            # 旧 BOTMain 在这里调用不存在的方法，会抛 AttributeError。
            # 迁移时先不让它把消息任务打崩，行为标记为 TODO。
            log.warning("[历史] get_message_history 未实现（保留旧 TODO）")
            return
        history = await getter(message_id)
        log.info("%s", history)
        await self.mp.send_message(event, history["context"])

    # ════════════════════ 校园网助手（保留入口） ════════════════════
    async def _route_to_campus_assistant(
        self, event: dict, clean_text: str, iclhistory=None
    ) -> None:
        campus = self._campus_getter() if self._campus_getter else None
        if campus is None:
            log.warning("CampusAssistant 尚未加载完成，无法转交校园网问题")
            return
        log.info("[转交校园网助手] 用户提问: %s", clean_text)
        await self.mp.ensure_connected()
        reply = await campus.chat(
            message=clean_text,
            icl=(
                iclhistory
                if iclhistory is not None
                else await self.mp.get_history_msg(event)
            ),
            group_id=event.get("group_id"),
        )
        if not reply:
            return
        if "[我不能回答]" in reply:
            return
        reply = reply.replace("[我能回答]", "").strip()
        await self.mp.send_message(event, reply)

    # ════════════════════ 小工具 ════════════════════
    def _aliases(self) -> list[str]:
        """Bot 的别名（配置 ``interject.aliases``），用于识别「喊名字没 @」。"""
        value = self.settings.get("interject.aliases", [])
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item or "").strip()]
        return []

    def _attention(self, event: dict) -> Attention:
        """这条消息在叫它、在说它，还是跟它无关。判定失败按 ambient 处理。"""
        is_reply = getattr(self.mp, "is_reply", None)
        replies_to_bot = getattr(self.mp, "replies_to_bot", None)
        replies_to_me = False
        try:
            if callable(is_reply) and callable(replies_to_bot):
                replies_to_me = bool(is_reply(event)) and bool(replies_to_bot(event))
        except Exception:  # noqa: BLE001 - 判定失败不能阻断消息
            replies_to_me = False
        try:
            return detect_attention_of(
                event,
                self_id=self.mp.user_id,
                replies_to_me=replies_to_me,
                aliases=self._aliases(),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[注意力] 判定失败，按 ambient 处理: %s", exc)
            return Attention(AMBIENT)

    def _hot_seconds(self) -> float:
        value = self.settings.get("interject.hot_minutes", 10)
        try:
            return float(value) * 60
        except (TypeError, ValueError):
            return 600.0

    def _emoji_function(self, event: dict) -> None:
        """旧版彩蛋：收集指定群的 emoji 包信息，写入 data/emojilist.json。"""
        target = str(self.settings.get("emoji.collect_group_id", 20001))
        if str(event.get("group_id", "")) != target:
            return
        for item in event.get("message", []):
            if item.get("type", "") != "image":
                return
            data = item.get("data", {}) or {}
            if not data.get("emoji_package_id"):
                continue
            self.emoji_list.append(
                {
                    "emoji_package_id": data.get("emoji_package_id"),
                    "emoji_id": data.get("emoji_id"),
                    "key": data.get("key"),
                    "summary": data.get("summary"),
                }
            )
            try:
                path = self.settings.paths.data_file("emojilist.json")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(self.emoji_list, indent=4, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as exc:
                log.warning("[emoji] 写入失败: %s", exc)


__all__ = ["MessagePipeline"]
