"""端到端验证 bot 真实链路：<meme>名字</meme> → CQ → 消息段 → 真发出去。

**手动脚本**，不参与 pytest（文件名不匹配 `test_*.py` / `*_test.py`）。
它会**真的往测试群发消息**，别在生产环境跑。

走的是 bot 真正用的那几个函数：
    fetch_custom_faces → merge_custom_faces → render_message_body → to_segments → 发包

    python tests/manual_verify_bot_face.py
"""

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from AbstractionLayer.face import fetch_custom_faces  # noqa: E402
from AbstractionLayer.kernal import OneBotKernel, capabilities_for, default_token  # noqa: E402
from agent.output import render_message_body  # noqa: E402
from messaging.meme import merge_custom_faces, to_segments  # noqa: E402

#: 本机测试用 token；换成别的环境时用 ``ONEBOT_TOKEN`` 覆盖。
TOKEN = os.environ.get("ONEBOT_TOKEN", default_token())
GROUP = 1132313813


async def main() -> int:
    for backend, port in (("snowluma", 3210), ("napcat", 3000)):
        print(f"\n{'=' * 62}\n【{backend}】")
        kernel = OneBotKernel.http(f"http://127.0.0.1:{port}", TOKEN, backend=backend)
        kernel.set_capabilities(capabilities_for(backend))

        # 1. 抓收藏表情 → 并进表情包库
        faces = await fetch_custom_faces(kernel)
        library = merge_custom_faces(faces)
        print(f"表情库现在共 {len(library)} 条；收藏表情 {len(faces)} 条")

        # 2. 模型输出 → CQ 码
        body = "<message>看这个<meme>不够色情</meme></message>"
        rendered = render_message_body(body)
        print(f"CQ：{rendered}")

        # 3. CQ 码 → 消息段
        segments = to_segments(rendered)
        assert segments is not None, "没有转成消息段！"
        print(f"消息段：{segments}")

        image_seg = [s for s in segments if s["type"] == "image"]
        assert image_seg, "没有 image 段！"
        assert image_seg[0]["data"].get("sub_type") == 1, "sub_type 丢了，会退化成普通图片！"

        # 4. 真发出去
        action = "send_group_msg"
        result = await kernel.call(
            action, {"group_id": GROUP, "message": segments}
        )
        print(f"发送：ok={result.ok} data={result.data} error={result.error!r}")
        if not result.ok:
            return 1

        await asyncio.sleep(3)

    # 5. 回读确认是收藏表情
    print(f"\n{'=' * 62}\n【回读】")
    kernel = OneBotKernel.http("http://127.0.0.1:3000", TOKEN, backend="napcat")
    kernel.set_capabilities(capabilities_for("napcat"))
    hist = await kernel.call(
        "get_group_msg_history", {"group_id": GROUP, "count": 6, "reverse_order": False}
    )
    ok = True
    for msg in hist.messages:
        for seg in msg.get("message", []):
            if seg.get("type") != "image":
                continue
            data = seg.get("data") or {}
            sub = data.get("sub_type")
            kind = "收藏表情" if sub == 1 else "普通图片"
            print(
                f"  seq={msg.get('real_seq')} from={(msg.get('sender') or {}).get('user_id')} "
                f"{kind} sub_type={sub} summary={data.get('summary')!r}"
            )
            if sub != 1:
                ok = False
    print("\n✅ 整条链路跑通，发出去的都是收藏表情" if ok else "\n❌ 有消息不是收藏表情")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
