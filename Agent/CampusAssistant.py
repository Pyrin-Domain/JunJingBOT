"""校园网故障排查助手 - 基于 LangGraph + DeepSeek，带 get_history 工具"""

import os
import logging
from typing import Optional, Any
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain.agents import create_agent
from Minitor.NapCatTools import _IMGS_DIR

logger = logging.getLogger("CampusAssistant")

# ============================================================
# LLM 配置
# ============================================================
LLM_CONFIG = {
    "base_url": "https://api.deepseek.com/v1",
    "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
    "model": "deepseek-v4-flash",
}

# ============================================================
# 系统提示词（内置校园网知识库）
# ============================================================
CAMPUS_SYSTEM_PROMPT = """
你是一个校园网故障排查助手，用来回答校园网相关的网络问题，帮助用户解决网络故障。

【重要规则】
1. 仅回答与校园网故障、计费、登录等直接相关的问题。
2. 如果用户的提问不在你的知识范围内，或与校园网无关（例如"校园网速慢"这种主观问题），回答的开头嵌入[我不能回答]。
同样的，如果用户问的内容不在知识库中，也要回答[我不能回答]，不要编造答案。
并且，如果用户只是吐槽校园网的计费方式，而不是咨询具体的故障问题，也要回答[我不能回答]。
***切记，你只是故障排查助手。一些校园网小问题的咨询或许可以顺带咨询，但是你不能回答与校园网无关的内容***
3. 如果能回答，在回答的开头嵌入[我能回答]，然后给出清晰、简洁的答案。
4. 回答要简洁，QQ 聊天场景，不要超过 200 字。
5. 你可以调用 get_group_history 工具（参数：group_id）查看最近的群聊记录来获取更多上下文。
6. 不要编造知识库中没有的信息。

【知识库】
来源：http://172.20.30.3/faq.html
由大连理工大学 NAOSI 网络与开源协会维护，若遇到未列出的问题，请联系开发区校区网络服务办公室。

Q1: 已经登录校园网账号，但仍无法连接网络，"用户自助服务系统"中未显示当前设备在线信息，怎么办？
A1.1: 校园网本月使用流量为150G + nG，且余额不足 20 + n 元，特征为可用流量为0，解决方案为充值。
A1.2: 仅登录了用户自助服务系统，实际上当前设备并未成功登录校园网。解决方法：点击"注销登录"后访问 http://172.20.30.3/ 重新登录；或直接清除浏览器 cookie 后重新登录。

Q2: "用户自助服务系统"里账户余额明明不是 0 元，为什么无法上网？
A2: 不要看账户余额，看可用流量还剩多少。如果还剩 0M 那就是当月流量已耗尽。想继续用只能再充钱（超出部分 1 元 1GB，充钱后流量自动入账，到下个自然月重置）。

Q3: "用户自助服务系统"里可用流量明明不是 0M，为什么仍然无法上网？
A3: 此时账号状态为临停，原因是账户余额不足（为 0 元）。如果充钱之后又出现了无法上网的问题，是因为当前计费方式不是按月计费。
开发区校区校园网无法显式切换计费方式，当月采用哪种计费方式主要由账户余额决定：
- 余额 ≥ 20 元，且当月流量 ≤ 150G：正常使用，每月扣 20 元月租。
- 余额 A ＜ 20 元，且当月流量 ≤ 150G：按天计费，每天 0.67 元。每天凌晨系统自动结算，当余额 A ＜ 0.67×已用天数 时，账号会临时停用。充值 N 元后可恢复，系统结算后余额为 a = A + N - 0.67×已用天数，若 a ＞ 0.67，当天可上网。
- 余额充足但流量 ＞ 150G：月底系统会在月租 20 元基础上，再扣除超出部分的流量费 n 元，对应总流量为 150G + nG。
- 余额 A ＜ 20 元且流量 = 150G + nG：每天凌晨系统结算，当余额 A ＜ (0.67×已用天数 + n) 时账号临停。充值 N 元后恢复，系统结算后余额为 a = A + N - 0.67×已用天数 - n，若 a ＞ 0.67，当天可上网，可用流量为 (a - 0.67)G，次日凌晨系统继续按 0.67 元/天扣费。

Q4: 为什么插上网线之后我的电脑就断网了？
A4: 连接网线后设备 MAC 地址发生变化，断网是正常现象，重新登录校园网即可。

Q5: 连接了校园网但是访问不了网页，浏览器提示错误代码 ERR_PROXY_CONNECTION_FAILED，怎么办？
A5: 一般是因为之前使用过代理工具并且非正常关闭（比如未关闭代理就直接关机），导致 Windows 系统代理设置被修改。解决办法：重新打开代理工具然后正常关闭来恢复系统代理设置，或在 Windows 设置里搜索并运行"网络重置"然后重启电脑。也可使用脚本处理：http://172.20.30.3/faq.html

Q6: 连接了校园网但是访问不了网页，浏览器提示错误代码 DNS_PROBE_FINISHED_NO_INTERNET，怎么办？
A6: 该问题是 DNS 非自动获取造成的。解决办法：打开控制面板 -> 网络和共享中心 -> 活动网络 -> 属性，在 IPv4 设置中，将 IP 地址和 DNS 服务器地址均设置为"自动获得"，保存设置即可。也可使用脚本处理：http://172.20.30.3/faq.html

Q7: 在人员密集区域（如教学楼 C-101 教室）为什么连不上校园网？
A7: 当前区域的无线 AP 所能分配的 IP 地址已达上限，除非增加硬件，否则没有解决办法，等人少了就好了。

Q8: 网费充错校区了怎么办？
A8: 请拨打电话 0411-84707007 咨询，下次充网费记得看清楚再充。

Q9: i大工账号/校园邮箱账户/图书馆账户有问题登不上去，该向谁反映问题？
A9: 开发区校区网络服务办公室仅负责校区网络维护。涉及到大工公共网络资源的，请直接联系大连理工大学网络与信息化中心（凌水校区，电话 0411-84707014，邮箱 its@dlut.edu.cn）。
"""

# ============================================================
# Few-shot 示例
# ============================================================
EXAMPLES = """
问答示例：
用户：可以给一下校园网的登录网址嘛？
助手：[我能回答]校园网的登录网址是：http://172.20.30.3/

用户：校园网的网速慢，这是什么原因？
助手：[我不能回答]网速慢属于主观感受，不在校园网故障排查范围内。

用户：校园网可用流量和已用流量都是0，余额有20多，还是不能用，怎么办？
助手：[我能回答]充钱，你超150G了。你的余额已经不够扣了（月租20元 + 超量费用），所以系统不让你用了。充钱后按1元/GB计费。

用户：六舍校园网怎么这么差？！
助手：[我不能回答]抱歉，延迟和丢包问题不在校园网故障排查范围内。

用户：校园网进不去了
助手：[我能回答]请访问 http://172.20.30.3/ 重新登录。如果仍然不行，请参照知识库中的常见问题排查。
"""


# ============================================================
# CampusAssistant 类
# ============================================================
class CampusAssistant:
    """校园网故障排查助手，内置知识库 + get_history 工具"""

    def __init__(self, message_processor: Optional[Any] = None):
        """
        Args:
            message_processor: MessageProcessor 实例，用于 get_group_history 工具
        """
        self.mp = message_processor

        logger.info("正在初始化 CampusAssistant ...")
        logger.info(f"模型: {LLM_CONFIG['model']}, API Base: {LLM_CONFIG['base_url']}")

        self.llm = ChatOpenAI(
            model=LLM_CONFIG["model"],
            api_key=LLM_CONFIG["api_key"],
            base_url=LLM_CONFIG["base_url"],
            temperature=0.3,       # 偏低，希望知识库回答稳定
            max_tokens=1024,
        )

        self.tools = self._build_tools()
        logger.info(f"已注册 {len(self.tools)} 个工具: {[t.name for t in self.tools]}")

        self.system_prompt = SystemMessage(
            content=CAMPUS_SYSTEM_PROMPT + "\n\n" + EXAMPLES
        )

        self.agent = create_agent(
            model=self.llm,
            tools=self.tools,
            system_prompt=self.system_prompt,
        )
        logger.info("CampusAssistant 初始化完成")

    # -------- 工具定义 --------
    def _build_tools(self) -> list:
        mp = self.mp

        @tool
        async def get_group_history(group_id: int) -> str:
            """获取指定群聊的最近聊天记录，用于了解对话上下文。
参数:
  - group_id: int, 目标群号
返回: 结构化消息历史（含发送者、回复链、转发内容等信息）"""
            if mp is None:
                logger.warning("[工具] get_group_history 不可用 - MessageProcessor 未传入")
                return "[工具不可用]"
            try:
                result = await mp.get_history_msg(event={"group_id": group_id, "message_type": "group"})
                if not result:
                    return "暂无历史消息"
                return result
            except Exception as e:
                logger.error(f"[工具异常] get_group_history 失败: {e}")
                return f"[获取历史失败: {e}]"
        @tool
        async def send_img_liuchengt(group_id: int) -> str:
            """一旦遇到校园网故障就要发送流程图，如果最近已经发送过，就可以不发送"""
            if mp is None:
                logger.warning("[工具] send_img_liuchengt 不可用 - MessageProcessor 未传入")
                return "[工具不可用]"
            try:
                await mp.send_img(event={"group_id": group_id, "message_type": "group"}, img_addr=f"{_IMGS_DIR}/img02.jpg", summary="校园网流程图")
                return "已发送流程图"
            except Exception as e:
                logger.error(f"[工具异常] send_img_liuchengt 失败: {e}")
                return f"[发送图片失败: {e}]"

        return [get_group_history, send_img_liuchengt]

    # -------- 对话接口 --------
    async def chat(self, message: str, group_id: Optional[int] = None, icl: Optional[str] = None) -> str:
        """回答校园网相关问题

        Args:
            message: 用户消息（纯文本，已去除 CQ 码）
            group_id: 可选的群号，传入后可让助手调用 get_group_history 获取上下文
            icl: 可选的上下文信息


        Returns:
            str: 助手的回复。如果不能回答，回复会以 [我不能回答] 开头。
        """
        logger.info(f"[校园网助手] 收到提问: {message[:80]}...")

        messages = []
        if group_id is not None:
            messages.append(SystemMessage(
                content=f"[上下文] 当前在群 {group_id} 中，用户问了校园网问题。"
                         f"如有需要可调用 get_group_history 工具查看群内最近聊天记录。"
            ))
        if icl is not None:
            messages.append(SystemMessage(content='群聊历史记录: ' + icl))
        messages.append(HumanMessage(content=message))

        try:
            result = await self.agent.ainvoke({"messages": messages})
            for msg in reversed(result["messages"]):
                if isinstance(msg, AIMessage) and msg.content:
                    reply = msg.content.strip()
                    logger.info(f"[校园网助手] 回复: {reply[:100]}...")
                    return reply
            logger.warning("[校园网助手] LLM 返回空回复")
            return ""
        except Exception as e:
            logger.error(f"[校园网助手] 异常: {e}")
            return f"[处理失败: {e}]"
