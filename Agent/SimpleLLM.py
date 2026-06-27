from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
import os
from copy import deepcopy
import re

model = ChatOpenAI(
    base_url = 'https://api.deepseek.com/v1',
    api_key = os.environ.get("DEEPSEEK_API_KEY"),
    model= "deepseek-v4-flash"
)

SYSTEM_PROMPT = """
你是一个助手用于判断适不适合调用工具，只需要返回True或者False就可以。
如果只发送校园网三个字，默认是大家知道你这个工具，返回True
注意：延迟和丢包不属于校园网故障
工具:发送校园网故障维修指南，解决问题：能否上网
返回结果:
True 或者 False
"""
class SimpleChatModule:
    def __init__(self):
        self.agent = create_agent(model=model)
        self.messages = [
        {"role":"system","content":SYSTEM_PROMPT},
        {"role":"user","content":"校园网"},
        {"role":"assistant","content":"True"},
        {"role":"user","content":"校园网格生成大赛开始了！"},
        {"role":"assistant","content":"False"},
        {"role":"user","content":"六舍校园网怎么这么差？！"},
        {"role":"assistant","content":"False"},
        {"role":"user","content":"校园网进不去了怎么办？"},
        {"role":"assistant","content":"True"},
        ]

    async def ask(self,message) -> bool:
        temp = deepcopy(self.messages)
        temp.append({"role":"user","content":message})
        result = await self.agent.ainvoke({'messages': temp})
        # 从消息列表中提取最后一条 AIMessage 的内容
        msgs = result.get("messages", [])
        if msgs:
            last = msgs[-1]
            res = (last.content if hasattr(last, "content") else str(last))
        else:
            res = (result.get("output", str(result)))
        match = re.search(r'True',res)
        if match :
            return True
        return False



if __name__ ==  '__main__':
    LLM = SimpleChatModule()
    print("LLM init Success")
    while True:
        msg = input("You can Type something!")
        match = re.search(r"exit",msg)
        if match:
            exit()
        LLM.ask(msg)
    