"""日志必须真的落盘。

现场：``run.py`` 明明调了 ``setup_logging(log_file=logs/bot.log)``，``logs/`` 下
却一个文件都没有 —— 因为模块级 ``get_logger()`` 在 import 期先调了一次 ``setup()``
（不带文件），把「已配置」标记立住了，后面带文件的调用直接 return。
结果就是：控制台有日志，磁盘上一个字没有，出了问题只能靠人回忆。
"""

import logging

from core import logging as core_logging


def test_log_file_is_applied_after_early_get_logger(tmp_path):
    core_logging._CONFIGURED = False
    try:
        logger = core_logging.get_logger("test.logging")  # 先来一次「只有控制台」
        target = tmp_path / "bot.log"

        core_logging.setup(log_file=target)  # 启动时才指定文件
        logger.info("落盘验证 %s", 42)
        for handler in logging.getLogger().handlers:
            handler.flush()

        assert target.exists()
        assert "落盘验证 42" in target.read_text(encoding="utf-8")
    finally:
        # 恢复成只有控制台：不然这个文件句柄会跟着整个测试会话，
        # tmp_path 清理时会因为文件被占用而报错。
        core_logging._CONFIGURED = False
        core_logging.setup()


def test_repeated_setup_without_file_does_not_duplicate_handlers():
    core_logging._CONFIGURED = False
    try:
        core_logging.setup()
        before = len(logging.getLogger().handlers)
        core_logging.get_logger("test.logging.again")
        assert len(logging.getLogger().handlers) == before
    finally:
        core_logging._CONFIGURED = False
        core_logging.setup()
