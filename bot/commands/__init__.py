"""聊天命令门面。

旧 ``Minitor/Commands.py`` 里的一个大类按命令拆成同目录下的独立文件：

- :mod:`bot.commands.export` —— export
- :mod:`bot.commands.recall` —— recall
- :mod:`bot.commands.fabric` —— fabric
- :mod:`bot.commands.music` —— get_music
- :mod:`bot.commands.image_forward` —— img_forward_create_task
- :mod:`bot.commands.jm` —— jm（占位，仅私聊）

``CommandHandlers`` 保留原来的样子（同一批方法名），
``bot/commands/registry.py`` 的 action 表只认这个门面，不需要知道实现细节。
"""

from __future__ import annotations

from ..services.range_task import RangeTaskService
from . import export as export_cmd
from . import fabric as fabric_cmd
from . import image_forward as image_forward_cmd
from . import jm as jm_cmd
from . import music as music_cmd
from . import recall as recall_cmd
from .export import parse_range

# 旧名字：`from Minitor.Commands import _parse_range` 的迁移版本
_parse_range = parse_range

__all__ = [
    "CommandHandlers",
    "parse_range",
    "_parse_range",
]


class CommandHandlers:
    """聊天功能指令实现，依赖文本层 + 转发服务 + 区间任务服务。"""

    def __init__(
        self,
        *,
        mp,
        api=None,
        forward=None,
        ranges=None,
        settings=None,
        media=None,
        downloads=None,
        # ── 旧参数名，保留兼容（新代码请用上面的名字）──
        nc=None,
        range_tasks=None,
        config=None,
    ):
        """
        :param mp: MessageProcessor 实例（文本处理层）
        :param api: NapCatAPI 实例（与 BotApp 共享同一条连接）
        :param forward: ForwardService 实例（export 复用其 auto_forward）
        :param ranges: RangeTaskService 实例（export / recall / img_fd 共用）；
                       缺省时自建一个
        :param settings: BotSettings（master_qq / 自动搬运目标群都从这里读）
        :param media: MediaService（点歌等媒体能力）
        :param downloads: DownloadService（media 不可用时的退路）
        """
        self.mp = mp
        self.api = api if api is not None else nc
        self.forward = forward
        self.ranges = ranges if ranges is not None else (range_tasks or RangeTaskService())
        self.settings = settings if settings is not None else config
        self.media = media
        self.downloads = downloads

        # 旧字段名，兼容按属性访问的调用方
        self.nc = self.api
        self.config = self.settings
        self.range_tasks = self.ranges

        # 三个命令共用：按群收集 begin/end，收齐后回调各自的执行函数
        self.ranges.register("export", self._run_export_range)
        self.ranges.register("recall", self._run_recall_range)
        self.ranges.register("img_forward_create_task", self._run_img_range)

    def image_forward_auto_targets(self) -> list:
        """img_fd -a 时使用的默认目标群列表（来自配置）"""
        return image_forward_cmd.auto_targets(self)

    # ════════════════════ 命令 / 功能 ════════════════════
    async def export(self, event):
        return await export_cmd.export(self, event)

    async def recall(self, event):
        return await recall_cmd.recall(self, event)

    async def fabric(self, event):
        return await fabric_cmd.fabric(self, event)

    async def get_music(self, event):
        return await music_cmd.get_music(self, event)

    # 旧名字（BOTMain 的 action_registry / 老代码都叫 GetMusic）
    GetMusic = get_music

    async def img_forward_create_task(self, event):
        return await image_forward_cmd.create_task(self, event)

    async def jm(self, event):
        """``/jm <id>``：占位命令，只在私聊响应，不抓取、不发群。"""
        return await jm_cmd.jm(self, event)

    # ════════════════════ 区间任务回调 ════════════════════
    async def _run_export_range(self, event, data):
        return await export_cmd.run_export_range(self, event, data)

    async def _run_recall_range(self, event, data):
        return await recall_cmd.run_recall_range(self, event, data)

    async def _run_img_range(self, event, data):
        return await image_forward_cmd.run_img_range(self, event, data)
