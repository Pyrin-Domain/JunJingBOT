"""对话 JSON 日志记录器。

每次 LLM 调用的上下文、工具调用、回复均写入结构化 JSON 文件，
按日期分文件存储：``logs/agent/YYYY-MM-DD.json``（旧版是 ``Agent/logs``）。

语义与旧版一致：整个文件是一个 JSON 数组，每次追加一条记录（读 → append → 写回）。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.paths import AppPaths

logger = logging.getLogger("QQBotAgent.JsonLogger")

_LOCK = threading.RLock()


def _default_log_dir() -> str:
    """没显式配置时写到项目的 logs/agent/。"""
    try:
        return str(AppPaths.discover().logs_dir / "agent")
    except Exception:  # pragma: no cover - 极端环境下 Path 都不可用
        return str(Path(__file__).resolve().parent.parent / "logs" / "agent")


# 日志存放目录（可被 runtime 用 settings.paths.logs_dir 覆盖）
LOG_DIR = _default_log_dir()


def configure_log_dir(path: str | Path | None) -> str:
    """指定日志目录；传 None 恢复默认。返回最终目录。"""
    global LOG_DIR
    LOG_DIR = str(path) if path else _default_log_dir()
    return LOG_DIR


def reset_log_dir() -> str:
    return configure_log_dir(None)


def _ensure_log_dir() -> None:
    """确保日志目录存在。"""
    path = Path(LOG_DIR)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        logger.info(f"创建日志目录: {path}")


def _log_file_path() -> str:
    """返回当天的日志文件路径。"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    return str(Path(LOG_DIR) / f"{date_str}.json")


def append_log(entry: dict) -> None:
    """追加一条日志条目到当天的 JSON 文件。

    Args:
        entry: 日志条目字典
    """
    _ensure_log_dir()
    file_path = Path(_log_file_path())

    # 给条目加上时间戳
    entry.setdefault("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    try:
        with _LOCK:
            # 读取已有记录
            records: list[dict] = []
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    records = json.loads(content)

            # 追加新条目
            records.append(entry)

            # 写回
            file_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )

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
    status: Optional[str] = None,
    think: Optional[str] = None,
    raw_response: Optional[str] = None,
) -> dict:
    """构造一条结构化的对话日志条目。

    Args:
        thread_id: 会话 ID
        user_message: 用户消息
        ai_response: AI 回复（已渲染成实际发送的消息文本）
        extra_context: 额外上下文（群聊信息等）
        method_context: Method 上下文
        tool_calls: 工具调用记录列表
        error: 错误信息
        status: 模型输出的 <status> 状态栏（仅日志）
        think: 模型输出的思考（仅日志）
        raw_response: 模型原始输出（含 XML 标签，仅日志）

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
    if status:
        entry["status"] = status
    if think:
        entry["think"] = think
    if raw_response:
        entry["raw_response"] = raw_response
    if error:
        entry["error"] = error

    return entry


__all__ = [
    "LOG_DIR",
    "append_log",
    "build_log_entry",
    "configure_log_dir",
    "reset_log_dir",
]
