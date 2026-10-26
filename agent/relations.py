r"""人物关系档案：关系标签、人格画像、近期状态、重要记忆。

设计参考 ``D:\Code\SuperMaGe\Config\Profiles``，但这里不只是只读资料：

* **关系标签**（labels）可变，例如 ``10001 -> 主人``；可人工维护，也可由模型记录。
* **人格画像**（persona / relation）是对一个人的长期理解，变化很慢。
* **近期状态**（states）一定带时间，并且**会过期**；几天前的难过不能带进今天。
* **重要记忆**（memories）长期保留，但只在相关时注入，不逐条复述。

数据落在 ``data/relations/``（可在 ``bot_config.json`` 的 ``relations`` 段改）::

    data/relations/
    ├─ users/<QQ>.json
    ├─ groups/<群号>.json
    └─ self.json              # 君景自己的心情 / 状态，按轮更新

这个模块只负责“存 / 取 / 渲染 / 过期”，不调用模型。
自动整理（auto extract）在 :mod:`agent.runtime` 里用判断模型跑，
写回时仍然走这里的 :meth:`RelationStore.apply_extraction`，保证格式和边界一致。
"""

from __future__ import annotations

import json
import re
import threading
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from core.jsonstore import read_json, write_json

# ── 默认上限（都能被 bot_config.json 的 relations 段覆盖）─────────
DEFAULT_MAX_PEOPLE = 6
DEFAULT_MAX_CHARS = 1200
DEFAULT_MAX_PERSON_CHARS = 320
DEFAULT_STATE_TTL_HOURS = 72
DEFAULT_STATE_MAX_HOURS = 168
DEFAULT_SELF_TTL_HOURS = 12
DEFAULT_MAX_STATES = 6
DEFAULT_MAX_MEMORIES = 30
DEFAULT_MAX_LABELS = 8

# 自动整理：每多少条“当面消息”跑一次、两次之间至少隔多久
DEFAULT_AUTO_EVERY = 8
DEFAULT_AUTO_MIN_SECONDS = 600

_LABEL_SPLIT = re.compile(r"[、,，/|；;]+")
_SAFE_TAG = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+")
_TS_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


# ══════════════════════════════════════════════════════════
# 小工具
# ══════════════════════════════════════════════════════════
def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _as_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _clip(text: str, limit: int) -> str:
    text = _text(text)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _split_items(value: Any, *, limit: int = 12) -> list[str]:
    """把 ``["a","b"]`` / ``"a、b"`` / ``"a\\nb"`` 统一成去重列表。"""
    if value is None:
        return []
    raw: Iterable[Any]
    if isinstance(value, str):
        raw = re.split(r"[、,，/|；;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = [value]
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("text") or item.get("name") or ""
        item = _text(item)
        if item and item not in out:
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _now() -> datetime:
    return datetime.now()


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_time(value: Any) -> Optional[datetime]:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1]
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _age_label(at: Optional[datetime], now: Optional[datetime] = None) -> str:
    if at is None:
        return ""
    now = now or _now()
    seconds = max(0.0, (now - at).total_seconds())
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}分钟前"
    if seconds < 86400:
        return f"{max(1, int(seconds // 3600))}小时前"
    return f"{max(1, int(seconds // 86400))}天前"


def _safe_tag(value: str, fallback: str = "unknown") -> str:
    text = _SAFE_TAG.sub("_", _text(value)).strip("_")
    return text or fallback


def _clean_label(value: Any) -> str:
    """标签要短、不能换行；太长的一律丢掉（避免模型塞整句话进来）。"""
    text = _text(value).replace("\n", " ")
    text = re.sub(r"[\[\]{}<>]", "", text)
    if not text or len(text) > 10:
        return ""
    return text


def _dedupe_states(states: Any) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for item in states or []:
        if isinstance(item, str):
            item = {"text": item, "at": _iso(_now())}
        if not isinstance(item, dict):
            continue
        text = _clip(item.get("text"), 80)
        if not text:
            continue
        key = re.sub(r"\s+", "", text)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "text": text,
                "at": _text(item.get("at")) or _iso(_now()),
                "expires_at": _text(item.get("expires_at")),
                "source": _text(item.get("source")) or "manual",
            }
        )
    return out


# ══════════════════════════════════════════════════════════
# 存储
# ══════════════════════════════════════════════════════════
class RelationStore:
    """按人 / 按群维护关系档案，并渲染成本轮的 ``<relations>`` 提示块。"""

    def __init__(
        self,
        root: str | Path | None,
        *,
        enabled: bool = True,
        master_ids: Iterable[str] = (),
        master_label: str = "主人",
        max_people: int = DEFAULT_MAX_PEOPLE,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_person_chars: int = DEFAULT_MAX_PERSON_CHARS,
        state_ttl_hours: int = DEFAULT_STATE_TTL_HOURS,
        state_max_hours: int = DEFAULT_STATE_MAX_HOURS,
        self_ttl_hours: int = DEFAULT_SELF_TTL_HOURS,
        max_states: int = DEFAULT_MAX_STATES,
        max_memories: int = DEFAULT_MAX_MEMORIES,
        max_labels: int = DEFAULT_MAX_LABELS,
        auto_extract: bool = True,
        auto_every: int = DEFAULT_AUTO_EVERY,
        auto_min_seconds: int = DEFAULT_AUTO_MIN_SECONDS,
    ):
        self.enabled = bool(enabled) and root is not None
        self.root = Path(root) if root is not None else None
        self.users_dir = self.root / "users" if self.root else None
        self.groups_dir = self.root / "groups" if self.root else None
        self.self_path = self.root / "self.json" if self.root else None
        self.master_ids = [str(qq) for qq in master_ids if str(qq)]
        self.master_label = master_label or "主人"
        self.max_people = max(1, int(max_people))
        self.max_chars = max(200, int(max_chars))
        self.max_person_chars = max(80, int(max_person_chars))
        self.state_ttl = max(1, int(state_ttl_hours))
        self.state_max = max(self.state_ttl, int(state_max_hours))
        self.self_ttl = max(1, int(self_ttl_hours))
        self.max_states = max(1, int(max_states))
        self.max_memories = max(1, int(max_memories))
        self.max_labels = max(1, int(max_labels))
        self.auto_extract = bool(auto_extract)
        self.auto_every = max(1, int(auto_every))
        self.auto_min_seconds = max(0, int(auto_min_seconds))

        self._lock = threading.RLock()
        self._users: dict[str, dict] = {}
        self._groups: dict[str, dict] = {}
        self._self_status: dict = {}
        self._seen_ids: list[str] = []
        self._counters: dict[str, int] = {}
        self._last_extract: dict[str, float] = {}

    # ── 构造 ──
    @classmethod
    def from_settings(cls, settings: Any = None) -> "RelationStore":
        if settings is None:
            return cls(None, enabled=False)
        section = {}
        try:
            section = settings.section("relations") or {}
        except Exception:  # noqa: BLE001 - 旧配置没有这个段
            section = {}
        root: Path | None = None
        try:
            root = Path(settings.paths.data_dir) / "relations"
        except Exception:  # noqa: BLE001
            root = None
        raw_dir = _text(section.get("data_dir"))
        if raw_dir:
            path = Path(raw_dir).expanduser()
            if not path.is_absolute():
                base = getattr(getattr(settings, "paths", None), "root", None)
                path = (Path(base) if base else Path.cwd()) / path
            root = path
        try:
            masters = list(settings.master_qq)
        except Exception:  # noqa: BLE001
            masters = []
        return cls(
            root,
            enabled=_as_bool(section.get("enable"), True),
            master_ids=masters,
            master_label=_text(section.get("master_label")) or "主人",
            max_people=_as_int(section.get("max_people"), DEFAULT_MAX_PEOPLE),
            max_chars=_as_int(section.get("max_chars"), DEFAULT_MAX_CHARS, minimum=200),
            max_person_chars=_as_int(
                section.get("max_person_chars"), DEFAULT_MAX_PERSON_CHARS, minimum=80
            ),
            state_ttl_hours=_as_int(
                section.get("state_ttl_hours"), DEFAULT_STATE_TTL_HOURS
            ),
            state_max_hours=_as_int(
                section.get("state_max_hours"), DEFAULT_STATE_MAX_HOURS
            ),
            self_ttl_hours=_as_int(
                section.get("self_ttl_hours"), DEFAULT_SELF_TTL_HOURS
            ),
            max_states=_as_int(section.get("max_states"), DEFAULT_MAX_STATES),
            max_memories=_as_int(section.get("max_memories"), DEFAULT_MAX_MEMORIES),
            max_labels=_as_int(section.get("max_labels"), DEFAULT_MAX_LABELS),
            auto_extract=_as_bool(section.get("auto_extract"), True),
            auto_every=_as_int(section.get("auto_every"), DEFAULT_AUTO_EVERY),
            auto_min_seconds=_as_int(
                section.get("auto_extract_min_seconds"), DEFAULT_AUTO_MIN_SECONDS,
                minimum=0,
            ),
        )

    # ── 读写 ──
    def _user_path(self, qq: str) -> Path:
        return self.users_dir / f"{_safe_tag(qq)}.json"  # type: ignore[union-attr]

    def _group_path(self, group_id: str) -> Path:
        return self.groups_dir / f"{_safe_tag(group_id)}.json"  # type: ignore[union-attr]

    @staticmethod
    def _default_user(qq: str) -> dict:
        return {
            "qq": str(qq),
            "name": "",
            "aliases": [],
            "labels": [],
            "persona": "",
            "relation": "",
            "description": "",
            "tags": [],
            "boundaries": [],
            "states": [],
            "memories": [],
            "group_notes": {},
            "stats": {"first_seen": "", "last_seen": "", "messages": 0, "last_reply_at": ""},
            "updated_at": "",
        }

    @staticmethod
    def _default_group(group_id: str) -> dict:
        return {
            "group_id": str(group_id),
            "name": "",
            "description": "",
            "topics": [],
            "lore": [],
            "catchphrases": [],
            "notes": "",
            "common_users": [],
            "updated_at": "",
        }

    @staticmethod
    def _normalize_user(data: Any, qq: str) -> dict:
        base = RelationStore._default_user(qq)
        if not isinstance(data, dict):
            return base
        base.update({key: data[key] for key in data if key in base})
        base["qq"] = _text(base.get("qq")) or str(qq)
        base["name"] = _text(base.get("name"))
        base["persona"] = _text(base.get("persona"))
        base["relation"] = _text(base.get("relation"))
        base["description"] = _text(base.get("description"))
        for key in ("aliases", "tags", "boundaries"):
            base[key] = _split_items(base.get(key))
        labels: list[dict] = []
        for item in base.get("labels") or []:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = _clean_label(item.get("text"))
            if text:
                labels.append(
                    {
                        "text": text,
                        "at": _text(item.get("at")) or _iso(_now()),
                        "source": _text(item.get("source")) or "manual",
                    }
                )
        base["labels"] = labels
        base["states"] = _dedupe_states(base.get("states"))
        memories: list[dict] = []
        for item in base.get("memories") or []:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = _clip(item.get("text"), 120)
            if text:
                memories.append(
                    {
                        "text": text,
                        "at": _text(item.get("at")) or _iso(_now()),
                        "importance": _as_int(item.get("importance"), 2, minimum=1),
                        "source": _text(item.get("source")) or "manual",
                    }
                )
        base["memories"] = memories
        if not isinstance(base.get("group_notes"), dict):
            base["group_notes"] = {}
        stats = base.get("stats") if isinstance(base.get("stats"), dict) else {}
        base["stats"] = {
            "first_seen": _text(stats.get("first_seen")),
            "last_seen": _text(stats.get("last_seen")),
            "messages": _as_int(stats.get("messages"), 0, minimum=0),
            "last_reply_at": _text(stats.get("last_reply_at")),
        }
        return base

    @staticmethod
    def _normalize_group(data: Any, group_id: str) -> dict:
        base = RelationStore._default_group(group_id)
        if isinstance(data, dict):
            base.update({key: data[key] for key in data if key in base})
        base["group_id"] = _text(base.get("group_id")) or str(group_id)
        for key in ("name", "description", "notes"):
            base[key] = _text(base.get(key))
        for key in ("topics", "lore", "catchphrases", "common_users"):
            base[key] = _split_items(base.get(key), limit=30)
        return base

    def user(self, qq: str, *, create: bool = False) -> dict:
        """取一个人的档案；``create=False`` 时不存在就返回空字典。"""
        qq = _text(qq)
        if not self.enabled or not qq:
            return {}
        with self._lock:
            if qq in self._users:
                return self._users[qq]
            data = self._normalize_user(read_json(self._user_path(qq), {}), qq)
            # 人工维护的 JSON 可能只写了 description / relation / states 之类，
            # 不能只认 labels / persona / memories，否则“直接改 JSON”这条维护路径会失效。
            exists = any(
                (
                    data.get("name"),
                    data.get("aliases"),
                    data.get("labels"),
                    data.get("persona"),
                    data.get("relation"),
                    data.get("description"),
                    data.get("tags"),
                    data.get("boundaries"),
                    data.get("states"),
                    data.get("memories"),
                    data.get("group_notes"),
                    data.get("stats", {}).get("first_seen"),
                )
            )
            if exists:
                self._users[qq] = data
                return data
            if not create:
                return data if data.get("name") or data.get("labels") else {}
            data["stats"]["first_seen"] = _iso(_now())
            self._users[qq] = data
            return data

    def group(self, group_id: str, *, create: bool = False) -> dict:
        group_id = _text(group_id)
        if not self.enabled or not group_id:
            return {}
        with self._lock:
            if group_id in self._groups:
                return self._groups[group_id]
            data = self._normalize_group(read_json(self._group_path(group_id), {}), group_id)
            if any(
                (
                    data.get("name"),
                    data.get("description"),
                    data.get("topics"),
                    data.get("lore"),
                    data.get("catchphrases"),
                    data.get("notes"),
                    data.get("common_users"),
                )
            ):
                self._groups[group_id] = data
                return data
            if not create:
                return {}
            self._groups[group_id] = data
            return data

    def _save_user(self, data: dict) -> None:
        if not self.enabled or self.users_dir is None:
            return
        data["updated_at"] = _iso(_now())
        write_json(self._user_path(data["qq"]), data)

    def _save_group(self, data: dict) -> None:
        if not self.enabled or self.groups_dir is None:
            return
        data["updated_at"] = _iso(_now())
        write_json(self._group_path(data["group_id"]), data)

    def _save_self(self) -> None:
        if not self.enabled or self.self_path is None:
            return
        write_json(self.self_path, self._self_status)

    # ── 观察 / 统计 ──
    def observe_context(self, context: Any) -> None:
        """每条消息的自动落账：先见时间、最后活跃、消息数、群名。

        只做无 LLM 的统计；关系标签 / 记忆的自动整理见 :meth:`should_extract`。
        """
        if not self.enabled or context is None:
            return
        try:
            names = context.namebook()
        except Exception:  # noqa: BLE001
            names = {}
        group_id = _text(getattr(context, "group_id", ""))
        now = _iso(_now())

        with self._lock:
            # 群档案：第一次见到就把群名记下来
            if getattr(context, "chat_type", "") == "group" and group_id:
                group = self.group(group_id, create=True)
                if group:
                    dirty = False
                    if not group.get("name"):
                        name = _text(getattr(context, "group_name", ""))
                        if name:
                            group["name"] = name
                            dirty = True
                    current = getattr(context, "current", None)
                    qq = _text(getattr(getattr(current, "sender", None), "qq", ""))
                    if qq and qq not in group["common_users"]:
                        group["common_users"] = (group["common_users"] + [qq])[-30:]
                        dirty = True
                    if dirty:
                        self._save_group(group)

            # 主人：第一次见到就种上「主人」标签（只补，不覆盖人工改过的）
            for master in self.master_ids:
                profile = self.user(master, create=True)
                if not profile:
                    continue
                dirty = False
                if not profile.get("name"):
                    name = _text(names.get(master))
                    if name:
                        profile["name"] = name
                        dirty = True
                if self._add_label(profile, self.master_label, source="seed"):
                    dirty = True
                if dirty:
                    self._save_user(profile)

            # 当前说话人：消息数 / 最后活跃（同一条 message_id 只算一次）
            current = getattr(context, "current", None)
            sender = getattr(current, "sender", None)
            qq = _text(getattr(sender, "qq", ""))
            message_id = _text(getattr(current, "message_id", ""))
            if qq and not getattr(sender, "is_self", False):
                if not message_id or message_id not in self._seen_ids:
                    if message_id:
                        self._seen_ids.append(message_id)
                        self._seen_ids = self._seen_ids[-200:]
                    profile = self.user(qq, create=True)
                    if profile:
                        profile["name"] = profile.get("name") or _text(
                            names.get(qq) or getattr(sender, "name", "")
                        )
                        stats = profile.setdefault("stats", {})
                        stats["first_seen"] = stats.get("first_seen") or now
                        stats["last_seen"] = now
                        stats["messages"] = _as_int(stats.get("messages"), 0, minimum=0) + 1
                        self._save_user(profile)

    def record_reply(self, context: Any) -> None:
        """君景成功回复了谁：只更新对方的 last_reply_at 和「当前状态」的参考时间。"""
        if not self.enabled or context is None:
            return
        sender = getattr(getattr(context, "current", None), "sender", None)
        qq = _text(getattr(sender, "qq", ""))
        if not qq:
            return
        with self._lock:
            profile = self.user(qq, create=True)
            if profile:
                profile.setdefault("stats", {})["last_reply_at"] = _iso(_now())
                self._save_user(profile)

    def record_self_status(self, status: Mapping[str, Any], thread_id: str = "") -> None:
        """把这一轮的 ``<status>`` 存成君景自己的短期状态（默认 12 小时过期）。"""
        if not self.enabled or not status:
            return
        values = {
            key: _clip(value, 40)
            for key, value in (status or {}).items()
            if _text(value)
        }
        if not values:
            return
        now = _now()
        with self._lock:
            self._self_status = {
                "thread_id": _text(thread_id),
                "status": values,
                "at": _iso(now),
                "expires_at": _iso(now + timedelta(hours=self.self_ttl)),
            }
            self._save_self()

    # ── 标签 / 记忆 / 状态 ──
    def _add_label(self, profile: dict, label: str, *, source: str = "manual") -> bool:
        label = _clean_label(label)
        if not label:
            return False
        labels = profile.setdefault("labels", [])
        for item in labels:
            if _text(item.get("text")) == label:
                return False
        labels.append({"text": label, "at": _iso(_now()), "source": source})
        profile["labels"] = labels[-self.max_labels:]
        return True

    def _remove_label(self, profile: dict, label: str, *, only_auto: bool = False) -> bool:
        label = _clean_label(label)
        if not label:
            return False
        labels = profile.setdefault("labels", [])
        kept = [
            item
            for item in labels
            if not (
                _text(item.get("text")) == label
                and (not only_auto or _text(item.get("source")) == "auto")
            )
        ]
        if len(kept) == len(labels):
            return False
        profile["labels"] = kept
        return True

    def _add_state(
        self, profile: dict, text: str, *, hours: Optional[int] = None, source: str = "manual"
    ) -> bool:
        text = _clip(text, 60)
        if not text:
            return False
        ttl = _as_int(hours, self.state_ttl) if hours else self.state_ttl
        ttl = min(max(1, ttl), self.state_max)
        now = _now()
        item = {
            "text": text,
            "at": _iso(now),
            "expires_at": _iso(now + timedelta(hours=ttl)),
            "source": source,
        }
        states = [
            old
            for old in _dedupe_states(profile.get("states"))
            if re.sub(r"\s+", "", _text(old.get("text"))) != re.sub(r"\s+", "", text)
        ]
        states.append(item)
        profile["states"] = states[-self.max_states:]
        return True

    def _add_memory(
        self, profile: dict, text: str, *, importance: int = 2, source: str = "manual"
    ) -> bool:
        text = _clip(text, 120)
        if not text:
            return False
        key = re.sub(r"\s+", "", text)
        memories = [
            item
            for item in (profile.get("memories") or [])
            if isinstance(item, dict)
            and re.sub(r"\s+", "", _text(item.get("text"))) != key
        ]
        memories.append(
            {
                "text": text,
                "at": _iso(_now()),
                "importance": min(3, max(1, int(importance or 2))),
                "source": source,
            }
        )
        # 重要的、新的优先保留
        memories.sort(
            key=lambda item: (
                _as_int(item.get("importance"), 2, minimum=1),
                _text(item.get("at")),
            )
        )
        profile["memories"] = memories[-self.max_memories:]
        return True

    def _append_persona(self, profile: dict, text: str, *, limit: int = 300) -> bool:
        text = _clip(text, 120)
        if not text:
            return False
        old = _text(profile.get("persona"))
        if text in old:
            return False
        profile["persona"] = _clip(f"{old}；{text}" if old else text, limit)
        return True

    # ── 对外维护接口（工具 / 自动整理共用）──────────────────
    def update_person(
        self,
        qq: Any,
        *,
        label_add: Any = "",
        label_remove: Any = "",
        state: str = "",
        state_hours: Any = None,
        memory: str = "",
        importance: Any = 2,
        portrait: str = "",
        relation: str = "",
        description: str = "",
        name: str = "",
        source: str = "manual",
    ) -> dict:
        """新增 / 修改一个人的档案，返回本次实际发生的变更。"""
        qq = _text(qq)
        if not self.enabled or not qq:
            return {"ok": False, "reason": "disabled"}
        changes: dict[str, Any] = {"ok": True, "qq": qq}
        with self._lock:
            profile = self.user(qq, create=True)
            if not profile:
                profile = self._default_user(qq)
                self._users[qq] = profile
            if _text(name):
                profile["name"] = _clip(name, 40)
            added = [label for label in _split_items(label_add) if self._add_label(profile, label, source=source)]
            removed = [
                label
                for label in _split_items(label_remove)
                if self._remove_label(profile, label, only_auto=(source == "auto"))
            ]
            if added:
                changes["labels_added"] = added
            if removed:
                changes["labels_removed"] = removed
            if state and self._add_state(profile, state, hours=state_hours, source=source):
                changes["state"] = _clip(state, 60)
            if memory and self._add_memory(
                profile, memory, importance=_as_int(importance, 2), source=source
            ):
                changes["memory"] = _clip(memory, 120)
            if portrait and self._append_persona(profile, portrait):
                changes["portrait"] = _clip(portrait, 120)
            if relation:
                profile["relation"] = _clip(relation, 200)
                changes["relation"] = profile["relation"]
            if description:
                profile["description"] = _clip(description, 200)
                changes["description"] = profile["description"]
            self._save_user(profile)
        return changes

    def update_group(
        self,
        group_id: Any,
        *,
        name: str = "",
        description: str = "",
        topics: Any = "",
        lore: Any = "",
        notes: str = "",
        source: str = "manual",
    ) -> dict:
        group_id = _text(group_id)
        if not self.enabled or not group_id:
            return {"ok": False, "reason": "disabled"}
        changes: dict[str, Any] = {"ok": True, "group_id": group_id}
        with self._lock:
            group = self.group(group_id, create=True)
            if _text(name):
                group["name"] = _clip(name, 60)
            if _text(description):
                group["description"] = _clip(description, 200)
            for key, value in (("topics", topics), ("lore", lore)):
                items = _split_items(value)
                if items:
                    merged = list(group.get(key) or [])
                    for item in items:
                        if item not in merged:
                            merged.append(item)
                    group[key] = merged[-30:]
                    changes[key] = items
            if _text(notes):
                group["notes"] = _clip(notes, 300)
                changes["notes"] = group["notes"]
            self._save_group(group)
        return changes

    # ── 自动整理 ──
    def should_extract(self, thread_id: str, *, direct: bool = False) -> bool:
        """到点了吗：按“当面消息”计数 + 最小时间间隔，两个条件都满足才跑。"""
        if not self.enabled or not self.auto_extract:
            return False
        thread_id = _text(thread_id) or "default"
        with self._lock:
            count = self._counters.get(thread_id, 0) + (1 if direct else 0)
            self._counters[thread_id] = count
            if count < self.auto_every:
                return False
            last = self._last_extract.get(thread_id, 0.0)
            if _time.time() - last < self.auto_min_seconds:
                return False
            self._counters[thread_id] = 0
            self._last_extract[thread_id] = _time.time()
            return True

    def build_extract_prompt(self, context: Any, replies: Optional[list[str]] = None) -> str:
        """给判断模型看的紧凑记录；只带最近几条，省 token。"""
        lines: list[str] = []
        try:
            names = context.namebook()
            history = list(getattr(context, "history", []) or [])[-8:]
            current = getattr(context, "current", None)
            items = [*history, current] if current is not None else history
            from messaging.models import render_message

            for item in items:
                lines.append(render_message(item, names, now=getattr(context, "now", None), limit=120))
        except Exception:  # noqa: BLE001 - 记录不全不影响主流程
            lines = []
        known: list[str] = []
        for qq in self._known_ids(context):
            profile = self.user(qq)
            if profile and (profile.get("name") or profile.get("labels")):
                labels = "、".join(_text(x.get("text")) for x in profile.get("labels") or [])
                known.append(f"{profile.get('name') or qq}({qq}) 标签：{labels or '无'}")
        reply_text = "\n".join(f"- {item}" for item in (replies or [])[:3])
        return (
            "# 最近记录\n"
            + ("\n".join(lines) if lines else "（无）")
            + "\n\n# 已知人物\n"
            + ("\n".join(known) if known else "（无）")
            + "\n\n# 君景刚发的回复\n"
            + (reply_text or "（无）")
            + "\n\n# 任务\n"
            "只从上面的记录里，找出**明确由本人说出、以后还用得上**的信息。"
            "不要猜、不要把玩笑当事实、不要记录隐私。没有就输出 {\"people\":[],\"group\":{}}。"
        )

    def apply_extraction(self, raw: Any, context: Any) -> dict:
        """解析并写入自动整理结果；任何格式错误都静默放弃。"""
        if not self.enabled:
            return {"ok": False, "reason": "disabled"}
        data = self._parse_json_object(raw)
        if not data:
            return {"ok": False, "reason": "invalid_json"}
        known = set(self._known_ids(context))
        changed: list[str] = []
        for item in data.get("people") or []:
            if not isinstance(item, dict):
                continue
            qq = _text(item.get("qq") or item.get("user_id"))
            if not qq or qq not in known:
                continue
            try:
                confidence = float(item.get("confidence", 0.8))
            except (TypeError, ValueError):
                confidence = 0.0
            if confidence < 0.6:
                continue
            result = self.update_person(
                qq,
                label_add=item.get("labels_add") or "",
                label_remove=item.get("labels_remove") or "",
                state=_text(item.get("state")),
                state_hours=item.get("state_hours"),
                memory=_text(item.get("memory")),
                importance=item.get("importance", 2),
                relation=_text(item.get("relation")),
                source="auto",
            )
            if result.get("ok") and len(result) > 2:
                changed.append(qq)
        group = data.get("group")
        group_id = _text(getattr(context, "group_id", ""))
        if isinstance(group, dict) and group_id:
            self.update_group(
                group_id,
                topics=group.get("topics") or "",
                lore=group.get("lore") or "",
                notes=_text(group.get("notes")),
                source="auto",
            )
        return {"ok": True, "updated": changed}

    @staticmethod
    def _parse_json_object(raw: Any) -> dict:
        text = _text(raw)
        if not text:
            return {}
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            data = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    # ── 选择 / 渲染 ──
    def _known_ids(self, context: Any) -> list[str]:
        ids: list[str] = []

        def add(qq: Any) -> None:
            value = _text(qq)
            self_id = _text(getattr(context, "self_id", ""))
            if value and value != self_id and value not in ids:
                ids.append(value)

        current = getattr(context, "current", None)
        add(getattr(getattr(current, "sender", None), "qq", ""))
        for qq in getattr(current, "mentions", []) or []:
            add(qq)
        quote = getattr(current, "quote", None)
        if quote is not None:
            add(getattr(getattr(quote, "sender", None), "qq", ""))
        for master in self.master_ids:
            add(master)
        for message in reversed(getattr(context, "history", []) or []):
            add(getattr(getattr(message, "sender", None), "qq", ""))
            quote = getattr(message, "quote", None)
            if quote is not None:
                add(getattr(getattr(quote, "sender", None), "qq", ""))
        group = self.group(getattr(context, "group_id", ""))
        for qq in group.get("common_users") or []:
            add(qq)
        return ids

    def _fresh_states(self, profile: dict, now: Optional[datetime] = None) -> list[dict]:
        now = now or _now()
        fresh: list[tuple[datetime, dict]] = []
        for item in profile.get("states") or []:
            if not isinstance(item, dict):
                continue
            at = _parse_time(item.get("at")) or now
            expires = _parse_time(item.get("expires_at"))
            if expires is None:
                expires = at + timedelta(hours=self.state_ttl)
            if expires <= now:
                continue
            fresh.append((at, item))
        fresh.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in fresh[:3]]

    def _render_person(
        self, qq: str, data: dict, group_id: str, names: Mapping[str, str]
    ) -> str:
        name = _text(data.get("name")) or _text(names.get(qq)) or qq
        aliases = [item for item in _split_items(data.get("aliases")) if item != name]
        labels = [
            _text(item.get("text"))
            for item in (data.get("labels") or [])
            if _text(item.get("text"))
        ]
        parts: list[str] = []
        if labels:
            parts.append("标签：" + "、".join(labels[: self.max_labels]))
        if aliases:
            parts.append("别名：" + "/".join(aliases[:3]))
        if _text(data.get("persona")):
            parts.append("画像：" + _text(data.get("persona")))
        if _text(data.get("relation")):
            parts.append("关系：" + _text(data.get("relation")))
        if _text(data.get("description")):
            parts.append("描述：" + _text(data.get("description")))
        states = self._fresh_states(data)
        if states:
            now = _now()
            parts.append(
                "近期状态："
                + "；".join(
                    f"{_text(item.get('text'))}（{_age_label(_parse_time(item.get('at')), now)}）"
                    for item in states
                )
            )
        memories = [
            item
            for item in (data.get("memories") or [])
            if isinstance(item, dict) and _text(item.get("text"))
        ]
        memories.sort(
            key=lambda item: (
                _as_int(item.get("importance"), 2, minimum=1),
                _text(item.get("at")),
            ),
            reverse=True,
        )
        if memories:
            parts.append(
                "重要记忆："
                + "；".join(
                    f"{_clip(item.get('text'), 60)}"
                    + (f"（{_text(item.get('at'))[:10]}）" if item.get("at") else "")
                    for item in memories[:4]
                )
            )
        local = _text((data.get("group_notes") or {}).get(group_id))
        if local:
            parts.append("本群：" + local)
        head = f"- {name}({qq})"
        if not parts:
            return ""
        return _clip(head + "：" + "；".join(parts), self.max_person_chars)

    def _render_group(self, group_id: str) -> str:
        data = self.group(group_id) if group_id else {}
        if not data:
            return ""
        lines: list[str] = []
        name = _text(data.get("name")) or group_id
        lines.append(f"- 群：{name}（{group_id}）")
        for label, key in (("简介", "description"), ("备注", "notes")):
            value = _text(data.get(key))
            if value:
                lines.append(f"- {label}：{value}")
        for label, key in (("常聊", "topics"), ("群梗", "lore"), ("口头禅", "catchphrases")):
            values = _split_items(data.get(key))
            if values:
                lines.append(f"- {label}：{'；'.join(values[:8])}")
        common = _split_items(data.get("common_users"))
        if common:
            names = []
            for qq in common[:12]:
                profile = self.user(qq)
                shown = _text(profile.get("name")) or qq
                names.append(f"{shown}({qq})")
            lines.append("- 常出现：" + "、".join(names))
        return "\n".join(lines)

    def _render_self(self) -> str:
        if not self._self_status:
            if self.self_path is None:
                return ""
            self._self_status = read_json(self.self_path, {}) or {}
        data = self._self_status if isinstance(self._self_status, dict) else {}
        status = data.get("status") if isinstance(data.get("status"), dict) else {}
        if not status:
            return ""
        at = _parse_time(data.get("at"))
        expires = _parse_time(data.get("expires_at"))
        now = _now()
        if expires is not None and expires <= now:
            return ""
        if expires is None and at is not None:
            if at + timedelta(hours=self.self_ttl) <= now:
                return ""
        parts = [
            f"{key}：{_clip(value, 40)}"
            for key, value in status.items()
            if _text(value)
        ]
        if not parts:
            return ""
        head = "## 我现在的状态"
        if at is not None:
            head += f"（{_age_label(at, now)}）"
        return head + "\n" + "；".join(parts)

    def render(self, context: Any) -> str:
        """把这一轮最相关的群 / 人物 / 自身状态渲染成 ``<relations>`` 块。"""
        if not self.enabled or context is None:
            return ""
        names = {}
        try:
            names = context.namebook()
        except Exception:  # noqa: BLE001
            names = {}
        group_id = _text(getattr(context, "group_id", ""))
        blocks: list[str] = []
        self_text = self._render_self()
        if self_text:
            blocks.append(self_text)
        group_text = self._render_group(group_id)
        if group_text:
            blocks.append("## 这个群\n" + group_text)

        lines: list[str] = []
        used = 0
        for qq in self._known_ids(context):
            if len(lines) >= self.max_people:
                break
            profile = self.user(qq)
            if not profile:
                continue
            line = self._render_person(qq, profile, group_id, names)
            if not line:
                continue
            if lines and used + len(line) > self.max_chars:
                break
            lines.append(line)
            used += len(line)
        if lines:
            blocks.append("## 我认识的人\n" + "\n".join(lines))
        if not blocks:
            return ""
        return "<relations>\n" + "\n\n".join(blocks) + "\n</relations>"

    # ── 测试 / 维护辅助 ──
    def reset(self) -> None:
        """清掉内存缓存（测试和多实例场景用；不删磁盘文件）。"""
        with self._lock:
            self._users.clear()
            self._groups.clear()
            self._self_status = {}
            self._seen_ids.clear()
            self._counters.clear()
            self._last_extract.clear()


__all__ = [
    "RelationStore",
    "DEFAULT_MAX_PEOPLE",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_PERSON_CHARS",
    "DEFAULT_STATE_TTL_HOURS",
    "DEFAULT_SELF_TTL_HOURS",
    "DEFAULT_AUTO_EVERY",
    "DEFAULT_AUTO_MIN_SECONDS",
]
