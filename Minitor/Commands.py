"""
命令处理（CommandHandlers）—— 聊天里触发的功能指令

- export：把一段历史消息批量搬运到指定群 / 用户
- fabric：按 [qq:xxx][name:xxx][context:xxx] 模板拼一条转发节点发出去
- GetMusic：点歌（“我想听 / MUSIC_GET / GETMUSIC”）
- img_forward_create_task：img_fd 命令，创建图片批量搬运任务（-g/-u/-a + -b/-e）

关键词（regex）和参数都在 bot_config.json 的 keyword_actions 里配置，
BOTMain 只负责在消息匹配到关键词时把事件交给这里。
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import re

try:  # 兼容 run.py 的扁平 import 和 `Minitor.Commands` 包内 import
    from MusicServer import search_and_resolve_first_song
except ImportError:  # pragma: no cover
    from Minitor.MusicServer import search_and_resolve_first_song


class CommandHandlers:
    """聊天功能指令实现，依赖文本层 + 转发服务 + 图片搬运任务管理。"""

    def __init__(self, mp, nc, forward=None, image_forward=None, config=None):
        """
        :param mp: MessageProcessor 实例（文本处理层）
        :param nc: NapCatAPIInterface 实例（与 BOTMain 共享同一条连接）
        :param forward: ForwardService 实例（export 复用其 auto_forward）
        :param image_forward: image_forward_service 实例（img_fd 创建搬运任务）
        :param config: BotConfig 实例，缺省时使用内置默认参数
        """
        self.mp = mp
        self.nc = nc
        self.forward = forward
        self.ifs = image_forward
        self.config = config

    def image_forward_auto_targets(self) -> list:
        """img_fd -a 时使用的默认目标群列表（来自 bot_config.json）"""
        if self.config is None:
            return []
        return list(self.config.image_forward_auto_targets)

    # ════════════════════ 命令 / 功能 ════════════════════
    async def export(self, event):
        DEBUG = True
        message_id = self.mp.is_reply(event)
        if not message_id:
            return False
        clean_msg = self.mp.get_clean_context(event)
        match1 = re.search(r"(-group|-g) (\d+)", clean_msg)
        match2 = re.search(r"(-user|-u) (\d+)", clean_msg)
        if not match1 and not match2:
            DEBUG and print("Find no Target")
            return False
        if match1:
            target_group = int(match1.group(2))
        if match2:
            target_user_id = int(match2.group(2))
        end_id = event["message_id"]
        if match1:
            await self.mp.get_long_history_only_processor(
                group_id=event["group_id"],
                start_id=message_id,
                end_id=end_id,
                processor=self.forward.auto_forward,
                params={
                    "groupid_list": [event["group_id"]],
                    "target_groupid": [target_group],
                },
            )
        if match2:
            print("Prepare to start export user")
            await self.mp.get_long_history_only_processor(
                group_id=event["group_id"],
                start_id=message_id,
                end_id=end_id,
                processor=self.forward.auto_forward,
                params={
                    "groupid_list": [event["group_id"]],
                    "task_userid": [target_user_id],
                },
            )
        return True

    async def fabric(self, event):
        clean_message = self.mp.get_clean_context(event)
        pat = r"\[qq:(\d+)\].*?\[context:(.*)\]"
        match = re.search(pat, clean_message, re.S)
        name_match = re.search(r"\[name:([^\]]*)\]", clean_message)
        if not match:
            return
        if event.get("message_type") == "group":
            await self.mp.ensure_connected()
            await self.nc.send_group_forward_msg(
                group_id=event.get("group_id"),
                messages=[
                    {
                        "type": "node",
                        "data": {
                            "uin": int(match.group(1)),
                            "name": name_match.group(1) if name_match else "QQ用户",
                            "content": [
                                {"type": "text", "data": {"text": match.group(2)}}
                            ],
                        },
                    }
                ],
            )
            return
        return

    async def GetMusic(self, event):
        clean_msg = self.mp.get_clean_context(event)
        match = re.search(r"\[([^\]]*)\]",clean_msg)
        if not match:
            return
        query = match.group(1)
        res = search_and_resolve_first_song(query)
        url = res.get("music_url")
        print(f"[GetMusic] query={query}, url={url}")
        await self.mp.ensure_connected()
        await self.nc.send_group_message(
            group_id=event.get("group_id"), message=[{"type": "record", "data":{"url": url}}]
        )

    async def img_forward_create_task(self, event):
        clean_msg = self.mp.get_clean_context(event=event)
        rpy = self.mp.is_reply(event)
        target_group_id_list = None
        match = re.search(r"(-g|-group) (\d+)", clean_msg)
        if match:
            target_id = int(match.group(2))
            await self.ifs.create_task(
                group_id=event["group_id"], target_id_list=[target_id], msg_type="group"
            )
        else:
            match = re.search(r"(-u|-user) (\d+)", clean_msg)
            if match:
                target_id = int(match.group(2))
                await self.ifs.create_task(
                    group_id=event["group_id"],
                    target_id_list=[target_id],
                    msg_type="private",
                )
            else:
                match = re.search(r"(-a|-auto)", clean_msg)
                if match:
                    target_group_id_list = self.image_forward_auto_targets()
                    await self.ifs.create_task(
                        group_id=event["group_id"],
                        target_id_list=target_group_id_list,
                        msg_type="group",
                    )

        match_1 = re.search(r"(-begin|-b)", clean_msg)
        if match_1:
            await self.ifs.set_begin_id(
                group_id=event["group_id"], begin_id=rpy or event["message_id"]
            )
        match_2 = re.search(r"(-end|-e)", clean_msg)
        if match_2 and match_1:
            await self.ifs.set_end_id(
                group_id=event["group_id"], end_id=event["message_id"]
            )
        elif match_2:
            await self.ifs.set_end_id(
                group_id=event["group_id"], end_id=rpy or event["message_id"]
            )
        return
