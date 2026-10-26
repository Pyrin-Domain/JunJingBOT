"""
InterjectService —— 群聊主动插话

君景不再只在被 @ 时说话：群里聊到一定条数、或者安静了一阵但攒了几条新消息，
就把那段对话扒下来，先让模型判断「现在插一句自不自然」，再决定要不要接话。

每个群一份状态（就是用户提的那个列表）：

    {group_id, count: 新消息数, frequent: 多少条触发判断, timer}

两条触发路径：
1. **攒够条数**：count >= frequent → 立刻判断一次（不必等定时器）
2. **定时器到点**：倒计时结束 → 有新消息就判断，没有新消息就只是重新倒计时

触发后 count 归零；两次插话之间有 cooldown，防止刷屏。
冷却期间的新消息不会直接丢掉：先记下 pending，冷却结束再补一次判断，
避免 Bot 刚回完话、对方马上追问时，一直等到 idle 定时器才处理。
断点恢复（is_recover=True）的消息**不计数**——那些是补处理掉线期间的历史，
让模型对着几小时前的对话插一句没有意义。

配置（bot_config.json 的 interject 段，网页版可改）：
    enable / frequent / extra_history / idle_minutes / cooldown_minutes
    groups（白名单，留空=所有群）/ blacklist_groups
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from messaging.attention import Attention, detect_attention_of
from messaging.interest import read_interests

# nod 用的表情：只在「想表示在场但没内容」时贴一个，不打字
_NOD_EMOJIS = ("424", "76", "66")


def _num(value, default: float, minimum: float = 0.0) -> float:
    """配置里的数字容错：写错了就退回默认值，别让插话功能把主流程带崩。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(minimum, number)


def _running_loop():
    """当前运行中的事件循环；没有就返回 None。

    用 ``get_running_loop`` 而不是 ``asyncio.create_task``：后者在没有循环时
    先建出 coroutine 再抛 RuntimeError，那个 coroutine 永远不会被 await，
    会留下 "was never awaited" 警告。
    """
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


@dataclass
class GroupState:
    """一个群的插话状态。"""

    group_id: str
    count: int = 0                      # 上次判断之后又来了几条新消息
    last_event: dict | None = None      # 最新一条消息（用来扒历史 + 发送）
    task: asyncio.Task | None = None    # 这个群的倒计时任务
    cooldown_task: asyncio.Task | None = None  # 冷却结束后的补检查任务
    pending: bool = False               # 冷却期间有值得补看的新消息
    busy: bool = False                  # 正在判断/生成，别重复触发
    last_trigger_at: float = 0.0        # 上次判断的时间（cooldown 用）
    triggered: int = 0                  # 累计成功插话次数（调试用）
    nodded: int = 0                     # 累计「只贴表情不出声」次数（调试用）
    last_act: str = ""                  # 上次判断出来的动作（调试用）


class InterjectService:
    """按群累计消息 → 触发插话判断。"""

    def __init__(self, *, mp, settings=None, agent_getter=None, config=None):
        """
        :param mp: MessageProcessor（扒历史、发消息）
        :param settings: BotSettings（每次触发都重新读 interject 段，面板改完即生效）；
                         ``config`` 是它的旧名字，保留兼容
        :param agent_getter: 无参函数，返回 QQBotAgent 或 None（没加载好就跳过）
        """
        self.mp = mp
        self.settings = settings if settings is not None else config
        self.config = self.settings        # 旧字段名，兼容别处引用
        self._agent_getter = agent_getter or (lambda: None)
        self._states: dict[str, GroupState] = {}
        self._closed = False

    # ──────────────── 配置 ────────────────
    def _section(self) -> dict:
        """读 interject 段：BotSettings 走 section()，纯 dict 直接取键。"""
        if self.settings is None:
            return {}
        reader = getattr(self.settings, "section", None)
        if callable(reader):
            try:
                value = reader("interject")
            except Exception:
                value = {}
        elif isinstance(self.settings, dict):
            value = self.settings.get("interject", self.settings)
        else:
            value = {}
        return value if isinstance(value, dict) else {}

    def _params(self) -> dict:
        section = self._section() or {}
        groups = section.get("groups") or []
        blacklist = section.get("blacklist_groups") or []
        interests = section.get("interests") or {}
        if not isinstance(interests, dict):
            interests = {}
        return {
            "enable": bool(section.get("enable", True)),
            "frequent": int(_num(section.get("frequent"), 10, 2)),
            "extra": int(_num(section.get("extra_history"), 5, 0)),
            "idle": _num(section.get("idle_minutes"), 30, 1) * 60,
            "cooldown": _num(section.get("cooldown_minutes"), 5, 0) * 60,
            # 敏感期：Bot 刚说过话之后，门槛降下来，别等攒够 10 条
            "hot_minutes": _num(section.get("hot_minutes"), 10, 1),
            "hot_frequent": int(_num(section.get("hot_frequent"), 3, 1)),
            "hot_idle": _num(section.get("hot_idle_minutes"), 5, 1) * 60,
            "groups": [str(g) for g in groups] if isinstance(groups, list) else [],
            "blacklist": [str(g) for g in blacklist] if isinstance(blacklist, list) else [],
            "interest_high": interests.get("high") or [],
            "interest_low": interests.get("low") or [],
        }

    @property
    def enabled(self) -> bool:
        return self._params()["enable"]

    def _agent_enabled(self) -> bool:
        value = getattr(self.settings, "agent_enabled", None)
        if value is not None:
            return bool(value)
        if isinstance(self.settings, dict):
            agent = self.settings.get("agent")
            if isinstance(agent, dict):
                return bool(agent.get("enable", True))
        return True

    def _master_qq(self) -> list[str]:
        value = getattr(self.settings, "master_qq", None)
        if value is not None:
            return [str(qq) for qq in value] if isinstance(value, (list, tuple)) else [str(value)]
        if isinstance(self.settings, dict):
            raw = self.settings.get("master_qq") or []
            return [str(qq) for qq in raw] if isinstance(raw, (list, tuple)) else [str(raw)]
        return []

    def _allowed(self, group_id: str, params: dict) -> bool:
        if group_id in params["blacklist"]:
            return False
        # 白名单为空 = 所有群都插话
        return not params["groups"] or group_id in params["groups"]

    def _aliases(self) -> list[str]:
        """Bot 的别名（配置 ``interject.aliases``），用于识别「喊名字没 @」。"""
        value = (self._section() or {}).get("aliases")
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item or "").strip()]
        return []

    def _detect_attention(self, event: dict) -> Attention | None:
        """pipeline 没把 attention 传进来时自己判一次（兼容旧调用方 / 测试）。"""
        is_reply = getattr(self.mp, "is_reply", None)
        replies_to_bot = getattr(self.mp, "replies_to_bot", None)
        replies_to_me = False
        try:
            if callable(is_reply) and callable(replies_to_bot):
                replies_to_me = bool(is_reply(event)) and bool(replies_to_bot(event))
        except Exception:  # noqa: BLE001 - 判定失败不影响计数
            replies_to_me = False
        try:
            return detect_attention_of(
                event,
                self_id=getattr(self.mp, "user_id", ""),
                replies_to_me=replies_to_me,
                aliases=self._aliases(),
            )
        except Exception:  # noqa: BLE001
            return None

    # ──────────────── 外部入口 ────────────────
    def note_message(self, event: dict, *, attention: Attention | None = None) -> int | None:
        """每条群消息进来时调用；返回该群当前累计条数（没计数就返回 None）。

        断点恢复的消息不要传进来（由 BOTMain 判断 is_recover）。
        ``attention`` 由 pipeline 算好传进来；不传就自己判一次（测试 / 旧调用方兼容）。

        当面叫它的消息（@我 / 引用我 / 直呼名字）不计数：那条由正式回复接走，
        而且「对着我说的话」不该被当成「群里随便聊起来的一段」。
        """
        if not event or event.get("message_type") != "group":
            return None
        if attention is None:
            attention = self._detect_attention(event)
        if attention is not None and attention.is_summon:
            return None
        if not self._agent_enabled():
            return None          # AI 整个关掉了，插话也没人开口
        params = self._params()
        if not params["enable"]:
            return None
        group_id = str(event.get("group_id") or "")
        if not group_id or not self._allowed(group_id, params):
            return None
        # 自己发的消息不算「群里有人说话」
        if str(event.get("user_id")) == str(getattr(self.mp, "user_id", "")):
            return None

        state = self._state(group_id)
        state.count += 1
        state.last_event = event
        self._ensure_timer(state, params)
        # 敏感期（Bot 刚说过话）里门槛更低：对方很可能在接着跟你聊
        hot = self._is_hot(state, params)
        threshold = params["hot_frequent"] if hot else params["frequent"]
        if state.count >= threshold:
            self._schedule(state, "攒够条数", params)
        elif hot and self._cooldown_left(state, params) > 0:
            # 冷却中的单条追问也要在冷却结束时看一眼，不能丢到 idle 定时器
            state.pending = True
            self._ensure_cooldown_wake(state)
        return state.count

    def _is_hot(self, state: "GroupState", params: dict) -> bool:
        """Bot 刚在这个群说过话吗（敏感期）。"""
        checker = getattr(self.mp, "thread_is_hot", None)
        if checker is None:
            return False
        try:
            return bool(
                checker(f"group_{state.group_id}", hot_seconds=params["hot_minutes"] * 60)
            )
        except Exception:
            return False

    def _cooldown(self, state: "GroupState", params: dict) -> float:
        """敏感期里冷却也缩短：刚聊起来，别卡 5 分钟不接话。"""
        base = params["cooldown"]
        return min(base, 60.0) if self._is_hot(state, params) else base

    def _cooldown_left(self, state: "GroupState", params: dict) -> float:
        """距离冷却结束还有多少秒；已经结束就是 0。"""
        return max(
            0.0,
            self._cooldown(state, params)
            - (time.monotonic() - state.last_trigger_at),
        )

    def note_bot_spoke(self, group_id) -> None:
        """Bot 刚在这个群说过话 → 清空累计并进入冷却，避免马上又插一句。"""
        state = self._states.get(str(group_id))
        if state is None:
            return
        state.count = 0
        state.pending = False
        state.last_trigger_at = time.monotonic()

    def close(self) -> None:
        """停掉所有倒计时任务（关机 / 测试收尾用）。"""
        self._closed = True
        for state in self._states.values():
            for task in (state.task, state.cooldown_task):
                if task and not task.done():
                    task.cancel()
            state.task = None
            state.cooldown_task = None
            state.pending = False

    # ──────────────── 内部状态 ────────────────
    def _state(self, group_id: str) -> GroupState:
        state = self._states.get(group_id)
        if state is None:
            state = GroupState(group_id=group_id)
            self._states[group_id] = state
        return state

    def _ensure_timer(self, state: GroupState, params: dict) -> None:
        if self._closed:
            return
        if state.task and not state.task.done():
            return
        loop = _running_loop()
        if loop is None:
            # 没有事件循环（同步调用 / 测试）——插话是纯异步功能，直接跳过
            state.task = None
            return
        state.task = loop.create_task(self._timer_loop(state))

    def _ensure_cooldown_wake(self, state: GroupState) -> None:
        """冷却结束时补一次检查；每个群最多一个补检查任务。"""
        if self._closed:
            return
        if state.cooldown_task and not state.cooldown_task.done():
            return
        loop = _running_loop()
        if loop is None:
            state.cooldown_task = None
            return
        state.cooldown_task = loop.create_task(self._cooldown_loop(state))

    async def _timer_loop(self, state: GroupState) -> None:
        """倒计时：到点看有没有新消息。没有就重新倒计时，有就判断一次。"""
        while not self._closed:
            params = self._params()
            if not params["enable"]:
                return
            # 敏感期用更短的倒计时：对方刚回完话，别等半小时
            idle = params["hot_idle"] if self._is_hot(state, params) else params["idle"]
            try:
                await asyncio.sleep(idle)
            except asyncio.CancelledError:
                return
            if self._closed:
                return
            if state.count <= 0:
                # 没有新消息：什么都不做，进入下一轮倒计时
                continue
            if state.busy:
                continue
            if self._cooldown_left(state, params) > 0:
                continue
            # 这批消息交给判断，之后新来的重新累计
            state.count = 0
            await self._run(state, "安静了一阵", params)

    async def _cooldown_loop(self, state: GroupState) -> None:
        """冷却结束：如果冷却期间有新消息，补一次判断。

        普通阈值触发和敏感期单条追问都会走到这里。判断仍由
        ``maybe_interject`` 的 judge 完成，不自然就继续沉默。
        """
        try:
            while not self._closed:
                params = self._params()
                if not params["enable"]:
                    state.pending = False
                    return
                remaining = self._cooldown_left(state, params)
                if remaining > 0:
                    await asyncio.sleep(remaining)
                    continue
                if not state.pending:
                    return
                if state.busy:
                    return
                if state.count <= 0:
                    state.pending = False
                    return
                state.pending = False
                state.busy = True
                state.count = 0
                await self._run(state, "冷却结束", params)
                # 判断/生成期间又来了新消息：重新按冷却时间等一轮
                if state.pending and state.count > 0:
                    continue
                return
        except asyncio.CancelledError:
            return
        finally:
            if state.cooldown_task is asyncio.current_task():
                state.cooldown_task = None

    def _schedule(self, state: GroupState, reason: str, params: dict) -> None:
        if self._closed:
            return
        if state.busy:
            # 判断/生成期间攒够的这批消息，等当前任务结束后再补
            state.pending = True
            return
        if self._cooldown_left(state, params) > 0:
            # 冷却中：不丢消息，记下来，等冷却结束再判断
            state.pending = True
            self._ensure_cooldown_wake(state)
            return
        state.busy = True
        state.pending = False
        state.count = 0
        loop = _running_loop()
        if loop is None:
            state.busy = False
            return
        loop.create_task(self._run(state, reason, params))

    # ──────────────── 真正插话 ────────────────
    async def _run(self, state: GroupState, reason: str, params: dict | None = None) -> None:
        params = params or self._params()
        event = state.last_event
        state.busy = True
        state.pending = False
        state.last_trigger_at = time.monotonic()
        try:
            agent = self._agent_getter()
            if agent is None or not hasattr(agent, "maybe_interject"):
                return          # AI 没启用 / 还在加载
            if not event:
                return
            context = await self._build_context(event, params)
            if context is None:
                return
            if getattr(context, "directed_at_me", False):
                # 最新那条是在叫它 / 回它 / 直呼它 → 已由 pipeline 的正式回复接走，
                # 插话让开，否则同一条消息会被回两遍。
                return

            plan = await agent.maybe_interject(
                context,
                thread_id=f"group_{state.group_id}",
                reason=reason,
            )
            if plan is None:
                return

            # 兼容旧实现（直接返回 list[str]）：按 join 处理
            if isinstance(plan, str):
                act, messages = "join", [plan]
            elif isinstance(plan, (list, tuple)):
                act, messages = "join", [str(item) for item in plan if item]
            else:
                act = str(getattr(plan, "act", "join") or "join")
                messages = [str(item) for item in (getattr(plan, "messages", None) or []) if item]
            state.last_act = act

            if act == "nod":
                await self._nod(state, event)
                return

            for message in messages:
                if not message:
                    continue
                result = await self.mp.send_message(event, message)
                # 记进临时历史并进入敏感期：对方接下来回的就是这句
                self.mp.remember_sent_message(
                    event, message, result, hot_seconds=params["hot_minutes"] * 60
                )
            state.triggered += 1
            print(f"[插话] 群 {state.group_id} 已插话（{reason}，动作 {act}）")
        except Exception as exc:  # 插话失败不能影响主流程
            print(f"[插话] 群 {state.group_id} 失败: {exc}")
        finally:
            state.busy = False
            # 本次判断/生成期间又攒了消息：等冷却结束再补一次
            if state.pending and state.count > 0:
                self._ensure_cooldown_wake(state)

    async def _nod(self, state: GroupState, event: dict) -> None:
        """只想表示「我在」：给最后一条消息贴个表情，不打字。

        群里最自然的参与方式本来就不一定是说话。这条路比什么都不做多一点存在感，
        比硬说一句便宜得多，也几乎不可能让人反感。
        """
        message_id = str((event or {}).get("message_id") or "")
        api = getattr(self.mp, "api", None)
        if not message_id or api is None:
            return
        try:
            await api.set_msg_emoji_like(message_id, random.choice(_NOD_EMOJIS))
        except Exception as exc:  # noqa: BLE001 - 贴表情失败不能影响主流程
            print(f"[插话] 群 {state.group_id} 表情回应失败: {exc}")
            return
        state.nodded += 1
        print(f"[插话] 群 {state.group_id} 只贴了表情（在场，没说话）")

    async def _build_context(self, event: dict, params: dict):
        """扒最近 frequent + extra 条消息，组装成 ChatContext。"""
        count = params["frequent"] + params["extra"]
        try:
            context = await self.mp.build_chat_context(
                event=event,
                history_count=count,
                note="",
                master_ids=self._master_qq(),
                aliases=self._aliases(),
            )
        except Exception as exc:
            print(f"[插话] 群 {event.get('group_id')} 组上下文失败: {exc}")
            return None
        # 插话要看完整一段对话，比 @ 回复放宽历史预算
        context.max_history_messages = min(count + 2, 20)
        context.max_history_chars = 3200
        # 兴趣只用来提示「这个话题我在意」，不构成开口理由（判断仍在 judge 里）
        history = getattr(context, "history", None) or []
        current = getattr(context, "current", None)
        context.interest_hits = read_interests(
            [*history, *([current] if current is not None else [])],
            high=params.get("interest_high") or (),
            low=params.get("interest_low") or (),
        )
        return context


__all__ = ["GroupState", "InterjectService"]
