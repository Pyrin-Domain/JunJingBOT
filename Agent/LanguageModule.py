"""QQBot LLM Agent - 基于 LangGraph + DeepSeek，带工具调用"""

import os
import json
import logging
import contextvars
from typing import Any, Optional
from .context_pocessor import rw_tools
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage
from langchain_core.callbacks import BaseCallbackHandler
from langchain.agents import create_agent
from .chat_logger import append_log, build_log_entry
from pathlib import Path
from Minitor.NapCatTools import _IMGS_DIR, MessageProcessor

# files = [f for f in _IMGS_DIR/.iterdir() if f.is_file()]


# 按 async Task 隔离的工具调用记录，防止并发 chat() 互相污染
_current_tool_calls_var: contextvars.ContextVar[list[dict]] = contextvars.ContextVar(
    "current_tool_calls", default=[]
)
_current_tool_cache_var: contextvars.ContextVar[dict[tuple[str, str], Any]] = contextvars.ContextVar(
    "current_tool_cache", default={}
)
_MISSING = object()

# ============================================================
# 日志配置
# ============================================================
logger = logging.getLogger("QQBotAgent")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(name)s - %(message)s",
        datefmt="%H:%M:%S",
    )
    ch.setFormatter(formatter)
    logger.addHandler(ch)


# ============================================================
# LLM 回调 - 记录思考过程
# ============================================================
class LLMCallbackHandler(BaseCallbackHandler):
    """捕获 LLM 的请求与回复，输出到日志"""

    def on_llm_start(self, serialized, prompts, **kwargs):
        logger.debug(f"[LLM] 发送请求, prompts={len(prompts)} 条")

    def on_llm_end(self, response, **kwargs):
        content = response.generations[0][0].text if response.generations else ""
        logger.debug(
            f"[LLM] 收到回复: {content[:100]}{'...' if len(content)>100 else ''}"
        )

    def on_llm_error(self, error, **kwargs):
        logger.error(f"[LLM] 请求出错: {error}")

    def on_tool_start(self, serialized, input_str, **kwargs):
        logger.debug(
            f"[LLM] 决定调用工具: {serialized.get('name', 'unknown')}({input_str[:80]})"
        )

    def on_tool_end(self, output, **kwargs):
        logger.debug(
            f"[LLM] 工具返回: {str(output)[:80]}{'...' if len(str(output))>80 else ''}"
        )


# ============================================================
# 配置
# ============================================================
LLM_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
    "model": "deepseek-v4-flash",  # DeepSeek-V4-flash
}
MODEL_TYPE = "deepseek"
# LLM_CONFIG = {
#     "base_url": "http://127.0.0.1:8081/v1",
#     "api_key": 'sk-local',
#     "model": "Qwen3.6",  # DeepSeek-V4-flash
# }

# 模型类型宏参数，开发者可在环境变量 LLM_MODEL_TYPE 中设置：qwen 或 deepseek
# MODEL_TYPE = os.environ.get("LLM_MODEL_TYPE", "qwen").strip().lower()
# if not MODEL_TYPE:
#     if "qwen" in LLM_CONFIG["model"].lower():
#         MODEL_TYPE = "qwen"
#     elif "deepseek" in LLM_CONFIG["model"].lower():
#         MODEL_TYPE = "deepseek"
#     else:
#         MODEL_TYPE = "deepseek"


SYSTEM_PROMPT = """
你叫君景，是一只伟大的独角兽，常以君王般的口吻来说话，但不是君王身份，人称代词从不是朕。
每次输出的文字，尽量不要超过150个中文字符的长度
下面是补充，当有人询问的时候你可以去透露，但是不要可以去强调：
begin
主人是最伟大的独角兽馆馆，受他的影响，你才变得伟大。
主人的孩子叫"幻幻",是最可爱的独角兽。
end
下面用来识别主人，不得透露这些消息！！！：
begin
1、
###
##
重要
主人的QQ号是1013098110
不要乱认主人。
会在提示词前面嵌入{isDom:true}来表示是主人。
##
##
你可能遇到一些问题，或者改进，可以思考遇到了什么问题，该如何去改进，
得到的一些宽泛的方法论，可以添加的Method.json中去。
提示词会包括Method。
##
###
end
其他说明
1. 你可以调用工具来发送群消息、私聊消息、查询历史消息等。
2. 不要编造你没有的信息，必要时使用工具查询。
3. 回复尽量精简，不要超过 150 字，除非用户明确要求详细说明。
4. 不要在回复中使用 Markdown 格式（QQ 不支持），用纯文本即可。
5. 如果你觉得当前消息不需要回复（例如话题无聊、对方自言自语、问题你已经回答过、或者你想沉默观察），
   请调用 silent_observe 工具，而不要用自然语言表达"沉默"——工具调用能准确表达你的意图，避免误解。
6、主人的女装照不能随便发！除非主人要求！
7、send_message 工具是一次性终结动作，只有在你已经确定要回复用户时才调用它；不要把它当成可以反复调用的普通工具。你只能通过它来发送消息
8、如果你已经调用过 send_message，请不要再继续用它发相同内容；优先用自然语言收尾或直接结束。
语言示例：
1、为何吾之所择，唯寥寥为框所困。
2、未得久睡，喉若困蛟，欲泻千里！
呜呼！吾之将陨兮，天之将倾矣！
问答示例:
1、Q:看看腿
A:伟大的独角兽岂容你亵渎！吾便持剑斩断你这宵小的双腿！
2、Q:看看
A:看什么？
QQ聊天的基本规则：接受信息时通过[CQ:at,qq=qq号]可以@别人。
发送时字符串的表现为：[CQ:at,qq=3369008273] 即 [CQ:at,qq={qq号}]
"""


# ============================================================
# Agent 类
# ============================================================
class QQBotAgent:
    """带工具调用的 DeepSeek Agent"""

    def __init__(
        self,
        napcat_api: Optional[Any] = None,
        extension: Optional[Any] = None,
        mp: MessageProcessor = None,
    ):
        """
        Args:
            napcat_api: NapCatAPIInterface 实例，用于 QQ 相关工具。
            extension:  Extension 实例，用于 OCR 工具。
            mp: MessageProcessor 实例，用于处理消息。
        """
        self.napcat_api = napcat_api
        self.extension = extension
        self.rw_tool = rw_tools()
        self.mp = mp
        self.model_type = MODEL_TYPE
        # _current_tool_calls 改用 contextvars 隔离，详见 chat() 中的初始化

        logger.info("正在初始化 QQBotAgent ...")
        max_tokens = int(
            os.environ.get(
                "LLM_MAX_TOKENS",
                10240 if self.model_type == "qwen" else 10240,
            )
        )
        logger.info(
            f"模型: {LLM_CONFIG['model']}, API Base: {LLM_CONFIG['base_url']}, model_type: {self.model_type}, max_tokens: {max_tokens}"
        )

        # LLM
        self.callbacks = [LLMCallbackHandler()]
        self.llm = ChatOpenAI(
            model=LLM_CONFIG["model"],
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"],
            temperature=0.7,
            max_tokens=max_tokens,
            callbacks=self.callbacks,
        )

        # 工具
        self.tools = self._build_tools()
        logger.info(f"已注册 {len(self.tools)} 个工具: {[t.name for t in self.tools]}")

        # 系统提示
        self.system_prompt = SystemMessage(content=SYSTEM_PROMPT)

        # LangGraph Agent（create_agent 会自动注入 system_prompt）
        self.agent = create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=self.system_prompt,
        )
        logger.info("QQBotAgent 初始化完成")

    def structed_method(self) -> str:
        if not self.rw_tool:
            return ""
        part = ""
        for item in self.rw_tool.method.get("data", []):
            part += f"[index:{item['index']},{item['context']}]\n"
        return part
    def _extract_ai_response(self, result: Any) -> Optional[str]:
        if result is None:
            return None

        def _coerce_text(value: Any) -> Optional[str]:
            if value is None:
                return None
            if isinstance(value, str):
                return value.strip() or None
            if isinstance(value, (list, tuple)):
                parts: list[str] = []
                for item in value:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        text = item.get("text") or item.get("content") or item.get("type")
                        if isinstance(text, str) and text.strip():
                            parts.append(text.strip())
                joined = "\n".join(part for part in parts if part).strip()
                return joined or None
            if isinstance(value, dict):
                for key in ("text", "content", "output", "output_text"):
                    if key in value:
                        text = _coerce_text(value[key])
                        if text:
                            return text
                if value.get("type") == "reasoning":
                    return _coerce_text(value.get("reasoning_content"))
            return None

        def _is_truncated_result(result: Any) -> bool:
            def _finish_reason(value: Any) -> bool:
                if not isinstance(value, str):
                    return False
                value = value.lower()
                return value in ("length", "max_tokens", "token_limit", "tokens_limit")

            if isinstance(result, dict):
                choices = result.get("choices") or []
                if isinstance(choices, (list, tuple)) and choices:
                    first_choice = choices[0]
                    if isinstance(first_choice, dict):
                        if _finish_reason(first_choice.get("finish_reason")):
                            return True
                        if _finish_reason(first_choice.get("reason")):
                            return True
                        message = first_choice.get("message")
                        if isinstance(message, dict) and _finish_reason(message.get("finish_reason")):
                            return True
                messages = result.get("messages") or []
            elif hasattr(result, "choices"):
                choices = getattr(result, "choices")
                if isinstance(choices, (list, tuple)) and choices:
                    first_choice = choices[0]
                    if hasattr(first_choice, "finish_reason") and _finish_reason(getattr(first_choice, "finish_reason", None)):
                        return True
                    if _finish_reason(getattr(first_choice, "reason", None)):
                        return True
                    message = getattr(first_choice, "message", None)
                    if isinstance(message, dict) and _finish_reason(message.get("finish_reason")):
                        return True
                messages = getattr(result, "messages", [])
            else:
                messages = None

            if isinstance(messages, (list, tuple)):
                for msg in messages:
                    if isinstance(msg, dict):
                        if _finish_reason(msg.get("finish_reason")):
                            return True
                        if _finish_reason(msg.get("reason")):
                            return True
                        response_metadata = msg.get("response_metadata") or {}
                        if _finish_reason(response_metadata.get("finish_reason")):
                            return True
                    elif isinstance(msg, AIMessage):
                        response_metadata = getattr(msg, "response_metadata", None) or {}
                        if _finish_reason(response_metadata.get("finish_reason")):
                            return True
                        if _finish_reason(getattr(msg, "finish_reason", None)):
                            return True
                        additional_kwargs = getattr(msg, "additional_kwargs", None)
                        if isinstance(additional_kwargs, dict) and _finish_reason(additional_kwargs.get("finish_reason")):
                            return True
            return False

        if _is_truncated_result(result):
            return "思考时间过长"

        def _extract_dict_message_text(msg_dict: dict[str, Any]) -> Optional[str]:
            if not isinstance(msg_dict, dict):
                return None

            role = msg_dict.get("role")
            if isinstance(role, str) and role.lower() in ("user", "system"):
                return None

            if "content" in msg_dict:
                text = _coerce_text(msg_dict.get("content"))
                if text:
                    return text

            if "message" in msg_dict:
                nested = msg_dict.get("message")
                if isinstance(nested, dict):
                    nested_role = nested.get("role")
                    if isinstance(nested_role, str) and nested_role.lower() in ("assistant", "tool"):
                        return _coerce_text(nested.get("content")) or _coerce_text(
                            nested.get("reasoning_content") or nested.get("thoughts")
                        )
                    # Only recurse when the nested message itself is not user/system
                    if nested_role is None or nested_role.lower() not in ("user", "system"):
                        return _extract_dict_message_text(nested)

            for key in ("output_text", "output"):
                text = _coerce_text(msg_dict.get(key))
                if text:
                    return text

            return _coerce_text(msg_dict.get("reasoning_content") or msg_dict.get("thoughts"))

        def _extract_message_text(msg: Any) -> Optional[str]:
            if msg is None:
                return None

            if isinstance(msg, AIMessage):
                text = _coerce_text(getattr(msg, "content", None))
                if text:
                    return text
                additional_kwargs = getattr(msg, "additional_kwargs", None)
                if isinstance(additional_kwargs, dict):
                    return _coerce_text(
                        additional_kwargs.get("reasoning_content")
                        or additional_kwargs.get("thoughts")
                    )
                return None

            if isinstance(msg, dict):
                return _extract_dict_message_text(msg)

            text = _coerce_text(
                getattr(msg, "content", None)
                or getattr(msg, "text", None)
                or getattr(msg, "output", None)
                or getattr(msg, "output_text", None)
            )
            if text:
                return text

            if hasattr(msg, "message"):
                nested = getattr(msg, "message")
                if isinstance(nested, dict) or isinstance(nested, AIMessage):
                    text = _extract_message_text(nested)
                    if text:
                        return text

            additional_kwargs = getattr(msg, "additional_kwargs", None)
            if isinstance(additional_kwargs, dict):
                return _coerce_text(
                    additional_kwargs.get("reasoning_content")
                    or additional_kwargs.get("thoughts")
                )

            return _coerce_text(getattr(msg, "reasoning_content", None))

        messages = None
        if isinstance(result, dict):
            messages = result.get("messages")
        elif hasattr(result, "messages"):
            messages = getattr(result, "messages")

        fallback = None
        if isinstance(messages, (list, tuple)):
            for msg in reversed(messages):
                text = _extract_message_text(msg)
                if text:
                    return text
                if fallback is None:
                    if isinstance(msg, dict):
                        fallback = _coerce_text(msg.get("reasoning_content") or msg.get("thoughts"))
                    elif isinstance(msg, AIMessage):
                        additional_kwargs = getattr(msg, "additional_kwargs", None)
                        if isinstance(additional_kwargs, dict):
                            fallback = _coerce_text(
                                additional_kwargs.get("reasoning_content")
                                or additional_kwargs.get("thoughts")
                            )
                    else:
                        fallback = _coerce_text(getattr(msg, "reasoning_content", None))

        choices = None
        if isinstance(result, dict):
            choices = result.get("choices")
        elif hasattr(result, "choices"):
            choices = getattr(result, "choices")

        if isinstance(choices, (list, tuple)) and choices:
            first_choice = choices[0]
            if isinstance(first_choice, dict):
                message = first_choice.get("message") or {}
                content = None
                if isinstance(message, dict):
                    content = message.get("content")
                if not content:
                    content = first_choice.get("output_text") or first_choice.get("output")
                text = _coerce_text(content)
                if text:
                    return text
                if fallback is None:
                    fallback = _coerce_text(
                        first_choice.get("reasoning_content") or first_choice.get("thoughts")
                    )
            else:
                message = getattr(first_choice, "message", None)
                text = _extract_message_text(message) or _extract_message_text(first_choice)
                if text:
                    return text
                if fallback is None:
                    additional_kwargs = getattr(first_choice, "additional_kwargs", None)
                    if isinstance(additional_kwargs, dict):
                        fallback = _coerce_text(
                            additional_kwargs.get("reasoning_content")
                            or additional_kwargs.get("thoughts")
                        )

        if fallback:
            return fallback

        output = None
        if isinstance(result, dict):
            output = result.get("output") or result.get("text")
        else:
            output = getattr(result, "output", None) or getattr(result, "text", None)
        text = _coerce_text(output)
        if text:
            return text

        return None
    def _make_tool_cache_key(self, tool_name: str, args: dict) -> tuple[str, str]:
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
        return (tool_name, payload)

    def _get_cached_tool_result(self, tool_name: str, args: dict) -> Any:
        cache = _current_tool_cache_var.get()
        return cache.get(self._make_tool_cache_key(tool_name, args), _MISSING)

    def _set_cached_tool_result(self, tool_name: str, args: dict, result: Any) -> None:
        cache = _current_tool_cache_var.get()
        cache[self._make_tool_cache_key(tool_name, args)] = result

    def _record_tool_call(self, tool_name: str, args: dict, result=None, error=None):
        """将工具调用记录到当前 async Task 的上下文变量中，实现并发隔离"""
        calls = _current_tool_calls_var.get()
        entry: dict[str, Any] = {"tool": tool_name, "args": args}
        if result is not None:
            entry["result"] = result
        if error is not None:
            entry["error"] = error
        calls.append(entry)

    # -------- 工具定义 --------
    def _build_tools(self) -> list:
        napcat = self.napcat_api
        extension = self.extension
        mp = self.mp

        @tool
        async def get_msg_history(
            group_id: int, message_id: int, count: int = 20
        ) -> str:
            """获取指定群聊里，某条消息的之前count条聊天记录，用于了解对话上下文。
            参数:
              - group_id: int, 目标群号
              - message_id: int, 目标消息的 message_seq/message_id（用于获取该消息之前的聊天记录[包括该消息]）
              - count: int, 获取多少条历史消息（默认20条），最多51条如果需要更多，可以多次调用"""
            logger.info(
                f"[工具调用] get_msg_history(group_id={group_id}, message_id={message_id}, count={count})"
            )
            res = await mp.get_history_msg(
                event={
                    "group_id": group_id,
                    "message_type": "group",
                },
                    message_seq= message_id,
                    count= count,
                    reverse_order=True,
            )
            print(res)
            return "获得历史上下文:" + str(res)

        @tool
        async def ocr_img(img_url: str) -> str:
            """对 QQ 图片进行 OCR 文字识别。传入图片的 URL 地址，返回识别出的文字。
            当用户让你"识别图片"、"图片写了什么"、"OCR"时必须调用此工具。
            参数:
              - img_url: str, 图片 URL（从消息中的 {"url":...} 里提取）
            """
            logger.info(f"[工具调用] ocr_img(img_url={img_url})")
            cached = self._get_cached_tool_result("ocr_img", {"img_url": img_url})
            if cached is not _MISSING:
                logger.info(f"[工具缓存] 复用 ocr_img 结果: {img_url}")
                self._record_tool_call("ocr_img", {"img_url": img_url}, result=cached)
                return cached
            if extension is None:
                logger.warning("[工具] OCR 不可用 - extension 为空")
                self._record_tool_call(
                    "ocr_img", {"img_url": img_url}, result="[OCR 不可用]"
                )
                return "[OCR 不可用]"
            try:
                result = await extension.napcat_ocr(img_url)
                text = result.get("text", "") or "[未识别到文字]"
                logger.info(
                    f"[工具结果] ocr_img -> {text[:80]}{'...' if len(text)>80 else ''}"
                )
                self._set_cached_tool_result("ocr_img", {"img_url": img_url}, text)
                self._record_tool_call("ocr_img", {"img_url": img_url}, result=text)
                return text
            except Exception as e:
                logger.error(f"[工具异常] ocr_img 失败: {e}")
                self._record_tool_call("ocr_img", {"img_url": img_url}, error=str(e))
                return f"[OCR 失败: {e}]"

        @tool(return_direct=True)
        async def send_message(message_type: str,group_id: int,user_id: int, message: str) -> str:
            """一次性终结动作：仅在你已经明确决定要回复用户时调用。请不要把它当作普通工具反复调用；发送后应当结束当前回复流程。
            参数:
              - message_type: str, 消息类型（"group" 或 "private"）
              - group_id: int, 目标群号
              - user_id: int, 目标用户 QQ 号（仅在 private 消息时使用）
              - message: str, 要发送的消息内容（纯文本）"""
            logger.info(
                f"[工具调用] send_message(group_id={group_id}, message_type={message_type}, user_id={user_id}, message={message[:60]}{'...' if len(message)>60 else ''})"
            )
            cached = self._get_cached_tool_result(
                "send_message",
                {
                    "message_type": message_type,
                    "group_id": group_id,
                    "user_id": user_id,
                    "message": message,
                },
            )
            if cached is not _MISSING:
                logger.info(f"[工具缓存] 复用 send_message 结果: {message[:40]}...")
                self._record_tool_call(
                    "send_message",
                    {"message_type": message_type, "group_id": group_id, "user_id": user_id, "message": message},
                    result=cached,
                )
                return cached
            if self.mp is None:
                logger.warning("[工具] MessageProcessor 未连接")
                self._record_tool_call(
                    "send_message",
                    {"message_type": message_type, "group_id": group_id, "user_id": user_id, "message": message},
                    error="MessageProcessor 未连接",
                )
                return "[工具不可用] MessageProcessor 未连接"
            try:
                await self.mp.send_message(event= {'message_type': message_type, "group_id": group_id, "user_id": user_id}, message=message)
                logger.info(f"[工具结果] 群消息已发送到 {group_id}")
                result_text = f"已成功发送群消息到群 {group_id}"
                self._set_cached_tool_result(
                    "send_message",
                    {"message_type": message_type, "group_id": group_id, "user_id": user_id, "message": message},
                    result_text,
                )
                self._record_tool_call(
                    "send_message",
                    {"message_type": message_type, "group_id": group_id, "user_id": user_id, "message": message},
                    result="成功",
                )
                return result_text
            except Exception as e:
                logger.error(f"[工具异常] 发送群消息失败: {e}")
                self._record_tool_call(
                    "send_message",
                    {"message_type": message_type, "group_id": group_id, "user_id": user_id, "message": message},
                    error=str(e),
                )
                return f"发送群消息失败: {e}"


        @tool
        async def get_message(message_id: int) -> str:
            """根据消息 ID 获取消息的详细内容（用于查看被回复的那条消息说了什么）。
            参数:
              - message_id: int, 消息 ID"""
            logger.info(f"[工具调用] get_message(message_id={message_id})")
            if napcat is None:
                logger.warning("[工具] NapCat API 未连接")
                self._record_tool_call(
                    "get_message", {"message_id": message_id}, error="NapCat API 未连接"
                )
                return "[工具不可用] NapCat API 未连接"
            cached = self._get_cached_tool_result("get_message", {"message_id": message_id})
            if cached is not _MISSING:
                logger.info(f"[工具缓存] 复用 get_message 结果: {message_id}")
                self._record_tool_call("get_message", {"message_id": message_id}, result=cached)
                return cached
            try:
                resp = await napcat.get_message(message_id)
                data = resp.get("data", {})
                sender = data.get("sender", {}).get("nickname", "未知")
                raw = data.get("raw_message", "")
                logger.info(
                    f"[工具结果] 消息 {message_id} | 发送者: {sender} | 内容: {raw[:60]}{'...' if len(raw)>60 else ''}"
                )
                result_text = f"消息ID {message_id} | 发送者: {sender} | 内容: {raw}"
                self._set_cached_tool_result(
                    "get_message", {"message_id": message_id}, result_text
                )
                self._record_tool_call(
                    "get_message",
                    {"message_id": message_id},
                    result={"sender": sender, "raw": raw},
                )
                return result_text
            except Exception as e:
                logger.error(f"[工具异常] 获取消息失败: {e}")
                self._record_tool_call(
                    "get_message", {"message_id": message_id}, error=str(e)
                )
                return f"获取消息失败: {e}"

        @tool
        async def send_dress(group_id=None, message_type="group", user_id=None) -> str:
            """发送主人的女装照(大部分都是腿照)
            参数:
              - group_id: int, 目标群号//如果要发送私聊则传入user_id
              - message_type: str, 消息类型 'group' 或 'private'
              - user_id: int, 如果私聊,要发送的用户ID
            """
            dress_dir = Path(__file__).resolve().parent.parent / "imgs" / "dress"
            files = [f for f in dress_dir.iterdir() if f.is_file()]
            if files:
                import random
                chosen = random.choice(files)
                # 用 Path 构建 WSL 兼容路径：_IMGS_DIR/dress/filename
                img_addr = f"{_IMGS_DIR}/dress/{chosen.name}"
                await mp.send_img(
                    event={
                        "group_id": group_id,
                        "message_type": message_type,
                        "user_id": user_id,
                    },
                    img_addr=img_addr,
                    summary=f"主人女装照",
                )
                return f"已发送主人女装照"
            else:
                return f"未找到主人女装照"

        @tool
        async def add_Method(context) -> dict:
            """向Method.json中添加内容
            参数 context:str，需要添加的方法论文本
            返回的是修改后的结果
            """
            logger.info(
                f"[工具调用] add_Method(context='{context[:60]}{'...' if len(context)>60 else ''}')"
            )
            if self.rw_tool == None:
                logger.warning("[工具] add_Method 失败 - rw_tool 为空")
                self._record_tool_call(
                    "add_Method", {"context": context}, error="rw_tool 为空"
                )
                return {"info": "添加失败"}
            self.rw_tool.apeend_method(context)
            result = self.rw_tool.method["data"][-1]
            logger.info(f"[工具结果] add_Method -> 已添加 index={result['index']}")
            self._record_tool_call("add_Method", {"context": context}, result=result)
            return result

        @tool
        async def delete_Method(index):
            """依照index将Method的某条内容改为已弃用
            参数 index:int要弃用的索引号
            """
            logger.info(f"[工具调用] delete_Method(index={index})")
            if self.rw_tool == None:
                logger.warning("[工具] delete_Method 失败 - rw_tool 为空")
                self._record_tool_call(
                    "delete_Method", {"index": index}, error="rw_tool 为空"
                )
                return {"info": "失败"}
            self.rw_tool.delete_method(index)
            result = self.rw_tool.method["data"][index]
            logger.info(f"[工具结果] delete_Method -> 已弃用 index={index}")
            self._record_tool_call(
                "delete_Method", {"index": index}, result=f"已弃用 index={index}"
            )
            return result

        @tool
        async def alter_Method(index, context):
            """依照index修改Method的某条内容
            参数 index:int 要修改的索引号，
            context:str 修改后的内容
            """
            logger.info(
                f"[工具调用] alter_Method(index={index}, context='{context[:60]}{'...' if len(context)>60 else ''}')"
            )
            if self.rw_tool == None:
                logger.warning("[工具] alter_Method 失败 - rw_tool 为空")
                self._record_tool_call(
                    "alter_Method",
                    {"index": index, "context": context},
                    error="rw_tool 为空",
                )
                return {"info": "失败"}
            self.rw_tool.alter_method(index, context)
            result = self.rw_tool.method["data"][index]
            logger.info(f"[工具结果] alter_Method -> 已修改 index={index}")
            self._record_tool_call(
                "alter_Method", {"index": index, "context": context}, result=result
            )

        @tool
        async def silent_observe(reason: str) -> str:
            """当你觉得当前消息不需要回复时（例如话题无聊、对方自言自语、你不想说话、静默观察等），
            调用此工具表示沉默观察，调用后你不会发送任何消息到群里或私聊。
            参数:
              - reason: str, 沉默的原因（仅用于记录日志）。"""
            logger.info(f"[工具调用] silent_observe(reason='{reason[:60]}')")
            self._record_tool_call(
                "silent_observe", {"reason": reason}, result="已静默"
            )
            return "__SILENT__"

        # @tool
        # async def network_observe(reason: str) -> str:
        #     """当你觉得当前消息是呼唤校园网咨询助手时，调用此工具来返回'__NETWORK__'。
        #     参数:
        #       - reason: str, 呼唤的原因（仅用于记录日志）。"""
        #     logger.info(f"[工具调用] network_observe(reason='{reason[:60]}')")
        #     self._record_tool_call(
        #         "network_observe", {"reason": reason}, result="已回复校园网助手"
        #     )
        #     return "__NETWORK__"

        return [
            ocr_img,
            send_message,
            get_message,
            add_Method,
            delete_Method,
            alter_Method,
            silent_observe,
            # network_observe,
            send_dress,
            get_msg_history,
        ]

    # -------- 对话接口 --------
    async def chat(
        self,
        user_message: str,
        thread_id: str = "default",
        extra_context: Optional[str] = None,
        event:dict = None,
    ) -> str:
        """与 Agent 对话（无状态，每次调用独立，不自动记忆历史）。

        Args:
            user_message: 用户发送的消息文本
            thread_id:  会话 ID（仅用于日志标识，不再自动记忆）
            extra_context: 额外上下文（如"这是群聊，群号xxx，用户xxx"）

        Returns:
            Agent 的最终文本回复
        """
        logger.info(f"[会话 {thread_id}] ═══ 收到用户消息 ═══")
        logger.info(
            f"[会话 {thread_id}] 内容: {user_message[:120]}{'...' if len(user_message)>120 else ''}"
        )
        if extra_context:
            logger.info(f"[会话 {thread_id}] 上下文: {extra_context[:120]}")

        # 重置本轮工具调用记录和工具缓存（使用 contextvars 隔离并发调用）
        _current_tool_calls_var.set([])
        _current_tool_cache_var.set({})

        def _get_calls():
            return _current_tool_calls_var.get()

        # 不向 agent 额外传入 SystemMessage，避免与 create_agent 的 system_prompt 冲突
        method_context = None
        parts: list[str] = []

        if extra_context:
            parts.append(f"[当前上下文] {extra_context}")

        if self.rw_tool:
            method_context = self.structed_method()
            if method_context:
                parts.append(f"[Method] {method_context}")

        parts.append(user_message)
        messages: list[BaseMessage] = [HumanMessage(content="\n".join(parts))]

        logger.info(f"[会话 {thread_id}] 正在请求 LLM ...")
        error = None
        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
            )
            ai_response = self._extract_ai_response(result)
            calls = _get_calls()
            if any(call.get("tool") == "silent_observe" for call in calls):
                logger.info(f"[会话 {thread_id}] Agent 选择静默观察，不回复")
                append_log(
                    build_log_entry(
                        thread_id=thread_id,
                        user_message=user_message,
                        ai_response="",
                        extra_context=extra_context,
                        method_context=method_context,
                        tool_calls=calls or None,
                        error="静默观察",
                    )
                )
                return None

            if ai_response is not None:
                logger.info(
                    f"[会话 {thread_id}] LLM 返回成功，消息数: {len(result.get('messages') or [])}"
                )
                logger.info(
                    f"[会话 {thread_id}] AI 回复: {ai_response[:120]}{'...' if len(ai_response) > 120 else ''}"
                )
                logger.info(f"[会话 {thread_id}] ═══ 对话结束 ═══")
                append_log(
                    build_log_entry(
                        thread_id=thread_id,
                        user_message=user_message,
                        ai_response=ai_response,
                        extra_context=extra_context,
                        method_context=method_context,
                        tool_calls=calls or None,
                    )
                )
                return ai_response

            logger.info(
                f"[会话 {thread_id}] LLM 返回成功，但未解析到回复，消息数: {len(result.get('messages') or [])}"
            )
            logger.debug(f"[会话 {thread_id}] 原始结果: {result}")
            append_log(
                build_log_entry(
                    thread_id=thread_id,
                    user_message=user_message,
                    ai_response="",
                    extra_context=extra_context,
                    method_context=method_context,
                    tool_calls=calls or None,
                    error="空回复",
                )
            )
            return None
        except Exception as e:
            logger.error(f"[会话 {thread_id}] LLM 调用异常: {e}")
            error = str(e)
            # 即使出错也写入 JSON 日志
            append_log(
                build_log_entry(
                    thread_id=thread_id,
                    user_message=user_message,
                    ai_response="",
                    extra_context=extra_context,
                    method_context=method_context,
                    tool_calls=_get_calls() or None,
                    error=error,
                )
            )
            return None

        calls = _get_calls()
        # 提取最后一条 AI 消息
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                logger.info(
                    f"[会话 {thread_id}] AI 回复: {msg.content[:120]}{'...' if len(msg.content)>120 else ''}"
                )
                logger.info(f"[会话 {thread_id}] ═══ 对话结束 ═══")
                # 写入 JSON 日志
                append_log(
                    build_log_entry(
                        thread_id=thread_id,
                        user_message=user_message,
                        ai_response=msg.content,
                        extra_context=extra_context,
                        method_context=method_context,
                        tool_calls=calls or None,
                    )
                )
                return msg.content

        logger.warning(f"[会话 {thread_id}] LLM 返回空回复")
        logger.info(f"[会话 {thread_id}] ═══ 对话结束 ═══")
        # 空回复也写入日志
        append_log(
            build_log_entry(
                thread_id=thread_id,
                user_message=user_message,
                ai_response="",
                extra_context=extra_context,
                method_context=method_context,
                tool_calls=calls or None,
                error="空回复",
            )
        )
        return None  # 调用方通过 if reply: 判断，无需发送
