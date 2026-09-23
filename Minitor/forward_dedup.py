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
* **weak 指纹**（`正文文字数 < 图片数 x text_per_image`，缺省 15，见 is_text_sparse）：
  只认"文本完全一致"，不做模糊相似——否则不同人的图片刷屏会被相似度误归成同一份。
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
from collections import Counter
from pathlib import Path

try:  # 兼容 run.py 的扁平 import 和 `Minitor.forward_dedup` 包内 import
    from Paths import Paths
except ImportError:  # pragma: no cover
    from Minitor.Paths import Paths

_PROJECT_ROOT = Path(__file__).resolve().parent.parent   # 仅作兼容，别在新代码里用
# 数据库 / 日志在 data/，配置在 config/（老根目录布局仍然认）
DEFAULT_DB_PATH = Paths.data_file("forward_dedup.db")
DEFAULT_LOG_PATH = Paths.data_file("forward_dedup_log.jsonl")
CONFIG_PATH = Paths.config_file("config.json")


def _data_path(value, default: Path) -> Path:
    """相对路径只当文件名看 → 落到 data/（老部署还是根目录）；绝对路径原样用。"""
    if not value:
        return Path(default)
    path = Path(str(value))
    if path.is_absolute() or path.parent != Path("."):
        return path
    return Paths.data_file(path.name)

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
"""


def load_settings(overrides: dict | None = None) -> dict:
    """默认值 -> config.json["forward_dedup"] -> 显式参数，依次覆盖。"""
    settings = dict(_DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            user_cfg = (json.load(f) or {}).get("forward_dedup") or {}
        for key, value in user_cfg.items():
            if key in settings and value is not None:
                settings[key] = value
    except Exception:
        pass
    for key, value in (overrides or {}).items():
        if key in settings and value is not None:
            settings[key] = value
    return settings


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


def clean_group_ids(values) -> list[str]:
    """只留纯数字的群号（去重、保序），传 list / 单个值 / 逗号串都行。

    群号是纯数字，所以非数字/空值在这里就挡掉：否则会存进 "None"、
    "1054955587.0" 这种永远比不上的东西（从库里读回来的就是 "123,456"）。
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
# 有正文的转发里，昵称一律隐成这个常量（见 mask_senders）
_MASK_NAME = "user"


def normalize_line(line: str) -> str:
    """把摘要里的一行规整成 `发送者: [图片]`：保留昵称前缀（不丢发送者）、所有媒体
    占位（[动画表情]/[视频]/[文件]…）统一成 [图片]、合并空白。

    纯图转发的每行就是 `浅奈: [图片]`，除"谁 + 几张图"外没有别的文本，而这恰恰是
    这类记录的区分特征；既没昵称、去掉占位又什么都不剩的行（孤立 `[图片]`）返回空串。
    """
    text = re.sub(r"\s+", " ", (line or "").strip())
    if not text:
        return ""
    match = _NAME_PREFIX_RE.match(text)
    name = match.group(0).strip() if match else ""
    body = text[match.end() :] if match else text
    body = _MEDIA_RE.sub("[图片]", body)
    body = re.sub(r"(?:\[图片\])+", "[图片]", body).strip()
    return f"{name} {body}".strip()


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

    :param media_cnt: 真实图片张数。传了就用它；不传则按行里的占位符数
        （`[图片][图片]` -> 2）。行是 normalize_line 出来的，连续占位符会被它并
        成一个占位符，所以数得到真实张数的地方（forward_content_lines）会传进来。
    """
    text_cnt = 0
    image_cnt = 0
    for line in lines or []:
        text_cnt += len(line_body(line))  # `浅奈: 晚安[图片]` -> 2
        image_cnt += len(_MEDIA_RE.findall(line or ""))  # `[图片][图片]` -> 2
    if media_cnt is not None:
        image_cnt = int(media_cnt)  # 数好的真实张数不用再猜，比占位符数准
    return text_cnt < image_cnt * TEXT_PER_IMAGE


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
    "video": "[图片]",
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


def forward_content_lines(messages) -> tuple[list[str], int, int]:
    """把 get_forward_msg 取回的转发内容拼成摘要行（写法与卡片摘要保持一致）。

    get_forward_msg 返回的每条都是一整个消息事件（有 sender / raw_message /
    message），所以拼出来的 `浅奈: [图片]` 跟 xmlContent 里的 `<title>` 是同一种东西。

    顺手数出**真实图片张数**：normalize_line 会把一条消息里的 `[图片][图片]`
    并成一个占位符，只看行里的占位符就会把图片数看少（weak 判据「正文文字数 <
    图片数 x text_per_image」就漏了），所以在合并之前数。

    :return: (摘要行列表, 记录条数, 媒体占位符张数)
    """
    lines: list[str] = []
    total = 0
    media = 0
    for msg in messages or []:
        if not isinstance(msg, dict):
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
        line = normalize_line(f"{name}: {body}" if name else body)
        if line:
            lines.append(line)
    return lines, total, media


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


def empty_fingerprint(kind: str, images: list[str]) -> dict:
    """摘要一个字都解析不出来时的空指纹（不生成指纹，否则会互相撞车）"""
    return {"text": "", "weak": True, "kind": kind, "preview": "", "images": images}


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
    forward_messages: list | None = None,
) -> dict:
    """生成转发指纹。

    :param kind: "miniapp" / "multimsg" / "old_forward"
    :param image_hashes: 仅 weak（文本过少）时算出来的图片双哈希列表
    :param forward_messages: get_forward_msg 取回的转发内容（旧版转发专用），
        传了就优先用它拼指纹——raw / xmlContent 时有时无，不能当依赖
    :return: {"text": 指纹文本(与转发者无关), "weak": 文本区分度是否不足,
              "kind": kind, "preview": 前 120 字, "images": 图片双哈希列表}

    三类卡片各自拼成一行 `kind|...`（每段先过 normalize_line，空段丢掉）；
    `weak`（文本过少）、`images`（图片双哈希）的用法见模块说明。
    """
    images = clean_image_hashes(image_hashes)
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
        lines, total, media = forward_content_lines(forward_messages)
        if not lines:
            return empty_fingerprint(kind, images)
        # 文本够多时把昵称隐成 user，详见 mask_senders；media 是合并前的真实张数
        lines, weak = fingerprint_lines(lines, media)
        parts = ["old_forward", f"tsum{total}" if total else "", "|".join(lines)]
    elif kind == "old_forward":
        info = get_old_forward_xml_info(event)
        # 第一行通常是卡片抬头（与 <source> 同名，如"群聊的聊天记录"），不算内容
        titles = [t for t in info["titles"] if t.strip() != info["source"].strip()]
        if not any(is_informative_line(t) for t in titles) and not info["source"]:
            # 摘要一个字都没解析出来（消息结构变了）：不生成指纹，
            # 否则所有这类转发都会拼成 "old_forward" 而互相撞车
            return empty_fingerprint(kind, images)
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
        print(f"[转发去重] 写日志失败: {exc}")


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

    def __init__(self, **overrides):
        settings = load_settings(overrides)
        self.db_path = _data_path(settings["db_path"], DEFAULT_DB_PATH)
        self.hamming_threshold = int(settings["hamming_threshold"])
        self.min_tokens = int(settings["min_tokens"])
        self.token_ratio = float(settings["token_ratio"])
        self.keep_days = settings["keep_days"]
        self.max_rows = settings["max_rows"]
        self.img_hamming_threshold = int(settings["img_hamming_threshold"])
        self.img_match_ratio = float(settings["img_match_ratio"])
        self.img_max_count = int(settings["img_max_count"])
        self.img_timeout = float(settings["img_timeout"])
        self._conn: sqlite3.Connection | None = None
        self._thread_lock = threading.Lock()
        self._async_lock = asyncio.Lock()
        self._last_prune = 0.0

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
        read-modify-write 覆盖；图片行同步标记，保持 group_ids 一致。
        """
        groups = clean_group_ids([group_id])
        if groups:
            target = groups[0]
            for table, key in (("forward_fingerprint", "id"),
                               ("forward_image", "fp_id")):
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
        cur_group: str,
        required: bool = False,
    ) -> sqlite3.Row | None:
        """文本命中的多个候选里挑一个：先过图片那一关，再优先挑「该群有记录」的。

        required（本次是 weak 指纹）或候选自己就是 weak 行时，图片那一关是强制
        的（见 _image_ok）：文本一字不差也只是"可能"，图对不上就不算搬过。
        优先该群有记录，是为了同一份内容散成多行时不漏（正常情况下只有一行）；
        真挑不出来就退回第一行，由调用方按「该群没记录」处理。
        """
        usable = [
            cand
            for cand in rows
            if self._image_ok(
                conn, cand["id"], images, required=required or bool(cand["weak"])
            )
        ]
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

    def _find_by_images(
        self, conn: sqlite3.Connection, images: list[str], cur_group: str
    ) -> sqlite3.Row | None:
        """图片双哈希兜底检索：文本漂移但图片完全一致时也算搬过。"""
        placeholders = ",".join("?" * len(images))
        rows = conn.execute(
            f"{self._SELECT} WHERE id IN ("
            f"SELECT fp_id FROM forward_image WHERE hash IN ({placeholders})"
            ")",
            tuple(images),
        ).fetchall()
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
        cur_group: str,
        weak: bool,
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
            # weak（本次或候选）必须过图片这一关，见 _image_ok 的 required
            if not self._image_ok(
                conn, cand["id"], images, required=bool(weak or cand["weak"])
            ):
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
        group_ids: list[str] | None = None,
        cur_group: str = "",
        reserve: bool = False,
        kind: str = "",
        preview: str = "",
    ) -> dict | None:
        """按 整串指纹 → SimHash 相似度 → 图片双哈希 的顺序查历史。

        命中即刷新 last_seen、hit_count +1（"这份东西被搬过几次"），返回结果的
        `seen_here` = 本次事件所在群在不在标记集合里（True 去发【发过了喵】，
        False 直接结束），两种都不返回"要搬"；`group_ids` 是这次要一起标记进库的群
        （目标群 + 来源群），`images` 是本次这批图的双哈希（见模块说明），weak 指纹
        必须靠它确认（拿不到图就不算搬过）。

        reserve=True 把"查 + 认领"合成一次原子操作：判定为"没搬过、要搬"时立刻把
        group_ids 写进库，让几乎同时到达的第二条来源消息（a 群、b 群各发一次）能
        看见这条认领记录，不会两边都判"没搬过"而在目标群里各发一遍；被抢先认领时
        返回一条结果，由调用方按 `seen_here` 决定发提示还是直接结束。
        """
        tokens = tokenize(text)
        images = clean_image_hashes(images)
        marked = clean_group_ids(group_ids)
        cur_group = str(cur_group or "")
        if not tokens:
            return None
        fp = fingerprint_of(tokens)
        now = int(time.time())
        with self._thread_lock:
            conn = self._ensure_conn()
            # 整串指纹完全一致（含 tSum/全部行）就算搬运过；weak 不例外，但必须由
            # 图片确认：少图模板聊天记录文本一字不差太常见了。
            rows = self._rows_by_content_hash(conn, fp["content_hash"])
            row = self._pick_candidate(conn, rows, images, cur_group, required=weak)
            reason = "content_hash" if row is not None else ""
            similarity = 1.0
            # 模糊相似度（候选怎么挑见 _simhash_row）
            if row is None and len(tokens) >= self.min_tokens:
                row, dist = self._simhash_row(
                    conn, fp, len(tokens), images, cur_group, weak
                )
                if row is not None:
                    reason = "simhash"
                    similarity = 1 - dist / SIMHASH_BITS
            # 图片双哈希兜底：文本（条数/抬头）漂了但图片完全一致，仍是同一份
            if row is None and images:
                row = self._find_by_images(conn, images, cur_group)
                if row is not None:
                    reason = "image"
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
        now: int,
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
            # 同一个内容哈希下面可能躺着多行（摘要一样但图片不是同一批），
            # 所以要按和外面一样的规矩挑候选，不能只看第一行（weak 同样要图确认）
            row = self._pick_candidate(conn, rows, images, cur_group, required=weak)
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
                now=now,
            )
            conn.commit()
        except sqlite3.Error as exc:
            print(f"[转发去重] 认领指纹失败，按未认领继续: {exc}")
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
        now: int = 0,
    ) -> int:
        """在已持锁的连接上"插入或合并"一行指纹（调用方自己 commit / prune）。

        同一份内容（content_hash 一致）只留一行：重复登记时把群并进原记录，所以标记
        的群聊一直累积，不会散成多行互相看不见；图片哈希也挂在这一行的 fp_id 下。
        """
        groups = clean_group_ids(groups)
        images = clean_image_hashes(images)
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
        return fp_id

    def _record_sync(
        self,
        text: str,
        kind: str = "",
        group_ids: list[str] | None = None,
        preview: str = "",
        weak: bool = False,
        images: list[str] | None = None,
    ) -> int:
        """登记一次搬运（同一份内容只留一行、群并进原记录，见 _upsert_locked）。"""
        tokens = tokenize(text)
        if not tokens:
            # 没有正文的指纹没有检索价值，不入库
            return 0
        fp = fingerprint_of(tokens)
        now = int(time.time())
        with self._thread_lock:
            conn = self._ensure_conn()
            fp_id = self._upsert_locked(
                conn,
                fp,
                kind=kind,
                groups=group_ids,
                preview=preview,
                text=text,
                weak=weak,
                images=images,
                now=now,
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
            # 图片双哈希跟着指纹走，指纹没了就删掉（否则表会无限膨胀）
            conn.execute(
                "DELETE FROM forward_image WHERE fp_id NOT IN ("
                "SELECT id FROM forward_fingerprint)"
            )
            conn.commit()
        except Exception as exc:  # 清理失败不影响主流程
            print(f"[转发去重] 清理旧指纹失败: {exc}")

    def _stats_sync(self) -> dict:
        with self._thread_lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT COUNT(*) AS total, MAX(last_seen) AS latest "
                "FROM forward_fingerprint"
            ).fetchone()
            images = conn.execute("SELECT COUNT(*) AS total FROM forward_image").fetchone()
            return {
                "db": str(self.db_path),
                "total": row["total"],
                "latest": row["latest"],
                "images": images["total"],
            }

    # ---------------- 异步包装 ----------------
    async def find(
        self,
        text: str,
        weak: bool = False,
        images: list[str] | None = None,
        group_ids: list[str] | None = None,
        cur_group: str = "",
        reserve: bool = False,
        kind: str = "",
        preview: str = "",
    ) -> dict | None:
        """查历史指纹（判定语义与参数说明见 _find_sync）。

        :param kind / preview: 认领时新建指纹行要用的类型和预览
        :return: None = 没搬过（调用方搬运 + 记录）；否则"搬过了"，按结果里的
            `seen_here` 决定去发【发过了喵】还是什么都不做
        """
        async with self._async_lock:
            return await asyncio.to_thread(
                self._find_sync,
                text,
                weak=weak,
                images=images,
                group_ids=group_ids,
                cur_group=cur_group,
                reserve=reserve,
                kind=kind,
                preview=preview,
            )

    async def record(
        self,
        text: str,
        kind: str = "",
        group_ids: list[str] | None = None,
        preview: str = "",
        weak: bool = False,
        images: list[str] | None = None,
    ) -> int:
        """登记一次搬运。

        :param group_ids: 这次要标记的群（目标群 + 来源群），与 find 保持一致
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
            )

    async def stats(self) -> dict:
        """库里现在有多少条指纹（含图片双哈希条数）"""
        async with self._async_lock:
            return await asyncio.to_thread(self._stats_sync)
