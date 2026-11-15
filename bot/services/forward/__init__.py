"""自动搬运：识别（detect）→ 分派（record / image）→ 发送。

对外只有 :class:`ForwardService`：``forward(event, **auto_forward_kwargs)``，
返回 True 表示这条消息已按搬运逻辑处理完毕（不是「搬成功了」）。

    from bot.services.forward import ForwardService, ForwardJob, GateOutcome
"""

from __future__ import annotations

from .job import (
    FORWARD_KINDS,
    KIND_IMAGE,
    KIND_MINIAPP,
    KIND_RECORD,
    ForwardJob,
    GateOutcome,
    RecoveryBatch,
    StagedForward,
)
from .service import ForwardService

__all__ = [
    "ForwardService",
    "ForwardJob",
    "GateOutcome",
    "RecoveryBatch",
    "StagedForward",
    "KIND_RECORD",
    "KIND_MINIAPP",
    "KIND_IMAGE",
    "FORWARD_KINDS",
]
