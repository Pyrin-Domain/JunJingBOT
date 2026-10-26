"""Bot 服务：搬运 / 去重 / 区间任务 / 插话。

对外只需要::

    from bot.services import (
        ForwardService, ForwardDedupStore, RangeTaskService, InterjectService,
        build_fingerprint, simhash, clean_group_ids, dedup_log,
    )
"""

from __future__ import annotations

from .dedup import (
    BAND_BITS,
    BAND_COUNT,
    CLAIM_FRESH_SECONDS,
    DEFAULT_DB_PATH,
    DEFAULT_LOG_PATH,
    HASH_HEX_LEN,
    IMG_HASH_HEX_LEN,
    LOG_ENABLED,
    LOG_PATH,
    SIMHASH_BITS,
    SIM_ALGO,
    TEXT_PER_IMAGE,
    TOKEN_WEIGHT_CAP,
    ForwardDedupStore,
    build_fingerprint,
    clean_group_ids,
    clean_image_hashes,
    dedup_log,
    empty_fingerprint,
    event_snapshot,
    fingerprint_items,
    fingerprint_lines,
    fingerprint_of,
    forward_content_items,
    forward_content_lines,
    get_miniapp_info,
    get_multimsg_detail,
    get_multimsg_resid,
    get_old_forward_xml_info,
    hamming_hex,
    load_settings as load_dedup_settings,
    normalize_line,
    simhash,
    simhash64,
    simhash_hex,
    split_bands,
    tokenize,
)
from .forward import ForwardService
from .interject import GroupState, InterjectService
from .range_task import RangeTaskService

__all__ = [
    "ForwardService",
    "ForwardDedupStore",
    "RangeTaskService",
    "InterjectService",
    "GroupState",
    # 指纹算法 / 常量
    "simhash",
    "simhash64",
    "simhash_hex",
    "fingerprint_of",
    "fingerprint_lines",
    "fingerprint_items",
    "build_fingerprint",
    "empty_fingerprint",
    "tokenize",
    "split_bands",
    "hamming_hex",
    "clean_group_ids",
    "clean_image_hashes",
    "normalize_line",
    "forward_content_items",
    "forward_content_lines",
    # 事件 / 卡片解析
    "event_snapshot",
    "get_miniapp_info",
    "get_multimsg_detail",
    "get_multimsg_resid",
    "get_old_forward_xml_info",
    "dedup_log",
    "load_dedup_settings",
    # 常量
    "SIMHASH_BITS",
    "BAND_BITS",
    "BAND_COUNT",
    "HASH_HEX_LEN",
    "IMG_HASH_HEX_LEN",
    "TOKEN_WEIGHT_CAP",
    "SIM_ALGO",
    "CLAIM_FRESH_SECONDS",
    "TEXT_PER_IMAGE",
    "DEFAULT_DB_PATH",
    "DEFAULT_LOG_PATH",
    "LOG_ENABLED",
    "LOG_PATH",
]
