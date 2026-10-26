"""网页面板后端（前后端分离里的“后端”）。

对外的唯一入口：

    from web import WebServer
    panel = WebServer(bot)
    await panel.start_if_enabled()

单独启动面板：

    python -m web.standalone --port 8390
"""

from .server import WebServer

__all__ = ["WebServer"]
