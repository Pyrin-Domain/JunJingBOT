"""BotSettings —— 整个项目的配置对象。

它是**唯一**的内存配置真相：

    settings = load_settings()
    settings.get("agent.enable")
    settings.prompt("group_context")
    settings.save()          # 写 config/bot_config.json
    settings.reload()        # 面板保存后重新读盘

所有服务共享同一个实例。热加载时只替换内部数据，不重建服务对象。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.jsonstore import deep_merge, read_json, write_json

from .connection import ConnectionSettings
from .defaults import (
    copy_default,
    default_bot_config,
    default_connection,
    default_environment,
    default_sender_connection,
)
from .environment import EnvConfig, EnvKind, PathBridge, load_env_config
from .paths import AppPaths


class BotSettings:
    """内置默认值 ← bot_config.json ← 显式 overrides。"""

    AGENT_LLM_KEYS = (
        "base_url",
        "api_key",
        "api_key_env",
        "model",
        "temperature",
        "max_tokens",
    )

    AUTO_FORWARD_PARAMS = (
        "groupid_list",
        "userid_list",
        "target_groupid",
        "task_userid",
        "dedup",
        "auto_img_forward_from_bot",
        "strip_tail_ads",
    )
    AUTO_FORWARD_ALIASES = {
        "user_id_list": "userid_list",
        "user_ids": "userid_list",
    }

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        overrides: dict | None = None,
        env_overrides: dict | None = None,
        connection_overrides: dict | None = None,
        auto_create: bool = True,
    ):
        self.paths = AppPaths.discover(root)
        self.paths.ensure_dirs()
        self._auto_create = auto_create
        self._api_connection: ConnectionSettings | None = None
        self.overrides = dict(overrides or {})
        self._env_overrides = dict(env_overrides or {})
        self._connection_overrides = dict(connection_overrides or {})
        self._load(auto_create=auto_create)

    # ════════════════════ 加载 ════════════════════
    def _load(self, *, auto_create: bool) -> None:
        # 每次都从默认布局重新算，避免热加载时把上一次的目录覆盖当成新的 fallback
        base = AppPaths.discover(self.paths.root)

        # ── environment.json ──
        env_path = base.config_file("environment.json")
        env_raw = read_json(env_path, {})
        if not isinstance(env_raw, dict):
            env_raw = {}
        if not env_raw and auto_create and not env_path.exists():
            env_raw = default_environment()
            write_json(base.config_target("environment.json"), env_raw)

        # ── config.json ──
        conn_path = base.config_file("config.json")
        conn_raw = read_json(conn_path, {})
        if not isinstance(conn_raw, dict):
            conn_raw = {}
        if not conn_raw and auto_create and not conn_path.exists():
            conn_raw = default_connection()
            write_json(base.config_target("config.json"), conn_raw)

        # ── paths_config.json（旧配置，只读兼容）──
        legacy_paths = read_json(base.config_file("paths_config.json"), {})
        if not isinstance(legacy_paths, dict):
            legacy_paths = {}

        merged_env = {**env_raw, **self._env_overrides}
        env = load_env_config(merged_env, conn_raw)

        # 旧配置里常写 /mnt/d/...：如果没显式声明协议端环境，就据此推断
        imgs_value = merged_env.get("imgs_dir") or legacy_paths.get("imgs_dir")
        if (
            not any(
                key in merged_env
                for key in ("backend_env", "onebot_env", "napcat_env", "path_profile")
            )
            and str(imgs_value or "").strip().startswith("/mnt/")
            and env.bot_env is EnvKind.WINDOWS
        ):
            env = replace(env, backend_env=EnvKind.WSL, path_profile="windows_to_wsl")

        # 复用同一个 PathBridge：ImageResolver / MediaService 持有的是这个实例，
        # 热加载时只能 reconfigure，不能换对象。
        bridge = getattr(self, "bridge", None)
        if bridge is None:
            bridge = PathBridge(base.root, env)
        else:
            bridge.reconfigure(env)

        def local_dir(value: Any) -> Path | None:
            text = str(value or "").strip()
            if not text:
                return None
            return bridge.expand(text)

        self.paths = base.with_overrides(
            images_dir=local_dir(imgs_value),
            downloads_dir=local_dir(merged_env.get("downloads_dir")),
            temp_dir=local_dir(merged_env.get("temp_dir")),
        )
        self.paths.ensure_dirs()

        self.env = env
        self.bridge = bridge
        self.env_raw = dict(env_raw)
        self.conn_raw = dict(conn_raw)
        self._imgs_raw = str(imgs_value or "imgs")
        self._downloads_raw = str(merged_env.get("downloads_dir") or "data/downloads")
        self._temp_raw = str(merged_env.get("temp_dir") or "data/tmp")

        # ── 连接 ──
        merged_conn = {**conn_raw, **self._connection_overrides}
        self.connection = ConnectionSettings(
            merged_conn, path=conn_path, required=False
        )
        self._api_connection = None

        # ── bot_config.json ──
        bot_path = base.config_file("bot_config.json")
        raw_bot = read_json(bot_path, {})
        if not isinstance(raw_bot, dict):
            raw_bot = {}
        if not raw_bot and auto_create and not bot_path.exists():
            raw_bot = default_bot_config()
            write_json(base.config_target("bot_config.json"), raw_bot)
        self.path = base.config_target("bot_config.json")
        self.data = default_bot_config()
        deep_merge(self.data, raw_bot)
        deep_merge(self.data, self.overrides)

    # ════════════════════ 保存 / 重载 ════════════════════
    def save(self, path: str | Path | None = None) -> str:
        target = Path(path) if path is not None else self.path
        write_json(target, self.data)
        self.path = target
        return str(target)

    def save_environment(self) -> str:
        data = self.env.to_dict()
        data.update(
            {
                "imgs_dir": self._imgs_raw,
                "downloads_dir": self._downloads_raw,
                "temp_dir": self._temp_raw,
            }
        )
        target = self.paths.config_target("environment.json")
        write_json(target, data)
        return str(target)

    def save_connection(
        self, connection: ConnectionSettings | None = None, *, sender: bool = False
    ) -> str:
        target = self.paths.config_target(
            "sender_config.json" if sender else "config.json"
        )
        data = (connection or self.connection).to_dict()
        write_json(target, data)
        return str(target)

    def reload(self) -> "BotSettings":
        self._load(auto_create=False)
        return self

    def to_dict(self) -> dict:
        return deepcopy(self.data)

    def update(self, data: dict) -> "BotSettings":
        """用面板提交的数据覆盖当前配置（深合并）。"""
        deep_merge(self.data, data)
        return self

    def replace_data(self, data: dict) -> "BotSettings":
        self.data = copy_default(data)
        return self

    # ════════════════════ 取值 ════════════════════
    def get(self, key_path: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in str(key_path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key_path: str, value: Any) -> "BotSettings":
        parts = str(key_path).split(".")
        node = self.data
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        return self

    def section(self, name: str) -> dict:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def prompt(self, name: str, default: str = "") -> str:
        return str(self.section("prompts").get(name, default))

    def reply(self, name: str, default: str = "") -> str:
        return str(self.section("replies").get(name, default))

    @property
    def imgs_dir(self) -> str:
        return str(self.paths.images_dir)

    @property
    def env_dirs(self) -> dict[str, str]:
        """``environment.json`` 里写的原始目录值（面板编辑用，不是解析后的绝对路径）。"""
        return {
            "imgs_dir": self._imgs_raw,
            "downloads_dir": self._downloads_raw,
            "temp_dir": self._temp_raw,
        }

    # ════════════════════ 常用段落 ════════════════════
    @property
    def master_qq(self) -> list[str]:
        value = self.data.get("master_qq", [])
        if isinstance(value, (str, int)):
            return [str(value)]
        if not isinstance(value, (list, tuple)):
            return []
        return [str(qq) for qq in value]

    @property
    def agent_enabled(self) -> bool:
        return bool(self.get("agent.enable", True))

    @property
    def agent_models(self) -> list[dict]:
        value = self.get("agent.models", [])
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    @property
    def agent_judge_model(self) -> str:
        return str(self.get("agent.judge_model", "") or "").strip()

    @property
    def agent_fallback_models(self) -> list[str]:
        value = self.get("agent.fallback_models", [])
        return [str(item) for item in value] if isinstance(value, list) else []

    @property
    def agent_llm_config(self) -> dict:
        section = self.section("agent")
        return {key: deepcopy(section[key]) for key in self.AGENT_LLM_KEYS if key in section}

    @property
    def agent_model_routing(self) -> dict:
        return {
            "models": deepcopy(self.agent_models),
            "active_model": str(self.get("agent.active_model", "") or "").strip(),
            "judge_model": self.agent_judge_model,
            "fallback_models": self.agent_fallback_models,
        }

    @property
    def extraconfig(self) -> dict:
        value = self.get("message.extraconfig", {})
        return dict(value) if isinstance(value, dict) else {}

    @property
    def interject_enabled(self) -> bool:
        return bool(self.get("interject.enable", True))

    @property
    def heartbeat_range(self) -> tuple[float, float]:
        low = float(self.get("heartbeat.min_minutes", 10))
        high = float(self.get("heartbeat.max_minutes", 20))
        return (min(low, high), max(low, high))

    @property
    def auto_forward_enabled(self) -> bool:
        return bool(self.get("auto_forward.enable", True))

    @property
    def auto_forward_kwargs(self) -> dict:
        section = dict(self.section("auto_forward"))
        section.pop("enable", None)
        params: dict = {}
        for key, value in section.items():
            name = self.AUTO_FORWARD_ALIASES.get(key, key)
            if name in self.AUTO_FORWARD_PARAMS:
                params[name] = value
        return params

    @property
    def image_forward_auto_targets(self) -> list:
        return list(self.get("image_forward.auto_target_group_ids", []) or [])

    @property
    def keyword_actions(self) -> list:
        value = self.data.get("keyword_actions")
        return value if isinstance(value, list) else []

    # ════════════════════ 连接 ════════════════════
    @property
    def api_connection(self) -> ConnectionSettings:
        """发 API 用的连接：反向模式就是事件连接；正向模式读 sender_config。"""
        if not self.connection.is_forward:
            return self.connection
        if self._api_connection is None:
            sender_path = self.paths.config_file("sender_config.json")
            if not sender_path.exists():
                write_json(
                    self.paths.config_target("sender_config.json"),
                    default_sender_connection(self.connection.backend),
                )
            # sender_config.json 允许不写 backend：此时跟随事件连接的后端。
            self._api_connection = ConnectionSettings.from_file(
                sender_path,
                required=True,
                default_backend=self.connection.backend,
            )
        return self._api_connection

    def layout_report(self) -> str:
        return self.paths.layout_report()


def load_settings(
    root: str | Path | None = None, *, auto_create: bool = True, **kwargs: Any
) -> BotSettings:
    return BotSettings(root, auto_create=auto_create, **kwargs)


__all__ = ["BotSettings", "load_settings"]
