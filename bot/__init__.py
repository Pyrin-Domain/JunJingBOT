"""业务编排层。

只暴露“编排会用到的件”：命令门面、关键词构造、断点工具、各个服务。
真正的流程在 ``bot.pipeline``，组装在 ``bot.app``（由主代理负责）。

    from bot import ForwardService, CommandHandlers, build_patterns
"""

from __future__ import annotations

from .checkpoint import (
    checkpoint_dir,
    checkpoint_file,
    load as load_checkpoint,
    save as save_checkpoint,
    snapshot as snapshot_checkpoint,
)
from .commands import CommandHandlers, parse_range
from .commands.registry import ACTION_NAMES, build_action_registry
from .keyword import build_patterns, match as match_keywords
from .services import (
    ForwardDedupStore,
    ForwardService,
    GroupState,
    InterjectService,
    RangeTaskService,
    build_fingerprint,
    clean_group_ids,
    dedup_log,
    simhash,
)

__all__ = [
    # 服务
    "ForwardService",
    "ForwardDedupStore",
    "RangeTaskService",
    "InterjectService",
    "GroupState",
    # 命令
    "CommandHandlers",
    "parse_range",
    "build_action_registry",
    "ACTION_NAMES",
    # 关键词
    "build_patterns",
    "match_keywords",
    # 断点
    "checkpoint_dir",
    "checkpoint_file",
    "load_checkpoint",
    "save_checkpoint",
    "snapshot_checkpoint",
    # 指纹工具
    "build_fingerprint",
    "simhash",
    "clean_group_ids",
    "dedup_log",
]
