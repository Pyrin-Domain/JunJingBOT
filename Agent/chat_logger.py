"""
对话 JSON 日志记录器
每次 LLM 调用的上下文、工具调用、回复均写入结构化 JSON 文件
按日期分文件存储: Agent/logs/YYYY-MM-DD.json
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("QQBotAgent.JsonLogger")

# 日志存放目录
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


def _ensure_log_dir():
    """确保日志目录存在"""
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR)
        logger.info(f"创建日志目录: {LOG_DIR}")


def _log_file_path() -> str:
    """返回当天的日志文件路径"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"{date_str}.json")


def append_log(entry: dict) -> None:
    """追加一条日志条目到当天的 JSON 文件

    Args:
        entry: 日志条目字典
    """
    _ensure_log_dir()
    file_path = _log_file_path()

    # 给条目加上时间戳
    entry.setdefault("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    try:
        # 读取已有记录
        records: list[dict] = []
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    records = json.loads(content)

        # 追加新条目
        records.append(entry)

        # 写回
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.error(f"写入 JSON 日志失败: {e}")


def build_log_entry(
    thread_id: str,
    user_message: str,
    ai_response: str,
    extra_context: Optional[str] = None,
    method_context: Optional[str] = None,
    tool_calls: Optional[list[dict]] = None,
    error: Optional[str] = None,
) -> dict:
    """构造一条结构化的对话日志条目

    Args:
        thread_id: 会话 ID
        user_message: 用户消息
        ai_response: AI 回复
        extra_context: 额外上下文（群聊信息等）
        method_context: Method 上下文
        tool_calls: 工具调用记录列表
        error: 错误信息

    Returns:
        日志条目字典
    """
    entry = {
        "thread_id": thread_id,
        "user_message": user_message,
        "ai_response": ai_response,
    }
    if extra_context:
        entry["extra_context"] = extra_context
    if method_context:
        entry["method_context"] = method_context
    if tool_calls:
        entry["tool_calls"] = tool_calls
    if error:
        entry["error"] = error

    return entry
