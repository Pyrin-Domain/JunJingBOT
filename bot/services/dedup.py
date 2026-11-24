# -*- coding: utf-8 -*-
"""转发"搬运"去重：SimHash 文本指纹 + SQLite 存储

群里的「聊天记录 / 小程序卡片」经常被不同的人反复搬运，bot 在 auto_forward 中
会一次次重复转发（刷屏）。这里只从事件本身提取 **与转发者无关** 的文本喂给
SimHash，判定开销接近 0；库里只存 8 字节指纹 + 8 字节内容哈希，不落原文。

判定顺序（开销从低到高，见 ForwardDedupStore._find_sync）：
   a. content_hash 精确命中 → 相似度 1.0（走索引，最快）
   b. SimHash 16bit x4 分桶取候选（分桶只是召回：距离 <= 3 才能保证 4 个分桶里必有
      一个完全相同，阈值 8 允许的 4~8 距离不保证被召回，宁可漏也不误判）
      → 算汉明距离，阈值内即判定"搬过"（只有同版本 sim_algo 的行参与）
   c. 图片双哈希：weak 指纹（正文文字数 < 图片数 x text_per_image）**必须**过这一关，
      非 weak 只在上面两步都没命中时当兜底

   weak 那一路是强制的：两边任何一边拿不到图片就不算"搬过"（宁可多搬一次），
   否则「少图模板聊天记录」那种文本一模一样的转发会被文本判定误认成同一份。

几件必须守住的约定
------------------
* **昵称/媒体不进指纹**：摘要里一行规整成 `浅奈: [图片]`（各种媒体占位统一成
  [图片]，见 normalize_line），于是纯图转发的指纹就是
  `old_forward|tsum9|浅奈: [图片]|...`——"谁 + 几张图 + 总数"本身就是区分特征。
  昵称是 NapCat 渲染的，同一条记录回查两次可能给出完全不同的名字（线上实例：
  一次"焚风"、一次全渲染成"QQ用户"），所以**有正文**的转发一律把昵称隐成
  `user`（见 mask_senders）。resid 每转发一次都会重新生成，只用来回查内容。
* **weak 指纹**（`正文文字数 < 图片数 x text_per_image`，缺省 15，见 is_text_sparse /
  is_text_sparse_bodies）：只认"文本完全一致"，不做模糊相似——否则不同人的图片刷屏
  会被相似度误归成同一份。这里的"正文"不含昵称：回查拿到的 sender 与 raw_message
  本来就是两个字段，一律分开处理（见 forward_content_items）——昵称里带冒号时靠
  正则反解 `名字: 正文` 会切错边界，正文凭空多出十几个字，weak 判据直接翻面。
  这类文本区分度太低（模板聊天记录常见"文本相当、图完全不是一批"），所以判定
  **必须**过图片这一关：把转发里的图下载下来算 aHash+dHash（image_hash.py）存进
  `forward_image` 表，要求**本次这批图**能在历史里找到（见 `_image_match_ratio`）；
  两边任何一边拿不到图就一律判"没搬过"。文本够多（非 weak）时图片只当兜底。
* **group_ids** = 这条内容「被标记的群聊」（纯数字、逗号分隔，同一份内容只有一行，
  集合一直累积）= 搬进去过的目标群 ∪ 搬出来的来源群。判定是**内容级**的三种结果：
  - 没搬过 → 搬运，并把目标群 + 来源群一起标记进库；
  - 搬过了 + 本次事件所在群在标记里 → 发【发过了喵】，结束；
  - 搬过了 + 不在标记里 → 不搬运也不提示，只把该群补进 `group_ids`（下次再来就
    发【发过了喵】）。
  所以同一份内容在多个群里重复出现只会被搬一次。两种群都得标：只标目标群，
  "原来那个群又发了一遍"就认不出来；只标来源群，"已经搬进过哪几个群"又无从得知。
* **排查日志**默认开（config/config.json 里设 `log_enabled: false` 关掉），一次判定一行
  JSON：原始事件里有用的那几段（含 raw）、提取出的指纹、判定结论，以及图片那
  一路的 url / 命中缓存与否 / 下载失败原因（http_403、超时、解码失败…）。

配置
----
可在 config/config.json 中可选添加（缺省用下面的 _DEFAULTS）。相对路径的
db_path / log_path 只当文件名看，实际落在 data/ 目录：

    "forward_dedup": {
        "db_path": "forward_dedup.db",          # -> data/forward_dedup.db
        "hamming_threshold": 8,
        "min_tokens": 6,
        "token_ratio": 0.6,
        "text_per_image": 15,
        "keep_days": 180,
        "max_rows": 200000,
        "img_hamming_threshold": 20,
        "img_match_ratio": 0.6,
        "img_max_count": 5,
        "img_timeout": 8.0,
        "img_max_bytes": 16777216,
        "clean_enable": true,          # 入库清洗总开关
        "clean_scan_tail": 3,          # 末尾几条算广告区
        "clean_drop_promo": true,      # 装扮/气泡分享卡片（不看位置）
        "clean_drop_qr": true,         # 末尾二维码海报（要下载解码）
        "clean_drop_group_card": true,
        "clean_drop_link": true,
        "clean_image_timeout": 3.0,
        "img_max_bytes": 16 * 1024 * 1024,
        # 入库清洗（见 _DEFAULTS 里的同名项）
        "clean_enable": true,
        "clean_scan_tail": 3,
        "clean_drop_promo": true,
        "clean_drop_qr": true,
        "clean_drop_group_card": true,
        "clean_drop_link": true,
        "clean_image_timeout": 3.0,
        # 视频指纹：封面帧 + 第10帧（64 hex），字节 SHA-256 只在文件不超过
        # video_byte_max_bytes 时补算；视频被转码后字节会变，不能当唯一判据。
        "video_hamming_threshold": 32,
        "video_match_ratio": 0.6,
        "video_max_count": 5,
        "video_timeout": 15.0,
        "video_byte_max_bytes": 8 * 1024 * 1024,
        "log_enabled": true,
        "log_path": "forward_dedup_log.jsonl"   # -> data/forward_dedup_log.jsonl
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
import threading
import time
import zlib
from collections import Counter
from pathlib import Path

from config.paths import AppPaths
from core.logging import get_logger

log = get_logger("bot.services.dedup")

# 模块导入时的默认布局（不建目录、不读配置）；真正运行时由 settings.paths 决定。
_PATHS = AppPaths.discover()
# 数据库 / 日志在 data/，配置在 config/（旧根目录布局仍然认）
DEFAULT_DB_PATH = _PATHS.data_file("forward_dedup.db")
DEFAULT_LOG_PATH = _PATHS.data_file("forward_dedup_log.jsonl")
CONFIG_PATH = _PATHS.config_file("config.json")


def _data_path(value, default: Path, settings=None) -> Path:
    """相对路径只当文件名看 → 落到 data/；绝对路径原样用。"""
    if not value:
        return Path(default)
    path = Path(str(value))
    if path.is_absolute() or path.parent != Path("."):
        return path
    if settings is not None:
        try:
            return Path(settings.paths.data_file(path.name))
        except Exception:
            pass
    return _PATHS.data_file(path.name)

SIMHASH_BITS = 64
BAND_BITS = 16
BAND_COUNT = SIMHASH_BITS // BAND_BITS  # 4
HASH_HEX_LEN = 16  # blake2b(digest_size=8) -> 16 位 hex = 8 字节

# SimHash 投票时每个词最多算几票。指纹里 `user:` / `[图片]` 这类逐条重复的结构词
# 占比很高（一条 82 条的转发里它们占 16% 的 token），不封顶时几乎决定了全部 64
# 位——把正文全换成别的字、只留骨架，距离也只有 4~7，于是两段毫不相干的聊天
# 会撞成“同一份”。封顶后不相关文本的距离从 3 拉开到 10 以上（见 _simhash_row）。
TOKEN_WEIGHT_CAP = 2

# SimHash 算法版本，存在 forward_fingerprint.sim_algo 里。改过 simhash 算法就必须
# 递增：老行的 simhash/分桶是按旧算法算的，和新行不可比，混在一起算距离会撞车。
# 判定时只拿同版本的行来比（老行仍参与 content_hash 与图片判定）。
SIM_ALGO = 2

IMG_HASH_HEX_LEN = 32  # aHash(64bit) + dHash(64bit) -> 32 位 hex（见 image_hash.py）
VIDEO_SIG_HEX_LEN = 64  # cover(32) + frame10(32)（见 video_hash.py）
VIDEO_BYTE_HASH_HEX_LEN = 64  # sha256

# 命中记录是这个时间窗内刚写进去的 → 极可能是同一份内容的两条来源消息几乎同时到达
CLAIM_FRESH_SECONDS = 90

_DEFAULTS = {
    "db_path": str(DEFAULT_DB_PATH),
    # 64bit 中允许翻转的位数。8 是配合 simhash64 的封顶权重（TOKEN_WEIGHT_CAP）
    # 量出来的：「毫不相干」的两两距离实测最近也有 10，而「同一条记录少收/多收 1 条
    # 消息」的中位数在 7~8 —— 8 正好卡在两簇中间。改了任一算法都要重新量。
    "hamming_threshold": 8,
    "min_tokens": 6,  # 词数太少则不做相似度判断（只做精确命中）
    "token_ratio": 0.6,  # 两次指纹词数比例下限，避免长短文本互撞
    # weak 判据（见 is_text_sparse）：正文文字数 < 图片数 x 这个系数就转图片判定
    "text_per_image": 15,
    "keep_days": 180,  # 超过该天数的记录会被清理，0/None 表示不清理
    "max_rows": 200000,  # 最多保留的记录条数，0/None 表示不限制
    # 图片双哈希（只在 weak 指纹上启用）
    "img_hamming_threshold": 20,  # 128bit 双哈希允许的差异位数(约 0.84 相似)
    "img_match_ratio": 0.6,  # 两份转发里"对得上"的图片比例下限
    "img_max_count": 5,  # 一次最多下载几张图
    "img_timeout": 8.0,  # 单张图下载超时（秒）
    # 单张图允许下载的最大字节数。原图/截图常常 4MB 以上，卡太小会让 weak 指纹
    # 直接没有媒体锚点（线上：4.2MB 截图算不出双哈希 → 同一张图被搬两次）。
    "img_max_bytes": 16 * 1024 * 1024,
    # 入库清洗：拼指纹 / 算媒体哈希之前，把广告和占位卡片从转发内容里去掉。
    # 只影响「判定」，不影响发出去的样子（发送侧另有 auto_forward.strip_tail_ads）。
    "clean_enable": True,
    "clean_scan_tail": 3,          # 末尾扫几条
    "clean_drop_promo": True,      # 装扮/气泡分享卡片（任意位置都丢）
    "clean_drop_qr": True,         # 末尾图片里的二维码海报（要下载解码，可关）
    "clean_drop_group_card": True, # 末尾的群推荐卡片
    "clean_drop_link": True,       # 末尾的纯链接 / 吆喝 + 链接
    "clean_image_timeout": 3.0,    # 二维码检测的单图下载超时（秒）
    "clean_qr_images": 3,          # 整条记录最多下载几张图做二维码检测
    # 视频指纹（只在 weak 指纹上启用）：封面帧 + 第 10 帧，必要时补全量字节哈希
    "video_hamming_threshold": 32,  # 256bit 合并帧指纹允许的差异位数
    "video_match_ratio": 0.6,  # 两份转发里"对得上"的视频比例下限
    "video_max_count": 5,  # 一次最多解几个视频
    "video_timeout": 15.0,  # 单个视频解帧/下载超时（秒）
    "video_byte_max_bytes": 8 * 1024 * 1024,  # 超过这个大小不算全量 SHA-256
    # 排查日志：一次判定写一行 JSON（含原始事件、指纹、图片 url 与下载失败原因）
    "log_enabled": True,
    "log_path": str(DEFAULT_LOG_PATH),
}

# 分词：ascii 单词/数字 或 连续中日韩字符
_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS forward_fingerprint (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    simhash      TEXT    NOT NULL DEFAULT '',
    b0           TEXT    NOT NULL DEFAULT '',
    b1           TEXT    NOT NULL DEFAULT '',
    b2           TEXT    NOT NULL DEFAULT '',
    b3           TEXT    NOT NULL DEFAULT '',
    content_hash TEXT    NOT NULL DEFAULT '',
    -- 这一行的 simhash/分桶是按哪版算法算的（1 = 不封顶的老算法；见 SIM_ALGO）
    sim_algo     INTEGER NOT NULL DEFAULT 1,
    kind         TEXT    NOT NULL DEFAULT '',
    weak         INTEGER NOT NULL DEFAULT 0,
    token_cnt    INTEGER NOT NULL DEFAULT 0,
    preview      TEXT    NOT NULL DEFAULT '',
    -- 这条聊天记录「被标记的群聊」= 搬进去过的目标群 + 搬出来的来源群（逗号分隔）
    group_ids    TEXT    NOT NULL DEFAULT '',
    first_seen   INTEGER NOT NULL DEFAULT 0,
    last_seen    INTEGER NOT NULL DEFAULT 0,
    hit_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fwd_b0 ON forward_fingerprint(b0);
CREATE INDEX IF NOT EXISTS idx_fwd_b1 ON forward_fingerprint(b1);
CREATE INDEX IF NOT EXISTS idx_fwd_b2 ON forward_fingerprint(b2);
CREATE INDEX IF NOT EXISTS idx_fwd_b3 ON forward_fingerprint(b3);
CREATE INDEX IF NOT EXISTS idx_fwd_hash ON forward_fingerprint(content_hash);
CREATE INDEX IF NOT EXISTS idx_fwd_seen ON forward_fingerprint(last_seen);

-- 图片双哈希：一条转发指纹可以挂多张图（只在 weak 指纹上写）
CREATE TABLE IF NOT EXISTS forward_image (
    hash       TEXT    NOT NULL DEFAULT '',
    fp_id      INTEGER NOT NULL,
    group_ids  TEXT    NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fwdimg_hash ON forward_image(hash);
CREATE INDEX IF NOT EXISTS idx_fwdimg_fp ON forward_image(fp_id);

-- 视频指纹：sig = 封面帧 aHash+dHash + 第10帧 aHash+dHash（64 hex）
-- byte_hash = 全量 SHA-256（只在文件不超过配置上限时写，可能为空）
CREATE TABLE IF NOT EXISTS forward_video (
    sig        TEXT    NOT NULL DEFAULT '',
    byte_hash  TEXT    NOT NULL DEFAULT '',
    fp_id      INTEGER NOT NULL,
    group_ids  TEXT    NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL DEFAULT 0,
    bytes      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fwdvideo_sig ON forward_video(sig);
CREATE INDEX IF NOT EXISTS idx_fwdvideo_byte ON forward_video(byte_hash);
CREATE INDEX IF NOT EXISTS idx_fwdvideo_fp ON forward_video(fp_id);

-- 内容身份：同一份聊天记录被不同的人（或断点回放）从多个群重复转发时，
-- 展开后的叶子消息 id / 卡片 id 是稳定的；而 weak 指纹（纯图 / 纯视频）在取不到
-- 图片时只能靠「宁可多搬一次」，于是同一份内容会在多个群各发一遍。
-- 这张表把「稳定的内容身份」单独记下来，查库时先按身份命中，再走文本 / 媒体判定。
CREATE TABLE IF NOT EXISTS forward_content (
    identity   TEXT    NOT NULL,
    fp_id      INTEGER NOT NULL,
    first_seen INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity)
);
CREATE INDEX IF NOT EXISTS idx_fwdcontent_fp ON forward_content(fp_id);

-- 原始内容存档：入库时把「清洗前」的展开内容压一份存下来。
-- 为什么要它：清洗规则是会长大的（今天加气泡卡片，明天可能加别的），规则一改
-- content_hash 就变；只留指纹文本的话，旧内容永远对不上，只能重新爬一遍群。
-- 表头一行 = 一份内容（按 content_hash 去重），payload 是 zlib 压缩的 messages JSON。
CREATE TABLE IF NOT EXISTS forward_corpus (
    content_hash TEXT PRIMARY KEY,
    kind         TEXT    NOT NULL DEFAULT '',
    payload      BLOB,
    raw_size     INTEGER NOT NULL DEFAULT 0,
    created_at   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fwdc_created ON forward_corpus(created_at);
"""


def load_settings(overrides: dict | None = None, settings=None) -> dict:
    """默认值 -> config.json["forward_dedup"] / BotSettings 段落 -> 显式参数，依次覆盖。"""
    values = dict(_DEFAULTS)
    user_cfg: dict = {}
    if settings is not None:
        section = None
        reader = getattr(settings, "section", None)
        if callable(reader):
            try:
                section = reader("forward_dedup")
            except Exception:
                section = None
        elif isinstance(settings, dict):
            raw = settings.get("forward_dedup")
            section = raw if isinstance(raw, dict) else settings
        if isinstance(section, dict):
            user_cfg.update(section)
        else:
            # 传入的 settings 没有 forward_dedup 段 → 仍按旧行为读 config.json
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    user_cfg = (json.load(f) or {}).get("forward_dedup") or {}
            except Exception:
                pass
    else:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = (json.load(f) or {}).get("forward_dedup") or {}
        except Exception:
            pass
    for key, value in (user_cfg or {}).items():
        if key in values and value is not None:
            values[key] = value
    for key, value in (overrides or {}).items():
        if key in values and value is not None:
            values[key] = value
    return values


def tokenize(text: str) -> list[str]:
    """中文按「整串 + 相邻双字」切分（保留顺序，又能容忍个别字差异），ascii 按词/数字。"""
    tokens: list[str] = []
    for seg in _TOKEN_RE.findall((text or "").lower()):
        tokens.append(seg)
        if not seg[0].isascii():  # 中文串额外补上相邻双字
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def simhash64(tokens: list[str]) -> int:
    """经典 SimHash：每个词的 64bit 哈希按位投票，但**每个词最多算 TOKEN_WEIGHT_CAP
    票**。不封顶时重复出现的 `user:` / `[图片]` 会把票数堆成“有几条消息、几张图”
    的函数，实测那样连毫不相干的转发都只差 3 位。"""
    vector = [0] * SIMHASH_BITS
    for token, count in Counter(tokens).items():
        weight = min(count, TOKEN_WEIGHT_CAP)
        digest = int.from_bytes(
            hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
        )
        for i in range(SIMHASH_BITS):
            vector[i] += weight if digest >> i & 1 else -weight
    return sum(1 << i for i, vote in enumerate(vector) if vote > 0)


def simhash_hex(tokens: list[str]) -> str:
    return f"{simhash64(tokens):0{HASH_HEX_LEN}x}"


# 简写别名（新代码里 `simhash(...)` 更直观；旧名字 simhash64 保留）
simhash = simhash64


def split_bands(fingerprint_hex: str) -> tuple[str, ...]:
    step = BAND_BITS // 4  # 16bit = 4 位 hex
    return tuple(
        fingerprint_hex[i * step : (i + 1) * step] for i in range(BAND_COUNT)
    )


def hamming_hex(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def clean_image_hashes(images) -> list[str]:
    """只留合法的 32 位 hex 图片双哈希（去重、保序）。"""
    result: list[str] = []
    for item in images or []:
        value = str(item or "").strip().lower()
        if len(value) != IMG_HASH_HEX_LEN or value in result:
            continue
        try:
            int(value, 16)
        except ValueError:
            continue
        result.append(value)
    return result


def clean_video_sigs(videos) -> list[dict]:
    """只留合法的视频指纹（sig 64 hex / byte_hash 64 hex，至少有一个）。"""
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in videos or []:
        if not isinstance(item, dict):
            continue
        sig = str(item.get("sig") or "").strip().lower()
        byte_hash = str(item.get("byte_hash") or "").strip().lower()
        if sig and not _is_hex(sig, VIDEO_SIG_HEX_LEN):
            sig = ""
        if byte_hash and not _is_hex(byte_hash, VIDEO_BYTE_HASH_HEX_LEN):
            byte_hash = ""
        if not sig and not byte_hash:
            continue
        key = (sig, byte_hash)
        if key in seen:
            continue
        seen.add(key)
        try:
            size = int(item.get("bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        result.append({"sig": sig, "byte_hash": byte_hash, "bytes": max(0, size)})
    return result


def _is_hex(value: str, length: int) -> bool:
    if len(value) != length:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def clean_group_ids(values) -> list[str]:
    """只留纯数字的群号（去重、保序），传 list / 单个值 / 逗号串都行。

    群号是纯数字，所以非数字/空值在这里就挡掉：否则会存进 "None"、
    "20002.0" 这种永远比不上的东西（从库里读回来的就是 "123,456"）。
    """
    if values is None:
        items: list = []
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        items = str(values).split(",")
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text.isascii() and text.isdigit() and text not in result:
            result.append(text)
    return result


#: 内容身份前缀。都来自「展开后的内容本身」，不是本次转发的消息 id：
#: ``leaf:`` = 叶子消息在服务端的稳定 id（real_id 优先），``fwd:`` = 嵌套转发 id，
#: ``media:`` = 文件名里像哈希的那一段（单图 / 单视频转发唯一的稳定锚点）。
_IDENTITY_PREFIXES = ("leaf:", "fwd:", "media:")


def clean_identities(values) -> list[str]:
    """整理内容身份：只留已知前缀、去重保序（传 list / 单个值都行）。"""
    if values is None:
        items: list = []
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        items = [values]
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or not text.startswith(_IDENTITY_PREFIXES):
            continue
        if text not in result:
            result.append(text)
    return result


def _stable_media_name(file_name: str) -> str:
    """从媒体文件名里取「像哈希」的那一段，做内容锚点。

    实测 SnowLuma 的图片 ``data.file`` 就是内容哈希（``AE6DA8E8....jpg``），
    NapCat / 视频则可能是手机本地路径（``/storage/.../2848404867628177778517373192.mp4``），
    路径里的最后一段同样是稳定的。太短或太普通的名字（``image.jpg``）不能当锚点，
    否则所有图片都会互相撞。
    """
    name = str(file_name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not name:
        return ""
    stem = name.rsplit(".", 1)[0] if "." in name else name
    lowered = stem.lower()
    hex_like = sum(1 for ch in lowered if ch in "0123456789abcdef") >= 16
    if hex_like or len(stem) >= 16:
        return stem
    return ""


def content_identities(event: dict, messages: list | None = None) -> list[str]:
    """一条聊天记录卡片的**内容身份**（跨消息 id、跨来源群稳定）。

    为什么需要它：weak 指纹（纯图 / 纯视频）在图片下载失败、或后端只给本地视频路径时
    算不出媒体锚点，文本又只剩 ``昵称: [图片]``，判定只能退回「没搬过」——同一份内容
    被不同的人从多个群转发，就会每个群各搬一遍（线上 17:14 的三张卡）。这里用展开
    内容的稳定 id 做锚点：同一份转发不管从哪个群、换了几次卡片 id 进来，叶子消息 id /
    嵌套转发 id / 媒体文件名都不变。

    只认强锚点：叶子消息 id、嵌套转发 id、像哈希的媒体文件名。昵称 / 文本 / 数量都
    不进身份（它们区分不了「同一份」和「模板一样的两份」）。
    """
    identities: list[str] = []

    def add(value: str) -> None:
        text = str(value or "").strip()
        if not text or text in ("0", "None"):
            return
        if text not in identities:
            identities.append(text)

    def walk(node) -> None:
        if isinstance(node, dict):
            message_id = node.get("message_id")
            real_id = node.get("real_id")
            anchor = real_id if real_id not in (None, "", 0, "0") else message_id
            if anchor not in (None, "", 0, "0"):
                add(f"leaf:{anchor}")
            segs = node.get("message") or node.get("content") or []
            if isinstance(segs, list):
                for seg in segs:
                    if not isinstance(seg, dict):
                        continue
                    seg_type = seg.get("type")
                    data = seg.get("data") or {}
                    if not isinstance(data, dict):
                        continue
                    if seg_type == "forward":
                        add(f"fwd:{data.get('id') or data.get('resid') or ''}")
                    elif seg_type in ("image", "video"):
                        name = _stable_media_name(data.get("file") or "")
                        if name:
                            add(f"media:{name}")
                    # 转发段里的内联 content 也要往下走（NapCat 内联嵌套）
                    inline = data.get("content")
                    if isinstance(inline, list):
                        for item in inline:
                            walk(item)
            for key in ("content", "messages", "news"):
                value = node.get(key)
                if isinstance(value, list):
                    for item in value:
                        walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(messages or [])
    if not messages:
        walk((event or {}).get("message") or [])
    return identities[:8]


def identity_text(identities: list[str]) -> str:
    """没有正文指纹时，用身份算一行稳定的合成文本（保证一行一份内容）。"""
    joined = "|".join(sorted(clean_identities(identities)))
    if not joined:
        return ""
    return "content_id|" + hashlib.blake2b(
        joined.encode("utf-8"), digest_size=8
    ).hexdigest()


def fingerprint_of(tokens: list[str]) -> dict:
    """把词表压成入库需要的几个短字段（只有 8+8 字节主体，不存原文）。"""
    content_hash = hashlib.blake2b(
        " ".join(tokens).encode("utf-8"), digest_size=8
    ).hexdigest()
    sim_hex = simhash_hex(tokens)
    b0, b1, b2, b3 = split_bands(sim_hex)
    return {
        "simhash": sim_hex,
        "b0": b0,
        "b1": b1,
        "b2": b2,
        "b3": b3,
        "content_hash": content_hash,
        "token_cnt": len(tokens),
    }


# ---------------------------------------------------------------- 事件解析 --------


def _loads(text, default=None):
    """json.loads 的安全版：NapCat 的字段时有时无、时而是字符串时而是对象，
    解不出来就当没有。"""
    try:
        return json.loads(text)
    except Exception:
        return default


def iter_json_payloads(msg_event: dict):
    """依次 yield 消息里 json 卡片解析出来的 dict"""
    for seg in msg_event.get("message", []) or []:
        if not isinstance(seg, dict) or seg.get("type") != "json":
            continue
        raw = (seg.get("data") or {}).get("data")
        if not raw:
            continue
        payload = _loads(raw)
        if isinstance(payload, dict):
            yield payload


def get_miniapp_info(msg_event: dict) -> dict:
    """解析小程序卡片（appid / title / desc / link / preview / url），与转发者无关。"""
    for payload in iter_json_payloads(msg_event):
        if payload.get("app") != "com.tencent.miniapp_01":
            continue
        meta = payload.get("meta")
        detail = meta.get("detail_1") if isinstance(meta, dict) else None
        # title/desc 缺一个就当作结构不认识（不同版本 NapCat 的字段不一样）
        if not isinstance(detail, dict) or "title" not in detail or "desc" not in detail:
            continue
        title, desc = detail["title"], detail["desc"]
        return {
            "title": title,
            "desc": desc,
            "link": detail.get("qqdocurl") or detail.get("url") or "",
            "appid": str(detail.get("appid") or ""),
            "preview": detail.get("preview") or "",
            "url": detail.get("url") or "",
            "context": f"[title:{title}][desc:{desc}]",
        }
    return {}


def get_multimsg_detail(msg_event: dict) -> dict:
    """解析新版合并转发卡片（com.tencent.multimsg）的 meta.detail"""
    for payload in iter_json_payloads(msg_event):
        if payload.get("app") != "com.tencent.multimsg":
            continue
        detail = ((payload.get("meta") or {}).get("detail")) or {}
        if not isinstance(detail, dict):
            continue
        raw_extra = payload.get("extra")
        extra = _loads(raw_extra) if isinstance(raw_extra, str) else None
        return {**detail, "_extra": extra if isinstance(extra, dict) else {}}
    return {}


def get_multimsg_resid(msg_event: dict) -> str | None:
    detail = get_multimsg_detail(msg_event)
    resid = detail.get("resid")
    return str(resid) if resid else None


def normalize_url(url: str) -> str:
    """去掉协议与查询串：分享链接每次带的 ts/bbid 参数都不一样，只留 域名+路径。"""
    if not url:
        return ""
    text = str(url).split("://", 1)[-1]
    text = text.split("?", 1)[0].split("#", 1)[0]
    return text.rstrip("/")


def short_url(url: str, keep: int = 140) -> str:
    """日志用的短链接：砍掉查询串（图片 url 的签名很长），只留 scheme+host+path。

    日志里同时留了原始 url 全文（要手动重试下载时用），这个只是给人一眼看清
"图是从哪个域名/路径来的"。
    """
    text = str(url or "").strip()
    if not text:
        return ""
    base = text.split("?", 1)[0].split("#", 1)[0]
    if len(base) > keep:
        base = base[:keep] + "…"
    return base + ("?…" if ("?" in text or "#" in text) else "")


def _xml_attr(xml: str, name: str) -> str:
    match = re.search(rf'{name}="([^"]*)"', xml)
    return match.group(1) if match else ""


# 摘要里的一行："昵称: 内容" / "[图片]" / "查看15条转发消息"
_NAME_PREFIX_RE = re.compile(r"^\s*[^:：]{1,24}[:：]\s*")
# 媒体占位：[图片] [动画表情] [视频] [文件] ...
_MEDIA_RE = re.compile(r"\[[^\[\]]{0,10}\]")
# 视频必须保留自己的占位：它要走视频指纹，不能和图片混成 [图片]
_VIDEO_PLACEHOLDER = "[视频]"
_VIDEO_SENTINEL = "\x00video\x00"
# 有正文的转发里，昵称一律隐成这个常量（见 mask_senders）
_MASK_NAME = "user"


def normalize_body(body: str) -> str:
    """规整一条消息的**正文**（不含昵称）：合并空白、图片类占位统一成 [图片]、
    连续占位并成一个；``[视频]`` 单独保留，交给视频指纹。

    与 normalize_line 里对正文的处理是同一套写法，只是不走「拼成 `昵称: 正文` 再用
    正则切回来」那一步 —— get_forward_msg 的 sender 与 raw_message 本来就是分开的两个
    字段，没必要拼起来再猜边界（猜错的原因见 forward_content_items）。
    """
    text = re.sub(r"\s+", " ", (body or "").strip())
    text = text.replace(_VIDEO_PLACEHOLDER, _VIDEO_SENTINEL)
    text = _MEDIA_RE.sub("[图片]", text)
    text = re.sub(r"(?:\[图片\])+", "[图片]", text)
    return text.replace(_VIDEO_SENTINEL, _VIDEO_PLACEHOLDER).strip()


def body_text(body: str) -> str:
    """正文里的纯文字（去掉媒体占位）：weak 判据只数这个（见 is_text_sparse_bodies）"""
    return _MEDIA_RE.sub("", body or "").strip()


def compose_line(name: str, body: str) -> str:
    """(昵称, 正文) 拼回摘要行 `昵称: 正文`（与 xmlContent 摘要、卡片摘要同款写法）。

    正文为空时留着那个冒号（`浅奈:`）——老指纹就是这么拼的，改写法会让这些行的
    content_hash 漂移、白白丢一次精确命中。
    """
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        return body or ""
    return f"{name}: {body or ''}".rstrip()


def normalize_line(line: str) -> str:
    """把摘要里的一行规整成 `发送者: [图片]`：保留昵称前缀（不丢发送者）、所有媒体
    占位（[动画表情]/[视频]/[文件]…）统一成 [图片]、合并空白。

    纯图转发的每行就是 `浅奈: [图片]`，除"谁 + 几张图"外没有别的文本，而这恰恰是
    这类记录的区分特征；既没昵称、去掉占位又什么都不剩的行（孤立 `[图片]`）返回空串。

    只有拼好的 `名字: 正文` 可拿时（xmlContent 摘要那一路）才该这么做：昵称里带冒号
    会被 `_NAME_PREFIX_RE` 切错边界。sender / raw_message 分开的转发一律走
    forward_content_items，别再从行文本里反解。
    """
    text = re.sub(r"\s+", " ", (line or "").strip())
    if not text:
        return ""
    match = _NAME_PREFIX_RE.match(text)
    name = match.group(0).strip() if match else ""
    body = text[match.end() :] if match else text
    return f"{name} {normalize_body(body)}".strip()


def line_body(line: str) -> str:
    """规整后去掉媒体占位，剩下的"正文"（`浅奈: [图片]` -> 空串）"""
    body = _NAME_PREFIX_RE.sub("", normalize_line(line))
    return _MEDIA_RE.sub("", body).strip()


def is_informative_line(line: str) -> bool:
    """规整后是否还有区分度：带昵称的行（`浅奈: [图片]`）算有——纯图转发里"谁 + 几张
    图"就是全部信息；光秃秃的占位行（`[图片]`）没有主体，才算没有。
    """
    normalized = normalize_line(line)
    if not normalized:
        return False
    return bool(_NAME_PREFIX_RE.match(normalized)) or len(line_body(normalized)) >= 2


# weak 的判据（见 is_text_sparse）：正文文字数 < 图片数 × 这个系数，缺省 15，
# 可再用 config.json 的 forward_dedup.text_per_image 调（x 越大，越容易转图片判定）
TEXT_PER_IMAGE = max(1, int(load_settings().get("text_per_image") or 15))


def is_text_sparse(lines, media_cnt: int | None = None) -> bool:
    """正文文字数 是否 小于 图片数 × TEXT_PER_IMAGE —— 文字少到只能靠"谁 + 几张图"辨认。

    这类指纹（图片刷屏，或几句话配一堆图）没有多少可比对的文字：只接受完全一致的
    命中、不做模糊相似，而且**必须**过图片双哈希（见 _image_ok 的 required）。
    没有图片（图片数 0）时恒为 False，一律按正常文本处理。

    param media_cnt: 真实媒体段数（图片/视频）。传了就用它；不传则按行里的占位符数
        （`[图片][图片]` -> 2）。行是 normalize_line 出来的，连续占位符会被它并
        成一个占位符，所以数得到真实张数的地方（forward_content_items）会传进来。

    只拿得到拼好的行文本时才用这个；sender / raw_message 分开的转发走
    is_text_sparse_bodies —— 昵称不进这道计算。
    """
    text_cnt = 0
    image_cnt = 0
    for line in lines or []:
        text_cnt += len(line_body(line))  # `浅奈: 晚安[图片]` -> 2
        image_cnt += len(_MEDIA_RE.findall(line or ""))  # `[图片][图片]` -> 2
    if media_cnt is not None:
        image_cnt = int(media_cnt)  # 数好的真实张数不用再猜，比占位符数准
    return text_cnt < image_cnt * TEXT_PER_IMAGE


def is_text_sparse_bodies(bodies, media_cnt: int | None = None) -> bool:
    """is_text_sparse 的结构化版：bodies 是每条消息的**正文**（不含昵称，见
    forward_content_items）。

    昵称不能进这道计算：它是 NapCat 渲染的，同一个账号两次回查可能给出
    `厘了个谱:-0` 和 `真是厘了谱-_-` 两个名字；更要命的是昵称里的冒号会让
    `名字: 正文` 的边界切错（`厘了个谱:-0: 不是？哥们？！` 被切成正文
    `-0: 不是？哥们？！`），正文凭空多出十几个字，weak 判据直接翻面。线上实例：
    同一条转发一次数到 73 字（weak）、一次数到 107 字（不 weak），两条指纹对不上
    → 漏判重搬。

    :param media_cnt: 真实图片张数，语义与 is_text_sparse 完全一致。
    """
    texts = [body_text(b) for b in (bodies or [])]
    if media_cnt is None:
        image_cnt = sum(len(_MEDIA_RE.findall(b or "")) for b in (bodies or []))
    else:
        image_cnt = int(media_cnt)
    return sum(len(t) for t in texts) < image_cnt * TEXT_PER_IMAGE


# CQ 码 → 摘要里同款占位（`[CQ:image,...]` -> `[图片]`），后面还会过 normalize_line
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)[^\]]*\]")
_CQ_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[图片]",
    "mface": "[图片]",
    "marketface": "[图片]",
    "bface": "[图片]",
    "sface": "[图片]",
    "rps": "[图片]",
    "dice": "[图片]",
    "video": "[视频]",
    "record": "[图片]",
    "file": "[文件]",
    "json": "[分享]",
    "xml": "[分享]",
    "share": "[分享]",
    "miniapp": "[小程序]",
    "forward": "[聊天记录]",
    "node": "[聊天记录]",
    "poke": "[戳一戳]",
    "at": "",
    "reply": "",
}


def cq_to_placeholder(text: str) -> str:
    """把 CQ 码换成卡片摘要里的同款占位（xml 摘要里写的就是 `浅奈: [图片]`）。

    转发的图片 url 带签名、会过期，绝不能进指纹，所以媒体一律只留占位。
    """
    return _CQ_RE.sub(lambda m: _CQ_PLACEHOLDER.get(m.group(1).lower(), ""), text or "")


def segments_placeholder(segments) -> str:
    """把消息段拼成占位文本（拿不到 raw_message 时用）"""
    parts: list[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        else:
            parts.append(_CQ_PLACEHOLDER.get(seg_type.lower(), ""))
    return "".join(parts)


# ══════════════════════════════════════════════════════════
# 指纹清洗：把「广告 / 占位卡片 / 纯链接」从入库内容里去掉
# ══════════════════════════════════════════════════════════
# 只影响指纹，不影响发出去的内容 —— 发送侧另有 forward_filter.clean_forward_messages。
_JUNK_URL_RE = re.compile(r"https?://[^\s\u4e00-\u9fff]+")
# 不带协议的「裸域名 + 路径」：QQ 里的薅羊毛广告经常这么写
# （`零食✨好物🐑毛薅不停😋↓ qm.qq.com/q/VcCFoWscQU`）。必须有路径，避免
# 把「我们聊 qq.com 吧」这种正常文字当成链接。
_BARE_URL_RE = re.compile(
    r"(?<![\w/])(?:[\w-]+\.)+(?:com|cn|net|org|xyz|top|cc|me|tv|info|link|shop|vip|site|online)"
    r"/[^\s\u4e00-\u9fff]*",
    re.IGNORECASE,
)
# 去掉链接后剩这么多字以内，就当「这条消息只是链接 / 吆喝 + 链接」。
# 宽松值只对**末尾几条**生效（和发送侧 clean_forward_messages 同一个口径）：
# 正文中间的正常聊天里带链接不该被删。
_JUNK_LINK_REST = 6
_JUNK_LINK_REST_LOOSE = 20
#: 每条聊天记录默认检查末尾几条
JUNK_SCAN_TAIL = 3
# 装扮 / 气泡类分享的固定话术（与 forward_filter.PROMO_TEXT_HINTS 同源，这里不 import
# 是为了 dedup 保持零外部依赖）
_JUNK_TEXT_HINTS = (
    "免费气泡",
    "个性装扮",
    "聊天气泡",
    "超好看的装扮",
    "气泡装扮",
)
# 段占位符：json/xml 卡片本身不是内容
_JUNK_PLACEHOLDERS = frozenset({"", "[分享]"})


def _promo_reason(msg) -> str | None:
    """转发记录里的单条消息是不是装扮/气泡分享卡片（懒加载，缺依赖就跳过）。"""
    try:
        from messaging.forward_filter import promo_card_reason
    except Exception:  # pragma: no cover - 依赖缺失时不影响指纹
        return None
    try:
        return promo_card_reason(msg)
    except Exception:  # pragma: no cover - 脏数据不该打断入库
        return None


def is_noise_body(body: str, *, loose: bool = False) -> bool:
    """一段正文是不是纯噪声：空 / 纯链接 / 装扮话术。

    :param loose: 末尾几条用的宽松链接规则（去掉链接后 ≤ 30 字也算广告）

    注意**不**把 ``[分享]`` 算进来：真正的内容卡片（公众号文章、B 站分享）在
    指纹里就是 ``[分享]``，只有发送侧能按 json 内容区分，入库这边只能保守。
    """
    text = re.sub(r"\s+", " ", (body or "").strip())
    if not text:
        return True
    if any(hint in text for hint in _JUNK_TEXT_HINTS):
        return True
    limit = _JUNK_LINK_REST_LOOSE if loose else _JUNK_LINK_REST
    for pattern in (_JUNK_URL_RE, _BARE_URL_RE):
        if pattern.search(text):
            rest = pattern.sub("", text).strip()
            if len(rest) <= limit:
                return True
    return False


def _message_is_junk(msg, *, loose: bool = False) -> bool:
    """整条消息都不该进指纹：装扮/气泡卡片，或正文只剩链接/吆喝。"""
    if not isinstance(msg, dict):
        return True
    if _promo_reason(msg):
        return True
    raw = msg.get("raw_message")
    if isinstance(raw, str) and raw.strip():
        body = cq_to_placeholder(raw)
    else:
        body = segments_placeholder(msg.get("message") or msg.get("content") or [])
    return is_noise_body(normalize_body(body), loose=loose)


def _looks_like_fingerprint_line(part: str) -> bool:
    """指纹文本里的一段是不是「一条消息的行」（而不是 kind / tsum / source 这些头）。"""
    if not part:
        return False
    if part.startswith("[") and part.endswith("]") and len(part) <= 8:
        return True
    return bool(_NAME_PREFIX_RE.match(part))


def clean_fingerprint_text(text: str) -> str:
    """把指纹文本里噪声行去掉，并重算 ``tsum``（重建老库用，见 dedup_rebuild）。

    老行只有拼好的文本，没有段结构，所以这里只能按行清：

    * ``名字: [分享]`` —— 卡片占位，老数据里的气泡广告就长这样；
    * 纯链接 / 「吆喝 + 链接」（去掉链接后 ≤ 30 字）；
    * 装扮话术。

    全被判成噪声时**原样返回**：宁可不清，也不要把一条记录清成空指纹。
    """
    raw = str(text or "")
    if not raw:
        return raw
    parts = raw.split("|")
    kind = parts[0] if parts else ""
    if kind != "old_forward":
        # 小程序卡片是固定字段（appid|标题|描述|预览图），multimsg 的头也说不清
        # 哪段是 source —— 这两种没有「末尾广告」问题，原样返回最安全。
        return raw
    idx = 1
    tsum: int | None = None
    if idx < len(parts) and re.fullmatch(r"tsum\d+", parts[idx] or ""):
        tsum = int(parts[idx][4:])
        idx += 1
    source = ""
    lines: list[str] = []
    for part in parts[idx:]:
        if _looks_like_fingerprint_line(part):
            lines.append(part)
        elif not source and part:
            source = part
        else:
            lines.append(part)

    kept: list[str] = []
    tail_from = max(0, len(lines) - JUNK_SCAN_TAIL)
    for index, line in enumerate(lines):
        body = _line_body(line)
        if line == "[分享]" or line.endswith(": [分享]") or body == "[分享]":
            continue
        if is_noise_body(body, loose=index >= tail_from):
            continue
        kept.append(line)
    if not kept or len(kept) == len(lines):
        return raw
    # 清洗过头的保护：一条链接就把整条记录清成一句「这个真的好玩兄弟」时，
    # 指纹会变得毫无区分度（线上 id=1187）。这种情况原样保留。
    has_media = any(re.search(r"\[(?:图片|视频|文件)\]", line) for line in kept)
    plain_chars = sum(len(re.sub(r"\[(?:图片|视频|文件|分享)\]", "", _line_body(line)).strip())
                      for line in kept)
    if not has_media and len(kept) <= 1 and plain_chars < 10:
        return raw

    head = [kind]
    if tsum is not None:
        head.append(f"tsum{len(kept)}")
    if source:
        head.append(source)
    return "|".join([p for p in head if p] + kept)


def _line_body(line: str) -> str:
    """``名字: 正文`` → 正文；没有名字前缀就原样返回。"""
    match = _NAME_PREFIX_RE.match(line or "")
    return line[match.end():] if match else (line or "")


def clean_fingerprint_lines(lines, tail_n: int = JUNK_SCAN_TAIL) -> list[str]:
    """摘要行列表版的清洗（multimsg / xml 这两条只有文本的路走它）。"""
    items = [str(line or "") for line in lines or []]
    tail_from = max(0, len(items) - max(0, tail_n)) if tail_n else len(items)
    kept: list[str] = []
    for index, text in enumerate(items):
        if is_noise_body(_line_body(text), loose=index >= tail_from):
            continue
        kept.append(text)
    return kept or items


def forward_content_items(
    messages, drop_junk: bool = True, tail_n: int = JUNK_SCAN_TAIL
) -> tuple[list[tuple[str, str]], int, int]:
    """把 get_forward_msg 取回的转发内容拆成 [(昵称, 正文)]（写法与卡片摘要保持一致）。

    get_forward_msg 的每条都是一整个消息事件（有 sender / raw_message / message），
    昵称与正文在事件里**本来就是分开的两个字段**，所以这里一路分开处理，不再拼成
    `名字: 正文` 再用 _NAME_PREFIX_RE 反解 —— 昵称里带冒号（线上实例
    `厘了个谱:-0`）时正则只吃到第一个冒号，`-0: ` 混进正文，于是：

    * weak 判据（正文文字数 < 图片数 x text_per_image）被顶翻（73 字 -> 107 字，
      跨过 6 张图的阈值 90）；
    * 隐名后留下 `user: -0: …` 这种脏前缀。

    同一条转发两次回查就会算成两份不同的指纹（content_hash 不同、SimHash 距离 12 >
    阈值 8、又不是 weak 所以压根没下图片），表现就是"明明很像却判 miss"又搬一遍。

    顺手数出**真实媒体段数（图片/视频）**：normalize_body 会把一条消息里的 `[图片][图片]`
    并成一个占位符，只看行里的占位符就会把媒体数看少（weak 判据就漏了），所以在
    合并之前数。

    return: ([(昵称, 规整后的正文)], 记录条数, 媒体占位符张数（图片+视频）)
    """
    items: list[tuple[str, str]] = []
    total = 0
    media = 0
    all_messages = list(messages or [])
    tail_from = max(0, len(all_messages) - max(0, tail_n)) if tail_n else len(all_messages)
    for index, msg in enumerate(all_messages):
        if not isinstance(msg, dict):
            continue
        # 噪声（装扮/气泡卡片、纯链接、纯吆喝）不进指纹：同一份内容换个人转发，
        # 末尾挂的广告条数常常不一样，留着就会拼出两份指纹（线上 1132313813）。
        if drop_junk and _message_is_junk(msg, loose=index >= tail_from):
            continue
        total += 1
        sender = msg.get("sender") or {}
        name = str(sender.get("card") or sender.get("nickname") or "").strip()
        raw = msg.get("raw_message")
        if isinstance(raw, str) and raw.strip():
            body = cq_to_placeholder(raw)
        else:
            body = segments_placeholder(msg.get("message") or msg.get("content") or [])
        media += len(_MEDIA_RE.findall(body))  # 合并前数，`[图片][图片]` -> 2
        body = normalize_body(body)
        if name or body:  # 昵称/正文全空的（拿不到内容）丢掉
            items.append((name, body))
    return items, total, media


def forward_content_lines(messages) -> tuple[list[str], int, int]:
    """forward_content_items 的摘要行版本（拼回 `昵称: 正文`，供只吃行文本的地方用）。

    指纹这条路不要用它：昵称与正文的边界一经拼接就丢了，weak 判据只能靠正则猜
    （见 forward_content_items）。

    :return: (摘要行列表, 记录条数, 媒体占位符张数)
    """
    items, total, media = forward_content_items(messages)
    return [compose_line(name, body) for name, body in items], total, media


def mask_senders(lines) -> list[str]:
    """把每行的发送者昵称一律隐成 `user`，正文与顺序原样保留。

    昵称是 NapCat 渲染出来的，**同一条记录不同次回查可能给出完全不同的名字**，
    连"有几个人说话"都可能不一样。线上实例（同一份 21 条记录在库里躺成两行）：一次
    是 `QQ用户: 我发现个有点逆天的事情|…`（取不到资料时的占位昵称，整段塌成同一个
    名字），另一次是 `z: 我发现个有点逆天的事情|…|Roxy: AI呗|…`。昵称只要进指纹，
    内容一模一样的两份就会整段对不上（SimHash 汉明距离 37、四个 band 一个都不
    共享 → 线上漏判）。

    也**不能**按"第几个出现的人"编号（用户1/用户2…）：同一条记录一次解析出
    z/Roxy 两个人、另一次全塌成占位昵称，编号照样对不上。只有统一成常量才彻底
    免疫——正文顺序相同即视为同一份。代价是正文完全一样、只是说话人不同的两份会
    互相命中；这在群里本来就是同一张图/同一段段子被搬来搬去，判成重复正是想要的。

    纯图转发（`昵称: [图片]`）不走这里：那种记录除"谁 + 几张图"什么都没有，昵称是
    唯一的内容特征，必须原样保留（见 fingerprint_lines 的 is_text_sparse 分支）。
    能拿到 (昵称, 正文) 两个字段时用 mask_item：这里的前缀是靠 _NAME_PREFIX_RE
    从行文本里猜的，昵称带冒号会切错。
    """
    result: list[str] = []
    for line in lines or []:
        text = normalize_line(line)
        if not text:
            continue
        match = _NAME_PREFIX_RE.match(text)
        if not match:
            result.append(text)  # 没昵称的行（如孤立的 `[图片]`）原样保留
            continue
        body = text[match.end() :].strip()
        result.append(f"{_MASK_NAME}: {body}" if body else _MASK_NAME)
    return result


def fingerprint_lines(lines, media_cnt: int | None = None) -> tuple[list[str], bool]:
    """整理指纹用的摘要行，并给出 weak（文本是否过少）。

    文本够多 → 隐掉昵称（见 mask_senders）；文本过少（正文文字数 < 图片数 x
    text_per_image，见 is_text_sparse）→ 保留昵称：这类记录除了「谁 + 几张图」
    没有别的内容特征。

    :param media_cnt: 行里已经数好的真实图片张数；行被 normalize_line 合并过
        占位符时要传，否则弱判据会把图片数看少。
    """
    items = list(lines or [])
    weak = is_text_sparse(items, media_cnt)
    return (items if weak else mask_senders(items)), weak


def mask_item(name: str, body: str) -> str:
    """把一条 (昵称, 正文) 的昵称隐成常量 `user`（结果与 mask_senders 一致，但不用猜
    前缀：边界是 forward_content_items 直接给的）。

    没昵称的行（如孤立的 `[图片]`）原样保留，和 mask_senders 一样。
    """
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        return body or ""
    return f"{_MASK_NAME}: {body}" if body else _MASK_NAME


def fingerprint_items(items, media_cnt: int | None = None) -> tuple[list[str], bool]:
    """fingerprint_lines 的结构化版：items 是 forward_content_items 的 [(昵称, 正文)]。

    文本够多 → 隐掉昵称（见 mask_item）；文本过少（正文文字数 < 图片数 x
    text_per_image，见 is_text_sparse_bodies）→ 保留昵称：这类记录除了「谁 + 几张
    图」没有别的内容特征（这里说的"正文"不含昵称，昵称里带冒号也不会数错）。

    :param media_cnt: 已数好的真实图片张数；正文被 normalize_body 合并过占位符时
        要传，否则弱判据会把图片数看少。
    """
    pairs = [(str(name or "").strip(), str(body or "")) for name, body in items or []]
    weak = is_text_sparse_bodies([body for _, body in pairs], media_cnt)
    if weak:
        return [compose_line(name, body) for name, body in pairs], True
    return [mask_item(name, body) for name, body in pairs], False


def empty_fingerprint(
    kind: str, images: list[str], videos: list[dict] | None = None
) -> dict:
    """摘要一个字都解析不出来时的空指纹（不生成指纹，否则会互相撞车）"""
    return {
        "text": "",
        "weak": True,
        "kind": kind,
        "preview": "",
        "images": images,
        "videos": clean_video_sigs(videos),
    }


def _iter_dicts(node, max_nodes: int = 300):
    """广度优先遍历 JSON 树里的所有 dict（带节点上限，防病态嵌套）。"""
    stack = [node]
    seen = 0
    while stack and seen < max_nodes:
        current = stack.pop()
        seen += 1
        if isinstance(current, dict):
            yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _find_multiforward_element(msg_event: dict) -> dict:
    """找旧版转发的 multiForwardMsgElement（摘要在它的 xmlContent 里）。

    实时事件的真实位置是 `raw.elements[*].multiForwardMsgElement`
    （elementType=16，elementId 就是转发消息 id）。
    不同来源/版本也可能直接拍平在 `raw.multiForwardMsgElement`，
    或者 raw 是 JSON 字符串、层级更深，所以先按已知位置找，最后递归兜底。
    """
    raw = msg_event.get("raw") or {}
    raw = _loads(raw, {}) if isinstance(raw, str) else raw  # raw 可能是 JSON 字符串
    if not isinstance(raw, dict) or not raw:
        return {}

    candidates: list = [raw.get("multiForwardMsgElement")]
    for item in raw.get("elements") or []:
        if isinstance(item, dict):
            candidates.append(item.get("multiForwardMsgElement"))
    candidates.extend(_iter_dicts(raw))

    for element in candidates:
        if isinstance(element, dict) and (
            element.get("xmlContent") or element.get("resId")
        ):
            return element
    return {}


def get_old_forward_xml_info(msg_event: dict) -> dict:
    """旧版转发（[CQ:forward,id=...]）：从 multiForwardMsgElement 里取 xml 摘要"""
    element = _find_multiforward_element(msg_event)
    xml = str(element.get("xmlContent") or "")
    titles = [
        _unescape_xml(t)
        for t in re.findall(r"<title[^>]*>(.*?)</title>", xml, re.S)
        if t.strip()
    ]
    sources = re.findall(r'<source[^>]*name="([^"]*)"', xml)
    return {
        "titles": titles,
        "source": _unescape_xml(sources[0]) if sources else "",
        "tsum": _xml_attr(xml, "tSum"),
    }


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """旧库升级：补上缺的列、把历史群列并进 `group_ids`。

    CREATE TABLE IF NOT EXISTS 不会改已有表，所以新列得手动补。群信息这一列历史上
    换过几种形态，最后统一成 `group_ids`（见模块说明）；老的 `group_id`（只存来源
    群）和后来的 `source_ids` 都并进 `group_ids` 再删掉。
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(forward_fingerprint)")}
    if "weak" not in existing:
        conn.execute(
            "ALTER TABLE forward_fingerprint ADD COLUMN weak INTEGER NOT NULL DEFAULT 0"
        )
    if "group_ids" not in existing:
        conn.execute(
            "ALTER TABLE forward_fingerprint ADD COLUMN group_ids "
            "TEXT NOT NULL DEFAULT ''"
        )
    if "sim_algo" not in existing:
        # 老行的 simhash 是封装前的算法算的，标成 1 → 退出相似度判定（content_hash
        # 与图片判定照常；再被搬一次时会在 _upsert_locked 里自动升级成新版）
        conn.execute(
            "ALTER TABLE forward_fingerprint ADD COLUMN sim_algo "
            "INTEGER NOT NULL DEFAULT 1"
        )
    image_cols = {row[1] for row in conn.execute("PRAGMA table_info(forward_image)")}
    if "group_ids" not in image_cols:
        conn.execute(
            "ALTER TABLE forward_image ADD COLUMN group_ids TEXT NOT NULL DEFAULT ''"
        )
    # 历史列 → 并进 group_ids 再删掉（老 sqlite 不支持 DROP 时留着不用也不影响判定）
    for table, key, columns in (
        ("forward_fingerprint", "id", existing),
        ("forward_image", "rowid", image_cols),
    ):
        for legacy in ("group_id", "source_ids"):
            if legacy not in columns:
                continue
            rows = conn.execute(
                f"SELECT {key} AS k, group_ids, {legacy} FROM {table} "
                f"WHERE {legacy} <> ''"
            ).fetchall()
            for row in rows:
                merged = clean_group_ids(row["group_ids"])
                for one in clean_group_ids(row[legacy]):
                    if one not in merged:
                        merged.append(one)
                conn.execute(
                    f"UPDATE {table} SET group_ids = ? WHERE {key} = ?",
                    (",".join(merged), row["k"]),
                )
            try:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {legacy}")
            except sqlite3.Error:
                pass  # 老 sqlite 不支持删列：留着不用也不影响判定


# xml 摘要里的实体（&amp; 必须放最后，否则 "&amp;lt;" 会被二次解码）
_XML_ENTITIES = (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&apos;", "'"), ("&amp;", "&"))


def _unescape_xml(text: str) -> str:
    for entity, char in _XML_ENTITIES:
        text = text.replace(entity, char)
    return text


def build_fingerprint(
    event: dict,
    kind: str,
    miniapp_info: dict | None = None,
    image_hashes: list[str] | None = None,
    video_hashes: list[dict] | None = None,
    forward_messages: list | None = None,
    clean: bool = True,
) -> dict:
    """生成转发指纹。

    :param clean: 是否做「入库清洗」（丢装扮卡片 / 末尾广告）。传 False 复现清洗
        之前的拼法 —— 用来给清洗上线**前**入库的老行做别名查询（见
        :meth:`ForwardDedupStore.find` 的 ``alt_text``）。
    :param kind: "miniapp" / "multimsg" / "old_forward"
    :param image_hashes: 仅 weak（文本过少）时算出来的图片双哈希列表
    :param video_hashes: 仅 weak 时算出来的视频指纹（封面帧 + 第10帧 + 可选字节哈希）
    :param forward_messages: get_forward_msg 取回的转发内容（旧版转发专用），
        传了就优先用它拼指纹——raw / xmlContent 时有时无，不能当依赖
    :return: {"text": 指纹文本(与转发者无关), "weak": ...,
              "kind": kind, "preview": 前 120 字,
              "images": 图片双哈希列表, "videos": 视频指纹列表}

    三类卡片各自拼成一行 `kind|...`（每段先过 normalize_line，空段丢掉）；
    `weak`（文本过少）、`images`（图片双哈希）、`videos`（视频指纹）的用法见模块说明。
    """
    images = clean_image_hashes(image_hashes)
    videos = clean_video_sigs(video_hashes)
    weak = False
    if kind == "miniapp":
        info = miniapp_info or get_miniapp_info(event)
        parts = [
            "miniapp",
            str(info.get("appid") or ""),
            str(info.get("title") or ""),
            str(info.get("desc") or ""),
            normalize_url(info.get("preview") or info.get("url") or ""),
        ]
    elif kind == "multimsg":
        detail = get_multimsg_detail(event)
        lines = [str(n.get("text", "")) for n in (detail.get("news") or [])
                 if isinstance(n, dict)]
        if clean:
            lines = clean_fingerprint_lines(lines)
        # 文本够多时隐去昵称，文本过少时保留（见 fingerprint_lines）
        lines, weak = fingerprint_lines(lines)
        # 不含 resid / uniseq / filename —— 这些每次转发都会变，转发者无关才有意义
        parts = [
            "multimsg",
            str(detail.get("source") or ""),
            str(detail.get("summary") or ""),
            "|".join(normalize_line(t) for t in lines),
        ]
    elif kind == "old_forward" and forward_messages is not None:
        # 有真实内容就用它：与卡片摘要同一套写法，且不受 raw 有无影响
        items, total, media = forward_content_items(
            forward_messages,
            drop_junk=clean,
            tail_n=JUNK_SCAN_TAIL if clean else 0,
        )
        if not items:
            return empty_fingerprint(kind, images, videos)
        # 文本够多时把昵称隐成 user，详见 fingerprint_items / mask_item；media 是合并
        # 前的真实张数，weak 判据只数正文（不含昵称，见 is_text_sparse_bodies）
        lines, weak = fingerprint_items(items, media)
        parts = ["old_forward", f"tsum{total}" if total else "", "|".join(lines)]
    elif kind == "old_forward":
        info = get_old_forward_xml_info(event)
        # 第一行通常是卡片抬头（与 <source> 同名，如"群聊的聊天记录"），不算内容
        titles = [
            t for t in info["titles"] if t.strip() != info["source"].strip()
        ]
        if clean:
            titles = clean_fingerprint_lines(titles)
        if not any(is_informative_line(t) for t in titles) and not info["source"]:
            # 摘要一个字都没解析出来（消息结构变了）：不生成指纹，
            # 否则所有这类转发都会拼成 "old_forward" 而互相撞车
            return empty_fingerprint(kind, images, videos)
        lines, weak = fingerprint_lines(titles)
        parts = [
            "old_forward",
            info["tsum"] and f"tsum{info['tsum']}",
            info["source"],
            "|".join(normalize_line(t) for t in lines),
        ]
    else:
        parts = []
    text = "|".join(p for p in parts if p)
    return {
        "text": text,
        "weak": weak,
        "kind": kind,
        "preview": text[:120],
        "images": images,
        "videos": videos,
    }


# ------------------------------------------------------- 运行日志（默认开） ----
# 一行一条 JSON：原始事件里有用的那几段（含 raw）+ 提取出的指纹 + 判定结果，
# 图片那一路还记了 url / 下载结果与失败原因。开关与路径走 config.json。
_log_config = load_settings()
LOG_ENABLED = bool(_log_config.get("log_enabled", True))
LOG_PATH = _data_path(_log_config.get("log_path"), DEFAULT_LOG_PATH)
LOG_MAX_BYTES = 8 * 1024 * 1024  # 超过就只留最近 LOG_KEEP_LINES 行
LOG_KEEP_LINES = 300
LOG_MAX_STR = 20000  # 单个字符串上限，防一条事件把文件写爆
LOG_MAX_ITEMS = 200  # 单个列表/字典最多留多少项

_log_lock = threading.Lock()

# 事件里值得留的键：能解释"指纹是怎么拼出来的"就这些。
# message = 消息段（旧版转发是 type=forward + data.id）；
# raw = 有它才可能本地提出摘要（断点回放的历史消息没有）
_EVENT_KEYS = ("message_id", "message_type", "group_id", "user_id", "self_id",
               "sender", "raw_message", "message", "raw")


def _clip(value):
    """递归限长，保留结构（不丢字段名，只夹字符串/列表长度）"""
    if isinstance(value, str):
        return value if len(value) <= LOG_MAX_STR else value[:LOG_MAX_STR] + "…<截断>"
    if isinstance(value, dict):
        return {str(k): _clip(v) for k, v in list(value.items())[:LOG_MAX_ITEMS]}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in list(value)[:LOG_MAX_ITEMS]]
    if isinstance(value, (bytes, bytearray)):
        return f"<bytes {len(value)}>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def event_snapshot(event) -> dict:
    """原始事件里有用的那几段（含 raw），末尾附一份顶层键名清单"""
    if not isinstance(event, dict):
        return {"_type": type(event).__name__}
    snapshot = {key: _clip(event[key]) for key in _EVENT_KEYS if key in event}
    snapshot["_top_keys"] = sorted(str(k) for k in event.keys())
    return snapshot


def dedup_log(action: str, event=None, fingerprint: dict | None = None, **fields) -> None:
    """把一次去重判定写进 forward_dedup_log.jsonl（一行一条，便于对照）。

    :param action: gate_skip / hit / miss / record / image / error
    :param event: 原始事件（只留 _EVENT_KEYS 里有用的几段，含 raw）
    :param fingerprint: 本次提取出来的指纹（None / 空文本也如实记录）
    """
    if not LOG_ENABLED:
        return
    entry: dict = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action}
    if event is not None:
        entry["event"] = event_snapshot(event)
    if fingerprint is not None:
        entry["fingerprint"] = _clip(fingerprint)
    entry.update({k: _clip(v) for k, v in fields.items()})
    try:
        with _log_lock:
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                lines = LOG_PATH.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                LOG_PATH.write_text(
                    "\n".join(lines[-LOG_KEEP_LINES:]) + "\n", encoding="utf-8"
                )
            with LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # 日志绝不能影响转发主流程
        log.warning("[转发去重] 写日志失败: %s", exc)


# ---------------------------------------------------------------- 存储 --------


class ForwardDedupStore:
    """转发指纹库（SQLite）。

    `_xxx_sync` 是真实现；公开的 async 方法用 to_thread 丢到线程池、用 asyncio.Lock
    串行化，避免同一个进程里并发写冲突（跨进程靠 SQLite 事务，见 _claim_locked）。
    """

    _SELECT = (
        "SELECT id, simhash, content_hash, kind, weak, token_cnt, preview, sim_algo, "
        "group_ids, first_seen, last_seen, hit_count FROM forward_fingerprint"
    )

    def __init__(self, db_path=None, settings=None, **overrides):
        self.settings = settings
        config = load_settings(overrides, settings=settings)
        if db_path is None:
            raw = config.get("db_path")
            if raw and str(raw) != str(_DEFAULTS["db_path"]):
                # 配置里显式写了自定义库路径（旧行为）→ 按旧规则解析
                db_path = _data_path(raw, DEFAULT_DB_PATH, settings)
            elif settings is not None:
                # 默认位置永远跟项目布局走：<data>/forward_dedup.db
                db_path = settings.paths.data_file("forward_dedup.db")
            else:
                db_path = _data_path(raw, DEFAULT_DB_PATH, settings)
        self.db_path = Path(str(db_path))
        self.hamming_threshold = int(config["hamming_threshold"])
        self.min_tokens = int(config["min_tokens"])
        self.token_ratio = float(config["token_ratio"])
        self.keep_days = config["keep_days"]
        self.max_rows = config["max_rows"]
        self.img_hamming_threshold = int(config["img_hamming_threshold"])
        self.img_match_ratio = float(config["img_match_ratio"])
        self.img_max_count = int(config["img_max_count"])
        self.img_timeout = float(config["img_timeout"])
        self.img_max_bytes = int(config["img_max_bytes"])
        self.clean_enable = bool(config.get("clean_enable", True))
        self.clean_scan_tail = int(config.get("clean_scan_tail", 3))
        self.clean_drop_promo = bool(config.get("clean_drop_promo", True))
        self.clean_drop_qr = bool(config.get("clean_drop_qr", True))
        self.clean_drop_group_card = bool(config.get("clean_drop_group_card", True))
        self.clean_drop_link = bool(config.get("clean_drop_link", True))
        self.clean_image_timeout = float(config.get("clean_image_timeout", 3.0))
        self.clean_qr_images = int(config.get("clean_qr_images", 3))
        self.video_hamming_threshold = int(config["video_hamming_threshold"])
        self.video_match_ratio = float(config["video_match_ratio"])
        self.video_max_count = int(config["video_max_count"])
        self.video_timeout = float(config["video_timeout"])
        self.video_byte_max_bytes = int(config["video_byte_max_bytes"])
        self._conn: sqlite3.Connection | None = None
        self._thread_lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        self._last_prune = 0.0

    @property
    def clean_options(self) -> dict:
        """给 :func:`messaging.forward_filter.clean_forward_messages` 的参数。"""
        return {
            "enable": self.clean_enable,
            "scan_tail": self.clean_scan_tail,
            "drop_promo": self.clean_drop_promo,
            "drop_qr": self.clean_drop_qr,
            "drop_group_card": self.clean_drop_group_card,
            "drop_link": self.clean_drop_link,
            "loose_link": True,  # 入库判定可以比发送更狠：只影响指纹
            "image_timeout": self.clean_image_timeout,
            "max_qr_images": self.clean_qr_images,
        }

    # ---------------- 连接 ----------------
    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA_SQL)
        _ensure_columns(conn)
        conn.commit()
        self._conn = conn
        return conn

    def close(self) -> None:
        with self._thread_lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ---------------- 同步实现 ----------------
    def _token_count_ok(self, a: int, b: int) -> bool:
        if a <= 0 or b <= 0:
            return False
        return min(a, b) >= max(a, b) * self.token_ratio

    def _promote_locked(
        self, conn: sqlite3.Connection, row: sqlite3.Row, fp: dict, preview: str
    ) -> bool:
        """把一条「清洗前」的老指纹就地升级成清洗后的哈希。

        只在别名命中时调用，此时清洗后的 content_hash 已经查过、库里没有这一行，
        所以不会升级出重复行。`weak` / `kind` / 群标记 / 命中次数一概不动 ——
        清洗只改变哈希和预览，判定语义（weak 要不要图片确认）属于当初入库时的规则，
        不能拿今天的规则去改历史结论。
        """
        try:
            conn.execute(
                "UPDATE forward_fingerprint SET simhash = ?, b0 = ?, b1 = ?, b2 = ?, "
                "b3 = ?, content_hash = ?, token_cnt = ?, preview = ?, sim_algo = ? "
                "WHERE id = ?",
                (
                    fp["simhash"],
                    fp["b0"],
                    fp["b1"],
                    fp["b2"],
                    fp["b3"],
                    fp["content_hash"],
                    fp["token_cnt"],
                    (preview or "")[:120],
                    SIM_ALGO,
                    int(row["id"]),
                ),
            )
            conn.commit()
            return True
        except Exception as exc:  # noqa: BLE001 - 升级失败不影响这次判定
            log.warning("[转发去重] 老指纹升级失败（id=%s）: %s", row["id"], exc)
            try:
                conn.rollback()
            except Exception:
                pass
            return False

    def _rows_by_content_hash(
        self, conn: sqlite3.Connection, content_hash: str
    ) -> list[sqlite3.Row]:
        """按内容哈希取候选：同一个哈希下可能有多行（摘要一样但图片不是同一批）"""
        return conn.execute(
            f"{self._SELECT} WHERE content_hash = ? "
            "ORDER BY last_seen DESC LIMIT 20",
            (content_hash,),
        ).fetchall()

    # ---- 图片双哈希（只在 weak 指纹上有数据）----
    def _image_rows(self, conn: sqlite3.Connection, fp_id: int) -> list[str]:
        rows = conn.execute(
            "SELECT hash FROM forward_image WHERE fp_id = ?", (int(fp_id),)
        ).fetchall()
        return [row["hash"] for row in rows]

    def _image_match_ratio(self, a: list[str], b: list[str]) -> float | None:
        """当前这批图（b）有几成能在候选行（a）里找到配对的；没图可比特返回 None。

        分母必须是**当前这批图的张数**，不能用 max(len(a), len(b))：同一个
        content_hash 只留一行指纹（见 _upsert_locked），历史图片会不断累加到同一个
        fp_id 上，用 max() 会让分母单调增长 —— 同一批图第二次来反而跌到阈值以下
        （线上实例：第 3 轮存了 4 张不同的图，第 4 轮拿字节完全相同的 4 张来查只剩
        4/9 = 0.44 < 0.6 → 判"没搬过" → 又搬一遍）。
        """
        if not a or not b:
            return None
        used: set[int] = set()
        matched = 0
        for one in b:  # 当前这批图，逐张去 a（候选行的历史图）里找配对
            for index, other in enumerate(a):
                if index in used:
                    continue
                if hamming_hex(one, other) <= self.img_hamming_threshold:
                    used.add(index)
                    matched += 1
                    break
        return matched / len(b)

    def _image_ok(
        self,
        conn: sqlite3.Connection,
        fp_id: int,
        images: list[str],
        required: bool = False,
    ) -> bool:
        """候选指纹的图片集合是否和当前这条对得上。

        required=True 用在 weak 指纹上（正文文字数 < 图片数 x text_per_image）：这类
        转发（少图模板聊天记录）文本往往一模一样，图完全不同，文本判定根本不可靠，
        所以**【两边任何一边拿不到图就不算对上】**——宁可当"没搬过"多搬一次，也不
        能把这次的新内容当成旧内容吞掉。非 weak 时没图可对比就放过，交给文本判定。
        """
        cand_images = self._image_rows(conn, fp_id)
        ratio = self._image_match_ratio(cand_images, images)
        if ratio is None:
            return not required
        return ratio >= self.img_match_ratio

    # ---- 视频指纹（只在 weak 指纹上有数据）----
    def _video_rows(self, conn: sqlite3.Connection, fp_id: int) -> list[dict]:
        rows = conn.execute(
            "SELECT sig, byte_hash, bytes FROM forward_video WHERE fp_id = ?",
            (int(fp_id),),
        ).fetchall()
        return [
            {
                "sig": str(row["sig"] or ""),
                "byte_hash": str(row["byte_hash"] or ""),
                "bytes": int(row["bytes"] or 0),
            }
            for row in rows
        ]

    def _video_same(self, one: dict, other: dict) -> bool:
        """两个视频指纹是否同一段视频。

        全量 SHA-256 相同 → 直接算同一段（最可靠）。
        否则比 256bit 的帧指纹：封面帧 + 第 10 帧各 128bit。视频被 QQ 转码后
        字节会变，但画面通常只差几位，所以帧指纹是主判据。
        """
        a_byte = str(one.get("byte_hash") or "")
        b_byte = str(other.get("byte_hash") or "")
        if a_byte and b_byte and a_byte == b_byte:
            return True
        a_sig = str(one.get("sig") or "")
        b_sig = str(other.get("sig") or "")
        if not a_sig or not b_sig:
            return False
        try:
            return hamming_hex(a_sig, b_sig) <= self.video_hamming_threshold
        except (TypeError, ValueError):
            return False

    def _video_match_ratio(self, a: list[dict], b: list[dict]) -> float | None:
        """当前这批视频（b）有几成能在候选行（a）里找到配对的；没视频返回 None。"""
        if not a or not b:
            return None
        used: set[int] = set()
        matched = 0
        for one in b:
            for index, other in enumerate(a):
                if index in used:
                    continue
                if self._video_same(one, other):
                    used.add(index)
                    matched += 1
                    break
        return matched / len(b)

    def _video_ok(
        self,
        conn: sqlite3.Connection,
        fp_id: int,
        videos: list[dict],
        required: bool = False,
    ) -> bool:
        """候选指纹的视频集合是否和当前这条对得上。

        required=True 用在 video-only 的 weak 指纹上：文本只剩 `昵称: [视频]`，
        视频对不上就不算搬过；两边任何一边没指纹都返回 False（宁可多搬一次）。
        """
        cand_videos = self._video_rows(conn, fp_id)
        ratio = self._video_match_ratio(cand_videos, videos)
        if ratio is None:
            return not required
        return ratio >= self.video_match_ratio

    def _groups_locked(self, conn: sqlite3.Connection, fp_id: int) -> list[str]:
        """读回一行指纹的标记群集合（`group_ids`）。"""
        row = conn.execute(
            "SELECT group_ids FROM forward_fingerprint WHERE id = ?", (int(fp_id),)
        ).fetchone()
        return clean_group_ids(row["group_ids"] if row else [])

    def _mark_group_locked(
        self, conn: sqlite3.Connection, fp_id: int, group_id: str
    ) -> list[str]:
        """把群并入指纹的标记集合，返回合并后的集合；不 commit（调用方决定事务）。

        用单条 UPDATE 读出当前值并追加，避免两个进程同时补记不同群时发生
        read-modify-write 覆盖；图片/视频行同步标记，保持 group_ids 一致。
        """
        groups = clean_group_ids([group_id])
        if groups:
            target = groups[0]
            for table, key in (("forward_fingerprint", "id"),
                               ("forward_image", "fp_id"),
                               ("forward_video", "fp_id")):
                conn.execute(
                    f"UPDATE {table} SET group_ids = CASE "
                    "WHEN group_ids = '' THEN ? "
                    "WHEN instr(',' || group_ids || ',', ',' || ? || ',') > 0 "
                    "THEN group_ids ELSE group_ids || ',' || ? END "
                    f"WHERE {key} = ?",
                    (target, target, target, int(fp_id)),
                )
        return self._groups_locked(conn, fp_id)

    def _pick_candidate(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        images: list[str],
        videos: list[dict] | None = None,
        cur_group: str = "",
        image_required: bool = False,
        video_required: bool = False,
    ) -> sqlite3.Row | None:
        """文本命中的多个候选里挑一个：先过媒体指纹那一关，再优先挑「该群有记录」的。

        image_required / video_required（本次 weak 且确实有该类媒体）或候选自己
        就是 weak 行时，对应那一关是强制的：文本一字不差也只是"可能"，媒体对不上
        就不算搬过。候选 weak 但只有视频时，不能拿图片去卡它。
        优先该群有记录，是为了同一份内容散成多行时不漏（正常情况下只有一行）；
        真挑不出来就退回第一行，由调用方按「该群没记录」处理。
        """
        videos = videos or []
        usable: list[sqlite3.Row] = []
        for cand in rows:
            cand_images = self._image_rows(conn, cand["id"])
            cand_videos = self._video_rows(conn, cand["id"])
            cand_weak = bool(cand["weak"])
            image_need = image_required or (cand_weak and bool(cand_images))
            video_need = video_required or (cand_weak and bool(cand_videos))
            # 老记录 weak 但图片/视频都没落库（当时下载失败）：保守地仍按图片校验，
            # 避免它只靠相同文本吞掉新内容。
            if cand_weak and not cand_images and not cand_videos:
                image_need = True
            if not self._image_ok(conn, cand["id"], images, required=image_need):
                continue
            if not self._video_ok(conn, cand["id"], videos, required=video_need):
                continue
            usable.append(cand)
        for cand in usable:
            if self._group_seen(cand["group_ids"], cur_group):
                return cand
        return usable[0] if usable else None

    @staticmethod
    def _group_seen(row_groups, cur_group: str) -> bool:
        """这条记录「被标记的群聊」里有没有当前这个群（`cur_group` = 本次事件所在群）。

        在 → True（去发【发过了喵】）；不在 → False（不搬也不提示）；老记录群信息
        全空（脏数据，无从判断）→ True（保守：宁可少搬）；没有群（私聊）→ False。
        """
        groups = clean_group_ids(row_groups)
        if not groups:
            return True
        if not cur_group:
            return False
        return cur_group in set(groups)

    def _best_image_candidate(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        images: list[str],
        cur_group: str,
    ) -> sqlite3.Row | None:
        """从候选行里挑图片匹配度最高的一行；同分时优先本次所在群已标记过的。"""
        best: sqlite3.Row | None = None
        best_key: tuple[int, float] | None = None
        for cand in rows:
            cand_images = self._image_rows(conn, cand["id"])
            ratio = self._image_match_ratio(cand_images, images)
            if ratio is None or ratio < self.img_match_ratio:
                continue
            seen = self._group_seen(cand["group_ids"], cur_group)
            key = (0 if seen else 1, -ratio)  # 该群在标记里优先，其次图片越像越好
            if best_key is None or key < best_key:
                best, best_key = cand, key
        return best

    def _find_by_images(
        self, conn: sqlite3.Connection, images: list[str], cur_group: str
    ) -> sqlite3.Row | None:
        """图片双哈希兜底检索：文本漂移但图片对得上时也算搬过。

        先用 `hash IN (...)` 走索引；精确哈希找不到时再做一次模糊扫描。dHash 是
        感知哈希，同一张图被 QQ 转码后也会差几位，只认完全相等会把本来能配上的
        历史行直接筛掉（线上案例：旧行 5 张图，新消息 4 张图各差 1~2 位，文本条数
        从 13 变 11，结果精确哈希查不到、SimHash 又因 weak 要求距离 0 而落空）。
        图片表规模有限（一次转发最多 5 张），精确路径没结果时全表扫一遍做汉明比较。
        """
        placeholders = ",".join("?" * len(images))
        rows = conn.execute(
            f"{self._SELECT} WHERE id IN ("
            f"SELECT fp_id FROM forward_image WHERE hash IN ({placeholders})"
            ")",
            tuple(images),
        ).fetchall()
        best = self._best_image_candidate(conn, rows, images, cur_group)
        if best is not None:
            return best

        candidate_ids: set[int] = set()
        for row in conn.execute("SELECT fp_id, hash FROM forward_image"):
            try:
                matched = any(
                    hamming_hex(row["hash"], one) <= self.img_hamming_threshold
                    for one in images
                )
                if matched:
                    candidate_ids.add(int(row["fp_id"]))
            except (TypeError, ValueError):
                continue
        if not candidate_ids:
            return None
        marks = ",".join("?" * len(candidate_ids))
        rows = conn.execute(
            f"{self._SELECT} WHERE id IN ({marks})",
            tuple(candidate_ids),
        ).fetchall()
        return self._best_image_candidate(conn, rows, images, cur_group)

    def _find_by_identities(
        self,
        conn: sqlite3.Connection,
        identities: list[str],
        cur_group: str,
        content_hash: str = "",
    ) -> sqlite3.Row | None:
        """按「内容身份」查历史：同一份聊天记录换了卡片 id / 来源群也能认出来。

        两类身份强度不同：

        * ``leaf:`` / ``fwd:`` —— 展开后的叶子消息 id、嵌套转发 id，是**同一份内容**
          的直接证据，命中即算搬过；
        * ``media:`` —— 媒体文件名哈希。两份不同记录可能含同一张图，所以只在
          **正文指纹完全相同**（``content_hash`` 相等）时才认，用来补「纯图 / 纯视频
          取不到双哈希」那一档。

        只放宽到这一步：身份必须在入库时真的见过（``forward_content``），不做猜测。
        """
        wanted = clean_identities(identities)
        if not wanted:
            return None
        strong = {one for one in wanted if not one.startswith("media:")}
        media = [one for one in wanted if one.startswith("media:")]
        query_ids = list(strong) + media
        marks = ",".join("?" * len(query_ids))
        fp_ids = [
            int(row["fp_id"])
            for row in conn.execute(
                f"SELECT DISTINCT fp_id FROM forward_content WHERE identity IN ({marks})",
                tuple(query_ids),
            )
        ]
        if not fp_ids:
            return None
        id_marks = ",".join("?" * len(fp_ids))
        rows = conn.execute(
            f"{self._SELECT} WHERE id IN ({id_marks})", tuple(fp_ids)
        ).fetchall()
        usable: list[sqlite3.Row] = []
        for cand in rows:
            seen = {
                str(row["identity"])
                for row in conn.execute(
                    "SELECT identity FROM forward_content WHERE fp_id = ?",
                    (int(cand["id"]),),
                )
            }
            if seen & strong:
                usable.append(cand)
            elif cand["content_hash"] == content_hash and seen & set(media):
                usable.append(cand)
        if not usable:
            return None
        for cand in usable:
            if self._group_seen(cand["group_ids"], cur_group):
                return cand
        return usable[0]

    def _best_video_candidate(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
        videos: list[dict],
        cur_group: str,
    ) -> sqlite3.Row | None:
        """从候选行里挑视频匹配度最高的一行；同分时优先本次所在群已标记过的。"""
        best: sqlite3.Row | None = None
        best_key: tuple[int, float] | None = None
        for cand in rows:
            cand_videos = self._video_rows(conn, cand["id"])
            ratio = self._video_match_ratio(cand_videos, videos)
            if ratio is None or ratio < self.video_match_ratio:
                continue
            seen = self._group_seen(cand["group_ids"], cur_group)
            key = (0 if seen else 1, -ratio)
            if best_key is None or key < best_key:
                best, best_key = cand, key
        return best

    def _find_by_videos(
        self,
        conn: sqlite3.Connection,
        videos: list[dict],
        cur_group: str,
    ) -> sqlite3.Row | None:
        """视频指纹兜底检索：文本漂移但视频对得上时也算搬过。

        先走 sig / byte_hash 的索引精确查；精确查不到再做一次模糊扫描（QQ 转码
        可能让帧指纹差几位）。视频表规模有限（一次转发最多 video_max_count 个）。
        """
        if not videos:
            return None
        sigs = [v["sig"] for v in videos if v.get("sig")]
        byte_hashes = [v["byte_hash"] for v in videos if v.get("byte_hash")]
        clauses: list[str] = []
        params: list[str] = []
        if sigs:
            clauses.append(f"sig IN ({','.join('?' * len(sigs))})")
            params.extend(sigs)
        if byte_hashes:
            clauses.append(f"byte_hash IN ({','.join('?' * len(byte_hashes))})")
            params.extend(byte_hashes)
        rows: list[sqlite3.Row] = []
        if clauses:
            rows = conn.execute(
                f"{self._SELECT} WHERE id IN ("
                f"SELECT fp_id FROM forward_video WHERE {' OR '.join(clauses)}"
                ")",
                tuple(params),
            ).fetchall()
        best = self._best_video_candidate(conn, rows, videos, cur_group)
        if best is not None:
            return best

        candidate_ids: set[int] = set()
        for row in conn.execute("SELECT fp_id, sig, byte_hash FROM forward_video"):
            cand = {
                "sig": str(row["sig"] or ""),
                "byte_hash": str(row["byte_hash"] or ""),
            }
            if any(self._video_same(one, cand) for one in videos):
                candidate_ids.add(int(row["fp_id"]))
        if not candidate_ids:
            return None
        marks = ",".join("?" * len(candidate_ids))
        rows = conn.execute(
            f"{self._SELECT} WHERE id IN ({marks})",
            tuple(candidate_ids),
        ).fetchall()
        return self._best_video_candidate(conn, rows, videos, cur_group)

    def _mark_seen_locked(
        self, conn: sqlite3.Connection, row: sqlite3.Row, cur_group: str
    ) -> tuple[list[str], bool]:
        """命中一条记录时的收尾：把本次所在群补进标记集合。

        只有 seen_here=False 那半要补：skip 只表示这次不搬运，不代表该群没处理过
        这份内容；补上以后下次从该群再来就会命中【发过了喵】。
        返回 (合并后的群集合, 本次进库前的 seen_here)。不 commit。
        """
        row_groups = clean_group_ids(row["group_ids"])
        seen_here = self._group_seen(row_groups, cur_group)
        if not seen_here and cur_group:
            row_groups = self._mark_group_locked(conn, row["id"], cur_group)
        return row_groups, seen_here

    def _simhash_row(
        self,
        conn: sqlite3.Connection,
        fp: dict,
        token_cnt: int,
        images: list[str],
        videos: list[dict],
        cur_group: str,
        weak: bool,
        image_required: bool = False,
        video_required: bool = False,
    ) -> tuple[sqlite3.Row | None, int]:
        """SimHash 模糊相似度：分桶取候选，返回 (最合适的一行, 汉明距离)。

        只要有一边是 weak（正文文字数 < 图片数 x text_per_image）就只接受距离 0
        且图片必须对得上（见 _image_ok），否则不同人的图片刷屏会被相似度误归成
        同一份；候选里该群在标记里的优先，其次距离最近。

        非 weak 走 hamming_threshold（默认 8）。这个值是和 simhash64 的封顶权重
        （TOKEN_WEIGHT_CAP）配套量出来的，换一个就得重测：「毫不相干」的两两距离
        实测最近也有 10（改前只有 2~3），而「同一条记录少收/多收 1 条消息」中位
        在 7~8，所以 8 是两者之间比较稳的一刀。

        只拿 sim_algo = SIM_ALGO 的行来比：老行是封装前的算法算的，混进来算距离会
        得到毫无意义的数字（那些行仍然参与 content_hash 和图片判定）。
        """
        candidates = conn.execute(
            f"{self._SELECT} WHERE (b0 = ? OR b1 = ? OR b2 = ? OR b3 = ?) "
            "AND sim_algo = ?",
            (fp["b0"], fp["b1"], fp["b2"], fp["b3"], SIM_ALGO),
        ).fetchall()
        best: sqlite3.Row | None = None
        best_key: tuple[int, int] | None = None
        for cand in candidates:
            if not self._token_count_ok(token_cnt, cand["token_cnt"]):
                continue
            # weak（本次或候选）必须过媒体指纹这一关。候选 weak 但只有视频时，
            # 不能拿图片去卡它。
            cand_weak = bool(cand["weak"])
            cand_images = self._image_rows(conn, cand["id"])
            cand_videos = self._video_rows(conn, cand["id"])
            image_need = bool(weak or cand_weak)
            if not image_required and not cand_images and cand_videos:
                image_need = False
            video_need = video_required or (cand_weak and bool(cand_videos))
            if not self._image_ok(conn, cand["id"], images, required=image_need):
                continue
            if not self._video_ok(conn, cand["id"], videos, required=video_need):
                continue
            dist = hamming_hex(fp["simhash"], cand["simhash"])
            if dist > (0 if (weak or cand["weak"]) else self.hamming_threshold):
                continue
            key = (0 if self._group_seen(cand["group_ids"], cur_group) else 1, dist)
            if best_key is None or key < best_key:
                best, best_key = cand, key
        return (best, best_key[1]) if best is not None else (None, 0)

    def _find_sync(
        self,
        text: str,
        weak: bool = False,
        images: list[str] | None = None,
        videos: list[dict] | None = None,
        group_ids: list[str] | None = None,
        cur_group: str = "",
        reserve: bool = False,
        kind: str = "",
        preview: str = "",
        image_required: bool | None = None,
        video_required: bool | None = None,
        alt_text: str = "",
        identities: list[str] | None = None,
    ) -> dict | None:
        """按 整串指纹 → 内容身份 → 清洗前指纹（别名）→ SimHash 相似度 → 图片双哈希 的顺序查历史。

        命中即刷新 last_seen、hit_count +1（"这份东西被搬过几次"），返回结果的
        `seen_here` = 本次事件所在群在不在标记集合里（True 去发【发过了喵】，
        False 直接结束），两种都不返回"要搬"；`group_ids` 是这次要一起标记进库的群
        （目标群 + 来源群），`images` 是本次这批图的双哈希（见模块说明），weak 指纹
        必须靠它确认（拿不到图就不算搬过）。

        `alt_text` 是同一份内容在**清洗之前**拼出来的指纹文本。清洗（丢装扮卡片 /
        末尾广告）是后加的，之前入库的行存的是清洗前的 content_hash；只查清洗后的
        文本，这些老行永远匹配不上，同一份内容每重发一次就搬一次。命中别名时会把
        那一行就地升级成清洗后的哈希（下次再出现就能直接命中，换个马甲也认）。
        别名是**更严格**的比对（连广告都一样），所以不会带来新的误判。

        reserve=True 把"查 + 认领"合成一次原子操作：判定为"没搬过、要搬"时立刻把
        group_ids 写进库，让几乎同时到达的第二条来源消息（a 群、b 群各发一次）能
        看见这条认领记录，不会两边都判"没搬过"而在目标群里各发一遍；被抢先认领时
        返回一条结果，由调用方按 `seen_here` 决定发提示还是直接结束。
        """
        tokens = tokenize(text)
        images = clean_image_hashes(images)
        videos = clean_video_sigs(videos)
        image_required = weak if image_required is None else bool(image_required)
        video_required = False if video_required is None else bool(video_required)
        marked = clean_group_ids(group_ids)
        cur_group = str(cur_group or "")
        if not tokens:
            return None
        identities = clean_identities(identities)
        fp = fingerprint_of(tokens)
        now = int(time.time())
        with self._thread_lock:
            conn = self._ensure_conn()
            # 整串指纹完全一致（含 tSum/全部行）就算搬运过；weak 不例外，但必须由
            # 图片/视频确认：少图模板聊天记录文本一字不差太常见了。
            rows = self._rows_by_content_hash(conn, fp["content_hash"])
            row = self._pick_candidate(
                conn,
                rows,
                images,
                videos,
                cur_group,
                image_required=image_required,
                video_required=video_required,
            )
            reason = "content_hash" if row is not None else ""
            similarity = 1.0
            # 内容身份：同一份记录换了卡片 id / 来源群，叶子消息 id 不变。
            # 排在别名和 SimHash 之前 —— 它是直接证据，且能救「weak 取不到媒体锚点」
            # 那一档（文本只剩 `昵称: [图片]` 时，文本判定会一直说"没搬过"）。
            if row is None and identities:
                row = self._find_by_identities(
                    conn, identities, cur_group, content_hash=fp["content_hash"]
                )
                if row is not None:
                    reason = "identity"
            # 清洗之前的指纹（别名）：命中说明这是清洗上线前入过库的老内容。
            # 顺序放在 SimHash 之前 —— 整串相等比近似更准，而且是一次索引查询。
            if row is None and alt_text and alt_text != text:
                alt_tokens = tokenize(alt_text)
                alt_fp = fingerprint_of(alt_tokens) if alt_tokens else None
                if alt_fp and alt_fp["content_hash"] != fp["content_hash"]:
                    alt_rows = self._rows_by_content_hash(
                        conn, alt_fp["content_hash"]
                    )
                    row = self._pick_candidate(
                        conn,
                        alt_rows,
                        images,
                        videos,
                        cur_group,
                        image_required=image_required,
                        video_required=video_required,
                    )
                    if row is not None:
                        reason = "content_hash_legacy"
                        self._promote_locked(conn, row, fp, preview or text)
            # 模糊相似度（候选怎么挑见 _simhash_row）
            if row is None and len(tokens) >= self.min_tokens:
                row, dist = self._simhash_row(
                    conn,
                    fp,
                    len(tokens),
                    images,
                    videos,
                    cur_group,
                    weak,
                    image_required=image_required,
                    video_required=video_required,
                )
                if row is not None:
                    reason = "simhash"
                    similarity = 1 - dist / SIMHASH_BITS
            # 媒体指纹兜底：文本（条数/抬头）漂了但图片/视频完全一致，仍是同一份
            if row is None and images:
                row = self._find_by_images(conn, images, cur_group)
                if row is not None:
                    reason = "image"
            if row is None and videos:
                row = self._find_by_videos(conn, videos, cur_group)
                if row is not None:
                    reason = "video"
            if row is None:
                # 查不到 = 这次真的要搬 → 先把标记的群认领下来（见 _claim_locked）
                if reserve and marked:
                    conflict = self._claim_locked(
                        conn,
                        fp,
                        groups=marked,
                        cur_group=cur_group,
                        kind=kind,
                        preview=preview,
                        text=text,
                        weak=weak,
                        images=images,
                        videos=videos,
                        image_required=image_required,
                        video_required=video_required,
                        now=now,
                    )
                    if conflict is not None:
                        return conflict
                return None
            last_seen_before = int(row["last_seen"] or 0)
            row_groups, seen_here = self._mark_seen_locked(conn, row, cur_group)
            conn.execute(
                "UPDATE forward_fingerprint SET last_seen = ?, hit_count = hit_count + 1 "
                "WHERE id = ?",
                (now, row["id"]),
            )
            conn.commit()
            # 搬过就不再搬：该群在标记里的去发【发过了喵】，不在的直接结束（群已补进标记）
            return self._row_result(
                row, reason, similarity, cur_group, row["hit_count"] + 1,
                last_seen_before, row_groups=row_groups, seen_here=seen_here,
            )

    def _row_result(
        self,
        row: sqlite3.Row,
        reason: str,
        similarity: float,
        cur_group: str,
        hit_count: int,
        seen_at: int | None = None,
        row_groups: list[str] | None = None,
        seen_here: bool | None = None,
    ) -> dict:
        """把库里的一行整理成调用方看得懂的结果。

        `seen_here` = 本次事件所在群在"被标记的群聊"里有没有（True 去发【发过了喵】，
        False 直接结束，两者都不再搬运）；调用方若已在 skip 时把当前群补进库，仍应
        显式传 False，保留"进库前不在标记里"的判定。`seen_at` 是算 `fresh`（是不是
        刚刚才写进去的）用的时间戳，默认取这行自己的 last_seen。
        """
        groups = clean_group_ids(row["group_ids"] if row_groups is None else row_groups)
        if seen_here is None:
            seen_here = self._group_seen(groups, cur_group)
        stamp = int(seen_at if seen_at is not None else (row["last_seen"] or 0))
        return {
            "id": row["id"],
            "similarity": round(similarity, 4),
            "reason": reason,
            "kind": row["kind"],
            "preview": row["preview"],
            "hit_count": hit_count,
            "last_seen": row["last_seen"],
            "group_ids": groups,
            "cur_group": cur_group,
            "seen_here": seen_here,
            # 这条记录是刚刚（CLAIM_FRESH_SECONDS 内）才写进去的 → 疑似并发/重复来源
            "fresh": (int(time.time()) - stamp) <= CLAIM_FRESH_SECONDS,
        }

    def _claim_locked(
        self,
        conn: sqlite3.Connection,
        fp: dict,
        groups: list[str],
        cur_group: str,
        kind: str,
        preview: str,
        text: str,
        weak: bool,
        images: list[str],
        videos: list[dict],
        image_required: bool,
        video_required: bool,
        now: int,
        identities: list[str] | None = None,
    ) -> dict | None:
        """原子"认领"：判定为"要搬"时立刻把这次要标记的群写进库。返回 None = 认领成功。

        find 和 record 之间隔着好几次 await（重连、建转发任务、写库），同一份内容
        从 a 群和 b 群几乎同时进来时，两条都会查到"没搬过"，结果在目标群里各发一遍。
        把"重读 + 写"圈进一个写事务后（BEGIN IMMEDIATE 是为了连"两个进程共用同一
        个 db"也不出错），后到的那条能看见前一条已经认领过这份内容 → 返回一条结果，
        由 _row_result 按"该群有没有记录"决定发提示还是直接结束。认领本身失败（起不了
        事务等）时按未认领处理：宁可多发一次也不静默丢消息。
        """
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = self._rows_by_content_hash(conn, fp["content_hash"])
            # 同一个内容哈希下面可能躺着多行（摘要一样但图片/视频不是同一批），
            # 所以要按和外面一样的规矩挑候选，不能只看第一行（weak 同样要媒体确认）
            row = self._pick_candidate(
                conn,
                rows,
                images,
                videos,
                cur_group,
                image_required=image_required,
                video_required=video_required,
            )
            if row is None and identities:
                # 认领事务里也要看内容身份：否则「A 群刚认领，B 群的同一份内容
                # 同时进来」仍会各认领一次、各搬一遍（weak 取不到媒体锚点时必现）。
                row = self._find_by_identities(
                    conn, identities, cur_group, content_hash=fp["content_hash"]
                )
            if row is not None:
                # 别人抢先认领/登记了这份内容 → 这条不再搬（群照样补进标记）
                row_groups, seen_here = self._mark_seen_locked(conn, row, cur_group)
                conn.commit()
                return self._row_result(
                    row,
                    "claim_conflict",
                    1.0,
                    cur_group,
                    row["hit_count"],
                    row_groups=row_groups,
                    seen_here=seen_here,
                )
            self._upsert_locked(
                conn,
                fp,
                kind=kind,
                groups=groups,
                preview=preview,
                text=text,
                weak=weak,
                images=images,
                videos=videos,
                now=now,
                identities=identities,
            )
            conn.commit()
        except sqlite3.Error as exc:
            log.warning("[转发去重] 认领指纹失败，按未认领继续: %s", exc)
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
        return None

    def _upsert_locked(
        self,
        conn: sqlite3.Connection,
        fp: dict,
        kind: str = "",
        groups: list[str] | None = None,
        preview: str = "",
        text: str = "",
        weak: bool = False,
        images: list[str] | None = None,
        videos: list[dict] | None = None,
        now: int = 0,
        identities: list[str] | None = None,
    ) -> int:
        """在已持锁的连接上"插入或合并"一行指纹（调用方自己 commit / prune）。

        同一份内容（content_hash 一致）只留一行：重复登记时把群并进原记录，所以标记
        的群聊一直累积，不会散成多行互相看不见；图片/视频指纹也挂在这一行的 fp_id 下。
        内容身份（``forward_content``）同样并进这一行，供后续的跨卡片 id 判定。
        """
        groups = clean_group_ids(groups)
        images = clean_image_hashes(images)
        videos = clean_video_sigs(videos)
        identities = clean_identities(identities)
        if not now:
            now = int(time.time())
        found = self._rows_by_content_hash(conn, fp["content_hash"])
        existing = found[0] if found else None
        if existing is not None:
            # 已有这一行 → 把这次的群并进去（与 _mark_group_locked 同一套写法）
            fp_id = existing["id"]
            for one in groups:
                self._mark_group_locked(conn, fp_id, one)
            if existing["sim_algo"] != SIM_ALGO:
                # 老算法留下的 simhash → 趁这次拿到了原文，直接用新版重算一遍
                # （content_hash 相同 ⇒ 词表相同，重算的结果就是这一行自己的）
                conn.execute(
                    "UPDATE forward_fingerprint SET simhash = ?, b0 = ?, b1 = ?, "
                    "b2 = ?, b3 = ?, sim_algo = ? WHERE id = ?",
                    (fp["simhash"], fp["b0"], fp["b1"], fp["b2"], fp["b3"],
                     SIM_ALGO, fp_id),
                )
            conn.execute(
                "UPDATE forward_fingerprint SET last_seen = ? WHERE id = ?",
                (now, fp_id),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO forward_fingerprint ("
                "simhash, b0, b1, b2, b3, content_hash, kind, weak, token_cnt, "
                "preview, group_ids, first_seen, last_seen, hit_count, sim_algo"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                (
                    fp["simhash"],
                    fp["b0"],
                    fp["b1"],
                    fp["b2"],
                    fp["b3"],
                    fp["content_hash"],
                    kind or "",
                    1 if weak else 0,
                    fp["token_cnt"],
                    (preview or text)[:120],
                    ",".join(groups),
                    now,
                    now,
                    SIM_ALGO,
                ),
            )
            fp_id = cursor.lastrowid or 0
        if fp_id and images:
            known = set(self._image_rows(conn, fp_id))
            fresh = [one for one in images if one not in known]
            if fresh:
                conn.executemany(
                    "INSERT INTO forward_image ("
                    "hash, fp_id, group_ids, first_seen"
                    ") VALUES (?,?,?,?)",
                    [(one, fp_id, ",".join(groups), now) for one in fresh],
                )
        if fp_id and videos:
            known_videos = {
                (one["sig"], one["byte_hash"]) for one in self._video_rows(conn, fp_id)
            }
            fresh_videos = [
                one
                for one in videos
                if (one["sig"], one["byte_hash"]) not in known_videos
            ]
            if fresh_videos:
                conn.executemany(
                    "INSERT INTO forward_video ("
                    "sig, byte_hash, fp_id, group_ids, first_seen, bytes"
                    ") VALUES (?,?,?,?,?,?)",
                    [
                        (
                            one["sig"],
                            one["byte_hash"],
                            fp_id,
                            ",".join(groups),
                            now,
                            int(one.get("bytes") or 0),
                        )
                        for one in fresh_videos
                    ],
                )
        if fp_id and identities:
            known_ids = {
                str(row["identity"])
                for row in conn.execute(
                    "SELECT identity FROM forward_content WHERE fp_id = ?", (fp_id,)
                )
            }
            fresh_ids = [one for one in identities if one not in known_ids]
            if fresh_ids:
                conn.executemany(
                    "INSERT OR IGNORE INTO forward_content "
                    "(identity, fp_id, first_seen) VALUES (?,?,?)",
                    [(one, fp_id, now) for one in fresh_ids],
                )
        return fp_id

    def _record_sync(
        self,
        text: str,
        kind: str = "",
        group_ids: list[str] | None = None,
        preview: str = "",
        weak: bool = False,
        images: list[str] | None = None,
        videos: list[dict] | None = None,
        at: int | None = None,
        identities: list[str] | None = None,
        db_idx: int | None = None,
    ) -> int:
        """登记一次搬运（同一份内容只留一行、群并进原记录，见 _upsert_locked）。

        ``db_idx`` 非空时按主键回写（限流重试成功后的补登记）：不再按 content_hash
        重新查找，避免文本/清洗规则变化导致重试记录写到另一行上。
        """
        groups = clean_group_ids(group_ids)
        now = int(at or time.time())
        if db_idx:
            with self._thread_lock:
                conn = self._ensure_conn()
                try:
                    for one in groups:
                        self._mark_group_locked(conn, int(db_idx), one)
                    conn.execute(
                        "UPDATE forward_fingerprint SET last_seen = ? WHERE id = ?",
                        (now, int(db_idx)),
                    )
                    conn.commit()
                except sqlite3.Error as exc:
                    log.warning("[转发去重] 按主键回写失败（id=%s）: %s", db_idx, exc)
                    try:
                        conn.rollback()
                    except sqlite3.Error:
                        pass
                    return 0
                return int(db_idx)
        tokens = tokenize(text)
        if not tokens:
            # 没有正文的指纹没有检索价值，不入库
            return 0
        fp = fingerprint_of(tokens)
        with self._thread_lock:
            conn = self._ensure_conn()
            fp_id = self._upsert_locked(
                conn,
                fp,
                kind=kind,
                groups=groups,
                preview=preview,
                text=text,
                weak=weak,
                images=images,
                videos=videos,
                now=now,
                identities=identities,
            )
            conn.commit()
            self._maybe_prune(conn, now)
            return fp_id

    def _maybe_prune(self, conn: sqlite3.Connection, now: int) -> None:
        """一小时最多清理一次：先按天数删，再按条数截断，最后清掉孤儿图片行。"""
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        try:
            if self.keep_days:
                conn.execute(
                    "DELETE FROM forward_fingerprint WHERE last_seen < ?",
                    (now - int(self.keep_days) * 86400,),
                )
            if self.max_rows:
                conn.execute(
                    "DELETE FROM forward_fingerprint WHERE id NOT IN ("
                    "SELECT id FROM forward_fingerprint ORDER BY last_seen DESC LIMIT ?"
                    ")",
                    (int(self.max_rows),),
                )
            # 图片/视频/内容身份跟着指纹走，指纹没了就删掉（否则表会无限膨胀）
            conn.execute(
                "DELETE FROM forward_image WHERE fp_id NOT IN ("
                "SELECT id FROM forward_fingerprint)"
            )
            conn.execute(
                "DELETE FROM forward_video WHERE fp_id NOT IN ("
                "SELECT id FROM forward_fingerprint)"
            )
            conn.execute(
                "DELETE FROM forward_content WHERE fp_id NOT IN ("
                "SELECT id FROM forward_fingerprint)"
            )
            conn.commit()
        except Exception as exc:  # 清理失败不影响主流程
            log.warning("[转发去重] 清理旧指纹失败: %s", exc)

    def _stats_sync(self) -> dict:
        with self._thread_lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT COUNT(*) AS total, MAX(last_seen) AS latest "
                "FROM forward_fingerprint"
            ).fetchone()
            images = conn.execute("SELECT COUNT(*) AS total FROM forward_image").fetchone()
            videos = conn.execute("SELECT COUNT(*) AS total FROM forward_video").fetchone()
            return {
                "db": str(self.db_path),
                "total": row["total"],
                "latest": row["latest"],
                "images": images["total"],
                "videos": videos["total"],
            }

    # ---------------- 异步包装 ----------------
    async def find(
        self,
        text: str,
        weak: bool = False,
        images: list[str] | None = None,
        videos: list[dict] | None = None,
        group_ids: list[str] | None = None,
        cur_group: str = "",
        reserve: bool = False,
        kind: str = "",
        preview: str = "",
        image_required: bool | None = None,
        video_required: bool | None = None,
        alt_text: str = "",
        identities: list[str] | None = None,
    ) -> dict | None:
        """查历史指纹（判定语义与参数说明见 _find_sync）。

        :param kind / preview: 认领时新建指纹行要用的类型和预览
        :param alt_text: 同一份内容的**清洗前**指纹文本（别名）。清洗上线之前入库的
            老行存的是清洗前的哈希，只按清洗后的文本查永远对不上 —— 每次重发都会被
            当成新内容再搬一遍。命中后会把那一行就地升级成清洗后的哈希。
        :param identities: 内容身份（见 :func:`content_identities`）。同一份聊天记录换
            卡片 id / 来源群时，文本和媒体指纹都可能对不上，靠它认出来。
        :return: None = 没搬过（调用方搬运 + 记录）；否则"搬过了"，按结果里的
            `seen_here` 决定去发【发过了喵】还是什么都不做
        """
        async with self._async_lock:
            return await asyncio.to_thread(
                self._find_sync,
                text,
                weak=weak,
                images=images,
                videos=videos,
                group_ids=group_ids,
                cur_group=cur_group,
                reserve=reserve,
                kind=kind,
                preview=preview,
                image_required=image_required,
                video_required=video_required,
                alt_text=alt_text,
                identities=identities,
            )

    async def record(
        self,
        text: str,
        kind: str = "",
        group_ids: list[str] | None = None,
        preview: str = "",
        weak: bool = False,
        images: list[str] | None = None,
        videos: list[dict] | None = None,
        at: int | None = None,
        identities: list[str] | None = None,
        db_idx: int | None = None,
    ) -> int:
        """登记一次搬运。

        :param group_ids: 这次要标记的群（目标群 + 来源群），与 find 保持一致
        :param identities: 内容身份（见 :func:`content_identities`），与 find 保持一致
        :param at: 这次搬运/这条消息发生的时间（时间戳）。默认取当前时间；
            历史入库要传消息自己的时间，否则整库的 first_seen 全是「刚导入」
        :param db_idx: 非空时按主键回写（限流重试补登记），不再按 content_hash 查
        """
        async with self._async_lock:
            return await asyncio.to_thread(
                self._record_sync,
                text,
                kind=kind,
                group_ids=group_ids,
                preview=preview,
                weak=weak,
                images=images,
                videos=videos,
                at=at,
                identities=identities,
                db_idx=db_idx,
            )

    async def stats(self) -> dict:
        """库里现在有多少条指纹（含图片双哈希条数）"""
        async with self._async_lock:
            return await asyncio.to_thread(self._stats_sync)

    # ── 原始内容存档 ────────────────────────────────────────────
    # 存的是**清洗之前**的展开内容，所以任何时候都能按新规则重算一次指纹。
    # 按 content_hash 去重：同一份内容被搬十次也只占一份空间。
    async def save_corpus(
        self, text: str, kind: str, messages: list, *, at: int | None = None
    ) -> bool:
        """把一条转发的原始内容存进存档（按 content_hash 去重）。

        :param text: 已经拼好的指纹文本（从这里取 content_hash 作主键）
        :param messages: **清洗之前**的展开内容（``_expand_forward_for_dedup`` 的返回）
        :param at: 这条消息自己的时间；不传就用当前时间
        :return: True = 新增了一份，False = 已经有了 / 内容不可用
        """
        async with self._async_lock:
            return await asyncio.to_thread(
                self._save_corpus_sync, text, kind, messages, at=at
            )

    def _save_corpus_sync(
        self, text: str, kind: str, messages: list, *, at: int | None = None
    ) -> bool:
        if not messages:
            return False
        tokens = tokenize(text)
        if not tokens:
            return False
        content_hash = fingerprint_of(tokens)["content_hash"]
        try:
            raw = json.dumps(messages, ensure_ascii=False, default=str).encode("utf-8")
        except Exception:
            return False
        packed = zlib.compress(raw, 6)
        conn = self._ensure_conn()
        cur = conn.execute(
            "INSERT OR IGNORE INTO forward_corpus "
            "(content_hash, kind, payload, raw_size, created_at) VALUES (?, ?, ?, ?, ?)",
            (content_hash, kind or "", packed, len(raw), int(at or time.time())),
        )
        conn.commit()
        return cur.rowcount > 0

    def load_corpus(self, content_hash: str) -> list | None:
        """按 content_hash 取出存档的原始内容（没有就返回 None）。"""
        row = (
            self._ensure_conn()
            .execute(
                "SELECT payload FROM forward_corpus WHERE content_hash = ?",
                (content_hash,),
            )
            .fetchone()
        )
        if not row or not row[0]:
            return None
        try:
            return json.loads(zlib.decompress(row[0]).decode("utf-8"))
        except Exception:
            return None

    def iter_corpus(self, limit: int | None = None):
        """遍历存档：(content_hash, kind, messages)。坏行跳过。"""
        sql = "SELECT content_hash, kind, payload FROM forward_corpus ORDER BY rowid"
        if limit:
            sql += f" LIMIT {int(limit)}"
        for content_hash, kind, payload in self._ensure_conn().execute(sql):
            if not payload:
                continue
            try:
                yield content_hash, str(kind or ""), json.loads(
                    zlib.decompress(payload).decode("utf-8")
                )
            except Exception:
                continue

    def corpus_stats(self) -> dict:
        """存档规模：行数 / 原始字节 / 压缩后字节。"""
        row = (
            self._ensure_conn()
            .execute(
                "SELECT COUNT(*), COALESCE(SUM(raw_size), 0), "
                "COALESCE(SUM(LENGTH(payload)), 0) FROM forward_corpus"
            )
            .fetchone()
        )
        return {
            "rows": int(row[0] or 0),
            "raw_bytes": int(row[1] or 0),
            "packed_bytes": int(row[2] or 0),
        }


# ------------------------------------------------- 与 messaging 的边界 --------
# 卡片解析属于“消息数据层”：新实现放在 messaging/forward.py。这里保留同名
# 函数作为别名，旧的调用方（ForwardService / 测试 / 日志工具）不用改。
# messaging.forward 还没实现时（并行迁移期间）自动退回本文件的实现。
def _adopt_messaging_forward() -> None:
    try:
        from messaging import forward as _msg_forward
    except Exception:
        return
    miniapp = getattr(_msg_forward, "get_miniapp_info", None)
    resid = getattr(_msg_forward, "get_multimsg_resid", None)
    if callable(miniapp):
        globals()["get_miniapp_info"] = miniapp
    if callable(resid):
        globals()["get_multimsg_resid"] = resid


_adopt_messaging_forward()
