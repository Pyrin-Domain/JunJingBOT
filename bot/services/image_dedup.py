"""图片搬运的「送达库」——和聊天记录指纹库**不是同一个库**。

为什么要分开：

* 聊天记录比对的是**内容**（文本 + 图片双哈希 + SimHash），一条内容一行，群是
  「标记集合」，可以一条记录横跨很多群；
* 图片搬运没有正文可比，能认的只有**消息身份**（``real_id`` / ``message_id``）
  和「送到过哪些会话」。它是「谁送过给谁」的流水账，粒度是 (身份, 会话) 对，
  和内容指纹数据结构完全对不上。混在一个库里，两套主键会互相打架。

所以这里是独立的 SQLite 文件 ``data/forward_image.db``、独立的一张表：

    forward_image_delivery(identity, chat, first_seen, last_seen)
    PRIMARY KEY (identity, chat)

``identity`` 形如 ``real_id:123`` / ``message_id:20002:456``（同一条消息可能有多个
身份，见 ``ForwardService._message_identities``）；``chat`` 形如 ``g:20002`` /
``u:10001``。判「搬没搬过」= 这次任意一个身份 + 目标会话命中过任意一行。

除了消息身份，还有三条**内容级**身份，解决「同一张图换了消息 id 就重复搬」：

* ``image:*`` / ``image_fileid:*``：图片段 ``data.file`` / URL ``fileid`` 的哈希
  （文件名是 md5 时等价于字节哈希）；
* ``imgsha:*``：图片**真实字节**的哈希（文件名字段不是哈希时兜底）；
* ``phash:*``：图片本体的感知指纹（aHash+dHash），按汉明距离模糊匹配 ——
  QQ 重新编码后 md5 会变，只有它能认出「同一张图」。

它还是**持久化**的，所以重启后断点回放不会再把重启前搬过的图片搬一遍（旧实现是
进程内 OrderedDict，重启就忘）。
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time
from pathlib import Path

from config.paths import AppPaths

_PATHS = AppPaths.discover()
DEFAULT_DB_PATH = _PATHS.data_file("forward_image.db")

_DEFAULTS = {
    # 图片查重总开关；关闭后直接搬运，不查询 / 登记送达记录。
    "enable": True,
    # 空 = 用 settings.paths 下的默认位置（data/forward_image.db）；
    # 相对路径只当文件名，落在 data/ 下。
    "db_path": "",
    # 送达记录保留天数：0/None 表示不清理
    "keep_days": 30,
    # 图片内容级身份（image:*/image_fileid:*）的独立短保留：
    # 内容身份只用于「同一张图换了消息 id」的去重，不应永久抑制合法重发。
    # 0/None 表示不单独清理（用 keep_days 兜底）。
    "image_keep_days": 2,
    # 感知哈希兜底：按图片本体算 aHash+dHash，模糊匹配已送达的图。
    #
    # 为什么必须有：``image:*`` 身份是 ``data.file`` 的哈希，而 QQ 把同一张图
    # 重新上传/重新编码后文件名（md5）就变了 —— 2026-11-06 08:41 与 14:57 的
    # 同一组 5 张截图（像素完全相同、文件名大小写不同）就是这样各搬了一遍。
    # 关掉后只剩「消息身份 + 文件名哈希」两条线，重新编码的图会重复搬运。
    "perceptual": True,
    # 模糊匹配阈值：aHash(64bit)+dHash(64bit) 共 128bit 的汉明距离。
    # 实测同一张图重新压缩/缩放后距离 0~2；不同截图（同一条推文的两次截图）
    # 距离约 5，所以默认 4 —— 只认「同一张图」。
    "perceptual_max_distance": 4,
    # 感知指纹（phash:*）与字节身份（imgsha:*）的独立保留天数；
    # 0/None 表示不单独清理（用 keep_days 兜底）。
    "perceptual_keep_days": 0,
    # 单张图下载超时（秒）。图片过滤门已经下过时直接命中它的字节缓存，不走网络。
    "perceptual_timeout": 8.0,
}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS forward_image_delivery (
    identity   TEXT    NOT NULL,
    chat       TEXT    NOT NULL,
    first_seen INTEGER NOT NULL DEFAULT 0,
    last_seen  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity, chat)
);
CREATE INDEX IF NOT EXISTS idx_fwimg_delivery_seen
    ON forward_image_delivery(last_seen);
"""

__all__ = [
    "BYTES_PREFIX",
    "DEFAULT_DB_PATH",
    "ForwardImageStore",
    "PERCEPTUAL_BITS",
    "PERCEPTUAL_PREFIX",
    "bytes_identity",
    "chat_key",
    "perceptual_identity",
]

#: 图片字节内容身份前缀（``imgsha:<16 hex>``）：文件名字段不是哈希时（个别协议端
#: 给的是随机名 / 本地缓存名），靠它按**真实字节**认出同一张图。
BYTES_PREFIX = "imgsha:"
#: 感知指纹前缀（``phash:<32 hex>`` = aHash 16 hex + dHash 16 hex）。
PERCEPTUAL_PREFIX = "phash:"

#: 双哈希合计位数（aHash 64 + dHash 64），阈值上限就是它。
PERCEPTUAL_BITS = 128


def chat_key(*, group_id=None, user_id=None) -> str:
    """会话键：``g:<群号>`` / ``u:<QQ>``，群和私聊不会撞。"""
    if group_id is not None:
        return f"g:{group_id}"
    if user_id is not None:
        return f"u:{user_id}"
    return ""


def bytes_identity(data: bytes) -> str:
    """图片**真实字节**的内容身份：文件名换了也认得出同一张图。"""
    if not data:
        return ""
    return BYTES_PREFIX + hashlib.blake2b(bytes(data), digest_size=8).hexdigest()


def perceptual_identity(value: str) -> str:
    """把 ``media.image_hash.image_hash()`` 的 32 位 hex 包成感知身份。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return PERCEPTUAL_PREFIX + text


def _data_path(value, default: Path, settings=None) -> Path:
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


def _load_settings(overrides: dict | None = None, settings=None) -> dict:
    values = dict(_DEFAULTS)
    section = None
    if settings is not None:
        reader = getattr(settings, "section", None)
        if callable(reader):
            try:
                section = reader("forward_image_dedup")
            except Exception:
                section = None
        elif isinstance(settings, dict):
            raw = settings.get("forward_image_dedup")
            section = raw if isinstance(raw, dict) else None
    if isinstance(section, dict):
        values.update({k: v for k, v in section.items() if k in _DEFAULTS})
    if overrides:
        values.update({k: v for k, v in overrides.items() if k in _DEFAULTS})
    return values


class ForwardImageStore:
    """图片送达记录（身份 × 会话）。线程内共享一个连接，写操作串行。"""

    def __init__(self, db_path=None, settings=None, **overrides):
        self.settings = settings
        config = _load_settings(overrides, settings=settings)
        if db_path is None:
            default_path = DEFAULT_DB_PATH
            paths = getattr(settings, "paths", None) if settings is not None else None
            if paths is not None:
                try:
                    default_path = Path(paths.data_file("forward_image.db"))
                except Exception:
                    default_path = DEFAULT_DB_PATH
            self.db_path = _data_path(config.get("db_path"), default_path, settings)
        else:
            self.db_path = Path(str(db_path))
        self.enable = bool(config.get("enable", True))
        self.keep_days = config.get("keep_days")
        self.image_keep_days = config.get("image_keep_days")
        self.perceptual = bool(config.get("perceptual", True))
        self.perceptual_max_distance = config.get("perceptual_max_distance")
        self.perceptual_keep_days = config.get("perceptual_keep_days")
        self.perceptual_timeout = config.get("perceptual_timeout")
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._last_prune = 0.0

    # ---------------- 连接 ----------------
    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        raw = str(self.db_path)
        is_memory = raw in (":memory:", "")
        if not is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(raw, check_same_thread=False, timeout=15)
        conn.row_factory = sqlite3.Row
        if not is_memory:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA_SQL)
        conn.commit()
        self._conn = conn
        return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ---------------- 判定 / 登记 ----------------
    def delivered(self, identities, chats) -> set[str]:
        """这些身份在这几个会话里，哪些已经送达过。"""
        keys = [str(item) for item in (identities or []) if str(item)]
        targets = [str(item) for item in (chats or []) if str(item)]
        if not keys or not targets:
            return set()
        ident_sql = ",".join("?" * len(keys))
        chat_sql = ",".join("?" * len(targets))
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(
                "SELECT DISTINCT chat FROM forward_image_delivery "
                f"WHERE identity IN ({ident_sql}) AND chat IN ({chat_sql})",
                tuple(keys) + tuple(targets),
            ).fetchall()
        return {str(row["chat"]) for row in rows}

    async def delivered_async(self, identities, chats) -> set[str]:
        """异步入口：SQLite 查询放到工作线程，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.delivered, identities, chats)

    # ---------------- 感知指纹（模糊匹配） ----------------
    def _perceptual_settings(self, max_distance) -> tuple[int, int]:
        """(阈值, 时间截止点)。截止点 0 = 不按时间过滤。"""
        raw = self.perceptual_max_distance if max_distance is None else max_distance
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = 4
        limit = max(0, min(limit, PERCEPTUAL_BITS))
        try:
            days = int(self.perceptual_keep_days or 0)
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            try:
                days = int(self.keep_days)
            except (TypeError, ValueError):
                days = 0
        cutoff = int(time.time()) - days * 86400 if days > 0 else 0
        return limit, cutoff

    def matched_phash(self, chats, hashes, max_distance=None) -> dict[str, set[int]]:
        """这些会话里，候选图（按索引）哪些已经有「送过的近邻」。

        返回值 ``{chat: {候选图下标, ...}}``：调用方据此判断「这条消息里的每张图
        是不是都已经送过」。**不能只判一张命中就跳过** —— 多图消息里「旧图 + 新图」
        必须照常搬运，否则新图就被吞了。

        逐行算汉明距离（图片身份是小表，按 ``identity LIKE 'phash:%'`` 走主键
        前缀扫描，几百行量级），不做近似索引。
        """
        targets = [str(item) for item in (chats or []) if str(item)]
        wanted = [str(item) for item in (hashes or []) if str(item)]
        if not targets or not wanted:
            return {}
        values: list[int] = []
        for item in wanted:
            text = item[len(PERCEPTUAL_PREFIX):] if item.startswith(PERCEPTUAL_PREFIX) else item
            try:
                values.append(int(text, 16))
            except (TypeError, ValueError):
                values.append(-1)
        limit, cutoff = self._perceptual_settings(max_distance)
        chat_sql = ",".join("?" * len(targets))
        sql = (
            "SELECT identity, chat FROM forward_image_delivery "
            f"WHERE identity LIKE '{PERCEPTUAL_PREFIX}%' AND chat IN ({chat_sql})"
        )
        params: list = list(targets)
        if cutoff:
            sql += " AND last_seen >= ?"
            params.append(cutoff)
        with self._lock:
            conn = self._ensure_conn()
            rows = conn.execute(sql, tuple(params)).fetchall()
        matched: dict[str, set[int]] = {}
        for row in rows:
            identity = str(row["identity"])
            try:
                delivered_value = int(identity[len(PERCEPTUAL_PREFIX):], 16)
            except (TypeError, ValueError):
                continue
            chat = str(row["chat"])
            for index, value in enumerate(values):
                if value < 0:
                    continue
                if bin(value ^ delivered_value).count("1") <= limit:
                    matched.setdefault(chat, set()).add(index)
        return matched

    async def matched_phash_async(
        self, chats, hashes, max_distance=None
    ) -> dict[str, set[int]]:
        """异步入口：模糊匹配同样放到工作线程。"""
        return await asyncio.to_thread(
            self.matched_phash, chats, hashes, max_distance
        )

    def remember(self, identities, chats, now: int | None = None) -> None:
        """登记「这些身份已经送到这些会话」。同一条重复登记只刷新时间。"""
        keys = [str(item) for item in (identities or []) if str(item)]
        targets = [str(item) for item in (chats or []) if str(item)]
        if not keys or not targets:
            return
        stamp = int(now if now is not None else time.time())
        with self._lock:
            conn = self._ensure_conn()
            conn.executemany(
                "INSERT INTO forward_image_delivery(identity, chat, first_seen, last_seen) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(identity, chat) DO UPDATE SET last_seen = excluded.last_seen",
                [(key, chat, stamp, stamp) for key in keys for chat in targets],
            )
            conn.commit()
        self._maybe_prune(now=stamp)

    async def remember_async(
        self, identities, chats, now: int | None = None
    ) -> None:
        """异步入口：写库和定期清理都放到工作线程执行。"""
        await asyncio.to_thread(self.remember, identities, chats, now)

    def _maybe_prune(self, now: int | None = None) -> None:
        days = self.keep_days
        try:
            days = int(days)
        except (TypeError, ValueError):
            return
        if days <= 0:
            return
        stamp = int(now if now is not None else time.time())
        if stamp - self._last_prune < 3600:  # 每小时最多清一次
            return
        self._last_prune = stamp
        cutoff = stamp - days * 86400
        with self._lock:
            conn = self._ensure_conn()
            conn.execute(
                "DELETE FROM forward_image_delivery WHERE last_seen < ?", (cutoff,)
            )
            # 图片内容级身份（image:*/image_fileid:*）用更短 TTL，避免合法重发被永久吞掉
            image_days = self.image_keep_days
            try:
                image_days = int(image_days or 0)
            except (TypeError, ValueError):
                image_days = 0
            if image_days > 0:
                image_cutoff = stamp - image_days * 86400
                conn.execute(
                    "DELETE FROM forward_image_delivery "
                    "WHERE last_seen < ? AND (identity LIKE 'image:%' OR identity LIKE 'image_fileid:%')",
                    (image_cutoff,),
                )
            # 感知指纹 / 字节身份：同样是「内容级身份」，保留策略可以单独配
            perceptual_days = self.perceptual_keep_days
            try:
                perceptual_days = int(perceptual_days or 0)
            except (TypeError, ValueError):
                perceptual_days = 0
            if perceptual_days > 0:
                perceptual_cutoff = stamp - perceptual_days * 86400
                conn.execute(
                    "DELETE FROM forward_image_delivery "
                    "WHERE last_seen < ? AND (identity LIKE 'phash:%' OR identity LIKE 'imgsha:%')",
                    (perceptual_cutoff,),
                )
            conn.commit()

    # ---------------- 排查 ----------------
    def count(self) -> int:
        with self._lock:
            conn = self._ensure_conn()
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM forward_image_delivery"
            ).fetchone()
        return int(row["n"] if row else 0)
