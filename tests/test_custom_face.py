"""收藏表情（账号自定义表情）：两端字段归一 / 主键选择 / 发送分流。

实测依据（2026-11-01，NapCat:3000 / SnowLuma:3210，同一个测试群）：

- SnowLuma 的 ``fetch_custom_face_detail`` 给 ``emoji_id``，NapCat **没有**这个字段，
  只有 ``resId``；
- 同一个表情在两个账号上的 ``md5`` 与 ``desc`` 完全一致，``emoji_id``/``resId``
  却带各自的 uin 前缀——所以主键用 ``desc``（用户备注），没备注才退回 md5；
- SnowLuma 有 ``send_custom_face``，NapCat 没有（retcode=200）；
- 两个后端都能用 ``image`` 段（``sub_type=1``）发出收藏表情的外观。
"""

import asyncio

import pytest

from AbstractionLayer.face import (
    CustomFace,
    CustomFaceLibrary,
    CustomFaceService,
    fetch_custom_faces,
    normalize_face_key,
)
from AbstractionLayer.kernal import GENERIC, NAPCAT, SNOWLUMA, OneBotKernel

# SnowLuma 实测返回（3210）
SNOWLUMA_DETAIL = [
    {
        "emoji_id": "3122793026_0_0_0_1013D6547A68315893C5842A2637665B_0_0",
        "resId": "3122793026_0_0_0_1013D6547A68315893C5842A2637665B_0_0",
        "url": "https://p.qpic.cn/qq_expression/3122793026/3122793026_0_0_0_1013D65.../0",
        "md5": "1013D6547A68315893C5842A2637665B",
        "desc": "",
    },
    {
        "emoji_id": "3122793026_0_0_0_6F79B92CC4B756C6E6300AF90BCCB02E_0_0",
        "resId": "3122793026_0_0_0_6F79B92CC4B756C6E6300AF90BCCB02E_0_0",
        "url": "https://p.qpic.cn/qq_expression/3122793026/3122793026_0_0_0_6F79B9.../0",
        "md5": "6F79B92CC4B756C6E6300AF90BCCB02E",
        "desc": "小男娘说话挺逗",
    },
]

# NapCat 实测返回（3000）：没有 emoji_id，多了 uin / emoId / emoPath
NAPCAT_DETAIL = [
    {
        "uin": "3521775163",
        "emoId": 0,
        "emoPath": "C:\\Users\\10130\\Documents\\Tencent Files\\3521775163\\...\\16F2B7.jpg",
        "isExist": False,
        "resId": "3521775163_0_0_0_16F2B7082F3591DC4A561F34092F7E7F_0_0",
        "url": "https://p.qpic.cn/qq_expression/3521775163/3521775163_0_0_0_16F2B7.../0",
        "md5": "16F2B7082F3591DC4A561F34092F7E7F",
        "desc": "爸爸活",
    },
    {
        "uin": "3521775163",
        "emoId": 2,
        "resId": "3521775163_0_0_0_5746AD514836BA06CB2F8211CE553B94_0_0",
        "url": "https://p.qpic.cn/qq_expression/3521775163/3521775163_0_0_0_5746AD.../0",
        "md5": "5746AD514836BA06CB2F8211CE553B94",
        "desc": "不够色情",
    },
]


class _FakeTransport:
    """记录发出去的 action / params，并按 action 返回预设数据。"""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    async def call(self, action: str, params: dict) -> dict:
        self.calls.append((action, params))
        payload = self.responses.get(action)
        if payload is None:
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}
        return payload


def _kernel(responses=None, caps=None):
    return OneBotKernel(
        _FakeTransport(responses),
        capabilities=caps or SNOWLUMA,
        label="fake",
    )


# ════════════════════ key 归一化 ════════════════════
def test_normalize_strips_brackets_and_noise_words():
    assert normalize_face_key("【不够色情】") == "不够色情"
    assert normalize_face_key("不够色情表情") == "不够色情"
    assert normalize_face_key("  不够色情  ") == "不够色情"
    assert normalize_face_key("") == ""
    assert normalize_face_key(None) == ""


def test_normalize_keeps_meaningful_words():
    """「表情」这种装饰词要剥，但「色情」里的「情」不能动。"""
    assert normalize_face_key("小男娘说话挺逗") == "小男娘说话挺逗"


# ════════════════════ 两端字段归一 ════════════════════
def test_fetch_snowluma_uses_emoji_id():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": SNOWLUMA_DETAIL}},
        caps=SNOWLUMA,
    )
    faces = asyncio.run(fetch_custom_faces(kernel))
    assert len(faces) == 2
    assert faces[0].emoji_id.startswith("3122793026_")
    assert faces[1].desc == "小男娘说话挺逗"


def test_fetch_napcat_falls_back_to_res_id():
    """NapCat 没有 emoji_id，标识字段是 resId。"""
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=NAPCAT,
    )
    faces = asyncio.run(fetch_custom_faces(kernel))
    assert len(faces) == 2
    assert faces[0].emoji_id == "3521775163_0_0_0_16F2B7082F3591DC4A561F34092F7E7F_0_0"
    assert faces[0].desc == "爸爸活"


def test_fetch_sends_count_param():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": []}},
        caps=SNOWLUMA,
    )
    asyncio.run(fetch_custom_faces(kernel, count=17))
    action, params = kernel.transport.calls[0]
    assert action == "fetch_custom_face_detail"
    assert params["count"] == 17


def test_fetch_failure_returns_empty():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "failed", "retcode": 200, "message": "不支持"}},
        caps=SNOWLUMA,
    )
    assert asyncio.run(fetch_custom_faces(kernel)) == ()


# ════════════════════ 主键：desc 优先，空则 md5 ════════════════════
def test_key_is_desc_when_present():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": SNOWLUMA_DETAIL}},
        caps=SNOWLUMA,
    )
    faces = asyncio.run(fetch_custom_faces(kernel))
    assert faces[1].key == "小男娘说话挺逗"


def test_key_falls_back_to_md5_when_no_desc():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": SNOWLUMA_DETAIL}},
        caps=SNOWLUMA,
    )
    faces = asyncio.run(fetch_custom_faces(kernel))
    assert faces[0].desc == ""
    assert faces[0].key == "1013D6547A68315893C5842A2637665B"


def test_same_picture_has_same_key_on_both_accounts():
    """跨账号稳定性：同一个表情（md5 / desc 相同）在两边主键必须一样。"""
    shared = {
        "resId": "3521775163_0_0_0_5746AD514836BA06CB2F8211CE553B94_0_0",
        "url": "https://p.qpic.cn/qq_expression/3521775163/x/0",
        "md5": "5746AD514836BA06CB2F8211CE553B94",
        "desc": "不够色情",
    }
    snow = {**SNOWLUMA_DETAIL[0], "desc": "不够色情", "md5": shared["md5"]}
    k1 = _kernel({"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": [snow]}}, caps=SNOWLUMA)
    k2 = _kernel({"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": [shared]}}, caps=NAPCAT)
    f1 = asyncio.run(fetch_custom_faces(k1))[0]
    f2 = asyncio.run(fetch_custom_faces(k2))[0]
    assert f1.key == f2.key == "不够色情"
    # 但账号内的标识不同——所以发送时必须用各自那份
    assert f1.emoji_id != f2.emoji_id


# ════════════════════ 查表 ════════════════════
def test_library_find_is_lenient():
    library = CustomFaceLibrary(
        [
            CustomFace(key="不够色情", desc="不够色情", md5="AAA", emoji_id="x", url="u"),
        ]
    )
    assert library.get("不够色情") is not None
    assert library.find("【不够色情】") is not None
    assert library.find("不够色情表情") is not None
    assert library.find("完全没有") is None


def test_library_search_matches_desc_and_md5():
    library = CustomFaceLibrary(
        [
            CustomFace(key="a", desc="很好笑", md5="1013D6547A68315893C5842A2637665B"),
            CustomFace(key="b", desc="别的", md5="6F79B92CC4B756C6E6300AF90BCCB02E"),
        ]
    )
    assert [f.key for f in library.search("好笑")] == ["a"]
    assert [f.key for f in library.search("1013d6547a68315893c5842a2637665b")] == ["a"]


# ════════════════════ 发送分流 ════════════════════
def test_snowluma_uses_dedicated_action():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": SNOWLUMA_DETAIL}},
        caps=SNOWLUMA,
    )
    service = CustomFaceService(kernel)
    asyncio.run(service.refresh())
    result = asyncio.run(service.send("小男娘说话挺逗", group_id=20001))

    assert result.ok
    action, params = kernel.transport.calls[-1]
    assert action == "send_custom_face"
    assert params["group_id"] == 20001
    assert params["emoji_id"].startswith("3122793026_")


def test_napcat_falls_back_to_image_segment():
    """NapCat 没有 send_custom_face：退回 image 段，且必须带 sub_type=1。"""
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=NAPCAT,
    )
    service = CustomFaceService(kernel)
    asyncio.run(service.refresh())
    result = asyncio.run(service.send("不够色情", group_id=20001))

    assert result.ok
    action, params = kernel.transport.calls[-1]
    assert action == "send_group_msg"
    segment = params["message"][0]
    assert segment["type"] == "image"
    assert segment["data"]["sub_type"] == 1
    assert segment["data"]["summary"] == "[动画表情]"
    assert segment["data"]["file"].startswith("https://p.qpic.cn/qq_expression/")


def test_generic_backend_uses_image_segment():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=GENERIC,
    )
    service = CustomFaceService(kernel)
    asyncio.run(service.refresh())
    asyncio.run(service.send("不够色情", group_id=20001))
    action, _ = kernel.transport.calls[-1]
    assert action == "send_group_msg"


def test_private_send_uses_private_action():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=NAPCAT,
    )
    service = CustomFaceService(kernel)
    asyncio.run(service.refresh())
    asyncio.run(service.send("不够色情", user_id=10001))
    action, params = kernel.transport.calls[-1]
    assert action == "send_private_msg"
    assert params["user_id"] == 10001


def test_reply_to_is_kept_in_fallback():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=NAPCAT,
    )
    service = CustomFaceService(kernel)
    asyncio.run(service.refresh())
    asyncio.run(service.send("不够色情", group_id=20001, reply_to=12345))
    _, params = kernel.transport.calls[-1]
    assert params["message"][0]["type"] == "reply"
    assert params["message"][0]["data"]["id"] == "12345"


def test_unknown_key_fails_without_sending():
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=NAPCAT,
    )
    service = CustomFaceService(kernel)
    asyncio.run(service.refresh())
    calls_before = len(kernel.transport.calls)
    result = asyncio.run(service.send("压根没有的表情", group_id=20001))
    assert not result.ok
    assert "没有" in result.error
    assert len(kernel.transport.calls) == calls_before


def test_missing_target_is_rejected():
    kernel = _kernel(caps=NAPCAT)
    service = CustomFaceService(kernel)
    result = asyncio.run(service.send("随便什么", group_id=None, user_id=None))
    assert not result.ok


def test_send_failure_is_reported_as_failed_result():
    """底层抛异常时收敛成 ok=False，调用方只需要判断 ok。"""
    kernel = _kernel(
        {
            "fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL},
            "send_group_msg": {"status": "failed", "retcode": 100, "message": "被禁言"},
        },
        caps=NAPCAT,
    )
    service = CustomFaceService(kernel)
    asyncio.run(service.refresh())
    result = asyncio.run(service.send("不够色情", group_id=20001))
    assert not result.ok
    assert "禁言" in result.error


# ════════════════════ 并进表情包库 ════════════════════
def test_merge_custom_faces_into_meme_library():
    from messaging.meme import MemeLibrary

    base = MemeLibrary.from_dict(
        {
            "faces": [{"name": "赞", "describe": "夸人", "data": {"id": "76"}}],
            "mfaces": [],
            "stickers": [],
        }
    )
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=NAPCAT,
    )
    faces = asyncio.run(fetch_custom_faces(kernel))
    merged = base.merged_with_custom_faces(faces)

    # 静态表情还在
    assert merged.find("赞").kind == "face"
    # 收藏表情按备注名找得到
    entry = merged.find("不够色情")
    assert entry is not None
    assert entry.kind == "custom"
    # 渲染出来的段要带 sub_type=1
    segment = entry.segment()
    assert segment["type"] == "image"
    assert segment["data"]["sub_type"] == 1
    assert segment["data"]["summary"] == "[动画表情]"


def test_static_config_wins_over_custom_face():
    """同名时静态配置优先：配置文件是人工维护的。"""
    from messaging.meme import MemeLibrary

    base = MemeLibrary.from_dict(
        {
            "faces": [{"name": "不够色情", "describe": "手写的", "data": {"id": "76"}}],
            "mfaces": [],
            "stickers": [],
        }
    )
    kernel = _kernel(
        {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
        caps=NAPCAT,
    )
    faces = asyncio.run(fetch_custom_faces(kernel))
    merged = base.merged_with_custom_faces(faces)
    assert merged.find("不够色情").kind == "face"


def test_custom_face_cq_round_trip_keeps_sub_type():
    """CQ 码 → 消息段 的来回不能把 sub_type 弄丢，丢了就退化成普通图片。"""
    from messaging.meme import to_segments

    library = CustomFaceLibrary
    face = CustomFace(
        key="不够色情",
        desc="不够色情",
        md5="5746AD514836BA06CB2F8211CE553B94",
        emoji_id="3521775163_0_0_0_5746AD514836BA06CB2F8211CE553B94_0_0",
        url="https://p.qpic.cn/qq_expression/3521775163/x/0",
    )
    from AbstractionLayer.face import CustomFace as _CF  # noqa: F401

    from messaging.meme import MemeEntry

    entry = MemeEntry(
        name=face.label,
        describe="收藏表情",
        kind="custom",
        data={"url": face.url, "sub_type": 1, "summary": "[动画表情]"},
    )
    code = entry.cq()
    assert code.startswith("[CQ:image,")
    assert "sub_type=1" in code

    segments = to_segments(code)
    assert segments is not None
    assert segments[0]["type"] == "image"
    assert segments[0]["data"]["sub_type"] == 1
    assert segments[0]["data"]["summary"] == "[动画表情]"
    assert library  # 保持引用


def test_custom_face_alone_is_detected_as_segment():
    """只发一个收藏表情（没有文字）也要转成段，不能当普通文本发出去。"""
    from messaging.meme import has_cq_segment, is_custom_face_cq, to_segments

    code = "[CQ:image,file=https://x/y/0,sub_type=1,summary=&#91;动画表情&#93;]"
    assert has_cq_segment(code)
    assert is_custom_face_cq(code)
    segments = to_segments(code)
    assert segments is not None
    assert segments[0]["data"]["sub_type"] == 1


def test_plain_sticker_cq_is_not_treated_as_custom_face():
    """普通贴纸的 [CQ:image,file=…] 没有 sub_type，不能被当成收藏表情。"""
    from messaging.meme import has_cq_segment, is_custom_face_cq

    code = "[CQ:image,file=/tmp/a.jpg]"
    assert not is_custom_face_cq(code)
    assert not has_cq_segment(code)


def test_merge_custom_faces_updates_singleton():
    from messaging import meme as meme_mod

    meme_mod.reset()
    try:
        kernel = _kernel(
            {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
            caps=NAPCAT,
        )
        faces = asyncio.run(fetch_custom_faces(kernel))
        meme_mod.merge_custom_faces(faces)
        assert meme_mod.get_library().find("不够色情") is not None
    finally:
        meme_mod.reset()


def test_configure_keeps_custom_faces():
    """Agent 热重载会走 configure()，不能把抓来的收藏表情弄丢。"""
    from messaging import meme as meme_mod

    meme_mod.reset()
    try:
        kernel = _kernel(
            {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
            caps=NAPCAT,
        )
        faces = asyncio.run(fetch_custom_faces(kernel))
        meme_mod.merge_custom_faces(faces)

        # 模拟面板改配置后重载表情库
        meme_mod.configure()
        assert meme_mod.get_library().find("不够色情") is not None
    finally:
        meme_mod.reset()


def test_merge_custom_faces_replaces_previous_batch():
    """重抓（换账号 / 刷新）要替换掉上一批，不能把旧表情留在库里。"""
    from messaging import meme as meme_mod

    meme_mod.reset()
    try:
        snow = _kernel(
            {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": SNOWLUMA_DETAIL}},
            caps=SNOWLUMA,
        )
        nap = _kernel(
            {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
            caps=NAPCAT,
        )
        meme_mod.merge_custom_faces(asyncio.run(fetch_custom_faces(snow)))
        assert meme_mod.get_library().find("小男娘说话挺逗") is not None

        meme_mod.merge_custom_faces(asyncio.run(fetch_custom_faces(nap)))
        library = meme_mod.get_library()
        # 旧的一批没了
        assert library.find("小男娘说话挺逗") is None
        # 新的一批在，且用的是新账号的 URL
        entry = library.find("不够色情")
        assert entry is not None
        assert "3521775163" in entry.data["url"]
    finally:
        meme_mod.reset()


def test_merge_custom_faces_before_configure_is_kept():
    """先抓后配置也要在：抓取和 configure 的先后顺序不能影响结果。"""
    from messaging import meme as meme_mod

    meme_mod.reset()
    try:
        kernel = _kernel(
            {"fetch_custom_face_detail": {"status": "ok", "retcode": 0, "data": NAPCAT_DETAIL}},
            caps=NAPCAT,
        )
        faces = asyncio.run(fetch_custom_faces(kernel))
        meme_mod.merge_custom_faces(faces)
        meme_mod.configure()
        assert meme_mod.get_library().find("不够色情") is not None
    finally:
        meme_mod.reset()


@pytest.mark.parametrize("caps", [NAPCAT, SNOWLUMA, GENERIC])
def test_capabilities_expose_custom_face_fields(caps):
    data = caps.to_dict()
    assert "custom_face_send_action" in data
    assert "custom_face_id_field" in data
    assert data["custom_face_sub_type"] == 1
