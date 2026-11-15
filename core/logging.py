"""统一日志。

旧代码大量使用 ``print``，好处是启动日志直观，坏处是没法分级、没法落盘。
新项目保留控制台输出，同时提供 ``logging`` 接口：

    from core.logging import get_logger
    log = get_logger("bot.forward")
    log.info("搬到 %s", group_id)
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
_DATE_FORMAT = "%H:%M:%S"
#: 单个日志文件上限 / 保留份数。聊天机器人一天能写不少，只留最近几份够用。
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5


def setup(level: int = logging.INFO, log_file: str | Path | None = None) -> None:
    """配置根 logger。

    注意 ``log_file`` 必须能**事后补上**：模块级 ``get_logger()`` 会在 import 期
    就调用 ``setup()``（那时还没有 log_file），一旦把它标记成「已配置」，后面
    ``run.py`` 再带着 log_file 调进来就直接 return 了 —— 结果是控制台有日志、
    磁盘上一个字都没有。所以只有「已配置且这次也没要求落盘」才提前返回。
    """
    global _CONFIGURED
    if _CONFIGURED and not log_file:
        return
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=_MAX_BYTES,
                backupCount=_BACKUP_COUNT,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=level,
        format=_FORMAT,
        datefmt=_DATE_FORMAT,
        handlers=handlers,
        force=True,
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)


__all__ = ["setup", "get_logger"]
