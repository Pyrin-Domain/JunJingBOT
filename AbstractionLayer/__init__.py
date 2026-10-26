"""协议差异抽象层（NapCat / SnowLuma 共用一套上层代码）。

它解决的问题只有一个：**同一个 OneBot v11 动作，两个后端的返回语义不一样**，
而业务层不应该知道这件事。实测差异见 :mod:`AbstractionLayer.kernal` 的模块文档。

三个入口::

    from AbstractionLayer import OneBotKernel, HistoryPager, ForwardExpander

    kernel = OneBotKernel.http("http://127.0.0.1:3000", token, backend="napcat")
    await kernel.detect(group_id=20010)      # 实测能力，不靠猜

    pager = HistoryPager(kernel)
    walk = await pager.walk(group_id, start=latest_event, stop_at=checkpoint_event)
    # walk.messages 就是「断点之后」的消息，升序，直接回放

    expander = ForwardExpander(kernel)
    expansion = await expander.expand(forward_id)
    # expansion.messages 是摊平后的叶子消息，NapCat/SnowLuma 一样用

    relay = ForwardRelay(kernel)
    content = await relay.collect(forward_id)
    # content.nodes 可直接交给 send_group_forward_msg，嵌套聊天记录也打得开

想直接命令行调试::

    python -m AbstractionLayer.cli probe   --backend napcat
    python -m AbstractionLayer.cli history --backend snowluma --group 20010
    python -m AbstractionLayer.cli recover --backend napcat --group 20010 --from 184
    python -m AbstractionLayer.cli forward --backend napcat --id 7700868210009142942
    python -m AbstractionLayer.cli diff    --group 20010
"""

from __future__ import annotations

from .cursor import MessageCursor, as_int, same_value
from .forward import (
    ForwardEntry,
    ForwardExpander,
    ForwardExpansion,
    extract_forward_id,
    forward_count_from_card,
)
from .history import HistoryPage, HistoryPager, WalkResult
from .kernal import (
    BACKEND_PRESETS,
    DEFAULT_ENDPOINTS,
    GENERIC,
    NAPCAT,
    SNOWLUMA,
    ApiResult,
    BackendCapabilities,
    CallableTransport,
    HttpTransport,
    OneBotKernel,
    Transport,
    capabilities_for,
    default_token,
)
from .relay import (
    ForwardRelay,
    RelayContent,
    RelayPlan,
    build_node,
    has_nested_forward,
    has_opaque_nested,
    is_opaque_multimsg,
    plan_relay,
    segments_of,
    titles_of_multimsg,
)

__all__ = [
    "ApiResult",
    "BACKEND_PRESETS",
    "BackendCapabilities",
    "CallableTransport",
    "DEFAULT_ENDPOINTS",
    "ForwardEntry",
    "ForwardExpander",
    "ForwardExpansion",
    "ForwardRelay",
    "GENERIC",
    "HistoryPage",
    "HistoryPager",
    "HttpTransport",
    "MessageCursor",
    "NAPCAT",
    "OneBotKernel",
    "RelayContent",
    "RelayPlan",
    "SNOWLUMA",
    "Transport",
    "WalkResult",
    "as_int",
    "build_node",
    "capabilities_for",
    "default_token",
    "extract_forward_id",
    "forward_count_from_card",
    "has_nested_forward",
    "has_opaque_nested",
    "is_opaque_multimsg",
    "plan_relay",
    "titles_of_multimsg",
    "same_value",
    "segments_of",
]
