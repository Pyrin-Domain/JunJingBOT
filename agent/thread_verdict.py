"""多轮留存的裁决：这一段对话还要不要接着带下去。

为什么需要裁决
--------------
``ThreadTurns`` 把第一轮的完整上下文冻结下来，之后每轮只追加增量，前缀稳定、
服务端缓存才有机会命中。代价是**这段对话会一直跟着模型**，直到 8 轮 / 30 分钟
到期。群里换了话题之后还拖着上一段，模型就会拿旧前提接新话。

谁来裁决
--------
模型自己。它读的是整段对话，比任何前置判断器（只看当前消息 + 一小段历史）都
更清楚「这句是不是接着刚才」。代价是零——``<thread>`` 只是它本来就要产出的
几个 token，不用多跑一次小模型、也不多一轮延迟。

为什么必须有代码兜底
--------------------
小模型在长提示 + 多工具下漏写标签是常态（``parse_agent_output`` 里那一堆容错
就是为此写的）。所以：

* 模型写了 → 听模型的；
* 模型没写 → 用这里的 ``resolve_verdict`` 按**代码信号**算一个默认值。

默认值是「留」，不是「丢」
--------------------------
丢错的代价是不可逆的：用户说「挑几句」，模型手上没东西，只能重翻或者编。
留错的代价只是多花几千 token（而且只在上限内）。两者不对称，所以拿不准就留。
真正判「丢」只靠三个硬信号：模型明确说新话题、隔了太久没说话、对方话里带着
「换个话题 / 说点别的」这种明确切换语。

「隔太久」用 ``new_after`` 判，比 ``ThreadTurns.ttl`` 更短：TTL 是「这段还能不能
用」，这里是「这段还该不该带」，后者更严格。
"""

from __future__ import annotations

import re
import time
from typing import Any, Iterable

__all__ = [
    "VERDICT_KEEP",
    "VERDICT_NEW",
    "resolve_verdict",
]

#: 接着上一段
VERDICT_KEEP = "keep"
#: 丢掉前面那段，从这条重新开始
VERDICT_NEW = "new"

#: 对方明确说要换话题。只收「切换」意图，不收「追问」——追问要留。
_NEW_TOPIC_RE = re.compile(
    r"换个话题|换一个话题|换话题|说(?:点|个)别的|不说这个|不提这个|不提了|"
    r"别提这个|不聊这个|不聊了|算了不聊|另一个事|另外(?:一)?件事|"
    r"重新开始|从头(?:说|讲|来)"
)

#: 明确指向上一段的说法。命中就一定留（也是日志里的判据来源）。
_REFERENCE_RE = re.compile(
    r"这(?:个|些|条|句|事|样)?|那(?:个|些|条|句|事|样)?|它|"
    r"上面|刚才|刚说|前面|之前|继续|接着|还有|"
    r"你(?:说|刚|上)|原话|挑|展开|具体|详细|举例"
)


def resolve_verdict(
    model_verdict: str,
    *,
    turns: Iterable[Any] = (),
    user_text: str = "",
    now: float | None = None,
    new_after: float = 0.0,
) -> tuple[str, str]:
    """决定这一段留不留，返回 ``(verdict, source)``。

    ``source`` 只用于日志（``model`` / ``default:gap`` / …），不影响行为。
    """
    turns = list(turns)

    if model_verdict in (VERDICT_KEEP, VERDICT_NEW):
        return model_verdict, "model"

    if not turns:
        return VERDICT_NEW, "default:empty"

    moment = time.time() if now is None else float(now)
    last_at = float(getattr(turns[-1], "at", 0) or 0)
    if new_after > 0 and last_at and moment - last_at > new_after:
        return VERDICT_NEW, "default:gap"

    text = str(user_text or "")
    if _NEW_TOPIC_RE.search(text):
        return VERDICT_NEW, "default:new-topic"
    if _REFERENCE_RE.search(text):
        return VERDICT_KEEP, "default:reference"
    return VERDICT_KEEP, "default:keep"
