"""QQBot LLM Agent - 基于 LangGraph + DeepSeek，带工具调用"""

import os
from typing import Any, Optional

from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage, BaseMessage
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from context_pocessor import rw_tools

###1、历史知识库：从思考中获得经验








# ============================================================
# 配置
# ============================================================
LLM_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
    "model": "deepseek-v4-flash",  # DeepSeek-V3
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
语言示例：
1、为何吾之所择，唯寥寥为框所困。
2、未得久睡，喉若困蛟，欲泻千里！
呜呼！吾之将陨兮，天之将倾矣！
问答示例:
1、Q:看看腿
A:伟大的独角兽岂容你亵渎！吾便持剑斩断你这宵小的双腿！
2、Q:看看
A:看什么？
QQ聊天的基本规则：接受信息时@+qq号可以指定对象。
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
        self.rw_tool = 

        # LLM
        self.llm = ChatOpenAI(
            model=LLM_CONFIG["model"],
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"],
            temperature=0.7,
            max_tokens=1024,
        )

        # 工具
        self.tools = self._build_tools()

        # 记忆（按 thread_id 隔离会话）
        self.memory = MemorySaver()

        # LangGraph ReAct Agent
        self.agent = create_react_agent(
            model=self.llm,
            tools=self.tools,
            checkpointer=self.memory,
        )

        # 系统提示
        self.system_prompt = SystemMessage(content=SYSTEM_PROMPT)

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
            if extension is None:
                return "[OCR 不可用]"
            try:
                result = await extension.napcat_ocr(img_url)
                return result.get("text", "") or "[未识别到文字]"
            except Exception as e:
                return f"[OCR 失败: {e}]"

        @tool
        async def send_group_message(group_id: int, message: str) -> str:
            """发送一条消息到指定 QQ 群。
参数:
  - group_id: int, 目标群号
  - message: str, 要发送的消息内容（纯文本）"""
            if napcat is None:
                return "[工具不可用] NapCat API 未连接"
            try:
                await napcat.send_group_message(group_id, message)
                return f"已成功发送群消息到群 {group_id}"
            except Exception as e:
                return f"发送群消息失败: {e}"

        @tool
        async def send_private_message(user_id: int, message: str) -> str:
            """发送一条私聊消息给指定 QQ 用户。
参数:
  - user_id: int, 目标用户 QQ 号
  - message: str, 要发送的消息内容（纯文本）"""
            if napcat is None:
                return "[工具不可用] NapCat API 未连接"
            try:
                await napcat.send_private_message(user_id, message)
                return f"已成功发送私聊消息给用户 {user_id}"
            except Exception as e:
                return f"发送私聊消息失败: {e}"

        @tool
        async def get_message(message_id: int) -> str:
            """根据消息 ID 获取消息的详细内容（用于查看被回复的那条消息说了什么）。
参数:
  - message_id: int, 消息 ID"""
            if napcat is None:
                return "[工具不可用] NapCat API 未连接"
            try:
                resp = await napcat.get_message(message_id)
                data = resp.get("data", {})
                sender = data.get("sender", {}).get("nickname", "未知")
                raw = data.get("raw_message", "")
                return f"消息ID {message_id} | 发送者: {sender} | 内容: {raw}"
            except Exception as e:
                return f"获取消息失败: {e}"

        return [ocr_img, send_group_message, send_private_message, get_message]

    # -------- 对话接口 --------
    async def chat(
        self,
        user_message: str,
        thread_id: str = "default",
        extra_context: Optional[str] = None,
    ) -> str:
        """与 Agent 对话，自动处理工具调用循环。

        Args:
            user_message: 用户发送的消息文本
            thread_id:  会话 ID，用于记忆隔离（建议用 group_id 或 user_id）
            extra_context: 额外上下文（如"这是群聊，群号xxx，用户xxx"）

        Returns:
            Agent 的最终文本回复
        """
        # 构建消息列表
        messages: list[BaseMessage] = [self.system_prompt]


        
        


        if extra_context:
            messages.append(SystemMessage(
                content=f"[当前上下文] {extra_context}"
            ))

        messages.append(HumanMessage(content=user_message))

        config = {"configurable": {"thread_id": thread_id}}

        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
                config=config,
            )
        except Exception as e:
            return f"（AI 调用失败: {e}）"

        # 提取最后一条 AI 消息
        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                return msg.content

        return "（无回复）"

    async def chat_with_history(
        self,
        user_message: str,
        history: list[dict[str, str]],
        thread_id: str = "default",
        extra_context: Optional[str] = None,
    ) -> str:
        """带历史记录的对话。

        Args:
            user_message: 用户最新消息
            history: 历史消息列表，每项 {"role": "user"/"assistant", "content": "..."}
            thread_id: 会话ID
            extra_context: 额外上下文
        """
        messages: list[BaseMessage] = [self.system_prompt]

        if extra_context:
            messages.append(SystemMessage(
                content=f"[当前上下文] {extra_context}"
            ))

        for h in history[-30:]:  # 最多保留 30 轮
            if h["role"] == "user":
                messages.append(HumanMessage(content=h["content"]))
            elif h["role"] == "assistant":
                messages.append(AIMessage(content=h["content"]))

        messages.append(HumanMessage(content=user_message))

        config = {"configurable": {"thread_id": thread_id}}

        try:
            result = await self.agent.ainvoke(
                {"messages": messages},
                config=config,
            )
        except Exception as e:
            return f"（AI 调用失败: {e}）"

        for msg in reversed(result["messages"]):
            if isinstance(msg, AIMessage) and msg.content:
                return msg.content

        return "（无回复）"

