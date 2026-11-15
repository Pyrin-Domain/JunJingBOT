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
            # Agent 自己的口头禅库：JSON 数组，每项 {context, describe}。
            # 相对路径按项目根解析；留空 / false 可关闭。
            "catchphrase_file": "agent/catchphrases.json",
            "catchphrase_limit": 0,
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
        # 发文件 / 视频段时等协议端回执的秒数。协议端要等**整个上传结束**才回包
        # （实测 36MB≈149s），30 秒的传输层默认值不够，这里放宽到 10 分钟。
        # 超时后不重试、不换通道，也不回复任何文案。
        # bili_history：按 BV 号索引本地文件，同一条视频只下载一次；
        # 关掉后每次都重新下载（旧行为）。
        "media": {"send_timeout": 600, "bili_history": True},
        "meme": {
            "enable": True,
            "path": "data/memes.json",
            "prompt_limit": 0,
        },
        # 旧版彩蛋：只在这个群里收集 emoji 包信息（写 data/emojilist.json）。
        "emoji": {
            "collect_group_id": 20001,
        },
        # 多轮留存：第一轮的完整上下文冻结，之后每轮只追加增量（见
        # agent/thread_turns.py）。groups 留空 = 全部会话生效。
        # ab_day_parity 设成 1 / 0 时按「奇数日开 / 偶数日关」交替，用来做去噪声
        # 的对照实验：同一天内行为一致（连续几轮才能攒出可回放的上下文），跨天
        # 才能比 —— 按消息随机会让实验组永远从冷启动开始，测不到东西。
        "thread_memory": {
            "stable_prefix": False,
            "verdict": True,
            "new_after_minutes": 15,
            "groups": [],
            "ab_day_parity": None,
            "ttl_minutes": 30,
            "max_turns": 8,
            "max_chars": 12000,
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
            # 按「来源群 + 发送者」过滤聊天记录转发：
            # [{"group_id": 123456, "user_id": [10001, 10002]}]
            "filter": [],
            "auto_img_forward_from_bot": {
                "enable": True,
                "bot_list": [
                    {"bot_id": 10005, "type": "all", "group_id": [], "user_id_list": []},
                    {"bot_id": 10006, "type": "all", "group_id": [], "user_id_list": []},
                    {"bot_id": 10007, "type": "all", "group_id": [], "user_id_list": []},
                ],
            },
            # 「这条发过了」的提示表情（商城表情 mface）：只发这一个表情，
            # 表情自带表情名（summary），不用再配一句文字说明。
            # 三件套缺一不可：emoji_id（表情本体）+ emoji_package_id（表情包）
            # + key（包内密钥）；summary 是表情名，客户端就显示它。
            # 配不全 / 删掉 → 退回本地图片 imgs/news.gif + replies.forwarded_already。
            "forwarded_already_mface": {
                "summary": "[此条news已被其他群友发布过]",
                "emoji_id": "2d831a9731974a5bd0fa57e892bc0bd8",
                "emoji_package_id": 233590,
                "key": "fe89fd36de66bdc2",
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
        "forward_image_dedup": {
            # 是否按「消息身份 × 会话」查重。关闭后白名单机器人发的图会直接搬运，
            # 不查询 / 不登记送达记录（图片过滤门仍然生效）。
            "enable": True,
            # 图片送达记录（message identity × 会话）的保留天数。
            # 超过这个天数的送达记录会在下次写库时清掉；0 = 不清理。
            "keep_days": 30,
            # 图片内容级身份（image:*/image_fileid:*）的保留天数。
            # 只认 message_id/real_id 时，同一张图换消息 id（断点回放 / 重启后重新分配 id）
            # 会被当成新图重复搬运；开启内容级身份后，按图片文件哈希去重。
            # 设 0 表示不单独清理内容级身份（用 keep_days 兜底），
            # 相当于永久抑制同一张图的重复搬运；设 1~3 天可在去重与合法重发之间折中。
            "image_keep_days": 2,
            # 感知哈希兜底：按图片本体算 aHash+dHash 再模糊匹配。
            # 只认 message_id / 文件名 md5 时，QQ 把同一张图重新编码后 md5 会变，
            # 同一张图会被当成新图重复搬运；开启后按像素指纹认出来。
            "perceptual": True,
            # 模糊匹配阈值：aHash(64bit)+dHash(64bit) 共 128bit 的汉明距离。
            # 实测同一张图重新压缩/缩放后距离 0~2，不同截图约 5 以上，默认 4。
            "perceptual_max_distance": 4,
            # 感知指纹 / 字节身份的独立保留天数；0 = 用 keep_days 兜底。
            "perceptual_keep_days": 0,
            # 单张图下载超时（秒）。图片过滤门已经下过时直接命中缓存。
            "perceptual_timeout": 8.0,
        },
        "image_forward": {
            "auto_target_group_ids": [20003, 20002, 20011, 20005]
        },
        # 图片过滤门：过滤函数 + 回调，配置驱动的可开关列表。
        # 命中后按 single_tag_mode / record_tag_mode 把标签发到 audit_group_id。
        # 默认关闭；具体过滤项在 filters 里逐条 enable（见 docs/FLOWS.md）。
        "image_filter": {
            "enable": False,
            "audit_group_id": 0,
            # 审计群「复搬」的白名单 QQ；留空 = 审计群里谁都能复搬
            "audit_operators": [],
            "single_tag_mode": "single",
            "record_tag_mode": "reply",
            "block_matched": True,
            "scan_all_images": True,
            "short_circuit": False,
            # 0 = 不设上限。老默认 12 会让大记录后面的图直接不判（等于漏过）
            "max_images": 0,
            "image_timeout": 8,
            "max_image_bytes": 20971520,
            "max_tag_images": 6,
            "filters": [
                {
                    "id": "phoebe_bullshit",
                    "enable": False,
                    "params": {},
                },
                {
                    "id": "nsfw",
                    "enable": False,
                    "params": {"threshold": 0.5},
                },
            ],
        },
        # export -a 的默认目标群。留空（[]）就用 auto_forward.target_groupid，
        # 这样 -a 与自动搬运的目标群保持一致；想单独指定再填这里。
        "export": {
            "auto_target_group_ids": []
        },
        # 心理分析：群里发「分析心理」并用它回复一条消息 / 一段聊天记录，
        # 让**独立于主对话**的模型做一次心理侧写。
        #
        # 唤醒：整条消息基本等于 keywords 里某个词才触发（"分析心理学" 不算）。
        # 模型：model_id 可引用 agent.models 的某条；留空则用本节自己的
        #       base_url / api_key / model（都留空 = 功能未配置，触发时回提示）。
        # 图片：image_mode 三选一——
        #       image  原样发送图片（分析模型必须是多模态）
        #       ocr    不发送图片，只把 OCR 文字并进记录
        #       vision 先让视觉模型写客观描述，再交给分析模型（辅助视觉分析）
        "psychology": {
            "enable": False,
            "keywords": ["分析心理", "心理分析", "心理侧写"],
            # 回复形式：text = 一段总结；forward = 重建成一条聊天记录
            # （每条原话后面跟 <心理侧写>）；both = 两者都发（多一次模型调用）。
            "reply_mode": "forward",
            "image_mode": "vision",
            "model_id": "",
            "base_url": "",
            "api_key": "",
            "api_key_env": "",
            "model": "",
            "temperature": 0.5,
            "max_tokens": 900,
            # 辅助视觉分析 / 原样发图时可以单独指定视觉模型；
            # 留空表示用上面的分析模型（要求它支持图片输入）。
            "vision": {
                "model_id": "",
                "base_url": "",
                "api_key": "",
                "api_key_env": "",
                "model": "",
            },
            # 留空 = 用内置提示词；改这里可以换口径 / 换输出结构。
            "prompt": "",
            "vision_prompt": "",
            # 逐条侧写（聊天记录模式）的提示词与输出额度：条数多时需要更长的输出。
            "nodes_prompt": "",
            "forward_max_tokens": 2000,
            # 节点里是否带原图（图片已过过滤门；总预算 12MB，超了自动不附）。
            "forward_include_images": True,
            # 送进模型的图片张数（0 = 不带图）；记录条数与单张图上限。
            "max_images": 6,
            "image_max_bytes": 4194304,
            "image_max_side": 1568,
            "max_records": 60,
            # 发图前过一遍图片过滤门（命中就不发给模型）。
            "use_image_filter": True,
            # 同一会话两次分析的间隔，防刷屏。
            "cooldown_seconds": 30,
            "groups": [],
            "blacklist_groups": [],
            "allow_private": True,
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
            "cors_origins": [],
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
