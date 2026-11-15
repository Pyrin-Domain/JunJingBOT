"""QQBotAgent 运行时：LangGraph 对话 / 工具调用 / 模型降级。

这是旧 ``Agent/LanguageModule.py`` 的 ``QQBotAgent`` 部分。
提示词、输出解析、动作提示和工具已经拆到：

- :mod:`agent.prompts`
- :mod:`agent.output`
- :mod:`agent.hints`
- :mod:`agent.tools`
- :mod:`agent.router`
- :mod:`agent.config`
- :mod:`agent.logger`

本文件只保留「组装 + 调模型 + 续说 / 去复读 / 强制回复」的编排逻辑。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextvars import ContextVar
from datetime import date
from typing import Any, Optional

from messaging.meme import configure as configure_memes

from .catchphrase import catchphrase_hint, load_catchphrases_for_settings
from .config import (
    find_model_entry,
    model_label,
    resolve_llm_config,
    resolve_model_entry,
)
from .hints import _image_urls, build_action_hint, build_meme_hint, wants_followup
from .logger import append_log, build_log_entry, configure_log_dir
from .output import (
    _ANSWER_TOOLS,
    _DOM_PREFIX,
    _STRAY_TAG_RE,
    InterjectPlan,
    _looks_like_dodge,
    _parse_interject_judge,   # 保留 re-export：旧测试 / 外部代码还从这里取
    looks_like_repeat,
    parse_agent_output,
    parse_interject_verdict,
    pick_scene_line,
    split_sendable_messages,
)
from .prompts import (
    _CONTINUE_HINT,
    _DODGE_CONTINUE_HINT,
    _FORCE_REPLY_HINT,
    _INTERJECT_JUDGE_SYSTEM,
    _INTERJECT_REPLY_HINT,
    _RELATION_EXTRACT_SYSTEM,
    build_system_prompt,
    interject_hint,
    persona_voice_excerpt,
    _REPLY_CHAIN_HINT,
    _REPHRASE_SYSTEM,
    SYSTEM_PROMPT,
)
from .relations import RelationStore
from .result_vault import ResultVault, thread_key
from .router import ModelRoute, ModelRouter
from .thread_turns import ThreadTurns
from .thread_verdict import VERDICT_NEW, resolve_verdict
from .tools import (
    _MISSING,
    _current_context_var,
    _current_images_var,
    _current_tool_cache_var,
    _current_tool_calls_var,
    _current_user_text_var,
    build_tools,
    current_tool_calls,
    get_cached_tool_result,
    make_tool_cache_key,
    record_tool_call,
    set_cached_tool_result,
)
from .tools.methods import MethodLibrary

logger = logging.getLogger("QQBotAgent")

#: 当前这一轮是哪个会话、走的哪个实验臂。LLM 回调里用它给日志打标 ——
#: 没有这两项，事后只能靠「离哪条 [会话] 行最近」去猜，judge（GLM）的调用
#: 还会混进来，统计出来的命中率直接是错的。
_llm_session_var: ContextVar[str] = ContextVar("llm_session", default="")
_llm_arm_var: ContextVar[str] = ContextVar("llm_arm", default="")

# judge 判了 silent、理由却说「我能答」——这两种输出互相打架，
# 值得单独喊一嗓子，不然日志看着像闹矛盾，其实没人知道当时发生了什么。
_SILENT_BUT_ANSWERABLE = re.compile(
    r"(可以答|能答|该答|应该答|我来答|我来回答|能回答|可以回答|答得上|接得住)"
)

# ── langchain 可选依赖：没装也允许 import（BotApp 会捕获初始化失败）──
try:  # pragma: no cover - 有依赖时走这里
    from langchain_core.messages import (
        AIMessage,
        BaseMessage,
        HumanMessage,
        SystemMessage,
    )
except Exception:  # pragma: no cover - 测试 / 精简环境

    class BaseMessage:  # type: ignore[no-redef]
        def __init__(self, content: str = ""):
            self.content = content

    class HumanMessage(BaseMessage):  # type: ignore[no-redef]
        pass

    class AIMessage(BaseMessage):  # type: ignore[no-redef]
        pass

    class SystemMessage(BaseMessage):  # type: ignore[no-redef]
        pass


try:  # pragma: no cover
    from langchain_core.callbacks import BaseCallbackHandler
except Exception:  # pragma: no cover

    class BaseCallbackHandler:  # type: ignore[no-redef]
        pass


try:  # pragma: no cover
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover
    ChatOpenAI = None  # type: ignore[assignment]


try:  # pragma: no cover
    from langchain.agents import create_agent
except Exception:  # pragma: no cover
    create_agent = None  # type: ignore[assignment]


MODEL_TYPE = "deepseek"

# 续说次数 / 单轮最大消息数，与旧版一致
_MAX_CONTINUE_ROUNDS = 2
_MAX_OUT_MESSAGES = 6

# 承接上一轮内容时，自动带上的留存正文上限（字符）。再多的部分让模型用
# recall_result(offset=...) 自己取 —— 这里的目标是「一定看得到」，不是「全塞进去」。
_AUTO_INJECT_CHARS = 6000


def _render_vault_body(entry: Any, limit: int = _AUTO_INJECT_CHARS) -> str:
    """把留存正文直接铺进本轮提示（只在用户明确承接上一轮时用）。"""
    text = str(getattr(entry, "text", "") or "")
    shown = text[: max(0, int(limit))]
    result_id = getattr(entry, "result_id", "")
    label = getattr(entry, "label", "") or getattr(entry, "tool", "")
    head = (
        f"[上一轮的结果原文 · {result_id} · {label} · "
        f"已经自动带上，不用再 recall_result]"
    )
    rest = len(text) - len(shown)
    if rest <= 0:
        return f"{head}\n{shown}"
    return (
        f"{head}\n{shown}\n"
        f"…（原文共 {len(text)} 字，还有 {rest} 字没带上；"
        f'需要就 recall_result(result_id="{result_id}", offset={len(shown)})）'
    )


def _cached_tokens(response: Any) -> tuple[int, int]:
    """从 provider 的 usage 里抠出 ``(prompt tokens, 命中缓存 tokens)``。

    字段名随后端变：OpenAI 是 ``prompt_tokens_details.cached_tokens``，
    DeepSeek 是 ``prompt_cache_hit_tokens``。拿不到就当 0（不是错误，
    只说明这个网关没告诉你）。
    """
    payload = getattr(response, "llm_output", None)
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("token_usage") or payload.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0

    def _num(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    total = _num(usage.get("prompt_tokens"))
    cached = _num(usage.get("prompt_cache_hit_tokens"))
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = cached or _num(details.get("cached_tokens"))
    return total, cached


class LLMCallbackHandler(BaseCallbackHandler):
    """把 LLM / 工具的调试信息写进 ``QQBotAgent`` logger。"""

    def on_llm_start(self, serialized, prompts, **kwargs):
        logger.debug(f"[LLM] 发送请求, prompts={len(prompts)} 条")

    def on_llm_end(self, response, **kwargs):
        generations = getattr(response, "generations", None) or []
        content = generations[0][0].text if generations else ""
        logger.debug(
            f"[LLM] 收到回复: {content[:100]}{'...' if len(content) > 100 else ''}"
        )
        # 服务端 prompt 缓存到底有没有命中，只有 usage 里能看出来。不同网关字段
        # 名不一样（OpenAI 的 prompt_tokens_details.cached_tokens / DeepSeek 的
        # prompt_cache_hit_tokens），拿不到就打 0 —— 打出来才知道值不值得为它
        # 调整消息结构（稳定前缀放前面）。
        total, cached = _cached_tokens(response)
        if total:
            session = _llm_session_var.get()
            arm = _llm_arm_var.get()
            tag = f"[{session}|{arm}]" if session else ""
            logger.info(
                f"[LLM]{tag} prompt {total} tokens，缓存命中 {cached}"
                f"（{int(cached * 100 / total) if total else 0}%）"
            )

    def on_llm_error(self, error, **kwargs):
        logger.error(f"[LLM] 请求出错: {error}")

    def on_tool_start(self, serialized, input_str, **kwargs):
        logger.debug(
            f"[LLM] 决定调用工具: {serialized.get('name', 'unknown')}({input_str[:80]})"
        )

    def on_tool_end(self, output, **kwargs):
        text = str(output)
        logger.debug(f"[LLM] 工具返回: {text[:80]}{'...' if len(text) > 80 else ''}")


class QQBotAgent:
    """带工具调用的聊天 Agent（人格 / 输出协议见 ``agent.prompts``）。"""

    def __init__(
        self,
        *,
        api: Optional[Any] = None,
        messaging: Optional[Any] = None,
        media: Optional[Any] = None,
        images: Optional[Any] = None,
        settings: Optional[Any] = None,
        llm_config: Optional[dict] = None,
        # ── 旧参数名，保留兼容 ──
        napcat_api: Optional[Any] = None,
        mp: Optional[Any] = None,
        extension: Optional[Any] = None,
    ):
        self.api = api if api is not None else napcat_api
        self.napcat_api = self.api
        self.messaging = messaging if messaging is not None else mp
        self.mp = self.messaging
        self.media = media if media is not None else extension
        self.extension = self.media
        self.images = images
        self.settings = settings

        # 方法库：新路径 data/methods.json，首次运行只读回退旧 method.json
        self.rw_tool = MethodLibrary(settings)
        self._methods = self.rw_tool

        # 人物关系档案：data/relations/；没有 settings 时是禁用状态（测试友好）
        self.relations = RelationStore.from_settings(settings)

        # 工具结果的跨轮暂存：模型上一轮翻出来的大段内容，下一轮还能取回
        # （``recall_result``），不用重新翻一遍历史。见 agent/result_vault.py。
        self.result_vault = ResultVault()

        # 多轮留存：第一轮的完整上下文冻结，之后每轮只追加增量（见 thread_turns.py）。
        # 默认关闭 —— 先看日志里的「缓存命中 X%」有没有起来，再决定要不要开。
        memory_config: dict = {}
        if settings is not None:
            try:
                memory_config = settings.section("thread_memory") or {}
            except Exception:  # noqa: BLE001 - 旧配置没有这个段
                memory_config = {}
        self._thread_memory_enabled = bool(memory_config.get("stable_prefix", False))
        # 裁决：模型在输出里写 <thread>新话题</thread> 就丢掉前面那段；漏写则由
        # thread_verdict.resolve_verdict 按代码信号兜底（默认留）。
        self._thread_memory_verdict = bool(memory_config.get("verdict", True))
        # 「隔太久就当新话题」：比 ttl 更短，0 表示不按时间判（只信模型）。
        try:
            self._thread_memory_new_after = (
                float(memory_config.get("new_after_minutes") or 0) * 60
            )
        except (TypeError, ValueError):
            self._thread_memory_new_after = 15 * 60
        # 灰度：留空 = 全部会话生效；填了群号就只在这些群生效。
        self._thread_memory_groups = {
            str(item).strip()
            for item in (memory_config.get("groups") or [])
            if str(item).strip()
        }
        # 对照实验：1 = 奇数日开、0 = 偶数日开，None = 不按日轮换。
        # 按「天」而不是按「条」翻转，是因为这个功能要连续几轮才能攒出可回放的
        # 上下文；按条随机的话实验组永远从冷启动开始，等于没测。
        parity = memory_config.get("ab_day_parity")
        try:
            self._thread_memory_day_parity = (
                None if parity is None or parity == "" else int(parity) % 2
            )
        except (TypeError, ValueError):
            self._thread_memory_day_parity = None
        try:
            self.thread_turns = ThreadTurns(
                ttl=float(memory_config.get("ttl_minutes") or 30) * 60,
                max_turns=int(memory_config.get("max_turns") or 8),
                max_chars=int(memory_config.get("max_chars") or 12000),
            )
        except (TypeError, ValueError):
            self.thread_turns = ThreadTurns()

        # 启动就说清楚这条链路是不是真的开着 / 开在哪些群 —— 排查时第一眼看这个，
        # 别再靠「我明明改了配置」来推断。
        parity = self._thread_memory_day_parity
        logger.info(
            "多轮留存（stable_prefix）: %s｜灰度群: %s｜裁决: %s｜新话题阈值: %s｜对照实验: %s",
            "开" if self._thread_memory_enabled else "关",
            ",".join(sorted(self._thread_memory_groups)) or "全部会话",
            "开" if self._thread_memory_verdict else "关（只保留）",
            (
                f"{self._thread_memory_new_after / 60:.0f} 分钟"
                if self._thread_memory_new_after > 0
                else "不按时间判"
            ),
            (
                f"{'奇' if parity else '偶'}数日开"
                if parity is not None
                else "无（始终开）"
            ),
        )

        self.model_type = MODEL_TYPE
        self._recent_replies: dict[str, list[str]] = {}
        self._relation_tasks: set[asyncio.Task] = set()
        self._closed = False

        # 日志目录：settings 有就用 logs/agent
        if settings is not None:
            try:
                configure_log_dir(settings.paths.logs_dir / "agent")
            except Exception:
                pass

        merged_config = dict(llm_config or {})
        if settings is not None:
            try:
                merged_config = {
                    **settings.agent_llm_config,
                    **settings.agent_model_routing,
                    **merged_config,
                }
            except Exception:
                pass
        self.llm_config = resolve_llm_config(merged_config)

        logger.info("正在初始化 QQBotAgent ...")
        logger.info(
            f"模型: {self.llm_config['model']}, API Base: {self.llm_config['base_url']}, "
            f"model_type: {self.model_type}, max_tokens: {self.llm_config['max_tokens']}"
        )

        self.callbacks = [LLMCallbackHandler()]
        self.tools = self._build_tools()
        logger.info(
            f"已注册 {len(self.tools)} 个工具: {[getattr(t, 'name', '?') for t in self.tools]}"
        )

        self.catchphrases = load_catchphrases_for_settings(self.settings)
        self.system_prompt = SystemMessage(content=self._build_system_prompt())
        self._setup_model_routes(merged_config)
        logger.info("QQBotAgent 初始化完成")

    # ════════════════════ 模型分级 / 降级 ════════════════════
    def _build_system_prompt(self) -> str:
        """加载表情包库，并把可用表情目录填进提示词。

        表情包名单跟着配置文件走，所以不能硬编码在 SYSTEM_PROMPT 里；
        这里读一次配置、加载一次文件，之后整个进程共用（测试可调 ``meme.reset()``）。
        """
        section: dict = {}
        if self.settings is not None:
            try:
                section = self.settings.section("meme") or {}
            except Exception:  # noqa: BLE001 - 旧配置可能没有这个段
                section = {}
        enable = bool(section.get("enable", True))
        path = str(section.get("path") or "").strip()
        try:
            configure_memes(path or None, enable=enable)
        except Exception as exc:  # noqa: BLE001 - 表情包坏了不能影响启动
            logger.warning(f"[表情] 加载表情包失败: {exc}")
        try:
            limit = int(section.get("prompt_limit") or 0)
        except (TypeError, ValueError):
            limit = 0
        return build_system_prompt(limit, settings=self.settings)

    def _build_llm(self, config: dict):
        if ChatOpenAI is None:
            raise RuntimeError("未安装 langchain-openai，无法初始化 LLM")
        return ChatOpenAI(
            model=config["model"],
            api_key=config["api_key"],
            base_url=config["base_url"],
            temperature=float(config["temperature"]),
            max_tokens=int(config["max_tokens"]),
            callbacks=self.callbacks,
        )

    def _make_agent(self, llm, tools, system_prompt):
        if create_agent is None:
            raise RuntimeError("未安装 langchain / langgraph，无法创建 Agent")
        try:
            return create_agent(
                model=llm,
                tools=tools,
                system_prompt=system_prompt,
            )
        except TypeError:
            # 老版本 / langgraph.prebuilt 的参数名是 prompt
            return create_agent(model=llm, tools=tools, prompt=system_prompt)

    def _setup_model_routes(self, llm_config: dict) -> None:
        """按 ``agent.models`` / ``judge_model`` / ``fallback_models`` 建两条路由。"""
        models = llm_config.get("models")
        models = (
            [entry for entry in models if isinstance(entry, dict)]
            if isinstance(models, list)
            else []
        )
        base = self.llm_config

        def build_route(
            entry: dict,
            key: str,
            *,
            with_agent: bool,
            max_tokens_cap: int | None = None,
            required: bool = False,
        ) -> ModelRoute | None:
            config = resolve_model_entry(entry, base)
            if max_tokens_cap is not None and not entry.get("max_tokens"):
                config["max_tokens"] = min(
                    int(config["max_tokens"] or max_tokens_cap), max_tokens_cap
                )
            try:
                llm = self._build_llm(config)
                agent = (
                    self._make_agent(llm, self.tools, self.system_prompt)
                    if with_agent
                    else None
                )
            except Exception as exc:
                if required:
                    raise
                logger.warning(
                    f"[模型分级] 「{model_label(entry)}」初始化失败，已跳过: {exc}"
                )
                return None
            return ModelRoute(
                key=key, label=model_label(entry), llm=llm, agent=agent
            )

        active_id = str(llm_config.get("active_model") or "").strip()
        active_entry = find_model_entry(models, active_id)
        if active_entry is None and models:
            active_entry = models[0]
        primary_entry = (
            dict(active_entry)
            if active_entry
            else {
                "id": "primary",
                "name": base["model"],
                **{key: base[key] for key in ("base_url", "api_key", "model")},
            }
        )
        primary_entry.pop("temperature", None)
        primary_entry.pop("max_tokens", None)
        primary = build_route(
            primary_entry, "primary", with_agent=True, required=True
        )
        assert primary is not None
        self.llm = primary.llm
        self.agent = primary.agent

        reply_routes = [primary]
        seen_ids = {str(active_entry.get("id"))} if active_entry else set()
        for model_id in llm_config.get("fallback_models") or []:
            model_id = str(model_id or "").strip()
            if not model_id or model_id in seen_ids:
                continue
            entry = find_model_entry(models, model_id)
            if entry is None:
                logger.warning(
                    f"[模型降级] fallback_models 里的 {model_id!r} 不在 agent.models 中，已跳过"
                )
                continue
            seen_ids.add(model_id)
            fallback_route = build_route(
                entry, f"fallback:{model_id}", with_agent=True
            )
            if fallback_route is not None:
                reply_routes.append(fallback_route)
        self.reply_router = ModelRouter(
            reply_routes, logger=logger, role="回答模型"
        )

        judge_id = str(llm_config.get("judge_model") or "").strip()
        judge_entry = find_model_entry(models, judge_id)
        if judge_id and judge_entry is None:
            logger.warning(
                f"[模型分级] judge_model={judge_id!r} 不在 agent.models 中，判断器跟随主模型"
            )
        judge_route = None
        if judge_entry is not None and judge_id != str(
            active_entry.get("id") if active_entry else ""
        ):
            judge_route = build_route(
                judge_entry,
                f"judge:{judge_id}",
                with_agent=False,
                max_tokens_cap=256,
            )
        if judge_route is not None:
            judge_routes = [
                judge_route,
                ModelRoute(
                    key="judge:primary", label=primary.label, llm=primary.llm
                ),
            ]
        else:
            judge_routes = [
                ModelRoute(
                    key="judge:primary", label=primary.label, llm=primary.llm
                )
            ]
        self.judge_router = ModelRouter(
            judge_routes, logger=logger, role="插话判断器"
        )
        self.judge_llm = judge_routes[0].llm

        logger.info(
            f"回答模型: {' → '.join(self.reply_router.labels)}；"
            f"插话判断器: {' → '.join(self.judge_router.labels)}"
        )

    async def _invoke_agent(self, payload: Any) -> Any:
        router = getattr(self, "reply_router", None)
        if router is not None:
            return await router.ainvoke(
                payload,
                use_agent=True,
                can_fallback=lambda: not self._has_side_effect_call(),
            )
        return await self.agent.ainvoke(payload)

    # ════════════════════ 多轮留存 ════════════════════
    def _memory_arm(
        self, thread_id: str = "", *, today: date | None = None
    ) -> tuple[bool, str]:
        """这一轮走不走多轮留存，返回 ``(是否生效, 原因)``。

        原因会写进日志（``多轮 arm=on/off（…）``），事后按臂分桶统计时不用再猜。
        ``ab_day_parity`` 是给对照实验用的：同一天内所有群行为一致，隔天翻转，
        这样「网关今天缓存热不热」这种日级噪声会平均分到两边。
        """
        if not getattr(self, "_thread_memory_enabled", False):
            return False, "disabled"

        allowed = getattr(self, "_thread_memory_groups", None) or set()
        if allowed and thread_id:
            key = str(thread_id)
            gid = key.split("_", 1)[-1] if "_" in key else key
            # 群号写 "20001" 或 "group_20001" 都认
            if key not in allowed and gid not in allowed:
                return False, "not-in-groups"

        parity = getattr(self, "_thread_memory_day_parity", None)
        if parity is None:
            return True, "on"

        day = (today or date.today()).day
        label = "奇数日" if day % 2 else "偶数日"
        if day % 2 != int(parity) % 2:
            return False, f"ab-day {day} 号{label}（对照组）"
        return True, f"ab-day {day} 号{label}（实验组）"

    def _thread_memory(self, thread_id: str = ""):
        """开关打开时返回留存器；关闭返回 None（行为与旧版完全一致）。"""
        ok, _ = self._memory_arm(thread_id)
        return getattr(self, "thread_turns", None) if ok else None

    def _verdict_of(self, parsed: Any, *, turns: list, user_text: str) -> tuple[str, str]:
        """本轮该不该丢掉前面那段：返回 ``(verdict, source)``。

        裁决**下一轮生效**：模型说「新话题」时它已经生成完这一轮的回复了，
        这一轮仍然是带着旧 turns 生成的，来不及改。
        """
        if not getattr(self, "_thread_memory_verdict", True):
            return "", "disabled"
        return resolve_verdict(
            str((parsed or {}).get("thread") or ""),
            turns=turns,
            user_text=user_text,
            new_after=float(getattr(self, "_thread_memory_new_after", 0) or 0),
        )

    @staticmethod
    def _build_turn_messages(turns: list, current_text: str) -> list[BaseMessage]:
        """把留存的历史轮次拼成 messages，末尾接本轮增量。"""
        out: list[BaseMessage] = []
        for turn in turns:
            out.append(HumanMessage(content=turn.human))
            out.append(AIMessage(content=turn.ai))
        out.append(HumanMessage(content=current_text))
        return out

    @staticmethod
    def _drop_turns_for_new_topic(
        memory: Any, thread_id: str, turns: list, verdict: str
    ) -> None:
        """本轮没接话、但裁决是新话题：把旧的一段清掉，下一轮重新开始。"""
        if memory is None or verdict != VERDICT_NEW or not turns:
            return
        memory.reset(thread_id)
        logger.info(
            f"[会话 {thread_id}] 裁决为新话题（本轮没接话），多轮留存已清空"
        )

    def _remember_turn(
        self,
        memory: Any,
        thread_id: str,
        *,
        turns_before: list,
        full_text: str,
        incremental: str,
        ai_text: str,
        continued: bool,
        replied: bool,
        token: str = "",
        verdict: str = "",
    ) -> None:
        """本轮结束后记账；只在「真的回了话、且没续说」时记。

        续说那轮实际是 ``H→A→H→A``，这里只存得下一对 ``(H, A)``，回放序列对
        不上、前缀已经不成立，清掉重来更干净。

        ``verdict == "new"`` 表示这一段该断了：用**本轮的完整上下文**重开一段，
        而不是空着。本轮就是拿完整上下文答的，把它当成新一段的起点才自洽；
        丢掉它的话下一轮还得重渲染一遍完整上下文，白多一次前缀断裂。
        """
        try:
            if continued:
                memory.reset(thread_id)
                logger.info(f"[会话 {thread_id}] 续说过，多轮留存重置")
                return
            if not replied or not str(ai_text or "").strip():
                # 没接话也要认裁决：对方换了话题、这轮没吭声，下一轮不该还拖着
                # 上一段的上下文。
                self._drop_turns_for_new_topic(memory, thread_id, turns_before, verdict)
                return
            if verdict == VERDICT_NEW and turns_before:
                memory.start(thread_id, full_text, ai_text, token=token)
                logger.info(
                    f"[会话 {thread_id}] 裁决为新话题，多轮留存重开一段"
                    f"（完整上下文 {len(full_text)} 字）"
                )
                return
            if turns_before:
                if not memory.append(thread_id, incremental, ai_text, token=token):
                    logger.info(f"[会话 {thread_id}] 多轮留存到上限，已重置")
            else:
                memory.start(thread_id, full_text, ai_text, token=token)
                logger.info(
                    f"[会话 {thread_id}] 多轮留存起始（完整上下文 {len(full_text)} 字）"
                )
        except Exception as exc:  # noqa: BLE001 - 留存失败不能影响回复
            logger.warning(f"[会话 {thread_id}] 多轮留存写入失败: {exc}")

    async def _invoke_judge(self, messages: list[BaseMessage]) -> Any:
        # 判断器不是主模型：显式打标成 judge，否则它会继承上一轮 chat() 留在
        # ContextVar 里的会话号，被统计成「这个群的主模型调用」（上一次统计就是
        # 这么把 261 次判断器调用混进去的）。
        session_token = _llm_session_var.set("judge")
        arm_token = _llm_arm_var.set("")
        try:
            router = getattr(self, "judge_router", None)
            if router is not None:
                return await router.ainvoke(messages, use_agent=False)
            return await self.llm.ainvoke(messages)
        finally:
            _llm_session_var.reset(session_token)
            _llm_arm_var.reset(arm_token)

    async def _invoke_reply_light(self, messages: list[BaseMessage]) -> Any:
        router = getattr(self, "reply_router", None)
        if router is not None:
            return await router.ainvoke(messages, use_agent=False)
        return await self.llm.ainvoke(messages)

    @staticmethod
    def _has_side_effect_call() -> bool:
        try:
            calls = current_tool_calls()
        except Exception:
            return False
        side_effect_tools = _ANSWER_TOOLS | {
            "poke",
            "emoji_like",
            "set_ban",
            "recall_msg",
        }
        return any(call.get("tool") in side_effect_tools for call in calls)

    # ════════════════════ 去复读 / 改写 ════════════════════
    def _recent_replies_of(self, thread_id: str) -> list[str]:
        store = getattr(self, "_recent_replies", None)
        if store is None:
            store = {}
            self._recent_replies = store
        return store.setdefault(thread_id or "default", [])

    def _remember_replies(self, thread_id: str, messages: list[str]) -> None:
        bucket = self._recent_replies_of(thread_id)
        bucket.extend(m for m in messages if m and "[CQ:" not in m)
        del bucket[:-8]

    async def _de_repeat(
        self,
        thread_id: str,
        messages: list[str],
        user_message: str = "",
        is_dom: bool = False,
    ) -> list[str]:
        recent = self._recent_replies_of(thread_id)
        if not any(looks_like_repeat(m, recent) for m in messages):
            return messages

        logger.info(f"[会话 {thread_id}] 检测到照抄示例/重复发言，尝试改写")
        fixed: list[str] = []
        for msg in messages:
            if not looks_like_repeat(msg, recent):
                fixed.append(msg)
                continue
            new_msg = pick_scene_line(user_message, is_dom, recent + fixed)
            if new_msg is None:
                new_msg = await self._rephrase(
                    msg, recent, user_message=user_message
                )
            logger.info(
                f"[会话 {thread_id}] 改写: {msg[:40]} -> {(new_msg or '（保留原句）')[:40]}"
            )
            fixed.append(new_msg or msg)
        return fixed

    async def _rephrase(
        self, text: str, recent: list[str], user_message: str = ""
    ) -> Optional[str]:
        if getattr(self, "llm", None) is None and getattr(
            self, "reply_router", None
        ) is None:
            return None
        prompt = ""
        scene = (user_message or "").replace(_DOM_PREFIX, "").strip()
        if scene:
            prompt += f"对方说：{scene[-60:]}\n"
        prompt += f"你要说的是：{text}\n"
        if recent:
            prompt += f"你说过的（不要再用）：{' / '.join(recent[-4:])}\n"
        prompt += "请顺着对方的话换一种说法，意思相近但用词明显不同。"
        # 人设接上：改写提示词很短，光靠「你是君景」一句话模型会退回通用中文，
        # 改出来阴阳怪气、没有可爱感。把人设里「说话方式」那段接过去。
        system = _REPHRASE_SYSTEM
        try:
            voice = persona_voice_excerpt(self.settings)
        except Exception:  # noqa: BLE001 - 人设读不到就照常改写
            voice = ""
        if voice:
            system = f"{system}\n\n【君景说话的方式】\n{voice}"
        try:
            res = await self._invoke_reply_light(
                [
                    SystemMessage(content=system),
                    HumanMessage(content=prompt),
                ]
            )
        except Exception as exc:
            logger.warning(f"[反复读] 改写失败: {exc}")
            return None
        new_text = _STRAY_TAG_RE.sub(
            "", str(getattr(res, "content", "") or "")
        ).strip().strip("“”\"'")
        if (
            not new_text
            or len(new_text) > 60
            or looks_like_repeat(new_text, recent)
        ):
            return None
        return new_text

    # ════════════════════ 方法库 ════════════════════
    def structed_method(self) -> str:
        if not self.rw_tool:
            return ""
        part = ""
        for item in self.rw_tool.method.get("data", []):
            part += f"[index:{item['index']},{item['context']}]\n"
        return part

    # ════════════════════ 结果提取（兼容各种 LangGraph 返回）════════════════════
    def _extract_ai_response(self, result: Any) -> Optional[str]:
        if result is None:
            return None

        def _coerce_text(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                return value.strip() or None
            if isinstance(value, (list, tuple)):
                parts: list[str] = []
                for item in value:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        text = (
                            item.get("text")
                            or item.get("content")
                            or item.get("type")
                        )
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                joined = "\n".join(part for part in parts if part).strip()
                return joined or None
            if isinstance(value, dict):
                for key in ("text", "content", "output", "output_text"):
                    if key in value:
                        text = _coerce_text(value[key])
                        if text:
                            return text
                if value.get("type") == "reasoning":
                    return _coerce_text(value.get("reasoning_content"))
            return None

        def _is_truncated_result(result: Any) -> bool:
            def _finish_reason(value: Any) -> bool:
                if not isinstance(value, str):
                    return False
                return value.lower() in (
                    "length",
                    "max_tokens",
                    "token_limit",
                    "tokens_limit",
                )

            if isinstance(result, dict):
                choices = result.get("choices") or []
                if isinstance(choices, (list, tuple)) and choices:
                    first_choice = choices[0]
                    if isinstance(first_choice, dict):
                        if _finish_reason(first_choice.get("finish_reason")):
                            return True
                        if _finish_reason(first_choice.get("reason")):
                            return True
                        message = first_choice.get("message")
                        if isinstance(message, dict) and _finish_reason(
                            message.get("finish_reason")
                        ):
                            return True
                messages = result.get("messages") or []
            elif hasattr(result, "choices"):
                choices = getattr(result, "choices")
                if isinstance(choices, (list, tuple)) and choices:
                    first_choice = choices[0]
                    if hasattr(first_choice, "finish_reason") and _finish_reason(
                        getattr(first_choice, "finish_reason", None)
                    ):
                        return True
                    if _finish_reason(getattr(first_choice, "reason", None)):
                        return True
                    message = getattr(first_choice, "message", None)
                    if isinstance(message, dict) and _finish_reason(
                        message.get("finish_reason")
                    ):
                        return True
                messages = getattr(result, "messages", [])
            else:
                messages = None

            if isinstance(messages, (list, tuple)):
                for msg in messages:
                    if isinstance(msg, dict):
                        if _finish_reason(msg.get("finish_reason")):
                            return True
                        if _finish_reason(msg.get("reason")):
                            return True
                        response_metadata = msg.get("response_metadata") or {}
                        if _finish_reason(
                            response_metadata.get("finish_reason")
                        ):
                            return True
                    elif isinstance(msg, AIMessage):
                        response_metadata = (
                            getattr(msg, "response_metadata", None) or {}
                        )
                        if _finish_reason(
                            response_metadata.get("finish_reason")
                        ):
                            return True
                        if _finish_reason(
                            getattr(msg, "finish_reason", None)
                        ):
                            return True
                        additional_kwargs = (
                            getattr(msg, "additional_kwargs", None) or {}
                        )
                        if _finish_reason(
                            additional_kwargs.get("finish_reason")
                        ):
                            return True
            return False

        if _is_truncated_result(result):
            return "思考时间过长"

        def _extract_dict_message_text(msg_dict: dict[str, Any]) -> Optional[str]:
            if not isinstance(msg_dict, dict):
                return None
            role = msg_dict.get("role")
            if isinstance(role, str) and role.lower() in ("user", "system"):
                return None
            if "content" in msg_dict:
                text = _coerce_text(msg_dict.get("content"))
                if text:
                    return text
            if "message" in msg_dict:
                nested = msg_dict.get("message")
                if isinstance(nested, dict):
                    nested_role = nested.get("role")
                    if isinstance(nested_role, str) and nested_role.lower() in (
                        "assistant",
                        "tool",
                    ):
                        return _coerce_text(
                            nested.get("content")
                        ) or _coerce_text(
                            nested.get("reasoning_content")
                            or nested.get("thoughts")
                        )
                    if nested_role is None or nested_role.lower() not in (
                        "user",
                        "system",
                    ):
                        return _extract_dict_message_text(nested)
            for key in ("output_text", "output"):
                text = _coerce_text(msg_dict.get(key))
                if text:
                    return text
            return _coerce_text(
                msg_dict.get("reasoning_content") or msg_dict.get("thoughts")
            )

        def _extract_message_text(msg: Any) -> Optional[str]:
            if msg is None:
                return None
            if isinstance(msg, AIMessage):
                text = _coerce_text(getattr(msg, "content", None))
                if text:
                    return text
                additional_kwargs = (
                    getattr(msg, "additional_kwargs", None) or {}
                )
                if isinstance(additional_kwargs, dict):
                    return _coerce_text(
                        additional_kwargs.get("reasoning_content")
                        or additional_kwargs.get("thoughts")
                    )
                return None
            if isinstance(msg, dict):
                return _extract_dict_message_text(msg)
            text = _coerce_text(
                getattr(msg, "content", None)
                or getattr(msg, "text", None)
                or getattr(msg, "output", None)
                or getattr(msg, "output_text", None)
            )
            if text:
                return text
            if hasattr(msg, "message"):
                nested = getattr(msg, "message")
                if isinstance(nested, dict) or isinstance(nested, AIMessage):
                    text = _extract_message_text(nested)
                    if text:
                        return text
            additional_kwargs = getattr(msg, "additional_kwargs", None)
            if isinstance(additional_kwargs, dict):
                return _coerce_text(
                    additional_kwargs.get("reasoning_content")
                    or additional_kwargs.get("thoughts")
                )
            return _coerce_text(getattr(msg, "reasoning_content", None))

        messages = None
        if isinstance(result, dict):
            messages = result.get("messages")
        elif hasattr(result, "messages"):
            messages = getattr(result, "messages")

        fallback = None
        if isinstance(messages, (list, tuple)):
            for msg in reversed(messages):
                text = _extract_message_text(msg)
                if text:
                    return text
                if fallback is None:
                    if isinstance(msg, dict):
                        fallback = _coerce_text(
                            msg.get("reasoning_content")
                            or msg.get("thoughts")
                        )
                    elif isinstance(msg, AIMessage):
                        additional_kwargs = (
                            getattr(msg, "additional_kwargs", None) or {}
                        )
                        if isinstance(additional_kwargs, dict):
                            fallback = _coerce_text(
                                additional_kwargs.get("reasoning_content")
                                or additional_kwargs.get("thoughts")
                            )
                    else:
                        fallback = _coerce_text(
                            getattr(msg, "reasoning_content", None)
                        )

        choices = None
        if isinstance(result, dict):
            choices = result.get("choices")
        elif hasattr(result, "choices"):
            choices = getattr(result, "choices")

        if isinstance(choices, (list, tuple)) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                message = first_choice.get("message") or {}
                content = (
                    message.get("content")
                    if isinstance(message, dict)
                    else None
                )
                if not content:
                    content = first_choice.get("output_text") or first_choice.get(
                        "output"
                    )
                text = _coerce_text(content)
                if text:
                    return text
                if fallback is None:
                    fallback = _coerce_text(
                        first_choice.get("reasoning_content")
                        or first_choice.get("thoughts")
                    )
            else:
                message = getattr(first_choice, "message", None)
                text = _extract_message_text(message) or _extract_message_text(
                    first_choice
                )
                if text:
                    return text
                if fallback is None:
                    additional_kwargs = (
                        getattr(first_choice, "additional_kwargs", None) or {}
                    )
                    if isinstance(additional_kwargs, dict):
                        fallback = _coerce_text(
                            additional_kwargs.get("reasoning_content")
                            or additional_kwargs.get("thoughts")
                        )

        if fallback:
            return fallback

        if isinstance(result, dict):
            output = result.get("output") or result.get("text")
        else:
            output = getattr(result, "output", None) or getattr(
                result, "text", None
            )
        return _coerce_text(output)

    async def _force_reply(
        self,
        messages: list[BaseMessage],
        *,
        thread_id: str = "default",
    ) -> Optional[str]:
        """「该回却没回」时强制再生成一次。

        ``messages`` 可能是累积的多轮对话（[H1, A1, H2, A2, H3]），早先这里取
        ``messages[0]``，多轮之后会拿**第一轮的上下文**重新生成 —— 答非所问，
        而且这条路径只在沉默时触发，线上很难发现。所以取最后一条 HumanMessage。
        """
        base = ""
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                base = message.content if isinstance(message.content, str) else str(message.content)
                break
        if not base and messages:
            base = str(messages[0].content or "")
        retry_messages: list[BaseMessage] = [
            HumanMessage(content=f"{base}\n\n{_FORCE_REPLY_HINT}")
        ]
        try:
            result = await self._invoke_agent({"messages": retry_messages})
        except Exception as exc:
            logger.error(f"[会话 {thread_id}] 强制回复重试失败: {exc}")
            return None
        text = self._extract_ai_response(result)
        logger.info(
            f"[会话 {thread_id}] 强制回复重试结果: {(text or '空')[:100]}"
        )
        return text

    # ── 旧测试 / 外部代码用过的缓存辅助方法 ──
    def _make_tool_cache_key(self, tool_name: str, args: dict) -> tuple[str, str]:
        return make_tool_cache_key(tool_name, args)

    def _get_cached_tool_result(self, tool_name: str, args: dict) -> Any:
        return get_cached_tool_result(tool_name, args)

    def _set_cached_tool_result(
        self, tool_name: str, args: dict, result: Any
    ) -> None:
        return set_cached_tool_result(tool_name, args, result)

    def _record_tool_call(
        self, tool_name: str, args: dict, result=None, error=None
    ) -> None:
        return record_tool_call(tool_name, args, result=result, error=error)

    # ════════════════════ 工具 ════════════════════
    def _build_tools(self) -> list:
        return build_tools(self)

    # ════════════════════ 主对话 ════════════════════
    async def chat(
        self,
        user_message: str = "",
        thread_id: str = "default",
        extra_context: Optional[str] = None,
        event: dict = None,
        context: Optional[Any] = None,
        hint: str = "",
        action_check: bool = True,
    ) -> Optional[list[str]]:
        """与 Agent 对话（无状态；历史由 ``ChatContext`` 带入）。"""
        thread_id = thread_id or "default"

        context_text = ""
        if context is not None:
            context_text = context.render()
            user_text = context.current.text
            if not user_text and context.current.miniapp:
                user_text = context.current.miniapp
            is_dom = bool(context.current.sender.is_master)
        else:
            user_text = user_message or ""
            is_dom = user_text.startswith(_DOM_PREFIX)

        logger.info(f"[会话 {thread_id}] ═══ 收到用户消息 ═══")
        logger.info(
            f"[会话 {thread_id}] 内容: {user_text[:120]}{'...' if len(user_text) > 120 else ''}"
        )
        if context_text:
            try:
                logger.info(f"[会话 {thread_id}] 上下文: {context.summary()}")
            except Exception:
                pass
        elif extra_context:
            logger.info(f"[会话 {thread_id}] 上下文: {extra_context[:120]}")

        # 每轮隔离工具记录 / 缓存 / 用户原话 / 图片源
        _current_tool_calls_var.set([])
        _current_tool_cache_var.set({})
        _current_user_text_var.set(user_text)
        _current_context_var.set(context)
        if context is not None:
            _current_images_var.set(context.image_sources())
        else:
            _current_images_var.set(
                {"current": _image_urls(event), "quote": [], "history": {}}
            )

        relations = getattr(self, "relations", None)
        relation_text = ""
        if relations is not None and context is not None:
            try:
                relations.observe_context(context)
                relation_text = relations.render(context)
            except Exception as exc:  # noqa: BLE001 - 关系档案坏了不能阻断聊天
                logger.warning(f"[会话 {thread_id}] 关系档案读取失败: {exc}")

        def _get_calls():
            return _current_tool_calls_var.get()

        method_context = None
        parts: list[str] = []
        if context_text:
            parts.append(context_text)
        elif extra_context:
            parts.append(f"[当前上下文] {extra_context}")

        if relation_text:
            parts.append(relation_text)
            logger.info(
                f"[会话 {thread_id}] 注入关系档案 {len(relation_text)} 字"
            )

        if self.rw_tool:
            method_context = self.structed_method()
            if method_context:
                parts.append(f"<method>\n{method_context}\n</method>")

        if not context_text:
            parts.append(user_text)

        # 上一轮工具结果的去向：
        # - 用户这句话在承接（「挑几句 / 展开 / 具体说说」）→ **正文直接带上**，
        #   保证模型一定看得到，不赌它会不会调 recall_result；
        # - 其他情况 → 只给两三行索引，要不要取回由模型自己决定，不白占 token。
        vault = getattr(self, "result_vault", None)
        if vault is not None:
            # 工具里只能从 context 拼 key，这里可能拿到别的 thread_id，两个都试。
            keys = [thread_id]
            try:
                derived = thread_key(context)
            except Exception:  # noqa: BLE001
                derived = ""
            if derived and derived not in keys:
                keys.append(derived)

            entry = None
            for key in keys:
                if not key:
                    continue
                try:
                    entry = vault.get(key)
                except Exception as exc:  # noqa: BLE001 - 留存坏了不能挡聊天
                    entry = None
                    logger.debug(f"[会话 {thread_id}] 可复用结果读取失败: {exc}")
                if entry is not None:
                    break

            if entry is not None:
                if wants_followup(user_text):
                    parts.append(_render_vault_body(entry))
                    logger.info(
                        f"[会话 {thread_id}] 承接上一轮，自动带上 "
                        f"{entry.result_id}（{len(entry.text)} 字）"
                    )
                else:
                    hint = vault.render_hint(getattr(entry, "thread_id", "") or thread_id)
                    if hint:
                        parts.append(hint)
                        logger.info(
                            f"[会话 {thread_id}] 注入可复用结果索引 {len(hint)} 字"
                        )

        action_hints: list[str] = []
        if action_check:
            action_hint = build_action_hint(
                user_message=user_text,
                event=event,
                is_dom=is_dom,
                context=context,
            )
            if action_hint:
                action_hints.append(action_hint)
            meme_hint = build_meme_hint(user_text)
            if meme_hint:
                action_hints.append(meme_hint)
        if action_hints:
            combined_hint = "\n".join(action_hints)
            parts.append(f"[本轮动作检查]\n{combined_hint}")
            logger.info(
                f"[会话 {thread_id}] 注入动作检查: {combined_hint[:120]}"
            )

        catch_hint = catchphrase_hint(user_text, getattr(self, "catchphrases", ()))
        if catch_hint:
            parts.append(f"[本轮口头禅]\n{catch_hint}")
            logger.info(f"[会话 {thread_id}] 注入口头禅提示: {catch_hint[:120]}")

        if hint:
            parts.append(hint)
        elif context is not None and context.replies_to_me:
            parts.append(_REPLY_CHAIN_HINT)
            logger.info(f"[会话 {thread_id}] 续聊：对方回复了我上一条")

        full_text = "\n".join(parts)
        # 本轮身份：并发下两条消息同时在跑时，用它给多轮留存去重（不能用文本，
        # 连发两句一样的话会被误判成重复）。
        turn_token = ""
        if context is not None:
            turn_token = str(getattr(context.current, "message_id", "") or "")
        if not turn_token and event:
            turn_token = str(event.get("message_id") or "")

        arm_on, arm_reason = self._memory_arm(thread_id)
        memory = getattr(self, "thread_turns", None) if arm_on else None
        # 给 LLM 日志打标：会话 + 实验臂。事后统计命中率时直接按这两个字段分桶，
        # 不用再靠「离哪条 [会话] 行最近」去猜（judge 的调用还会混进来）。
        _llm_session_var.set(str(thread_id))
        _llm_arm_var.set("on" if arm_on else "off")
        logger.info(
            f"[会话 {thread_id}] 多轮 arm={'on' if arm_on else 'off'}（{arm_reason}）"
        )
        turns: list = []
        if memory is not None:
            try:
                turns = memory.get(thread_id)
            except Exception as exc:  # noqa: BLE001
                turns = []
                logger.warning(f"[会话 {thread_id}] 多轮留存读取失败: {exc}")

        incremental = ""
        if turns:
            # 已经有一段在跑：前缀（H1/A1/…）原样回放，本轮只追加增量。
            # 不能再渲染 <scene>/<history>，否则前缀每轮都变，缓存全废。
            incremental = (
                context.render_increment(since=getattr(turns[-1], "token", ""))
                if context is not None
                else full_text
            )
            messages = self._build_turn_messages(turns, incremental)
            logger.info(
                f"[会话 {thread_id}] 多轮模式：回放 {len(turns)} 轮 + 增量 "
                f"{len(incremental)} 字"
            )
        else:
            messages = [HumanMessage(content=full_text)]
        log_context = context_text or extra_context

        logger.info(f"[会话 {thread_id}] 正在请求 LLM ...")
        try:
            calls = _get_calls()

            async def _run_round(convo: list[BaseMessage]) -> dict:
                before = len(calls)
                result = await self._invoke_agent({"messages": convo})
                text = self._extract_ai_response(result)
                new_calls = calls[before:]
                parsed = parse_agent_output(text) if text is not None else None
                out = split_sendable_messages(parsed) if parsed else []
                if any(c.get("tool") == "send_message" for c in new_calls):
                    out = []
                return {
                    "result": result,
                    "text": text,
                    "parsed": parsed,
                    "out": out,
                    "calls": new_calls,
                }

            state = await _run_round(messages)
            result = state["result"]
            parsed = state["parsed"]
            messages_out = list(state["out"])

            # 这一段还留不留：模型在 <thread> 里说了算，漏写则由代码兜底。
            # 多轮留存没开时不用算（verdict 恒空，_remember_turn 也不会被调用）。
            if memory is None:
                verdict, verdict_src = "", "off"
            else:
                verdict, verdict_src = self._verdict_of(
                    parsed, turns=turns, user_text=user_text
                )
                if verdict:
                    logger.info(
                        f"[会话 {thread_id}] 多轮裁决={verdict or '未定'}"
                        f"（{verdict_src}，当前 {len(turns)} 轮）"
                    )

            silenced = any(
                c.get("tool") == "silent_observe" for c in state["calls"]
            )
            delivered = any(
                c.get("tool") in _ANSWER_TOOLS for c in state["calls"]
            )

            if silenced and (messages_out or delivered):
                logger.warning(
                    f"[会话 {thread_id}] 同时调用了 silent_observe 和回复，按回复处理"
                )
                silenced = False

            must_reply = context is not None and context.directed_at_me
            if must_reply and not messages_out and not delivered:
                logger.warning(
                    f"[会话 {thread_id}] 直接对话却选择了沉默，强制重新生成一次"
                )
                before = len(calls)
                retry_text = await self._force_reply(
                    messages, thread_id=thread_id
                )
                retry_calls = calls[before:]
                delivered = delivered or any(
                    c.get("tool") in _ANSWER_TOOLS for c in retry_calls
                )
                if retry_text is not None:
                    retry_parsed = parse_agent_output(retry_text)
                    retry_out = split_sendable_messages(retry_parsed)
                    if any(
                        c.get("tool") == "send_message" for c in retry_calls
                    ):
                        retry_out = []
                    if retry_out:
                        parsed = retry_parsed
                        messages_out = retry_out
                        silenced = False
                        state = {
                            **state,
                            "text": retry_text,
                            "parsed": retry_parsed,
                            "out": retry_out,
                        }

            if silenced and not messages_out and not delivered:
                logger.info(
                    f"[会话 {thread_id}] Agent 选择静默观察，不回复"
                )
                # 没接话也要认裁决：对方换了话题、这轮没吭声，下一轮不该还拖着
                # 上一段的上下文。
                self._drop_turns_for_new_topic(
                    memory, thread_id, turns, verdict
                )
                append_log(
                    build_log_entry(
                        thread_id=thread_id,
                        user_message=user_text,
                        ai_response="",
                        extra_context=log_context,
                        method_context=method_context,
                        tool_calls=calls or None,
                        error="静默观察",
                    )
                )
                return None

            # 续说：<next>继续</next>，或只敷衍一句 → 再补一轮
            convo: list[BaseMessage] = list(messages)
            extra = 0
            while (
                messages_out
                and extra < _MAX_CONTINUE_ROUNDS
                and len(messages_out) < _MAX_OUT_MESSAGES
            ):
                wants_next = (parsed or {}).get("next") == "continue"
                dodge = (
                    extra == 0
                    and not wants_next
                    and _looks_like_dodge(messages_out)
                )
                if not (wants_next or dodge):
                    break
                extra += 1
                convo.append(AIMessage(content=state["text"] or ""))
                convo.append(
                    HumanMessage(
                        content=(
                            _DODGE_CONTINUE_HINT if dodge else _CONTINUE_HINT
                        )
                    )
                )
                logger.info(
                    f"[会话 {thread_id}] 第 {extra} 次续说"
                    f"（{'敷衍未答到点上' if dodge else '<next>继续</next>'}）"
                )
                nxt = await _run_round(convo)
                if not nxt["out"]:
                    logger.info(
                        f"[会话 {thread_id}] 续说没有产生新内容，收尾"
                    )
                    break
                messages_out.extend(nxt["out"])
                state = nxt
                parsed = nxt["parsed"]
                result = nxt["result"]

            messages_out = messages_out[:_MAX_OUT_MESSAGES]

            # 给下一轮留好拼装材料（续说过 / 没回话都不记，见 _remember_turn）
            if memory is not None:
                self._remember_turn(
                    memory,
                    thread_id,
                    turns_before=turns,
                    full_text=full_text,
                    incremental=incremental,
                    ai_text=state.get("text") or "",
                    continued=extra > 0,
                    replied=bool(messages_out),
                    token=turn_token,
                    verdict=verdict if extra == 0 else "",
                )

            if parsed is not None:
                if messages_out:
                    messages_out = await self._de_repeat(
                        thread_id,
                        messages_out,
                        user_message=user_text,
                        is_dom=is_dom,
                    )
                    self._remember_replies(thread_id, messages_out)
                logger.info(
                    f"[会话 {thread_id}] LLM 返回成功，消息数: {len(getattr(result, 'get', lambda *_: [])('messages') or []) if isinstance(result, dict) else len(getattr(result, 'messages', []) or [])}"
                    f"，续说 {extra} 轮"
                )
                if parsed["status"]:
                    logger.debug(
                        f"[会话 {thread_id}] <status>: {parsed['status']}"
                    )
                    if relations is not None:
                        try:
                            relations.record_self_status(
                                parsed["status"], thread_id=thread_id
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug(f"[会话 {thread_id}] 自身状态保存失败: {exc}")
                if parsed["think"]:
                    logger.debug(
                        f"[会话 {thread_id}]  thinking: {parsed['think'][:200]}"
                    )
                for msg in messages_out:
                    logger.info(
                        f"[会话 {thread_id}] AI 回复: {msg[:120]}{'...' if len(msg) > 120 else ''}"
                    )
                if relations is not None and messages_out:
                    try:
                        relations.record_reply(context)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(f"[会话 {thread_id}] 关系回复时间保存失败: {exc}")
                    self._schedule_relation_extract(
                        context, messages_out, thread_id
                    )
                logger.info(f"[会话 {thread_id}] ═══ 对话结束 ═══")
                append_log(
                    build_log_entry(
                        thread_id=thread_id,
                        user_message=user_text,
                        ai_response="\n".join(messages_out),
                        extra_context=log_context,
                        method_context=method_context,
                        tool_calls=calls or None,
                        status=parsed["status"] or None,
                        think=parsed["think"] or None,
                        raw_response=parsed["raw"] or None,
                    )
                )
                return messages_out

            logger.info(
                f"[会话 {thread_id}] LLM 返回成功，但未解析到回复"
            )
            append_log(
                build_log_entry(
                    thread_id=thread_id,
                    user_message=user_text,
                    ai_response="",
                    extra_context=log_context,
                    method_context=method_context,
                    tool_calls=calls or None,
                    error="空回复",
                )
            )
            return None
        except Exception as exc:
            logger.error(f"[会话 {thread_id}] LLM 调用异常: {exc}")
            append_log(
                build_log_entry(
                    thread_id=thread_id,
                    user_message=user_text,
                    ai_response="",
                    extra_context=log_context,
                    method_context=method_context,
                    tool_calls=_get_calls() or None,
                    error=str(exc),
                )
            )
            return None

    # ════════════════════ 关系档案自动整理 ════════════════════
    def _schedule_relation_extract(
        self, context: Any, replies: list[str], thread_id: str
    ) -> Optional[asyncio.Task]:
        """低频异步整理：不阻塞本轮回复，失败也不影响聊天。"""
        if self._closed:
            return None
        store = getattr(self, "relations", None)
        if store is None or context is None or not getattr(store, "enabled", False):
            return None
        try:
            direct = bool(context.directed_at_me)
        except Exception:  # noqa: BLE001
            direct = True
        if not store.should_extract(thread_id, direct=direct):
            return None
        try:
            task = asyncio.get_running_loop().create_task(
                self._extract_relations(context, replies, thread_id),
                name=f"relation-extract:{thread_id}",
            )
        except RuntimeError:  # pragma: no cover - chat() 一定在事件循环里
            return None
        self._relation_tasks.add(task)
        task.add_done_callback(self._relation_tasks.discard)
        return task

    def close(self) -> None:
        """停止接受新的关系整理任务，并取消仍在运行的任务。"""
        self._closed = True
        tasks = list(getattr(self, "_relation_tasks", ()))
        self._relation_tasks.clear()
        for task in tasks:
            task.cancel()

    async def _extract_relations(
        self, context: Any, replies: list[str], thread_id: str
    ) -> None:
        store = getattr(self, "relations", None)
        if store is None:
            return
        try:
            prompt = store.build_extract_prompt(context, replies)
            message = await self._invoke_judge(
                [
                    SystemMessage(content=_RELATION_EXTRACT_SYSTEM),
                    HumanMessage(content=prompt),
                ]
            )
            raw = getattr(message, "content", "") or ""
            result = store.apply_extraction(raw, context)
            logger.info(f"[关系] {thread_id} 自动整理: {result}")
        except Exception as exc:  # noqa: BLE001 - 自动整理失败必须静默
            logger.debug(f"[关系] {thread_id} 自动整理失败（已忽略）: {exc}")

    # ════════════════════ 主动插话 ════════════════════
    async def maybe_interject(
        self,
        context: Any,
        thread_id: str = "default",
        *,
        reason: str = "",
    ) -> Optional["InterjectPlan"]:
        """没人叫它时，自己判断要不要说、以什么姿态说。

        返回 :class:`InterjectPlan`；``act`` 是 silent 时返回 None。
        ``nod`` 不动模型：它的语义就是「只表示在场，不打字」，让模型再生成一遍纯属浪费。
        """
        if context is None:
            return None
        tag = f"群 {context.group_id}" if context.group_id else "插话"
        # 旁听视角：<tail> 不是「要回的那条」，另带 <me> 自省和 <pulse> 局面。
        # 没有这个方法时退回普通渲染（兼容只实现了 render() 的上下文对象）。
        renderer = getattr(context, "render_as_bystander", None)
        rendered = renderer() if callable(renderer) else context.render()

        try:
            verdict_msg = await self._invoke_judge(
                [
                    SystemMessage(content=_INTERJECT_JUDGE_SYSTEM),
                    HumanMessage(content=rendered),
                ]
            )
            verdict = getattr(verdict_msg, "content", "") or ""
        except Exception as exc:
            logger.error(f"[插话] {tag} 判断失败: {exc}")
            return None

        plan = parse_interject_verdict(verdict)
        # 把 judge 的原话也记下来：只看解析结果的话，
        # 「日志说沉默、理由却说『我可以回答』」这种自相矛盾根本没法查。
        raw_preview = " ".join(verdict.split())
        if len(raw_preview) > 200:
            raw_preview = raw_preview[:200] + "…"
        if plan.act == "silent":
            logger.info(
                f"[插话] {tag} 判断为沉默（{reason or '触发'}）"
                + (f"：{plan.why}" if plan.why else "")
                + f"｜原文：{raw_preview}"
            )
            if _SILENT_BUT_ANSWERABLE.search(plan.why or ""):
                logger.warning(
                    f"[插话] {tag} 动作和理由打架：判了沉默，理由却说能答。"
                    f"原文：{raw_preview}"
                )
            return None

        logger.info(
            f"[插话] {tag} 判断为 {plan.act}（{reason or '触发'}）"
            + (f"：{plan.why}" if plan.why else "")
            + f"｜原文：{raw_preview}"
        )
        if plan.act == "nod":
            return plan

        try:
            replies = await self.chat(
                context=context,
                thread_id=thread_id,
                hint=interject_hint(plan.act),
                action_check=False,
            )
        except Exception as exc:
            logger.error(f"[插话] {tag} 生成失败: {exc}")
            return None
        if isinstance(replies, str):
            replies = [replies]
        plan.messages = [str(item) for item in (replies or []) if item]
        return plan if plan.messages else None


__all__ = [
    "QQBotAgent",
    "LLMCallbackHandler",
    "MODEL_TYPE",
]
