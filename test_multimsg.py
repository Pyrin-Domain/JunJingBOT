"""验证 check_is_multimsg_forward 函数"""
import json

# ---- 复制函数（独立测试版） ----
def check_is_multimsg_forward(msg_event: dict) -> str | None:
    for seg in msg_event.get("message", []):
        if seg["type"] == "json":
            try:
                json_data = json.loads(seg["data"]["data"])
                if json_data.get("app") == "com.tencent.multimsg":
                    return json_data["meta"]["detail"]["resid"]
            except Exception:
                continue
    return None


# ---- 测试数据 ----
event1 = {
    "self_id": 3369008273,
    "user_id": 3369008273,
    "time": 1782622883,
    "message_id": 948898635,
    "message_type": "group",
    "raw_message": "...",
    "message": [
        {
            "type": "json",
            "data": {
                "data": (
                    '{"app":"com.tencent.multimsg",'
                    '"config":{"autosize":1,"forward":1,"round":1,"type":"normal","width":300},'
                    '"desc":"[聊天记录]",'
                    '"extra":"{\\"filename\\":\\"ac922150\\",\\"tsum\\":34}\\n",'
                    '"meta":{'
                    '"detail":{'
                    '"news":[{"text":"用户A: 每日网络沙雕图"}],'
                    '"resid":"23DRBQ9rdiOXCiz+MJ7LVn1tD7Rpy8lTVRSmVcplobJnGXRTzgxnInHhYbtGtTll",'
                    '"source":"小C的聊天记录",'
                    '"summary":"查看34条转发消息",'
                    '"uniseq":"ac922150-fe7d-490e-88da-d578ac4bf31a"'
                    '}},'
                    '"prompt":"[聊天记录]",'
                    '"ver":"0.0.0.5",'
                    '"view":"contact"}'
                )
            }
        }
    ]
}

# 普通转发消息（有 CQ:forward）
event2 = {
    "message_type": "group",
    "group_id": 1079845768,
    "message_id": 12345,
    "raw_message": "[CQ:forward,id=abc123]",
    "message": [{"type": "text", "data": {"text": "[CQ:forward,id=abc123]"}}]
}

# 普通消息
event3 = {
    "message_type": "group",
    "raw_message": "你好世界",
    "message": [{"type": "text", "data": {"text": "你好世界"}}]
}

# 空 message
event4 = {"message": []}


# ---- 测试 ----
print("=== 测试1: 新版合并转发JSON卡片 ===")
res = check_is_multimsg_forward(event1)
print(f"  结果: {res}")
print(f"  bool: {bool(res)}  ← 应为 True")
assert res == "23DRBQ9rdiOXCiz+MJ7LVn1tD7Rpy8lTVRSmVcplobJnGXRTzgxnInHhYbtGtTll"

print("\n=== 测试2: 普通 CQ:forward 消息 ===")
res = check_is_multimsg_forward(event2)
print(f"  结果: {res}")
print(f"  bool: {bool(res)}  ← 应为 False")
assert res is None

print("\n=== 测试3: 普通文本消息 ===")
res = check_is_multimsg_forward(event3)
print(f"  结果: {res}")
print(f"  bool: {bool(res)}  ← 应为 False")
assert res is None

print("\n=== 测试4: 空 message ===")
res = check_is_multimsg_forward(event4)
print(f"  结果: {res}")
print(f"  bool: {bool(res)}  ← 应为 False")
assert res is None

print("\n✅ 全部测试通过！")
