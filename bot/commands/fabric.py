"""``fabric``：按模板伪造一条合并转发的节点消息。

模板（从清洗后的正文里解析）：``[qq:123][name:昵称][context:正文]``。
只支持群聊；发送失败由调用方（命令服务）兜住。
"""

from __future__ import annotations

import re

__all__ = ["fabric"]


async def fabric(handler, event):
    clean_message = handler.mp.get_clean_context(event)
    pat = r"\[qq:(\d+)\].*?\[context:(.*)\]"
    match = re.search(pat, clean_message, re.S)
    name_match = re.search(r"\[name:([^\]]*)\]", clean_message)
    if not match:
        return
    if event.get("message_type") == "group":
        await handler.mp.ensure_connected()
        await handler.api.send_group_forward_msg(
            group_id=event.get("group_id"),
            messages=[
                {
                    "type": "node",
                    "data": {
                        "uin": int(match.group(1)),
                        "name": name_match.group(1) if name_match else "QQ用户",
                        "content": [
                            {"type": "text", "data": {"text": match.group(2)}}
                        ],
                    },
                }
            ],
        )
        return
    return
