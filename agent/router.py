"""模型分级路由：主回答模型 / 插话判断器 / 429 降级。

从旧 ``Agent/model_router.py`` 原样搬运。

一个 `ModelRouter` 持有一串「候选模型」（主模型在前，备用模型在后），
每次调用都从第一个可用的开始；某个模型返回 429（限流 / 额度用尽）时，
把它标记成冷却状态，立刻换下一个候选模型重试，这样群里的对话不会因为
某个免费额度用光就整段哑掉。

只负责「按顺序换模型 + 冷却」，不依赖 langchain：
`ModelRoute` 里同时放 ChatOpenAI 实例（轻量调用）和 create_agent 建好的图
（带工具的完整对话），由 `use_agent` 决定用哪个。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Optional

# 某个模型 429 之后，多久之内不再优先选它（秒）
RATE_LIMIT_COOLDOWN_SECONDS = 60.0

# 有些 429 不是「这一分钟太挤」，而是「今天的免费额度用完了」：
# OpenRouter 的 free-models-per-day、各家 free tier 的日额度都是这种。
# 这类错误 60 秒后重试必然还是 429，冷却得拉长到下一个额度周期，
# 否则每分钟都会白撞一次再降级（日志噪音 + 多一次往返延迟）。
QUOTA_COOLDOWN_SECONDS = 30 * 60.0

# 429 不一定长成 HTTP 429：各家 SDK / 代理会把错误包成不同形状，
# 这里同时看 status_code 和错误文本，宁可多认几种也别把可降级的错误漏掉。
_RATE_LIMIT_TOKENS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "too many requests",
    "insufficient_quota",
    "quota exceeded",
    "throttl",
)

# 额度类 429 的特征：这些不是「稍后再试」，而是「今天别试了」
_QUOTA_TOKENS = (
    "per day",
    "per-day",
    "daily",
    "insufficient_quota",
    "quota exceeded",
    "exceeded your current quota",
    "credits",
    "billing",
    "free-models-per",
    "余额",
    "额度",
)

# 文本里的 429 必须独立成数：`message_id 429123` 这种只是恰好含 429，
# 不是限流，误判成 429 会让正常的参数错误被静默降级掉。
_RATE_LIMIT_CODE_RE = re.compile(r"(?<!\d)429(?!\d)")


def is_rate_limit_error(exc: BaseException) -> bool:
    """这个异常是不是「这个模型暂时用不了，换一个就好」的 429。"""
    for attr in ("status_code", "http_status", "code"):
        value = getattr(exc, attr, None)
        if value == 429 or str(value) == "429":
            return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    if _RATE_LIMIT_CODE_RE.search(text):
        return True
    return any(token in text for token in _RATE_LIMIT_TOKENS)


def is_quota_error(exc: BaseException) -> bool:
    """这个 429 是不是额度用完了（日额度 / 余额），而不是「这一分钟太挤」。

    只对已经被判定成 429 的异常有意义；额度类错误冷却时间要长得多。
    """
    text = str(exc).lower()
    return any(token in text for token in _QUOTA_TOKENS)


@dataclass
class ModelRoute:
    """一条候选模型：key 用于冷却记录，label 用于日志。"""

    key: str
    label: str
    llm: Any = None
    agent: Any = None

    def target(self, use_agent: bool) -> Any:
        if use_agent and self.agent is not None:
            return self.agent
        return self.llm if self.llm is not None else self.agent


class ModelRouter:
    """按顺序调用候选模型；429 时自动降级到下一个。"""

    def __init__(
        self,
        routes: list[ModelRoute],
        *,
        cooldown_seconds: float = RATE_LIMIT_COOLDOWN_SECONDS,
        quota_cooldown_seconds: float = QUOTA_COOLDOWN_SECONDS,
        logger: Optional[Any] = None,
        role: str = "主模型",
    ):
        self.routes = [route for route in routes if route.target(False) is not None]
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.quota_cooldown_seconds = max(0.0, float(quota_cooldown_seconds))
        self.logger = logger
        self.role = role
        self._limited_until: dict[str, float] = {}
        self._last_used: Optional[str] = None

    # ── 状态 ──
    @property
    def labels(self) -> list[str]:
        return [route.label for route in self.routes]

    def cooldown_left(self, route: ModelRoute) -> float:
        remaining = self._limited_until.get(route.key, 0.0) - time.monotonic()
        return max(0.0, remaining)

    def _log(self, level: str, message: str) -> None:
        if self.logger is None:
            return
        log = getattr(self.logger, level, None) or getattr(self.logger, "info", None)
        if log is not None:
            log(message)

    def _ordered(self) -> list[ModelRoute]:
        """没在冷却的模型优先；全都在冷却时退回原始顺序（尽力再试一次）。"""
        available = [route for route in self.routes if self.cooldown_left(route) <= 0]
        return available or list(self.routes)

    def mark_rate_limited(self, route: ModelRoute, exc: Optional[BaseException] = None) -> float:
        """标记冷却，返回这次冷却多少秒（额度类错误冷却更久）。"""
        seconds = self.quota_cooldown_seconds if exc is not None and is_quota_error(exc) else self.cooldown_seconds
        self._limited_until[route.key] = time.monotonic() + seconds
        return seconds

    # ── 调用 ──
    async def ainvoke(
        self,
        payload: Any,
        *,
        use_agent: bool = True,
        can_fallback: Optional[Any] = None,
    ) -> Any:
        """依次尝试候选模型；非 429 的异常直接抛出（不掩盖真正的 bug）。

        :param can_fallback: 可选的无参回调；返回 False 时遇到 429 不再换模型，
            直接把异常抛出去。用来避免「工具已经发了消息，换模型重跑又发一遍」。
        """
        routes = self._ordered()
        if not routes:
            raise RuntimeError(f"{self.role}没有可用的模型配置")

        last_error: Optional[BaseException] = None
        for index, route in enumerate(routes):
            target = route.target(use_agent)
            if target is None:
                continue
            try:
                result = await target.ainvoke(payload)
            except Exception as exc:
                if not is_rate_limit_error(exc):
                    raise
                last_error = exc
                seconds = self.mark_rate_limited(route, exc)
                if can_fallback is not None and not can_fallback():
                    self._log(
                        "warning",
                        f"[模型降级] {self.role}「{route.label}」返回 429，"
                        f"但本轮已经执行过有副作用的工具，跳过降级以免重复发送",
                    )
                    raise
                if index + 1 < len(routes):
                    next_label = routes[index + 1].label
                    self._log(
                        "warning",
                        f"[模型降级] {self.role}「{route.label}」返回 429"
                        f"（冷却 {seconds:.0f}s），改用「{next_label}」重试",
                    )
                else:
                    self._log(
                        "warning",
                        f"[模型降级] {self.role}「{route.label}」返回 429，"
                        f"没有更多备用模型可用了",
                    )
                continue
            if route.key != self._last_used:
                if self._last_used is not None:
                    self._log("info", f"[模型降级] {self.role}已切换到「{route.label}」")
                self._last_used = route.key
            return result

        assert last_error is not None
        raise last_error


__all__ = [
    "QUOTA_COOLDOWN_SECONDS",
    "RATE_LIMIT_COOLDOWN_SECONDS",
    "ModelRoute",
    "ModelRouter",
    "is_quota_error",
    "is_rate_limit_error",
]
