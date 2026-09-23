"""
BotConfig —— Bot 的可调参数（群号、回复文案、关键词、开关）

设计目标：**改参数不用改代码**。
所有写死在业务代码里的东西（搬哪些群、去重开不开、@ 时怎么拼上下文、
关键词回什么话、点歌触发词……）都收在这里，存在仓库根目录的 `bot_config.json`：

    BotConfig(path)  →  {"内置默认值"} 叠上 {"bot_config.json"} 里的内容

- 文件缺字段 / 整个文件不存在 → 用内置默认值兜底（缺文件时自动生成一份）
- 以后做网页版改参数，直接读写这个 JSON 即可（BotConfig.save() / reload()）
- 各服务只读自己关心的段落：`config.auto_forward_kwargs`、`config.keyword_actions` …

兼容性：默认值与重构前写死在 NapCatTools/BOTMain 里的取值完全一致，
所以不带 bot_config.json 跑起来行为不变。
"""
import json
import os
from copy import deepcopy
from pathlib import Path

try:  # 兼容 run.py 的扁平 import 和 `Minitor.BotConfig` 包内 import
    from Paths import Paths
except ImportError:  # pragma: no cover
    from Minitor.Paths import Paths

# config/bot_config.json（与 config.json / sender_config.json 同一目录；
# 还没搬过去的老部署会继续用仓库根目录那份）
BOT_CONFIG_FILE = Paths.config_file("bot_config.json")


# ──────────────────────────── 内置默认值 ────────────────────────────
def _default_config() -> dict:
    return {
        # 主人 QQ：@ 触发时写进上下文、并用来判断"是不是主人"
        "master_qq": "1013098110",
        # 心跳保活：每 min~max 分钟（区间内随机）调一次接口
        "heartbeat": {"min_minutes": 10, "max_minutes": 20},
        # AI Agent：关掉就完全不加载大模型（只走关键词 + 搬运）
        "agent": {"enable": True},
        # MessageProcessor 的运行时开关
        "message": {"extraconfig": {"tokenizer": False, "recent": True}},
        # 自动搬运：这些群里的转发卡片搬到那些群（enable=False 则整段跳过）
        "auto_forward": {
            "enable": True,
            "groupid_list": [
                1054955587,
                1079845768,
                320955551,
                569117421,
                662802665,
                973208344,
                949950852,
            ],
            "target_groupid": [1079845768, 1054955587, 320955551],
            # 私聊搬运：userid_list = 只搬这些 QQ 的私聊（留空 = 不搬私聊）
            "userid_list": [],
            # 搬运时另外私聊转发给这些好友
            "task_userid": [],
            "dedup": True,
            "auto_img_forward_from_bot": {
                "enable": True,
                # 每个机器人一条：type = group / private / all
                #   group_id      群聊范围，留空 = 所有群聊
                #   user_id_list  私聊范围，留空 = 所有私聊
                "bot_list": [
                    {"bot_id": 3282647559, "type": "all", "group_id": [], "user_id_list": []},
                    {"bot_id": 2027241761, "type": "all", "group_id": [], "user_id_list": []},
                    {"bot_id": 3181349085, "type": "all", "group_id": [], "user_id_list": []},
                ],
            },
        },
        # 图片批量搬运：img_fd -a 默认搬到哪些群
        "image_forward": {
            "auto_target_group_ids": [1079845768, 1054955587, 320955551, 973208344]
        },
        # AI 上下文模板（{xxx} 是占位符，会按事件内容填充）
        "prompts": {
            "group_context": (
                "当前是群聊，群号 {group_id}，发消息的用户 QQ 号是 {user_id}，"
                "消息 ID 是 {message_id},AT的对象的QQ号是{ated_user},"
                "你的QQ号是{self_id},主人的QQ号是{master_id}"
            ),
            "private_context": "当前是私聊，用户 QQ 号是 {user_id}",
            "history_context": "历史消息如下:{history}",
            "history_command_regex": "^历史",
        },
        # 固定回复文案
        "replies": {
            "forwarded_already": "[发过了喵]",
            "network_upgrade": "校园网升级，校园网助手取消",
        },
        # 网页版参数面板（Minitor/WebPanel.py）：改这里的参数不用改代码
        "web_panel": {
            "enable": True,
            "host": "127.0.0.1",  # 只允许本机访问；要让别人访问请设 token
            "port": 8390,
            "token": "",  # 留空 = 不需要 token（仅建议本机使用）
        },
        # 关键词 → 处理函数（action 名到函数的映射见 BOTMain._action_registry）
        # params 里的字符串支持 {imgs_dir} 占位符（自动替换成图片目录）
        "keyword_actions": [
            {
                "regex": r"(男娘|南梁|nn)",
                "action": "send_message",
                "params": {
                    "message": "哪有男娘",
                    "filter": {"type": "ban", "data": ["895977823", "1032125562"]},
                },
            },
            {
                "regex": r"女装",
                "action": "send_message",
                "params": {"message": ["看看女装", "羡慕女装"]},
            },
            # {
            #     "regex": r"药娘",
            #     "action": "send_img",
            #     "params": {"img_addr": "{imgs_dir}/img01.jpg", "summary": "死人妖"},
            # },
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
            {"regex": r"set_tokenizer", "action": "set_tokenizer"},
            {"regex": r"ocr", "action": "ocr"},
            {"regex": r"img_fd ", "action": "img_forward_create_task"},
            {"regex": r"fabric", "action": "fabric"},
            {"regex": r"(我想听|MUSIC_GET|GETMUSIC)", "action": "get_music"},
        ],
    }


def _merge(base: dict, extra: dict) -> dict:
    """递归合并：dict 逐层合并，其它类型（含 list）直接覆盖。"""
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


class BotConfig:
    """Bot 可调参数：内置默认值 ← bot_config.json ← 显式 overrides。"""

    def __init__(self, path=None, overrides: dict | None = None):
        """
        :param path: bot_config.json 路径（缺省用仓库根目录那个；文件不存在则只用默认值）
        :param overrides: 直接覆盖某些字段（测试 / 网页版临时改参数时用）
        """
        self.path = Path(path) if path is not None else BOT_CONFIG_FILE
        self.data = _default_config()
        _merge(self.data, self._read_file())
        if overrides:
            _merge(self.data, overrides)

    # ── 文件读写 ──
    def _read_file(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            return loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as exc:
            print(f"[配置] 读取 {self.path} 失败，改用内置默认值: {exc}")
            return {}

    def save(self, path=None) -> str:
        """把当前（含默认值的完整）参数写回 JSON——网页版保存参数走这里。"""
        target = Path(path) if path is not None else self.path
        os.makedirs(os.path.dirname(str(target)) or ".", exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        return str(target)

    def reload(self) -> "BotConfig":
        """重新读盘（网页版改完文件后调一次即可生效）。"""
        fresh = BotConfig(self.path)
        self.data = fresh.data
        return self

    def to_dict(self) -> dict:
        return deepcopy(self.data)

    # ── 取值 ──
    def get(self, key_path: str, default=None):
        """按点号路径取值：config.get("auto_forward.target_groupid", [])"""
        node = self.data
        for part in str(key_path).split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key_path: str, value) -> "BotConfig":
        """按点号路径改一个值（程序里临时改参数用）。

        例：config.set("replies.network_upgrade", "在升级喵")，改完记得 save()。
        """
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

    # ── 常用段落（各服务只读自己关心的）──
    @property
    def master_qq(self) -> str:
        return str(self.data.get("master_qq", ""))

    @property
    def agent_enabled(self) -> bool:
        return bool(self.get("agent.enable", True))

    @property
    def extraconfig(self) -> dict:
        return dict(self.get("message.extraconfig", {}) or {})

    @property
    def heartbeat_range(self) -> tuple[float, float]:
        """(最短, 最长) 分钟"""
        low = float(self.get("heartbeat.min_minutes", 10))
        high = float(self.get("heartbeat.max_minutes", 20))
        return (min(low, high), max(low, high))

    @property
    def auto_forward_enabled(self) -> bool:
        return bool(self.get("auto_forward.enable", True))

    # ForwardService.auto_forward(...) 认这些参数名。
    # 只把认识的键传下去：否则配置里写错一个键，就会在每条消息上抛 TypeError，
    # 把整条搬运流程打断（认不出来的键直接忽略）。
    AUTO_FORWARD_PARAMS = (
        "groupid_list",       # 监听的来源群
        "userid_list",        # 监听的私聊 QQ
        "target_groupid",     # 搬到哪些群
        "task_userid",        # 另外私聊转发给哪些好友
        "dedup",              # 是否去重
        "auto_img_forward_from_bot",  # 机器人发的图片也搬（bot_list）
    )
    # 配置里允许的别名：user_id_list / user_ids 都当 userid_list 用
    AUTO_FORWARD_ALIASES = {
        "user_id_list": "userid_list",
        "user_ids": "userid_list",
    }

    @property
    def auto_forward_kwargs(self) -> dict:
        """直接喂给 ForwardService.auto_forward(**kwargs) 的参数（不含 enable）"""
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
        actions = self.data.get("keyword_actions")
        return actions if isinstance(actions, list) else []


def load_bot_config(path=None) -> BotConfig:
    """读取 bot_config.json；文件不存在就按内置默认值生成一份，方便直接改。"""
    cfg_path = Path(path) if path is not None else BOT_CONFIG_FILE
    if not cfg_path.exists():
        BotConfig(path=cfg_path).save()
        print(f"[配置] 未找到 {cfg_path}，已按内置默认值生成")
    return BotConfig(path=cfg_path)
