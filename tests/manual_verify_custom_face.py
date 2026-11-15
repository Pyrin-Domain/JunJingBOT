"""端到端验证：两个测试端口上跑通「抓取 → 建库 → 按 key 发送」。

**手动脚本**，不参与 pytest（文件名不匹配 `test_*.py` / `*_test.py`）。
它会**真的往测试群发消息**，别在生产环境跑。

SnowLuma 3210 / NapCat 3000，同一个测试群 1132313813。
每边发出去的表情，都用**另一边的接口**回读，确认它是「收藏表情」
（sub_type=1 / summary=[动画表情]）而不是普通图片。

    python tests/manual_verify_custom_face.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from AbstractionLayer.face import CustomFaceService  # noqa: E402
from AbstractionLayer.kernal import OneBotKernel, capabilities_for, default_token  # noqa: E402

#: 本机测试用 token；换成别的环境时用 ``ONEBOT_TOKEN`` 覆盖。
TOKEN = os.environ.get("ONEBOT_TOKEN", default_token())
GROUP = 1132313813


def kernel_for(backend: str) -> OneBotKernel:
    port = {"napcat": 3000, "snowluma": 3210}[backend]
    k = OneBotKernel.http(f"http://127.0.0.1:{port}", TOKEN, backend=backend)
    k.set_capabilities(capabilities_for(backend))
    return k


async def history_via(backend: str, count: int = 4) -> list[dict]:
    k = kernel_for(backend)
    result = await k.call(
        "get_group_msg_history", {"group_id": GROUP, "count": count, "reverse_order": False}
    )
    out = []
    for msg in result.messages:
        for seg in msg.get("message", []):
            if seg.get("type") in ("image", "mface"):
                data = dict(seg.get("data") or {})
                data.pop("url", None)
                out.append(
                    {
                        "seq": msg.get("message_seq") or msg.get("real_seq"),
                        "from": (msg.get("sender") or {}).get("user_id"),
                        "seg": data,
                    }
                )
    return out


async def main() -> int:
    failures = []

    for backend in ("snowluma", "napcat"):
        print(f"\n{'=' * 62}\n【{backend}】")
        kernel = kernel_for(backend)
        service = CustomFaceService(kernel)

        library = await service.refresh()
        print(f"抓到 {len(library)} 个收藏表情：")
        for face in library.faces:
            print(
                f"  key={face.key!r:24} desc={face.desc!r:18} "
                f"md5={face.md5[:12]}… emoji_id={face.emoji_id[:28]}…"
            )

        if not library:
            failures.append(f"{backend}: 没抓到收藏表情")
            continue

        # 按备注名找（用户想要的就是这个 key）
        target = library.find("不够色情")
        if target is None:
            failures.append(f"{backend}: 按 '不够色情' 找不到")
            continue
        print(f"按 key 命中：{target.key!r} -> {target.url}")

        result = await service.send(target.key, group_id=GROUP)
        print(f"发送结果：ok={result.ok} action={result.action} data={result.data}")
        if not result.ok:
            failures.append(f"{backend}: 发送失败 {result.error}")
            continue

        await asyncio.sleep(3)

    # 用另一边的接口回读，确认发出去的是收藏表情
    print(f"\n{'=' * 62}\n【回读验证】")
    for backend, expect_from in (("napcat", "3122793026"), ("snowluma", "3521775163")):
        rows = await history_via(backend)
        mine = [r for r in rows if str(r["from"]) == expect_from]
        print(f"\n{backend} 看到的、来自 {expect_from} 的表情：")
        for row in mine[-4:]:
            seg = row["seg"]
            kind = "收藏表情" if seg.get("sub_type") == 1 else "普通图片"
            print(f"  seq={row['seq']} {kind}: {json.dumps(seg, ensure_ascii=False)}")

    if failures:
        print("\n❌ 失败：")
        for item in failures:
            print("  -", item)
        return 1
    print("\n✅ 两端都跑通了")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
