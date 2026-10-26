"""面板配置校验。

规则逐条搬运自旧 ``Minitor/WebPanel.py``：只改结构，不改判定标准。

``validate_config`` 返回 ``(errors, warnings)``：

- ``errors``   —— 拦住保存（类型 / 格式不对，写下去会跑不起来）
- ``warnings`` —— 允许保存（未知 action、非数字群号……运行时会被跳过）

所有检查都允许字段缺失，不会因为 ``None`` 或半截配置抛异常。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from config.backends import DEFAULT_BACKEND, normalize_backend
from config.connection import MODE_REVERSE, normalize_mode
from config.environment import EnvKind
from config.path_profiles import normalize_profile
from core.pathmap import looks_windows

# Bot 不在场（standalone）时的兜底 action 清单，与
# ``bot/commands/registry.py`` 的注册表保持一致。
FALLBACK_ACTIONS: tuple[str, ...] = (
    "send_message",
    "send_img",
    "get_long_history_test",
    "set_tokenizer",
    "ocr",
    "export",
    "recall",
    "fabric",
    "get_music",
    "img_forward_create_task",
    "try_send_bili_video",
    "try_send_reply_bili_card",
    "jm",
)

KNOWN_TOP_KEYS: tuple[str, ...] = (
    "master_qq",
    "heartbeat",
    "agent",
    "interject",
    "message",
    "meme",
    "emoji",
    "auto_forward",
    "image_forward",
    "jm",
    "prompts",
    "replies",
    "keyword_actions",
    "web_panel",
)

# 面板能改的连接字段；文件里的其它字段原样保留。
WS_KEYS: tuple[str, ...] = (
    "type",
    "backend",
    "access_token",
    "ws_host",
    "ws_port",
    "ws_reverse_host",
    "ws_reverse_port",
    "ws_reverse_strict_token",
)

# 路径映射页能改的 environment.json 字段；其余字段（imgs_dir 等）原样保留。
PATH_KEYS: tuple[str, ...] = (
    "bot_env",
    "backend_env",
    "path_profile",
    "wsl_distro",
    "wsl_mount_root",
    "path_map",
)


# ════════════════════════ 连接配置（config/config.json） ════════════════════════
def ws_section(raw: dict | None) -> dict:
    """从连接配置文件里挑出面板关心的字段。"""
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in WS_KEYS if key in raw}


def validate_ws_config(data: Any) -> tuple[list[str], dict]:
    """校验连接设置：返回 ``(errors, cleaned)``。"""
    if not isinstance(data, dict):
        return ["连接设置必须是 JSON 对象"], {}

    errors: list[str] = []
    cleaned: dict = {}

    raw_type = data.get("type")
    if raw_type is None or str(raw_type).strip() == "":
        cleaned["type"] = MODE_REVERSE
    else:
        mode = normalize_mode(raw_type, default="")
        if mode:
            cleaned["type"] = mode
        else:
            errors.append(f'type={raw_type!r} 不认识：反向填 "ws_re"，正向填 "ws"')

    if "backend" in data:
        raw_backend = data.get("backend")
        if raw_backend is None or str(raw_backend).strip() == "":
            cleaned["backend"] = DEFAULT_BACKEND
        else:
            backend = normalize_backend(raw_backend, default="")
            if backend:
                cleaned["backend"] = backend
            else:
                errors.append(
                    f"backend={raw_backend!r} 不认识："
                    "可用 napcat / snowluma（也接受 snowluna 等别名）"
                )

    if "access_token" in data and data["access_token"] is not None:
        cleaned["access_token"] = str(data["access_token"])

    for key in ("ws_host", "ws_reverse_host"):
        value = data.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{key} 必须是非空字符串，例如 127.0.0.1")
            continue
        cleaned[key] = value.strip()

    for key in ("ws_port", "ws_reverse_port"):
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            port = int(value)
        except (TypeError, ValueError):
            errors.append(f"{key} 必须是 1~65535 之间的整数")
            continue
        if not 1 <= port <= 65535:
            errors.append(f"{key} 必须在 1~65535 之间")
            continue
        cleaned[key] = port

    if data.get("ws_reverse_strict_token") is not None:
        cleaned["ws_reverse_strict_token"] = bool(data["ws_reverse_strict_token"])

    return errors, cleaned


# ════════════════════════ 路径映射（config/environment.json） ════════════════════════
def paths_section(raw: dict | None) -> dict:
    """从 environment.json 里挑出「路径映射」页关心的字段。"""
    if not isinstance(raw, dict):
        return {}
    data: dict = {}
    for key in (
        "bot_env",
        "backend_env",
        "onebot_env",
        "napcat_env",
        "path_profile",
        "wsl_distro",
        "wsl_mount_root",
    ):
        if key in raw:
            data[key] = raw[key]
    if "path_map" in raw:
        data["path_map"] = raw["path_map"]
    return data


def _absoluteish(text: str) -> bool:
    text = str(text or "").strip()
    return text.startswith(("/", "\\")) or bool(re.match(r"^[A-Za-z]:[\\/]", text))


def validate_paths_config(data: Any) -> tuple[list[str], list[str], dict]:
    """校验路径映射设置：返回 ``(errors, warnings, cleaned)``。

    与连接设置不同，路径翻译可以在运行时热替换，所以这里不要求重启。
    """
    if not isinstance(data, dict):
        return ["路径设置必须是 JSON 对象"], [], {}

    errors: list[str] = []
    warnings: list[str] = []
    cleaned: dict = {}

    def err(msg: str) -> None:
        errors.append(msg)

    def warn(msg: str) -> None:
        warnings.append(msg)

    bot_kind: EnvKind | None = None
    backend_kind: EnvKind | None = None

    # ── Bot 环境（面板只读，但 API 允许改） ──
    if "bot_env" in data:
        raw = data.get("bot_env")
        if raw is None or str(raw).strip() == "":
            err("bot_env 不能为空（windows / wsl / linux）")
        else:
            try:
                bot_kind = EnvKind.parse(raw)
            except ValueError as exc:
                err(str(exc))
            else:
                cleaned["bot_env"] = bot_kind.value

    # ── 协议端环境：backend_env 优先，兼容 onebot_env / napcat_env ──
    for key in ("backend_env", "onebot_env", "napcat_env"):
        if key not in data:
            continue
        raw = data.get(key)
        if raw is None or str(raw).strip() == "":
            err(f"{key} 不能为空（windows / wsl / linux）")
        else:
            try:
                backend_kind = EnvKind.parse(raw)
            except ValueError as exc:
                err(str(exc))
            else:
                cleaned["backend_env"] = backend_kind.value
        break

    # ── 翻译模式 ──
    if "path_profile" in data:
        raw = data.get("path_profile")
        if raw is None or str(raw).strip() == "":
            err("path_profile 不能为空")
        else:
            profile = normalize_profile(raw, default="")
            if not profile:
                err(
                    f"path_profile={raw!r} 不认识："
                    "可用 same / windows_to_wsl / wsl_to_windows / custom"
                )
            else:
                cleaned["path_profile"] = profile

    # ── WSL 挂载根 / 发行版 ──
    if "wsl_mount_root" in data:
        raw = data.get("wsl_mount_root")
        if raw is None or str(raw).strip() == "":
            cleaned["wsl_mount_root"] = "/mnt"
        elif not isinstance(raw, str):
            err("wsl_mount_root 必须是字符串，例如 /mnt")
        else:
            text = raw.strip().replace("\\", "/")
            if not text.startswith("/"):
                err("wsl_mount_root 必须以 / 开头，例如 /mnt")
            else:
                cleaned["wsl_mount_root"] = "/" + text.strip("/")

    if "wsl_distro" in data:
        raw = data.get("wsl_distro")
        if raw is None or str(raw).strip() == "":
            cleaned["wsl_distro"] = "Ubuntu"
        elif not isinstance(raw, str):
            err("wsl_distro 必须是字符串，例如 Ubuntu")
        else:
            cleaned["wsl_distro"] = raw.strip()

    # ── 自定义规则 ──
    if "path_map" in data:
        raw = data.get("path_map")
        if raw is None:
            cleaned["path_map"] = []
        elif not isinstance(raw, list):
            err("path_map 必须是数组")
        else:
            items: list[dict] = []
            seen: set[str] = set()
            for index, item in enumerate(raw):
                tag = f"path_map[{index}]"
                if not isinstance(item, dict):
                    err(f"{tag} 必须是对象")
                    continue
                source = str(item.get("from") or "").strip()
                target = str(item.get("to") or "").strip()
                note = str(item.get("note") or "").strip()
                if not source or not target:
                    err(
                        f'{tag} 需要非空的 from / to，'
                        '例如 {"from": "D:\\\\share", "to": "/data/share"}'
                    )
                    continue
                if not _absoluteish(source):
                    warn(f"{tag}.from={source!r} 看起来不是绝对路径，最长前缀匹配可能不符合预期")
                if not _absoluteish(target):
                    warn(f"{tag}.to={target!r} 看起来不是绝对路径")
                key = source.replace("\\", "/").rstrip("/")
                if looks_windows(source):
                    key = key.casefold()
                if key in seen:
                    warn(f"{tag}.from={source!r} 与前面的规则重复，只有第一条会生效")
                seen.add(key)
                entry = {"from": source, "to": target}
                if note:
                    entry["note"] = note
                items.append(entry)
            cleaned["path_map"] = items

    # ── 组合提示 ──
    profile = cleaned.get("path_profile")
    if profile and bot_kind is not None:
        if profile == "same" and backend_kind is not None and bot_kind is not backend_kind:
            warn(
                "path_profile=same，但 Bot 与协议端环境不同；"
                "两端路径不一致时请换成 windows_to_wsl / wsl_to_windows / custom"
            )
        elif profile == "windows_to_wsl":
            if bot_kind is not EnvKind.WINDOWS:
                warn("path_profile=windows_to_wsl，但 bot_env 不是 windows，规则可能不适用")
            if backend_kind is not None and backend_kind is not EnvKind.WSL:
                warn("path_profile=windows_to_wsl，但 backend_env 不是 wsl，规则可能不适用")
        elif profile == "wsl_to_windows":
            if bot_kind is not EnvKind.WSL:
                warn("path_profile=wsl_to_windows，但 bot_env 不是 wsl，规则可能不适用")
            if backend_kind is not None and backend_kind is not EnvKind.WINDOWS:
                warn("path_profile=wsl_to_windows，但 backend_env 不是 windows，规则可能不适用")
        elif (
            profile == "custom"
            and backend_kind is not None
            and bot_kind is not backend_kind
            and not cleaned.get("path_map")
        ):
            warn("path_profile=custom 但没有 path_map，路径会原样发送")

    return errors, warnings, cleaned


# ════════════════════════ 小工具 ════════════════════════
def _bool_field(node: dict, key: str, label: str, err) -> None:
    if key in node and not isinstance(node[key], bool):
        err(f"{label} 必须是 true / false")


def _int_list(value: Any, label: str, err, warn) -> None:
    """群号 / QQ 号列表：允许 int 或纯数字字符串，其它只警告。"""
    if value is None:
        return
    if not isinstance(value, list):
        err(f"{label} 必须是数组，例如 [123456, 234567]")
        return
    for index, item in enumerate(value):
        text = str(item).strip()
        if not text.isdigit():
            warn(f"{label}[{index}] = {item!r} 不是纯数字，运行时会忽略这一项")


def _bool_section(data: dict, section: str, label: str, err) -> dict:
    node = data.get(section)
    if node is None:
        return {}
    if not isinstance(node, dict):
        err(f"{label} 必须是对象")
        return {}
    return node


def _validate_strip_tail_ads(auto: dict, err, warn) -> dict:
    """末尾广告裁剪 + 尾部追加（``auto_forward.strip_tail_ads``）。

    结构::

        {
          "enable": true, "scan_tail": 3,
          "drop_qr": true, "drop_group_card": true, "drop_link": true,
          "image_timeout": 8,
          "append": {"enable": true, "items": [{"uin": 123, "name": "x", "content": "文本"}]}
        }
    """
    raw = auto.get("strip_tail_ads")
    if raw is None:
        return {}
    if isinstance(raw, bool):
        return {}
    if not isinstance(raw, dict):
        err("auto_forward.strip_tail_ads 必须是对象")
        return {}
    label = "auto_forward.strip_tail_ads"
    for key in ("enable", "drop_qr", "drop_group_card", "drop_link"):
        _bool_field(raw, key, f"{label}.{key}", err)

    scan_tail = raw.get("scan_tail")
    if scan_tail is not None and not isinstance(scan_tail, int):
        err(f"{label}.scan_tail 必须是整数（扫描末尾几条）")
    elif isinstance(scan_tail, int) and not 0 <= scan_tail <= 50:
        err(f"{label}.scan_tail 只能是 0~50")

    timeout = raw.get("image_timeout")
    if timeout is not None and not isinstance(timeout, (int, float)):
        err(f"{label}.image_timeout 必须是数字（秒）")

    append = raw.get("append")
    if append is None:
        return raw
    if isinstance(append, list):
        append = {"enable": True, "items": append}
    if not isinstance(append, dict):
        err(f"{label}.append 必须是对象或数组")
        return raw
    _bool_field(append, "enable", f"{label}.append.enable", err)
    items = append.get("items")
    if items is not None and not isinstance(items, list):
        err(f"{label}.append.items 必须是数组（可配多条文本 / 图片）")
        return raw
    for index, item in enumerate(items or []):
        tag = f"{label}.append.items[{index}]"
        if not isinstance(item, dict):
            err(f'{tag} 必须是对象，例如 {{"uin": 123456, "name": "昵称", "content": "文本"}}')
            continue
        if not str(item.get("content") or "").strip() and not isinstance(item.get("content"), (list, dict)):
            err(f"{tag}.content 不能为空")
        uin = str(item.get("uin") or item.get("user_id") or "").strip()
        if uin and not uin.isdigit():
            warn(f"{tag}.uin = {uin!r} 不是纯数字，后端会当成不存在的账号")
        if not str(item.get("name") or "").strip():
            warn(f"{tag}.name 为空，聊天记录里会显示成「QQ用户」")
    return raw


def _model_ids(models: Any) -> set[str]:
    if not isinstance(models, list):
        return set()
    return {
        entry["id"]
        for entry in models
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }


# ════════════════════════ 主配置校验 ════════════════════════
def validate_config(
    data: Any,
    imgs_dir: str = "",
    known_actions: Iterable[str] | None = None,
) -> tuple[list[str], list[str]]:
    """校验一份 ``bot_config``：返回 ``(errors, warnings)``。

    :param imgs_dir: 保留参数（旧接口形状），图片路径提示里会用到。
    :param known_actions: Bot 实际注册的 action 名；缺省用 ``FALLBACK_ACTIONS``。
    """
    del imgs_dir  # 仅用于旧接口兼容，校验规则本身不依赖它
    errors: list[str] = []
    warnings: list[str] = []

    def err(msg: str) -> None:
        errors.append(msg)

    def warn(msg: str) -> None:
        warnings.append(msg)

    if not isinstance(data, dict):
        return ["配置必须是一个 JSON 对象"], []

    known = set(known_actions or ()) | set(FALLBACK_ACTIONS)

    # ── 主人 QQ ──
    master = data.get("master_qq")
    if master is None:
        err("master_qq 不能为空")
    elif isinstance(master, (str, int)):
        text = str(master).strip()
        if not text:
            err("master_qq 不能为空")
        elif not text.isdigit():
            warn(f"master_qq={master!r} 不是纯数字，可能不是有效的 QQ 号")
    elif isinstance(master, list):
        if not master:
            err("master_qq 不能为空")
        else:
            for index, qq in enumerate(master):
                if not str(qq).strip().isdigit():
                    warn(
                        f"master_qq[{index}] = {qq!r} 不是纯数字，"
                        "可能不是有效的 QQ 号"
                    )
    else:
        err('master_qq 必须是数组，例如 ["10001"]')

    # ── 心跳 ──
    heartbeat = data.get("heartbeat") or {}
    if not isinstance(heartbeat, dict):
        err("heartbeat 必须是对象")
    else:
        for name in ("min_minutes", "max_minutes"):
            value = heartbeat.get(name)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                err(f"heartbeat.{name} 必须是正数（单位：分钟）")
        low = heartbeat.get("min_minutes")
        high = heartbeat.get("max_minutes")
        if (
            isinstance(low, (int, float))
            and not isinstance(low, bool)
            and isinstance(high, (int, float))
            and not isinstance(high, bool)
            and low > high
        ):
            err("heartbeat.min_minutes 不能大于 heartbeat.max_minutes")

    # ── Agent ──
    agent = _bool_section(data, "agent", "agent", err)
    _bool_field(agent, "enable", "agent.enable", err)
    if "provider_index" in agent:
        warn(
            "agent.provider_index 已废弃（模型改为 agent.models 列表维护），"
            "可以删掉这个字段"
        )

    models = agent.get("models")
    if models is not None and not isinstance(models, list):
        err("agent.models 必须是数组（每条是一项模型配置）")
    elif isinstance(models, list):
        seen_model_ids: set[str] = set()
        for index, entry in enumerate(models):
            tag = f"agent.models[{index}]"
            if not isinstance(entry, dict):
                err(f"{tag} 必须是对象")
                continue

            entry_id = entry.get("id")
            if entry_id is not None:
                if not isinstance(entry_id, str) or not entry_id.strip():
                    err(f"{tag}.id 必须是非空字符串")
                elif entry_id in seen_model_ids:
                    err(f"{tag}.id={entry_id!r} 与其它配置重复")
                else:
                    seen_model_ids.add(entry_id)

            entry_name = entry.get("name")
            if entry_name is not None and not isinstance(entry_name, str):
                err(f"{tag}.name 必须是字符串（备注名，可留空）")

            entry_base = entry.get("base_url")
            if entry_base is not None:
                if not isinstance(entry_base, str):
                    err(f"{tag}.base_url 必须是字符串")
                elif entry_base.strip() and not re.match(
                    r"^https?://", entry_base.strip(), re.I
                ):
                    err(f"{tag}.base_url 必须以 http:// 或 https:// 开头")

            entry_key = entry.get("api_key")
            if entry_key is not None and not isinstance(entry_key, str):
                err(f"{tag}.api_key 必须是字符串（留空 = 用环境变量）")

            entry_env = entry.get("api_key_env")
            if entry_env is not None and (
                not isinstance(entry_env, str)
                or (entry_env.strip() and not entry_env.strip().isidentifier())
            ):
                err(f"{tag}.api_key_env 必须是合法的环境变量名（例如 MY_API_KEY）")

            entry_model = entry.get("model")
            if entry_model is not None and not isinstance(entry_model, str):
                err(f"{tag}.model 必须是字符串")

            entry_temp = entry.get("temperature")
            if entry_temp is not None and (
                not isinstance(entry_temp, (int, float))
                or isinstance(entry_temp, bool)
                or not 0 <= float(entry_temp) <= 2
            ):
                err(f"{tag}.temperature 必须是 0~2 的数字（留空 = 跟随主模型）")

            entry_tokens = entry.get("max_tokens")
            if entry_tokens is not None and (
                not isinstance(entry_tokens, int)
                or isinstance(entry_tokens, bool)
                or entry_tokens <= 0
            ):
                err(f"{tag}.max_tokens 必须是正整数（留空 = 跟随主模型）")

    known_ids = _model_ids(models)

    active_model = agent.get("active_model")
    if active_model is not None and not isinstance(active_model, str):
        err("agent.active_model 必须是字符串（对应 agent.models 里某条的 id）")
    elif isinstance(active_model, str) and active_model.strip() and known_ids:
        if active_model not in known_ids:
            warn(
                f"agent.active_model={active_model!r} 不在 agent.models 里，"
                "面板会回落到第一条"
            )

    judge_model = agent.get("judge_model")
    if judge_model is not None and not isinstance(judge_model, str):
        err(
            "agent.judge_model 必须是字符串"
            "（agent.models 里某条的 id，留空 = 跟随主模型）"
        )
    elif isinstance(judge_model, str) and judge_model.strip() and known_ids:
        if judge_model not in known_ids:
            warn(
                f"agent.judge_model={judge_model!r} 不在 agent.models 里，"
                "插话判断器会跟随主模型"
            )

    fallback_models = agent.get("fallback_models")
    if fallback_models is not None and not isinstance(fallback_models, list):
        err("agent.fallback_models 必须是数组（按顺序填 agent.models 里的 id）")
    elif isinstance(fallback_models, list):
        seen_fallback: set[str] = set()
        for index, model_id in enumerate(fallback_models):
            tag = f"agent.fallback_models[{index}]"
            if not isinstance(model_id, str) or not model_id.strip():
                err(f"{tag} 必须是非空字符串（agent.models 里某条的 id）")
                continue
            if model_id in seen_fallback:
                warn(f"{tag}={model_id!r} 重复，运行时只会用一次")
            seen_fallback.add(model_id)
            if known_ids and model_id not in known_ids:
                warn(f"{tag}={model_id!r} 不在 agent.models 里，运行时会被跳过")
            if isinstance(active_model, str) and model_id == active_model:
                warn(f"{tag}={model_id!r} 就是当前主模型，降级时会被忽略")

    # LLM 接入（网页可改，保存后热加载）
    base_url = agent.get("base_url")
    if base_url is not None:
        if not isinstance(base_url, str) or not base_url.strip():
            err("agent.base_url 必须是非空字符串")
        elif not re.match(r"^https?://", base_url.strip(), re.I):
            err("agent.base_url 必须以 http:// 或 https:// 开头")

    api_key = agent.get("api_key")
    if api_key is not None and not isinstance(api_key, str):
        err("agent.api_key 必须是字符串（留空 = 用环境变量 DEEPSEEK_API_KEY）")

    api_key_env = agent.get("api_key_env")
    if api_key_env is not None and (
        not isinstance(api_key_env, str)
        or (api_key_env and not api_key_env.strip().isidentifier())
    ):
        err("agent.api_key_env 必须是合法的环境变量名（例如 QIAN_FAN_API_KEY）")

    model = agent.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        err("agent.model 必须是非空字符串")

    temperature = agent.get("temperature")
    if temperature is not None and (
        not isinstance(temperature, (int, float))
        or isinstance(temperature, bool)
        or not 0 <= float(temperature) <= 2
    ):
        err("agent.temperature 必须是 0~2 的数字")

    max_tokens = agent.get("max_tokens")
    if max_tokens is not None and (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        err("agent.max_tokens 必须是正整数（留空 = 环境变量 LLM_MAX_TOKENS / 10240）")

    # ── 消息处理 ──
    message = _bool_section(data, "message", "message", err)
    extra = message.get("extraconfig")
    if extra is not None and not isinstance(extra, dict):
        err("message.extraconfig 必须是对象")
    elif isinstance(extra, dict):
        _bool_field(extra, "tokenizer", "message.extraconfig.tokenizer", err)
        _bool_field(extra, "recent", "message.extraconfig.recent", err)

    # ── 搬运 ──
    auto = _bool_section(data, "auto_forward", "auto_forward", err)
    _bool_field(auto, "enable", "auto_forward.enable", err)
    _bool_field(auto, "dedup", "auto_forward.dedup", err)
    _int_list(auto.get("groupid_list"), "auto_forward.groupid_list", err, warn)
    _int_list(auto.get("target_groupid"), "auto_forward.target_groupid", err, warn)

    if "user_id_list" in auto and "userid_list" not in auto:
        warn("auto_forward.user_id_list 已改名为 userid_list，建议改名（旧名仍然生效）")
    _int_list(
        auto.get("userid_list", auto.get("user_id_list")),
        "auto_forward.userid_list",
        err,
        warn,
    )
    _int_list(auto.get("task_userid"), "auto_forward.task_userid", err, warn)

    from_bot = auto.get("auto_img_forward_from_bot")
    if from_bot is not None and not isinstance(from_bot, dict):
        err("auto_forward.auto_img_forward_from_bot 必须是对象")
    elif isinstance(from_bot, dict):
        _bool_field(
            from_bot, "enable", "auto_forward.auto_img_forward_from_bot.enable", err
        )
        # 旧写法：纯 QQ 号数组，等价于每个都是 type=all
        _int_list(
            from_bot.get("bot_id_list"),
            "auto_forward.auto_img_forward_from_bot.bot_id_list",
            err,
            warn,
        )
        bot_list = from_bot.get("bot_list")
        if bot_list is not None and not isinstance(bot_list, list):
            err("auto_forward.auto_img_forward_from_bot.bot_list 必须是数组")
        elif isinstance(bot_list, list):
            for index, entry in enumerate(bot_list):
                tag = f"auto_forward.auto_img_forward_from_bot.bot_list[{index}]"
                if not isinstance(entry, dict):
                    err(f'{tag} 必须是对象，例如 {{"bot_id": 123456, "type": "group"}}')
                    continue
                if not str(entry.get("bot_id", "")).strip():
                    err(f"{tag}.bot_id 不能为空")
                elif not str(entry.get("bot_id", "")).strip().isdigit():
                    warn(f"{tag}.bot_id = {entry.get('bot_id')!r} 不是纯数字")
                scope = str(entry.get("type", "all") or "all").strip().lower()
                if scope not in ("group", "private", "all"):
                    err(
                        f"{tag}.type 只能是 group / private / all"
                        f"（当前 {entry.get('type')!r}）"
                    )
                _int_list(entry.get("group_id"), f"{tag}.group_id", err, warn)
                _int_list(entry.get("user_id_list"), f"{tag}.user_id_list", err, warn)

    _strip = _validate_strip_tail_ads(auto, err, warn)

    image_forward = _bool_section(data, "image_forward", "image_forward", err)
    _int_list(
        image_forward.get("auto_target_group_ids"),
        "image_forward.auto_target_group_ids",
        err,
        warn,
    )

    # ── 占位命令 /jm ──
    jm = _bool_section(data, "jm", "jm", err)
    if jm and not isinstance(jm.get("enable", False), bool):
        err("jm.enable 必须是布尔值")

    # ── 提示词 / 回复文案 ──
    for section, label in (("prompts", "prompts"), ("replies", "replies")):
        node = data.get(section)
        if node is None:
            continue
        if not isinstance(node, dict):
            err(f"{label} 必须是对象")
            continue
        for key, value in node.items():
            if not isinstance(value, str):
                err(f"{label}.{key} 必须是字符串")

    history_regex = ((data.get("prompts") or {}).get("history_command_regex"))
    if isinstance(history_regex, str) and history_regex:
        try:
            re.compile(history_regex)
        except re.error as exc:
            err(f"prompts.history_command_regex 不是合法正则: {exc}")

    # ── 关键词 ──
    actions = data.get("keyword_actions")
    if actions is None:
        warn("keyword_actions 为空：关键词 / 命令都不会触发")
    elif not isinstance(actions, list):
        err("keyword_actions 必须是数组")
    else:
        for index, item in enumerate(actions):
            tag = f"keyword_actions[{index}]"
            if not isinstance(item, dict):
                err(f"{tag} 必须是对象")
                continue

            regex = item.get("regex")
            if not isinstance(regex, str) or not regex:
                err(f"{tag}.regex 不能为空")
            else:
                try:
                    re.compile(regex, re.I)
                except re.error as exc:
                    err(f"{tag}.regex 不是合法正则: {exc}")

            action = item.get("action")
            if action not in known:
                warn(f"{tag}.action={action!r} 未注册，运行时会跳过这条规则")

            params = item.get("params")
            if params is None:
                params = {}
                item["params"] = params
            if not isinstance(params, dict):
                err(f"{tag}.params 必须是对象")
                continue

            if action == "send_message" and not params.get("message"):
                warn(f"{tag}: send_message 需要 params.message（字符串或字符串数组）")

            if action == "send_img":
                addr = params.get("img_addr")
                if not isinstance(addr, str) or not addr:
                    err(f"{tag}: send_img 需要 params.img_addr")
                elif (
                    not addr.startswith(("/", "\\"))
                    and ":" not in addr
                    and "{imgs_dir}" not in addr
                ):
                    warn(
                        f"{tag}: img_addr={addr!r} 不是绝对路径"
                        "（推荐用 {imgs_dir}/文件名）"
                    )

    # ── 群聊插话 ──
    interject = _bool_section(data, "interject", "interject", err)
    if interject:
        _bool_field(interject, "enable", "interject.enable", err)
        for name, label, minimum in (
            ("frequent", "累计多少条消息触发判断", 2),
            ("extra_history", "触发时额外多扒的条数", 0),
            ("idle_minutes", "安静检查间隔", 1),
            ("cooldown_minutes", "两次插话最小间隔", 0),
            ("hot_minutes", "Bot 说话后的敏感期", 1),
            ("hot_frequent", "敏感期内的触发条数", 1),
            ("hot_idle_minutes", "敏感期内的检查间隔", 1),
        ):
            value = interject.get(name)
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                err(f"interject.{name} 必须是数字（{label}）")
            elif value < minimum:
                err(f"interject.{name} 不能小于 {minimum}（{label}）")
        for name in ("groups", "blacklist_groups"):
            value = interject.get(name)
            if value is not None and not isinstance(value, list):
                err(f"interject.{name} 必须是数组（群号列表）")
        aliases = interject.get("aliases")
        if aliases is not None and not isinstance(aliases, list):
            err("interject.aliases 必须是数组（机器人别名列表，用来识别「喊名字没 @」）")
        interests = interject.get("interests")
        if interests is not None:
            if not isinstance(interests, dict):
                err("interject.interests 必须是对象（high / low 两个词表）")
            else:
                for name in ("high", "low"):
                    value = interests.get(name)
                    if value is not None and not isinstance(value, list):
                        err(f"interject.interests.{name} 必须是数组（兴趣词表）")

    # ── 表情包 ──
    meme = _bool_section(data, "meme", "meme", err)
    if meme:
        _bool_field(meme, "enable", "meme.enable", err)
        path = meme.get("path")
        if path is not None and not isinstance(path, str):
            err("meme.path 必须是字符串（表情包 JSON 路径）")
        limit = meme.get("prompt_limit")
        if limit is not None:
            if not isinstance(limit, (int, float)) or isinstance(limit, bool):
                err("meme.prompt_limit 必须是数字（0 表示全部写进提示词）")
            elif limit < 0:
                err("meme.prompt_limit 不能小于 0")

    # ── 面板自身 ──
    panel = _bool_section(data, "web_panel", "web_panel", err)
    _bool_field(panel, "enable", "web_panel.enable", err)
    port = panel.get("port")
    if port is not None and (
        not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535)
    ):
        err("web_panel.port 必须是 1~65535 之间的整数")
    host = panel.get("host")
    if host is not None and not isinstance(host, str):
        err("web_panel.host 必须是字符串，例如 127.0.0.1")
    token = panel.get("token")
    if token is not None and not isinstance(token, str):
        err("web_panel.token 必须是字符串（留空表示不需要 token）")
    elif (
        isinstance(host, str)
        and host
        and host not in ("127.0.0.1", "localhost", "::1")
        and not token
    ):
        warn("面板监听非本机地址且没有设置 token，任何能访问该端口的人都能改参数")

    # ── 未知参数段 ──
    for key in data:
        if str(key).startswith("_"):
            continue
        if key not in KNOWN_TOP_KEYS:
            warn(f"未知参数段 {key!r}：Bot 不会读取，可能是拼写错误")

    return errors, warnings


__all__ = [
    "FALLBACK_ACTIONS",
    "KNOWN_TOP_KEYS",
    "PATH_KEYS",
    "WS_KEYS",
    "paths_section",
    "validate_config",
    "validate_paths_config",
    "validate_ws_config",
    "ws_section",
]
