"""默认配置。

`bot_config.json` 的默认值与旧项目保持一致（键名、默认值都不变），
这样旧配置复制过来可以直接用，网页面板也不需要改字段名。
"""

from __future__ import annotations

from copy import deepcopy

from .backends import DEFAULT_BACKEND, get_backend
from .environment import detect_env


def default_bot_config() -> dict:
    return {
        "master_qq": ["10001"],
        "heartbeat": {"min_minutes": 10, "max_minutes": 20},
        "agent": {
            "enable": True,
            # 人设拆成「角色卡 + 通用群聊行为」；相对项目根目录。
            # 文件缺失 / 为空时回退 agent/prompts.py 里的内置兜底。
            "persona_file": "agent/personas/junjing.md",
            "persona_core_file": "agent/personas/core_qq.md",
            "models": [],
            "active_model": "",
            "judge_model": "",
            "fallback_models": [],
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "",
            "api_key_env": "DEEPSEEK_API_KEY",
            "model": "deepseek-v4-flash",
            "temperature": 0.7,
            "max_tokens": None,
        },
        "message": {"extraconfig": {"tokenizer": False, "recent": True}},
        "meme": {
            "enable": True,
            "path": "data/memes.json",
            "prompt_limit": 0,
        },
        # 旧版彩蛋：只在这个群里收集 emoji 包信息（写 data/emojilist.json）。
        "emoji": {
            "collect_group_id": 20001,
        },
        # 人物关系档案：标签 / 人格画像 / 近期状态 / 重要记忆。
        # 数据在 data/relations/（users/<QQ>.json、groups/<群号>.json、self.json）。
        "relations": {
            "enable": True,
            "data_dir": "data/relations",
            "master_label": "主人",
            "max_people": 6,
            "max_chars": 1200,
            "max_person_chars": 320,
            "state_ttl_hours": 72,
            "state_max_hours": 168,
            "self_ttl_hours": 12,
            "max_states": 6,
            "max_memories": 30,
            "max_labels": 8,
            "auto_extract": True,
            "auto_every": 8,
            "auto_extract_min_seconds": 600,
        },
        "interject": {
            "enable": True,
            "frequent": 10,
            "extra_history": 5,
            "idle_minutes": 30,
            "cooldown_minutes": 5,
            "hot_minutes": 10,
            "hot_frequent": 3,
            "hot_idle_minutes": 5,
            "groups": [],
            "blacklist_groups": [],
            "aliases": ["君景", "独角兽"],
            "interests": {
                "high": ["女装", "独角兽", "男娘", "可爱", "小裙子"],
                "low": ["作业", "考试", "报错"],
            },
        },
        "auto_forward": {
            "enable": True,
            "groupid_list": [
                20002,
                20003,
                20011,
                20012,
                20013,
                20005,
                20006,
            ],
            "target_groupid": [20003, 20002, 20011],
            "userid_list": [],
            "task_userid": [],
            "dedup": True,
            "auto_img_forward_from_bot": {
                "enable": True,
                "bot_list": [
                    {"bot_id": 10005, "type": "all", "group_id": [], "user_id_list": []},
                    {"bot_id": 10006, "type": "all", "group_id": [], "user_id_list": []},
                    {"bot_id": 10007, "type": "all", "group_id": [], "user_id_list": []},
                ],
            },
            # 末尾广告裁剪 + 尾部追加。开启后搬运不再整卡转发，而是重建成新聊天记录：
            # 末尾 scan_tail 条里的二维码海报 / 群推荐卡片 / 纯链接整条丢掉，
            # 再把 append.items 一条条追加到末尾（像别人的广告一样）
            "strip_tail_ads": {
                "enable": True,
                "scan_tail": 3,
                "drop_qr": True,
                "drop_group_card": True,
                "drop_link": True,
                "image_timeout": 8,
                "append": {
                    "enable": True,
                    "items": [
                        {
                            "uin": 10004,
                            "name": "N552AA🍥🍜🍣🍕💊🍫",
                            "content": "独角兽君景搬运喵~",
                        }
                    ],
                },
            },
        },
        "image_forward": {
            "auto_target_group_ids": [20003, 20002, 20011, 20005]
        },
        # 占位命令：只返回本地占位图。enable=false 时 /jm 只回一句「未启用」，
        # private_only 由 bot.commands.jm 硬性保证，改这里也不会让它发群。
        "jm": {
            "enable": True,
            "note": "占位：返回本地 jpg，不抓取任何站点",
        },
        "prompts": {
            "group_context": "",
            "private_context": "",
            "history_command_regex": "^历史",
        },
        "replies": {
            "forwarded_already": "[发过了喵]",
            "network_upgrade": "校园网升级，校园网助手取消",
        },
        "web_panel": {
            "enable": True,
            "host": "127.0.0.1",
            "port": 8390,
            "token": "",
        },
        "keyword_actions": [
            {
                "regex": r"(男娘|南梁|nn)",
                "action": "send_message",
                "params": {
                    "message": "哪有男娘",
                    "filter": {"type": "ban", "data": ["20014", "20001"]},
                },
            },
            {
                "regex": r"女装",
                "action": "send_message",
                "params": {"message": ["看看女装", "羡慕女装"]},
            },
            {
                "regex": r"(月抛|约炮|(月|约|🈷️)(吗|么|嘛|🐴|🐎))",
                "action": "send_img",
                "params": {"img_addr": "{imgs_dir}/月抛找我.png", "summary": "月抛找我"},
            },
            {
                "regex": r"妈妈(?!])",
                "action": "send_img",
                "params": {"img_addr": "{imgs_dir}/妈妈.gif", "summary": "妈妈"},
            },
            {
                "regex": r"老大(?!])",
                "action": "send_img",
                "params": {"img_addr": "{imgs_dir}/老大.gif", "summary": "老大？"},
            },
            {
                "regex": r"抱",
                "action": "send_img",
                "params": {"img_addr": "{imgs_dir}/要抱.gif", "summary": "要抱"},
            },
            {
                "regex": r"(jjcn|dsn|dkn|顶死你|顶哭你|((jj|唧唧)(草|凿|燥|糙)(你|泥)))",
                "action": "send_img",
                "params": {"img_addr": "{imgs_dir}/速来.jpg", "summary": "速来"},
            },
            {
                "regex": r"(看看脚|白丝)",
                "action": "send_img",
                "params": {"img_addr": "{imgs_dir}/脚.gif", "summary": "脚"},
            },
            {"regex": r"historydebug", "action": "get_long_history_test"},
            {"regex": r"export", "action": "export"},
            {"regex": r"recall", "action": "recall"},
            {"regex": r"set_tokenizer", "action": "set_tokenizer"},
            {"regex": r"ocr", "action": "ocr"},
            {"regex": r"img_fd ", "action": "img_forward_create_task"},
            {"regex": r"fabric", "action": "fabric"},
            {"regex": r"(我想听|MUSIC_GET|GETMUSIC)", "action": "get_music"},
            {"regex": r"^\s*/jm\b", "action": "jm"},
        ],
    }


def default_environment() -> dict:
    """默认环境。

    旧项目默认就是「Bot 在 Windows、NapCat 在 WSL」（`_to_napcat_file_path`
    永远把 `D:\\...` 转成 `/mnt/d/...`），所以 Windows 上默认
    ``backend_env=wsl`` + ``path_profile=windows_to_wsl``。
    同机 Windows 部署把 ``backend_env`` 改成 ``windows``、
    ``path_profile`` 改成 ``same`` 即可。
    """
    bot = detect_env()
    if bot.value == "windows":
        backend = "wsl"
        profile = "windows_to_wsl"
    else:
        backend = bot.value
        profile = "same"
    return {
        "bot_env": bot.value,
        "backend_env": backend,
        "path_profile": profile,
        "wsl_distro": "Ubuntu",
        "wsl_mount_root": "/mnt",
        "imgs_dir": "imgs",
        "downloads_dir": "data/downloads",
        "temp_dir": "data/tmp",
        "path_map": [],
    }


def default_connection(backend: str = DEFAULT_BACKEND) -> dict:
    profile = get_backend(backend)
    return {
        "type": "ws_re",
        "backend": profile.id,
        "access_token": "",
        "ws_reverse_host": profile.default_reverse_host,
        "ws_reverse_port": profile.default_reverse_port,
        "ws_host": profile.default_ws_host,
        "ws_port": profile.default_ws_port,
    }


def default_sender_connection(backend: str = DEFAULT_BACKEND) -> dict:
    profile = get_backend(backend)
    return {
        "type": "ws",
        "backend": profile.id,
        "access_token": "",
        "ws_host": profile.default_ws_host,
        "ws_port": profile.default_sender_port,
    }


def copy_default(value: dict) -> dict:
    return deepcopy(value)


__all__ = [
    "default_bot_config",
    "default_environment",
    "default_connection",
    "default_sender_connection",
    "copy_default",
]
