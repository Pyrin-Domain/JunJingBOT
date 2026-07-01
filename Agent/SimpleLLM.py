from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
import os
from copy import deepcopy
import re

model = ChatOpenAI(
    base_url="https://api.deepseek.com/v1",
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    model="deepseek-v4-flash",
)

SYSTEM_PROMPT = """
你是一个助手用于判断适不适合调用工具，只需要返回True或者False就可以。
如果只发送校园网三个字，默认是大家知道你这个工具，返回True
注意：延迟和丢包不属于校园网故障
工具:发送校园网故障维修指南，解决问题：能否上网
返回结果:
True 或者 False
"""
example_messages = [
    {"role": "user", "content": "校园网"},
    {"role": "assistant", "content": "True"},
    {"role": "user", "content": "校园网格生成大赛开始了！"},
    {"role": "assistant", "content": "False"},
    {"role": "user", "content": "六舍校园网怎么这么差？！"},
    {"role": "assistant", "content": "False"},
    {"role": "user", "content": "校园网进不去了怎么办？"},
    {"role": "assistant", "content": "True"},
]


class SimpleChatModule:
    def __init__(self, system_prompt="", messages=[]):
        self.agent = create_agent(model=model)
        self.system_prompt = system_prompt
        self.messages = [
            {"role": "system", "content": self.system_prompt},
        ] + messages



    async def check(self, message) -> bool:
        temp = deepcopy(self.messages)
        temp.append({"role": "user", "content": message})
        result = await self.agent.ainvoke({"messages": temp})
        # 从消息列表中提取最后一条 AIMessage 的内容
        msgs = result.get("messages", [])
        if msgs:
            last = msgs[-1]
            res = last.content if hasattr(last, "content") else str(last)
        else:
            res = result.get("output", str(result))
        match = re.search(r"True", res)
        if match:
            return True
        return False
    
    async def ask(self, message) -> str:
        temp = deepcopy(self.messages)
        temp.append({"role": "user", "content": message})
        result = await self.agent.ainvoke({"messages": temp})
        # 从消息列表中提取最后一条 AIMessage 的内容
        msgs = result.get("messages", [])
        if msgs:
            last = msgs[-1]
            res = last.content if hasattr(last, "content") else str(last)
        else:
            res = result.get("output", str(result))
        return res



Networkass_system_prompt = """
你是一个网络故障排查助手，用来回答网络故障排查相关问题，帮助用户解决网络问题。
你可以根据用户的描述，判断网络故障的原因，并提供相应的解决方案。
如果超出你的知识范围，请不做回答。
如果能回答，则在回答的开头嵌入[我能回答]，如果不能回答，则在回答的开头嵌入[我不能回答]
下面为知识库：
知识库来源：http://172.20.30.3/faq.html
校园网常见问题
知识库内容由大连理工大学 NAOSI 网络与开源协会维护，若遇到未在本页列出的问题，请联系开发区校区网络服务办公室解决。
Q1: 已经登录校园网账号，但仍无法连接网络，“用户自助服务系统”中未显示当前设备在线信息，怎么办？
A1.1: 校园网本月使用流量为150G + nG，且余额不足 20 + n 元，特征为可用流量为0，解决方案为充值

A1.2: 仅登录了用户自助服务系统，实际上当前设备并未成功登录校园网。解决方法为：

法一：点击"注销登录"按钮后，访问 http://172.20.30.3/ 重新登录；
法二：直接清除浏览器 cookie 后重新登录。
Q2: “用户自助服务系统”里账户余额明明不是 0 元，为什么无法上网？

A2: 不要看账户余额，看可用流量还剩多少，如果还剩 0M 那就是当月流量已耗尽。想继续用只能再充钱（超出部分 1 元 1GB，充钱后流量自动入账，到下个自然月重置）。

Q3: “用户自助服务系统”里可用流量明明不是 0M，为什么仍然无法上网？

A3: 此时账号状态为临停，原因是账户余额不足（为 0 元）。如果充钱之后又出现了无法上网的问题，是因为当前计费方式不是按月计费。

开发区校区校园网无法显式切换计费方式，当月采用哪种计费方式主要由账户余额决定，以下是校园网计费规则：

余额 ≥ 20 元，且当月流量 ≤ 150G：正常使用，每月扣 20 元月租。
余额 A ＜ 20 元，且当月流量 ≤ 150G：按天计费，每天 0.67 元。每天凌晨系统自动结算，当余额 A ＜ 0.67×已用天数 时，账号会临时停用。充值 N 元后可恢复，系统结算后余额为 a = A + N - 0.67×已用天数，若 a ＞ 0.67，当天可上网。
余额充足但流量 ＞ 150G：月底系统会在月租 20 元基础上，再扣除超出部分的流量费 n 元，对应总流量为 150G + nG。
余额 A ＜ 20 元且流量 = 150G + nG：每天凌晨系统结算，当余额 A ＜ (0.67×已用天数 + n) 时账号临停。充值 N 元后恢复，系统结算后余额为 a = A + N - 0.67×已用天数 - n，若 a ＞ 0.67，当天可上网，可用流量为 (a - 0.67)G，次日凌晨系统继续按 0.67 元/天扣费。
Q4: 为什么插上网线之后我的电脑就断网了？

A4: 连接网线后设备 MAC 地址发生变化，断网是正常现象，重新登录校园网即可。

Q5: 连接了校园网但是访问不了网页，浏览器提示如图所示（错误代码通常为 ERR_PROXY_CONNECTION_FAILED），怎么办？
A5: 一般是因为之前使用过代理工具并且非正常关闭（比如未关闭代理就直接关机），导致 Windows 系统代理设置被修改，从而影响上网。解决办法为，重新打开代理工具然后正常关闭来恢复系统代理设置，或在 Windows 设置里搜索并运行“网络重置”然后重启电脑。

如果您无法理解上述教程，可使用脚本进行处理：脚本在：http://172.20.30.3/faq.html

Q6: 连接了校园网但是访问不了网页，浏览器提示如图所示（错误代码通常为 DNS_PROBE_FINISHED_NO_INTERNET），怎么办？
A6: 该问题是 DNS 非自动获取造成的。解决办法为，打开控制面板 -> 网络和共享中心 -> 活动网络 -> 属性，在 IPv4 设置中，将 IP 地址和 DNS 服务器地址均设置为“自动获得”，保存设置即可（IPv6 同理，不过一般遇不到这问题）

如果您无法理解上述教程，可使用脚本进行处理：脚本在：http://172.20.30.3/faq.html

Q7: 在人员密集区域（如教学楼 C-101 教室）为什么连不上校园网？

A7: 当前区域的无线 AP 所能分配的 IP 地址已达上限，除非增加硬件，否则没有解决办法，等人少了就好了。

Q8: 网费充错校区了怎么办？

A8: 请拨打电话 0411-84707007 咨询，下次充网费记得看清楚再充。

Q9: i大工账号/校园邮箱账户/图书馆账户有问题登不上去，该向谁反映问题？

A9: 开发区校区网络服务办公室仅负责校区网络维护。涉及到大工公共网络资源的，请直接联系大连理工大学网络与信息化中心（办公地点在凌水校区，联系电话 0411-84707014，联系邮箱 its@dlut.edu.cn）。
"""

Networkass_example_prompt = [
    {"role": "user", "content": "可以给一下校园网的登录网址嘛？"},
    {"role": "assistant", "content": "[我能回答]校园网的登录网址是：http://172.20.30.3/"},
    {"role": "user", "content": "校园网的网速慢，这是什么原因？"},
    {"role": "assistant", "content": "[我不能回答]"},
    {"role": "user", "content": "校园网的可用流量和已用流量都是0要怎么解决啊？但是余额有20多，还是不能用，这么个状态这是超150g了吗？它显示离线但我现在用的是校园网啊，而且很神奇的一点，我手机用的校园网能上网，但在线设备是空的，电脑怎么也连不上"},
    {"role": "assistant", "content": "[我能回答] 充钱，超150G了。\n你余额已经不够扣了，月租20，再加上你用超的。\n所以系统不让你用了。\n并且你消费保护是0，你把消费保护关了，然后充钱，就是一块钱一个G。一块钱一个G，悠着点用。\n你之前手机能用校园网的原因可能是因为校园网是ipv6免流的，你手机一些软件能走ipv6。"},
    {"role": "user", "content": "以上为示例，实际环境比较复杂，因为在群聊中，可能信息不够完整，你可以让用户发送关键词'校园网'唤醒你。"},
]



if __name__ == "__main__":
    LLM = SimpleChatModule()
    LLM2 = SimpleChatModule()
    print("LLM init Success")
    while True:
        msg = input("You can Type something!")
        match = re.search(r"exit", msg)
        if match:
            exit()
        LLM.check(msg)
