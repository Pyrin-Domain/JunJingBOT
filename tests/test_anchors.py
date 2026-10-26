"""``core.anchors``：NapCat / SnowLuma 双游标。"""

from core.anchors import (
    MessageAnchor,
    anchor_kwargs,
    anchor_matches,
    anchor_of,
    anchor_value,
    anchors_equal,
    same_id,
)


def test_same_id_is_lenient():
    assert same_id(12, "12")
    assert same_id(-3, "-3")
    assert not same_id(None, 1)
    assert not same_id("", 1)
    assert not same_id(1, 2)


def test_anchor_of_keeps_all_fields_and_falls_back():
    message = {"message_seq": 7, "message_id": -9, "real_id": -9}
    anchor = anchor_of(message)
    assert anchor.message_seq == 7
    assert anchor.message_id == -9
    assert anchor.real_id == -9
    assert anchor.has_anchor

    # message_id 缺失时用 real_id 兜底；反之亦然。
    fallback = anchor_of({"real_id": 5})
    assert fallback.message_id == 5
    assert fallback.real_id == 5

    assert not anchor_of(None).has_anchor
    assert not anchor_of({}).has_anchor


def test_anchor_preferred_respects_backend_field():
    anchor = anchor_of({"message_seq": 7, "message_id": -9})
    assert anchor.preferred("message_seq") == 7
    assert anchor.preferred("message_id") == -9
    # 后端偏好字段不存在时按 seq → id → real_id 兜底
    assert anchor_of({"real_id": 3}).preferred("message_seq") == 3
    assert anchor_value({"message_seq": 7, "message_id": -9}, "message_id") == -9


def test_anchor_matches_and_intersects_ignore_duplicate_real_id():
    anchor = MessageAnchor(message_seq=1, message_id=2, real_id=2)
    assert anchor.values() == (1, 2)
    assert anchor.matches(2)
    assert anchor.matches("1")
    assert not anchor.matches(3)
    assert anchor.intersects(MessageAnchor(message_id="2"))
    assert anchors_equal({"message_seq": 1}, {"message_id": 1})
    assert anchors_equal(anchor, {"message_id": 2})
    assert anchor_matches({"message_seq": 1, "message_id": 2}, "2")


def test_anchor_kwargs_combines_or_overrides():
    message = {"message_seq": 11, "message_id": 22}
    assert anchor_kwargs(message) == {"message_seq": 11, "message_id": 22}
    assert anchor_kwargs(message_seq=5, message_id=6) == {
        "message_seq": 5,
        "message_id": 6,
    }
    # 只给一个游标时同时当作两个（调用方拿到的通常是“消息 ID”）
    assert anchor_kwargs(message_seq=5) == {"message_seq": 5, "message_id": 5}
    assert anchor_kwargs(message_id=6) == {"message_seq": 6, "message_id": 6}
    # 显式游标优先，不混入 message 里的另一个字段
    assert anchor_kwargs(message, message_seq=5) == {
        "message_seq": 5,
        "message_id": 5,
    }
