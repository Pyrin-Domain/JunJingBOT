"""QQBot LLM Agent - 基于 LangGraph + DeepSeek，带工具调用"""

import os
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

# 按 async Task 隔离的工具调用记录，防止并发 chat() 互相污染
_current_tool_calls_var: contextvars.ContextVar[list[dict]] = contextvars.ContextVar('current_tool_calls', default=[])

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
        logger.debug(f"[LLM] 收到回复: {content[:100]}{'...' if len(content)>100 else ''}")

    def on_llm_error(self, error, **kwargs):
        logger.error(f"[LLM] 请求出错: {error}")

    def on_tool_start(self, serialized, input_str, **kwargs):
        logger.debug(f"[LLM] 决定调用工具: {serialized.get('name', 'unknown')}({input_str[:80]})")

    def on_tool_end(self, output, **kwargs):
        logger.debug(f"[LLM] 工具返回: {str(output)[:80]}{'...' if len(str(output))>80 else ''}")



# ============================================================
# 配置
# ============================================================
LLM_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
    "model": "deepseek-v4-flash",  # DeepSeek-V4-flash
}

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

    def __init__(self, napcat_api: Optional[Any] = None, extension: Optional[Any] = None):
        """
        Args:
            napcat_api: NapCatAPIInterface 实例，用于 QQ 相关工具。
            extension:  Extension 实例，用于 OCR 工具。
        """
        self.napcat_api = napcat_api
        self.extension = extension
        self.rw_tool = rw_tools()
        # _current_tool_calls 改用 contextvars 隔离，详见 chat() 中的初始化

        logger.info("正在初始化 QQBotAgent ...")
        logger.info(f"模型: {LLM_CONFIG['model']}, API Base: {LLM_CONFIG['base_url']}")

        # LLM
        self.callbacks = [LLMCallbackHandler()]
        self.llm = ChatOpenAI(
            model=LLM_CONFIG["model"],
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"],
            temperature=0.7,
            max_tokens=1024,
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
        if not self.rw_tool :
            return ''
        part = ''
        for item in self.rw_tool.method.get('data',[]):
            part += f'[index:{item['index']},{item['context']}]\n'
        return part
    
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

        @tool
        async def ocr_img(img_url: str) -> str:
            """对 QQ 图片进行 OCR 文字识别。传入图片的 URL 地址，返回识别出的文字。
当用户让你"识别图片"、"图片写了什么"、"OCR"时必须调用此工具。
参数:
  - img_url: str, 图片 URL（从消息中的 {"url":...} 里提取）"""
            logger.info(f"[工具调用] ocr_img(img_url={img_url})")
            if extension is None:
                logger.warning("[工具] OCR 不可用 - extension 为空")
                self._record_tool_call("ocr_img", {"img_url": img_url}, result="[OCR 不可用]")
                return "[OCR 不可用]"
            try:
                result = await extension.napcat_ocr(img_url)
                text = result.get("text", "") or "[未识别到文字]"
                logger.info(f"[工具结果] ocr_img -> {text[:80]}{'...' if len(text)>80 else ''}")
                self._record_tool_call("ocr_img", {"img_url": img_url}, result=text)
                return text
            except Exception as e:
                logger.error(f"[工具异常] ocr_img 失败: {e}")
                self._record_tool_call("ocr_img", {"img_url": img_url}, error=str(e))
                return f"[OCR 失败: {e}]"

        @tool
        async def send_group_message(group_id: int, message: str) -> str:
            """发送一条消息到指定 QQ 群。
参数:
  - group_id: int, 目标群号
  - message: str, 要发送的消息内容（纯文本）"""
            logger.info(f"[工具调用] send_group_message(group_id={group_id}, message={message[:60]}{'...' if len(message)>60 else ''})")
            if napcat is None:
                logger.warning("[工具] NapCat API 未连接")
                self._record_tool_call("send_group_message", {"group_id": group_id, "message": message}, error="NapCat API 未连接")
                return "[工具不可用] NapCat API 未连接"
            try:
                await napcat.send_group_message(group_id, message)
                logger.info(f"[工具结果] 群消息已发送到 {group_id}")
                self._record_tool_call("send_group_message", {"group_id": group_id, "message": message}, result="成功")
                return f"已成功发送群消息到群 {group_id}"
            except Exception as e:
                logger.error(f"[工具异常] 发送群消息失败: {e}")
                self._record_tool_call("send_group_message", {"group_id": group_id, "message": message}, error=str(e))
                return f"发送群消息失败: {e}"

        @tool
        async def send_private_message(user_id: int, message: str) -> str:
            """发送一条私聊消息给指定 QQ 用户。
参数:
  - user_id: int, 目标用户 QQ 号
  - message: str, 要发送的消息内容（纯文本）"""
            logger.info(f"[工具调用] send_private_message(user_id={user_id}, message={message[:60]}{'...' if len(message)>60 else ''})")
            if napcat is None:
                logger.warning("[工具] NapCat API 未连接")
                self._record_tool_call("send_private_message", {"user_id": user_id, "message": message}, error="NapCat API 未连接")
                return "[工具不可用] NapCat API 未连接"
            try:
                await napcat.send_private_message(user_id, message)
                logger.info(f"[工具结果] 私聊消息已发送给 {user_id}")
                self._record_tool_call("send_private_message", {"user_id": user_id, "message": message}, result="成功")
                return f"已成功发送私聊消息给用户 {user_id}"
            except Exception as e:
                logger.error(f"[工具异常] 发送私聊消息失败: {e}")
                self._record_tool_call("send_private_message", {"user_id": user_id, "message": message}, error=str(e))
                return f"发送私聊消息失败: {e}"

        @tool
        async def get_message(message_id: int) -> str:
            """根据消息 ID 获取消息的详细内容（用于查看被回复的那条消息说了什么）。
参数:
  - message_id: int, 消息 ID"""
            logger.info(f"[工具调用] get_message(message_id={message_id})")
            if napcat is None:
                logger.warning("[工具] NapCat API 未连接")
                self._record_tool_call("get_message", {"message_id": message_id}, error="NapCat API 未连接")
                return "[工具不可用] NapCat API 未连接"
            try:
                resp = await napcat.get_message(message_id)
                data = resp.get("data", {})
                sender = data.get("sender", {}).get("nickname", "未知")
                raw = data.get("raw_message", "")
                logger.info(f"[工具结果] 消息 {message_id} | 发送者: {sender} | 内容: {raw[:60]}{'...' if len(raw)>60 else ''}")
                self._record_tool_call("get_message", {"message_id": message_id}, result={"sender": sender, "raw": raw})
                return f"消息ID {message_id} | 发送者: {sender} | 内容: {raw}"
            except Exception as e:
                logger.error(f"[工具异常] 获取消息失败: {e}")
                self._record_tool_call("get_message", {"message_id": message_id}, error=str(e))
                return f"获取消息失败: {e}"
        @tool
        async def add_Method(context)->dict:
            """向Method.json中添加内容
            参数 context:str，需要添加的方法论文本
            返回的是修改后的结果
            """
            logger.info(f"[工具调用] add_Method(context='{context[:60]}{'...' if len(context)>60 else ''}')")
            if self.rw_tool == None:
                logger.warning("[工具] add_Method 失败 - rw_tool 为空")
                self._record_tool_call("add_Method", {"context": context}, error="rw_tool 为空")
                return {"info":"添加失败"}
            self.rw_tool.apeend_method(context)
            result = self.rw_tool.method['data'][-1]
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
                self._record_tool_call("delete_Method", {"index": index}, error="rw_tool 为空")
                return {"info":"失败"}
            self.rw_tool.delete_method(index)
            result = self.rw_tool.method['data'][index]
            logger.info(f"[工具结果] delete_Method -> 已弃用 index={index}")
            self._record_tool_call("delete_Method", {"index": index}, result=f"已弃用 index={index}")
            return result            
        @tool
        async def alter_Method(index,context):
            """依照index修改Method的某条内容
            参数 index:int 要修改的索引号，
            context:str 修改后的内容
            """
            logger.info(f"[工具调用] alter_Method(index={index}, context='{context[:60]}{'...' if len(context)>60 else ''}')")
            if self.rw_tool == None:
                logger.warning("[工具] alter_Method 失败 - rw_tool 为空")
                self._record_tool_call("alter_Method", {"index": index, "context": context}, error="rw_tool 为空")
                return {"info":"失败"}
            self.rw_tool.alter_method(index,context)
            result = self.rw_tool.method['data'][index]
            logger.info(f"[工具结果] alter_Method -> 已修改 index={index}")
            self._record_tool_call("alter_Method", {"index": index, "context": context}, result=result)

        @tool
        async def silent_observe(reason: str) -> str:
            """当你觉得当前消息不需要回复时（例如话题无聊、对方自言自语、你不想说话、静默观察等），
            调用此工具表示沉默观察，调用后你不会发送任何消息到群里或私聊。
            参数:
              - reason: str, 沉默的原因（仅用于记录日志）。"""
            logger.info(f"[工具调用] silent_observe(reason='{reason[:60]}')")
            self._record_tool_call("silent_observe", {"reason": reason}, result="已静默")
            return "__SILENT__"

        return [ocr_img, send_group_message, send_private_message, get_message, add_Method, delete_Method, alter_Method, silent_observe]

        

    # -------- 对话接口 --------
    async def chat(
        self,
        user_message: str,
        thread_id: str = "default",
        extra_context: Optional[str] = None,
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
        logger.info(f"[会话 {thread_id}] 内容: {user_message[:120]}{'...' if len(user_message)>120 else ''}")
        if extra_context:
            logger.info(f"[会话 {thread_id}] 上下文: {extra_context[:120]}")

        # 重置本轮工具调用记录（使用 contextvars 隔离并发调用）
        _current_tool_calls_var.set([])
        def _get_calls():
            return _current_tool_calls_var.get()

        # 构建消息列表（system_prompt 由 create_agent 自动注入，无需手动添加）
        messages: list[BaseMessage] = []

        if extra_context:
            messages.append(SystemMessage(
                content=f"[当前上下文] {extra_context}"
            ))

        method_context = None
        if self.rw_tool:
                method_context = self.structed_method()
                messages.append(SystemMessage(
                content=f"[Method] {method_context}"
            ))

        messages.append(HumanMessage(content=user_message))

        logger.info(f"[会话 {thread_id}] 正在请求 LLM ...")
        error = None
        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
            )
            logger.info(f"[会话 {thread_id}] LLM 返回成功，消息数: {len(result['messages'])}")

            calls = _get_calls()
            # 检测是否调用了 silent_observe（静默观察，不发送消息）
            if any(call.get("tool") == "silent_observe" for call in calls):
                logger.info(f"[会话 {thread_id}] Agent 选择静默观察，不回复")
                append_log(build_log_entry(
                    thread_id=thread_id,
                    user_message=user_message,
                    ai_response="",
                    extra_context=extra_context,
                    method_context=method_context,
                    tool_calls=calls or None,
                    error="静默观察",
                ))
                return None
        except Exception as e:
            logger.error(f"[会话 {thread_id}] LLM 调用异常: {e}")
            error = str(e)
            # 即使出错也写入 JSON 日志
            append_log(build_log_entry(
                thread_id=thread_id,
                user_message=user_message,
                ai_response="",
                extra_context=extra_context,
                method_context=method_context,
                tool_calls=_get_calls() or None,
                error=error,
            ))
            return None

        calls = _get_calls()
        # 提取最后一条 AI 消息
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                logger.info(f"[会话 {thread_id}] AI 回复: {msg.content[:120]}{'...' if len(msg.content)>120 else ''}")
                logger.info(f"[会话 {thread_id}] ═══ 对话结束 ═══")
                # 写入 JSON 日志
                append_log(build_log_entry(
                    thread_id=thread_id,
                    user_message=user_message,
                    ai_response=msg.content,
                    extra_context=extra_context,
                    method_context=method_context,
                    tool_calls=calls or None,
                ))
                return msg.content

        logger.warning(f"[会话 {thread_id}] LLM 返回空回复")
        logger.info(f"[会话 {thread_id}] ═══ 对话结束 ═══")
        # 空回复也写入日志
        append_log(build_log_entry(
            thread_id=thread_id,
            user_message=user_message,
            ai_response="",
            extra_context=extra_context,
            method_context=method_context,
            tool_calls=calls or None,
            error="空回复",
        ))
        return None  # 调用方通过 if reply: 判断，无需发送
