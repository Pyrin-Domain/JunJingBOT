# -*- coding: utf-8 -*-
"""机器人图片去重：按图片**本体**认图（字节哈希 + 感知指纹）。

线上出过的事故（2026-11-06）：

* 08:41 群里 5 张截图（``data.file`` 全是**大写** md5）搬了一轮；
* 14:57 同一组 5 张图被重新上传，md5 换了一组小写、像素完全相同 ——
  ``image:*`` 身份（文件名哈希）认不出来，于是又搬了一轮全部目标群。

根因：图片身份只认「消息 id + 文件名（md5）」，而 QQ 重新编码会改 md5。
这里锁住修复后的行为：

1. 同一张图重新编码（不同 md5 / 不同消息 id / 不同文件名）→ 只搬一次；
2. 不同图片 → 各搬一次；
3. 多图消息「旧图 + 新图」→ 照常搬（不能因为一张旧图把新图吞掉）；
4. 文件名字段不是哈希时，按真实字节身份去重；
5. 关掉 ``perceptual`` 后回到旧行为（会重复搬）。
"""

from __future__ import annotations

import asyncio
import io
import random

import pytest
from PIL import Image, ImageDraw

from bot.services.forward import ForwardService
from bot.services.image_dedup import ForwardImageStore


class _FakeMP:
    def is_contain_image(self, _event) -> bool:
        return True

    def check_is_miniapp(self, _event) -> dict:
        return {}

    def check_is_old_forward(self, _event) -> bool:
        return False

    def check_is_multimsg_forward(self, _event):
        return None


class _FakeGate:
    """只实现图片去重需要的几个口子：过滤门（不命中）+ 下载缓存。"""

    def __init__(self, images: dict[str, bytes]):
        self.images = images
        self.config: dict = {}
        self.enabled = False
        self.loads: list[str] = []

    def note_event(self, _event) -> None:
        return None

    async def screen_event(self, _event):
        return []

    async def publish_event(self, _event, _hits):
        return None

    def load_image(self, url: str, timeout: float = 8.0) -> bytes | None:
        self.loads.append(url)
        return self.images.get(url)

    def load_image_many(self, *_args, **_kwargs):  # pragma: no cover - 兼容
        return None


def _pattern(seed: int, size=(96, 96), quality: int = 90) -> bytes:
    """画一张有结构的图（纯色图的双哈希会全 0，区分不出内容，不能用来做样例）。"""
    rnd = random.Random(seed)
    image = Image.new(
        "RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
    )
    draw = ImageDraw.Draw(image)
    for _ in range(14):
        left = rnd.randrange(size[0])
        top = rnd.randrange(size[1])
        right = left + rnd.randrange(8, size[0] // 2)
        bottom = top + rnd.randrange(8, size[1] // 2)
        draw.rectangle(
            [left, top, right, bottom],
            fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)),
        )
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


def _reencode(data: bytes, size=(96, 96), quality: int = 55) -> bytes:
    """模拟 QQ 重新上传：缩放 + 重新压缩 → 像素几乎一样，字节/md5 全变。"""
    image = Image.open(io.BytesIO(data)).convert("RGB").resize(size, Image.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return buffer.getvalue()


BOT_ID = 1013098110
WHITELIST = {
    "enable": True,
    "bot_list": [{"bot_id": BOT_ID, "type": "all", "group_id": [], "user_id_list": []}],
}
TARGETS = [20002, 20003]


def _image_event(files: list[str], *, message_id: int, group_id: int = 949950852) -> dict:
    return {
        "message_type": "group",
        "user_id": BOT_ID,
        "group_id": group_id,
        "message_id": message_id,
        "message": [
            {
                "type": "image",
                "data": {"file": name, "url": f"https://img.example/{name}"},
            }
            for name in files
        ],
    }


def _service(images: dict[str, bytes], *, config: dict | None = None):
    settings = {"forward_image_dedup": config or {}}
    store = ForwardImageStore(db_path=":memory:", settings=settings)
    gate = _FakeGate(images)
    service = ForwardService(
        mp=_FakeMP(),
        api=None,
        settings=settings,
        image_store=store,
        image_gate=gate,
    )
    calls: list[list[str]] = []

    async def fake_batch(event, target_groupid=None, task_userid=None):
        calls.append([str(g) for g in (target_groupid or [])])
        return len(target_groupid or [])

    service.batch_forward_single_msg = fake_batch  # type: ignore[assignment]
    return service, store, gate, calls


def _run(service, event) -> bool:
    return asyncio.run(
        service.auto_forward(
            event=event,
            groupid_list=[949950852],
            userid_list=[],
            target_groupid=TARGETS,
            task_userid=[],
            auto_img_forward_from_bot=WHITELIST,
        )
    )


# ══════════════════════════════════════════════════════════
# 同一张图重新编码 → 只搬一次
# ══════════════════════════════════════════════════════════
def test_reencoded_image_is_forwarded_once():
    original = _pattern(1)
    again = _reencode(original)
    assert original != again, "样例必须真的不同字节"

    service, store, _gate, calls = _service(
        {
            "https://img.example/AAAA.jpg": original,
            "https://img.example/bbbb.jpg": again,
        }
    )

    assert _run(service, _image_event(["AAAA.jpg"], message_id=111)) is True
    assert _run(service, _image_event(["bbbb.jpg"], message_id=222)) is True

    assert calls == [["20002", "20003"]], "同一张图重新编码后又被搬了一遍"


def test_reencoded_image_with_new_targets_only_fills_the_gap():
    """已送过的群不重发，后来新增的目标群照旧补发。"""
    original = _pattern(2)
    again = _reencode(original)
    urls = {
        "https://img.example/CCCC.jpg": original,
        "https://img.example/dddd.jpg": again,
    }
    service, _store, _gate, calls = _service(urls)

    assert _run(service, _image_event(["CCCC.jpg"], message_id=11)) is True
    service2 = service  # 同一个 service，换目标列表

    # 同样的两张图、新增一个目标群
    for event in (
        _image_event(["dddd.jpg"], message_id=12),
    ):
        asyncio.run(
            service2.auto_forward(
                event=event,
                groupid_list=[949950852],
                userid_list=[],
                target_groupid=TARGETS + [20006],
                task_userid=[],
                auto_img_forward_from_bot=WHITELIST,
            )
        )

    assert calls == [["20002", "20003"], ["20006"]]


def test_different_images_are_both_forwarded():
    service, _store, _gate, calls = _service(
        {
            "https://img.example/one.jpg": _pattern(3),
            "https://img.example/two.jpg": _pattern(4),
        }
    )

    _run(service, _image_event(["one.jpg"], message_id=1))
    _run(service, _image_event(["two.jpg"], message_id=2))

    assert len(calls) == 2, "两张不同的图被误杀了一张"


# ══════════════════════════════════════════════════════════
# 多图消息：旧图 + 新图必须照常搬
# ══════════════════════════════════════════════════════════
def test_message_with_one_old_and_one_new_image_is_forwarded():
    old = _pattern(5)
    new = _pattern(6)
    service, _store, _gate, calls = _service(
        {
            "https://img.example/old.jpg": old,
            "https://img.example/new.jpg": new,
            "https://img.example/old2.jpg": _reencode(old),
        }
    )

    _run(service, _image_event(["old.jpg"], message_id=100))
    # 同一条消息里「旧图（重新编码过）+ 新图」→ 新图还没送过，必须搬
    _run(service, _image_event(["old2.jpg", "new.jpg"], message_id=101))

    assert len(calls) == 2, "旧图把整条消息吞了，新图没搬出去"


def test_all_images_already_sent_is_skipped():
    one = _pattern(7)
    two = _pattern(8)
    service, _store, _gate, calls = _service(
        {
            "https://img.example/a1.jpg": one,
            "https://img.example/a2.jpg": two,
            "https://img.example/b1.jpg": _reencode(one),
            "https://img.example/b2.jpg": _reencode(two),
        }
    )

    _run(service, _image_event(["a1.jpg", "a2.jpg"], message_id=201))
    _run(service, _image_event(["b1.jpg", "b2.jpg"], message_id=202))

    assert len(calls) == 1


def test_bytes_identity_requires_every_photo_matched():
    """字节身份同样要「每张图都送过」——只命中一张旧图不能吞掉新图。"""
    old = _pattern(13)
    new = _pattern(14)
    service, _store, _gate, calls = _service(
        {
            "https://img.example/same_old.jpg": old,
            "https://img.example/brand_new.jpg": new,
        }
    )

    _run(service, _image_event(["same_old.jpg"], message_id=211))
    # 同一张旧图（字节完全一致）+ 一张新图 → 新图没送过，必须搬
    _run(service, _image_event(["same_old.jpg", "brand_new.jpg"], message_id=212))

    assert len(calls) == 2, "只命中旧图的字节身份就把新图吞了"


def test_multiple_photos_all_sent_by_bytes_are_skipped():
    first = _pattern(15)
    second = _pattern(16)
    service, _store, _gate, calls = _service(
        {
            "https://img.example/p1.jpg": first,
            "https://img.example/p2.jpg": second,
            # 换一组文件名、顺序也反了，但字节相同
            "https://img.example/q1.jpg": second,
            "https://img.example/q2.jpg": first,
        }
    )

    _run(service, _image_event(["p1.jpg", "p2.jpg"], message_id=221))
    _run(service, _image_event(["q1.jpg", "q2.jpg"], message_id=222))

    assert len(calls) == 1


# ══════════════════════════════════════════════════════════
# 文件名字段不是哈希：按真实字节认
# ══════════════════════════════════════════════════════════
def test_bytes_identity_dedups_when_filename_is_not_hash_like():
    data = _pattern(9)
    service, _store, _gate, calls = _service(
        {
            "https://img.example/cache1.image": data,
            "https://img.example/cache2.image": data,
        }
    )

    _run(service, _image_event(["cache1.image"], message_id=301))
    _run(service, _image_event(["cache2.image"], message_id=302))

    assert len(calls) == 1, "文件名不是哈希时没有按字节认图"


def test_download_failure_falls_back_to_identity_dedup():
    """取不到图片字节时不能误杀：仍然按消息身份去重。"""
    service, _store, gate, calls = _service({})  # 下载全部失败

    _run(service, _image_event(["missing.jpg"], message_id=401))
    _run(service, _image_event(["missing.jpg"], message_id=401))

    assert len(calls) == 1
    assert gate.loads, "应该尝试过下载"


# ══════════════════════════════════════════════════════════
# 开关
# ══════════════════════════════════════════════════════════
def test_perceptual_can_be_disabled():
    original = _pattern(12)
    again = _reencode(original)
    service, _store, _gate, calls = _service(
        {
            "https://img.example/AAAA.jpg": original,
            "https://img.example/bbbb.jpg": again,
        },
        config={"perceptual": False},
    )

    _run(service, _image_event(["AAAA.jpg"], message_id=501))
    _run(service, _image_event(["bbbb.jpg"], message_id=502))

    assert len(calls) == 2, "关掉感知兜底后应该回到旧的重复搬运行为"


@pytest.mark.parametrize("distance", [0, 2, 4])
def test_store_matches_reencoded_hashes_within_threshold(distance):
    """重新编码后的指纹距离都在阈值内（阈值默认 4）。"""
    from media.image_hash import hash_distance, image_hash
    from bot.services.image_dedup import perceptual_identity

    original = _pattern(10)
    again = _reencode(original, size=(160, 160), quality=50)
    first = perceptual_identity(image_hash(original))
    second = perceptual_identity(image_hash(again))
    assert hash_distance(first[6:], second[6:]) <= distance


def test_store_fuzzy_match_is_per_chat():
    from bot.services.image_dedup import perceptual_identity
    from media.image_hash import image_hash

    store = ForwardImageStore(db_path=":memory:")
    original = _pattern(11)
    again = _reencode(original)
    store.remember([perceptual_identity(image_hash(original))], ["g:20002"])

    matched = store.matched_phash(
        ["g:20002", "g:20003"],
        [perceptual_identity(image_hash(again))],
    )
    assert matched.get("g:20002") == {0}
    assert "g:20003" not in matched
    store.close()

