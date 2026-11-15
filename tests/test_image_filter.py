"""图片过滤门：注册表、菲比暴论上下文 / 版式、标签消息形状。"""

from __future__ import annotations

import asyncio
from io import BytesIO

from bot.services.forward.image_gate import ImageAuditGate
from messaging.image_filter import (
    FilterHit,
    ImageFilterHub,
    ImageProbe,
    RecentCommandTracker,
    image_layout,
    match_phoebe_bullshit,
    normalize_filter_config,
    probes_from_messages,
)


def _layout_bytes(*, ratio: float = 3.0) -> bytes:
    from PIL import Image, ImageDraw

    width = int(600 * ratio)
    image = Image.new("RGB", (width, 600), (110, 80, 100))
    draw = ImageDraw.Draw(image)
    half = width // 2
    draw.rectangle([half, 0, width, 600], fill=(0, 0, 0))
    for y in range(80, 500, 60):
        draw.rectangle([half + 80, y, width - 80, y + 24], fill=(255, 255, 255))
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _probe(**kw) -> ImageProbe:
    base = dict(
        url="https://cdn.example/a.png",
        data=_layout_bytes(),
        source="single_image",
        group_id="740726382",
        sender_id="3792417235",
        sender_name="菲比大人",
        message_id="17321",
        index=0,
    )
    base.update(kw)
    return ImageProbe(**base)


def _phoebe_params() -> dict:
    return {
        "sender_ids": [3792417235],
        "group_ids": [740726382],
        "command": "菲比暴论",
        "command_window": 120,
    }


# ══════════════════ 配置 / 注册表 ══════════════════
def test_normalize_filter_config_defaults_and_modes():
    cfg = normalize_filter_config(
        {
            "enable": True,
            "audit_group_id": 1032125562,
            "single_tag_mode": "FORWARD_REPLY",
            "record_tag_mode": "before",
            "filters": ["nsfw"],
        }
    )
    assert cfg["enable"] is True
    assert cfg["audit_group_id"] == "1032125562"
    assert cfg["single_tag_mode"] == "forward_reply"
    assert cfg["record_tag_mode"] == "before"
    assert cfg["filters"] == ["nsfw"]


def test_registry_only_runs_configured_filters():
    hub = ImageFilterHub(
        settings={
            "image_filter": {
                "enable": True,
                "filters": [{"id": "phoebe_bullshit", "enable": True, "params": _phoebe_params()}],
            }
        }
    )
    hits = hub.evaluate([_probe(context={"recent_text": "菲比暴论", "recent_age": 1})])
    assert [hit.filter_id for hit in hits] == ["phoebe_bullshit"]


def test_registry_disabled_filter_does_not_run():
    hub = ImageFilterHub(
        settings={
            "image_filter": {
                "enable": True,
                "filters": [{"id": "phoebe_bullshit", "enable": False, "params": _phoebe_params()}],
            }
        }
    )
    assert hub.evaluate([_probe(context={"recent_text": "菲比暴论", "recent_age": 1})]) == []


# ══════════════════ 最近命令 / 菲比暴论 ══════════════════
def test_recent_command_tracker_and_context_hit():
    tracker = RecentCommandTracker()
    tracker.note_event({"group_id": 740726382, "message": [{"type": "text", "data": {"text": "菲比暴论"}}]})
    text, age = tracker.recent("740726382")
    assert text == "菲比暴论"
    assert age < 5
    probe = _probe(context={"recent_text": text, "recent_age": age})
    hit = match_phoebe_bullshit(probe, _phoebe_params())
    assert hit is not None and hit.filter_id == "phoebe_bullshit"
    assert hit.label == "菲比暴论"
    assert hit.detail.startswith("command:")


def test_phoebe_layout_fallback():
    probe = _probe(context={})
    hit = match_phoebe_bullshit(probe, _phoebe_params())
    assert hit is not None
    assert hit.detail.startswith("layout:")
    layout = image_layout(probe.data)
    assert layout is not None
    assert layout["ratio"] == 3.0
    assert layout["right_mean"] < layout["left_mean"]


def test_phoebe_rejects_wrong_sender_or_group():
    probe = _probe(sender_id="10001", context={"recent_text": "菲比暴论", "recent_age": 1})
    assert match_phoebe_bullshit(probe, _phoebe_params()) is None
    probe = _probe(group_id="123456", context={"recent_text": "菲比暴论", "recent_age": 1})
    assert match_phoebe_bullshit(probe, _phoebe_params()) is None


def test_phoebe_command_window_expired_but_layout_still_hits():
    probe = _probe(context={"recent_text": "菲比暴论", "recent_age": 999})
    hit = match_phoebe_bullshit(probe, _phoebe_params())
    assert hit is not None and hit.detail.startswith("layout:")


# ══════════════════ NSFW（mock 推理器，不依赖 onnxruntime） ══════════════════
def test_nsfw_skips_without_model_or_data():
    import messaging.image_filter as mod

    # 没有图片字节 / 没配 model_path → 直接放行，不触发模型加载
    assert mod.match_nsfw(_probe(data=None), {"threshold": 0.5}) is None
    assert mod.match_nsfw(_probe(), {"threshold": 0.5}) is None


def test_nsfw_threshold_and_hit_detail(monkeypatch):
    import messaging.image_filter as mod

    calls: list = []

    class _FakeScorer:
        def score_bytes(self, data, params):
            calls.append(params)
            return {"score": 0.87, "verdict": "nsfw", "worst_view": "crop"}

    monkeypatch.setattr(mod, "_get_nsfw_scorer", lambda path, threads, use_subprocess=True, *a: _FakeScorer())
    params = {"threshold": 0.5, "model_path": "D:/fake/model.onnx", "threads": 4}
    hit = mod.match_nsfw(_probe(), params)
    assert hit is not None
    assert hit.filter_id == "nsfw"
    assert hit.label == "色图"
    assert hit.score == 0.87
    assert "score=0.87" in hit.detail
    assert calls == [params]  # params 原样传给推理器


def test_nsfw_below_threshold_no_hit(monkeypatch):
    import messaging.image_filter as mod

    class _FakeScorer:
        def score_bytes(self, data, params):
            return {"score": 0.03, "verdict": "ok", "worst_view": "full"}

    monkeypatch.setattr(mod, "_get_nsfw_scorer", lambda path, threads, use_subprocess=True, *a: _FakeScorer())
    hit = mod.match_nsfw(_probe(), {"threshold": 0.5, "model_path": "D:/fake/model.onnx"})
    assert hit is None


def test_nsfw_scorer_unavailable_disables_filter(monkeypatch):
    """模型缺失 / onnxruntime 未装 → 该过滤安静停用（返回 None）。"""
    import messaging.image_filter as mod

    monkeypatch.setattr(mod, "_get_nsfw_scorer", lambda path, threads, use_subprocess=True, *a: None)
    hit = mod.match_nsfw(_probe(), {"threshold": 0.5, "model_path": "D:/missing.onnx"})
    assert hit is None


# ══════════════════ NSFW 子进程隔离 ══════════════════
# 用真的 Popen + 桩 worker 走完整管道协议（4 字节帧 + JSON），不依赖 onnxruntime。
_SUBPROCESS_STUB = r'''
import json, struct, sys

def _read(fp):
    h = fp.read(4)
    if len(h) < 4:
        return None
    (n,) = struct.unpack(">I", h)
    buf = b""
    while len(buf) < n:
        c = fp.read(n - len(buf))
        if not c:
            return None
        buf += c
    return buf

def _write(fp, payload):
    fp.write(struct.pack(">I", len(payload)))
    fp.write(payload)
    fp.flush()

mode = sys.argv[2].split(":")[-1]   # 借用 model_path 槽位传模式（构造器会拼 "stub:前缀"）
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
_write(stdout, json.dumps({"ready": True}).encode("utf-8"))
if mode == "crash_after_ready":
    sys.exit(1)
while True:
    head = _read(stdin)
    data = _read(stdin)
    if head is None or data is None:
        break
    if mode == "ok":
        _write(stdout, json.dumps(
            {"ok": True, "result": {"score": 0.42, "verdict": "ok", "worst_view": "full"}}
        ).encode("utf-8"))
    elif mode == "err":
        _write(stdout, json.dumps({"ok": False, "error": "boom"}).encode("utf-8"))
    elif mode == "crash_mid_request":
        sys.exit(1)
'''


def _sub_scorer(monkeypatch, mode: str):
    import messaging.image_filter as mod

    monkeypatch.setattr(mod, "_NSFW_WORKER_SRC", _SUBPROCESS_STUB)
    return mod._NsfwSubprocessScorer(f"stub:{mode}", threads=1)


def test_subprocess_scorer_roundtrip(monkeypatch):
    scorer = _sub_scorer(monkeypatch, "ok")
    result = scorer.score_bytes(b"fake-image", {"threshold": 0.5})
    assert result["score"] == 0.42
    # 同一进程复用：第二个请求仍然成功
    again = scorer.score_bytes(b"fake-image-2", {"threshold": 0.5})
    assert again["score"] == 0.42
    scorer._teardown()


def test_subprocess_scorer_business_error_not_counted_as_crash(monkeypatch):
    scorer = _sub_scorer(monkeypatch, "err")
    try:
        scorer.score_bytes(b"fake", {"threshold": 0.5})
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as exc:
        assert "boom" in str(exc)
    assert scorer._consecutive_crashes == 0, "业务级错误不算崩溃"
    assert scorer._proc is not None, "进程还活着，应复用"
    scorer._teardown()


def test_subprocess_scorer_crash_raises_and_respawns(monkeypatch):
    scorer = _sub_scorer(monkeypatch, "crash_after_ready")
    try:
        scorer.score_bytes(b"fake", {"threshold": 0.5})
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as exc:
        assert "崩溃" in str(exc) or "启动失败" in str(exc)
    assert scorer._consecutive_crashes == 1
    assert scorer._proc is None, "崩溃后要清理，下次重新拉起"
    # 换成正常桩：下次调用自动重开子进程并成功，计数清零
    scorer.model_path = "stub:ok"
    result = scorer.score_bytes(b"fake", {"threshold": 0.5})
    assert result["score"] == 0.42
    assert scorer._consecutive_crashes == 0
    scorer._teardown()


def test_subprocess_scorer_dead_after_three_crashes(monkeypatch):
    import messaging.image_filter as mod

    scorer = _sub_scorer(monkeypatch, "crash_after_ready")
    for _ in range(3):
        try:
            scorer.score_bytes(b"fake", {"threshold": 0.5})
        except RuntimeError:
            pass
    assert scorer._dead is True
    # 第 4 次：不再拉子进程，直接抛停用
    try:
        scorer.score_bytes(b"fake", {"threshold": 0.5})
        raise AssertionError("应当抛 RuntimeError")
    except RuntimeError as exc:
        assert "已停用" in str(exc)
    # match_nsfw 对 dead scorer 安静放行（不刷屏）
    monkeypatch.setattr(
        mod, "_get_nsfw_scorer",
        lambda path, threads, use_subprocess=True, *a: scorer,
    )
    assert mod.match_nsfw(_probe(), {"threshold": 0.5, "model_path": "x"}) is None


def test_close_nsfw_scorers_shutdowns_cached_scorer(monkeypatch):
    import messaging.image_filter as mod

    calls = []

    class FakeScorer:
        def shutdown(self):
            calls.append(True)

    key = ("stub", 1, True, False)
    monkeypatch.setitem(mod._NSFW_CACHE, key, FakeScorer())
    mod.close_nsfw_scorers()

    assert calls == [True]
    assert key not in mod._NSFW_CACHE


def test_frame_roundtrip(tmp_path):
    import io

    import messaging.image_filter as mod

    buf = io.BytesIO()
    mod._write_frame(buf, b"hello")
    mod._write_frame(buf, b"")
    mod._write_frame(buf, "图".encode("utf-8"))
    buf.seek(0)
    assert mod._read_frame(buf) == b"hello"
    assert mod._read_frame(buf) == b""
    assert mod._read_frame(buf) == "图".encode("utf-8")
    assert mod._read_frame(buf) is None  # EOF


# ══════════════════ 消息 → probe ══════════════════
def test_probes_from_messages_carry_sender_and_limit():
    messages = [
        {
            "user_id": 10003,
            "message_id": 7,
            "sender": {"user_id": 10003, "nickname": "甲", "card": "卡甲"},
            "message": [{"type": "image", "data": {"url": "https://cdn/1.png"}}],
        },
        {
            "user_id": 10004,
            "message_id": 8,
            "sender": {"user_id": 10004, "nickname": "乙", "card": ""},
            "message": [{"type": "image", "data": {"url": "https://cdn/2.png"}}],
        },
    ]
    probes = probes_from_messages(messages, max_images=1)
    assert len(probes) == 1
    assert probes[0].sender_id == "10003"
    assert probes[0].sender_name == "卡甲"
    assert probes[0].message_id == "7"
    assert probes[0].index == 0


def test_default_max_images_is_unlimited():
    """默认不设张数上限。

    老默认 ``12`` 是 2026-11-11 那批漏判的直接来源：SnowLuma 的嵌套记录单张
    就有 170 条消息 / 500 多张图，第 13 张之后一张没下、一张没判，却被当成
    「安全」放过去。这里锁住「不配就是不截断」，别再被谁悄悄改回去。
    """
    from messaging.image_filter import normalize_filter_config

    assert normalize_filter_config({})["max_images"] == 0
    assert normalize_filter_config(None)["max_images"] == 0

    messages = [
        {
            "user_id": 10003,
            "message_id": 100 + n,
            "message": [
                {"type": "image", "data": {"url": f"https://cdn/{n}.png"}}
            ],
        }
        for n in range(30)
    ]
    assert len(probes_from_messages(messages)) == 30, "默认必须全量，不能被 12 张截断"


# ══════════════════ 审计标签 ══════════════════
class _FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send_group_message(self, group_id, message, timeout=None):
        self.calls.append(("message", group_id, message))
        return {"status": "ok", "data": {"message_id": 9001}}

    async def forward_group_single_msg(self, group_id, message_id):
        self.calls.append(("forward_single", group_id, message_id))
        return {"status": "ok", "data": {"message_id": 7001}}

    async def send_group_forward_msg(self, group_id, messages, timeout=None):
        self.calls.append(("forward_record", group_id, len(messages)))
        return {"status": "ok", "data": {"message_id": 8001}}


def _gate(**config) -> tuple[ImageAuditGate, _FakeAPI]:
    api = _FakeAPI()
    base = {
        "enable": True,
        "audit_group_id": 1032125562,
        "single_tag_mode": "single",
        "record_tag_mode": "reply",
        "block_matched": True,
        "max_tag_images": 3,
    }
    base.update(config)
    gate = ImageAuditGate(api=api, settings={"image_filter": base})
    return gate, api


def _hit(**kw) -> FilterHit:
    return FilterHit(
        filter_id=kw.pop("filter_id", "phoebe_bullshit"),
        label=kw.pop("label", "菲比暴论"),
        probe=kw.pop("probe", _probe(data=b"fake-png", url="https://cdn/a.png")),
        **kw,
    )


def test_publish_event_single_mode_sends_text_then_image():
    gate, api = _gate(single_tag_mode="single")
    asyncio.run(gate.publish_event({"message_id": 1, "group_id": 740726382}, [_hit()]))
    assert [call[0] for call in api.calls] == ["message"]
    _, group_id, message = api.calls[0]
    assert str(group_id) == "1032125562"
    assert message[0]["type"] == "text"
    assert "菲比暴论" in message[0]["data"]["text"]
    assert message[1]["type"] == "image"
    assert message[1]["data"]["file"].startswith("base64://")


def test_publish_event_forward_reply_mode_forwards_then_replies():
    gate, api = _gate(single_tag_mode="forward_reply")
    asyncio.run(gate.publish_event({"message_id": 42, "group_id": 740726382}, [_hit()]))
    assert [call[0] for call in api.calls] == ["forward_single", "message"]
    assert api.calls[0][1:] == ("1032125562", "42")
    message = api.calls[1][2]
    assert message[0] == {"type": "reply", "data": {"id": "7001"}}


def test_publish_record_reply_mode_forwards_then_replies():
    gate, api = _gate(record_tag_mode="reply")
    event = {"message_id": 55, "group_id": 740726382}
    asyncio.run(gate.publish_record(event, [_hit()], nodes=[{"type": "node", "data": {}}]))
    assert [call[0] for call in api.calls] == ["forward_record", "message"]
    message = api.calls[1][2]
    assert message[0] == {"type": "reply", "data": {"id": "8001"}}
    assert "菲比暴论" in message[1]["data"]["text"]


def test_publish_record_before_and_after_order():
    gate, api = _gate(record_tag_mode="before")
    asyncio.run(gate.publish_record({"message_id": 55}, [_hit()], nodes=[{"type": "node"}]))
    assert [call[0] for call in api.calls] == ["message", "forward_record"]

    gate, api = _gate(record_tag_mode="after")
    asyncio.run(gate.publish_record({"message_id": 56}, [_hit(probe=_probe(message_id="56"))], nodes=[{"type": "node"}]))
    assert [call[0] for call in api.calls] == ["forward_record", "message"]


def test_publish_dedupes_same_hit():
    gate, api = _gate(single_tag_mode="single")
    event = {"message_id": 77, "group_id": 740726382}
    hit = _hit(probe=_probe(message_id="77"))
    asyncio.run(gate.publish_event(event, [hit]))
    asyncio.run(gate.publish_event(event, [hit]))
    assert len(api.calls) == 1


def test_file_size_precheck_skips_download(monkeypatch):
    """段里自带 file_size 且超上限 → 下载前就拦（NapCat 有此字段）。"""
    import messaging.image_filter as mod

    calls: list = []

    def _spy_download(url, timeout=8):
        calls.append(url)
        return b"x" * 10

    hub = ImageFilterHub(
        settings={"image_filter": {"enable": True, "max_images": 12, "filters": []}},
        download=_spy_download,
    )
    probe = ImageProbe(url="https://cdn/big.jpg", file_size=30 * 1024 * 1024)
    asyncio.run(hub._download_probes([probe]))
    assert probe.data is None
    assert "超过大小上限" in probe.error and "file_size 预检" in probe.error
    assert calls == [], "预检拦截后不应发起网络下载"


def test_local_cache_beats_file_size_precheck(tmp_path, monkeypatch):
    """file_size 超限但本地缓存有 → 直读磁盘照判（本地直读不花流量）。"""
    import messaging.image_filter as mod

    # 造一个本地缓存文件（md5 命名）
    md5 = "a" * 32
    pic = tmp_path / "nt_qq" / "nt_data" / "Pic" / "2026-11" / "Ori"
    pic.mkdir(parents=True)
    (pic / f"{md5}.jpg").write_bytes(b"local-bytes")

    monkeypatch.setattr(mod, "_pic_cache_roots", lambda: [tmp_path / "nt_qq" / "nt_data" / "Pic" / "2026-11"])
    monkeypatch.setattr(
        mod, "_download_default_ex",
        lambda url, timeout=8, max_bytes=None: pytest.fail("本地命中就不该走网络"),
    )
    hub = ImageFilterHub(
        settings={"image_filter": {"enable": True, "max_images": 12, "filters": []}}
    )
    probe = ImageProbe(
        url="https://cdn/big.jpg", file_id=f"{md5}.jpg", file_size=20 * 1024 * 1024
    )
    asyncio.run(hub._download_probes([probe], max_bytes=8 * 1024 * 1024))
    assert probe.data == b"local-bytes", "本地缓存命中要优先于 file_size 预检"


def test_file_size_precheck_skips_download(monkeypatch):
    """段里自带 file_size 且超上限 → 下载前就拦（NapCat 有此字段）。"""
    import messaging.image_filter as mod

    calls: list = []

    def _spy_download(url, timeout=8):
        calls.append(url)
        return b"x" * 10

    hub = ImageFilterHub(
        settings={"image_filter": {"enable": True, "max_images": 12, "filters": []}},
        download=_spy_download,
    )
    probe = ImageProbe(url="https://cdn/big.jpg", file_size=30 * 1024 * 1024)
    asyncio.run(hub._download_probes([probe]))
    assert probe.data is None
    assert "超过大小上限" in probe.error and "file_size 预检" in probe.error
    assert calls == [], "预检拦截后不应发起网络下载"


def test_local_cache_beats_file_size_precheck(tmp_path, monkeypatch):
    """file_size 超限但本地缓存有 → 直读磁盘照判（本地直读不花流量）。"""
    import messaging.image_filter as mod

    # 造一个本地缓存文件（md5 命名）
    md5 = "a" * 32
    pic = tmp_path / "nt_qq" / "nt_data" / "Pic" / "2026-11" / "Ori"
    pic.mkdir(parents=True)
    (pic / f"{md5}.jpg").write_bytes(b"local-bytes")

    monkeypatch.setattr(mod, "_pic_cache_roots", lambda: [tmp_path / "nt_qq" / "nt_data" / "Pic" / "2026-11"])
    monkeypatch.setattr(
        mod, "_download_default_ex",
        lambda url, timeout=8, max_bytes=None: pytest.fail("本地命中就不该走网络"),
    )
    hub = ImageFilterHub(
        settings={"image_filter": {"enable": True, "max_images": 12, "filters": []}}
    )
    probe = ImageProbe(
        url="https://cdn/big.jpg", file_id=f"{md5}.jpg", file_size=20 * 1024 * 1024
    )
    asyncio.run(hub._download_probes([probe], max_bytes=8 * 1024 * 1024))
    assert probe.data == b"local-bytes", "本地缓存命中要优先于 file_size 预检"


# ══════════════════ 命中即停（short_circuit） ══════════════════
def _sc_hub(settings_extra: dict, downloaded: list, hit_on: set[int]):
    """命中第 hit_on 张图的测试 hub：download 记录 url，自定义过滤器按序号命中。"""
    import messaging.image_filter as mod

    def _download(url, timeout=8):
        downloaded.append(url)
        return b"img-bytes"

    seen: list = []

    def _match(probe, params):
        seen.append(probe.index)
        if probe.index in hit_on:
            return mod.FilterHit(
                filter_id="trigger", label="命中", probe=probe, score=1.0, detail="x"
            )
        return None

    registry = mod.FilterRegistry()
    registry.register(
        mod.FilterSpec(filter_id="trigger", label="触发", match=_match)
    )
    return mod.ImageFilterHub(
        settings={"image_filter": {"enable": True, "max_images": 12, "filters": ["trigger"], **settings_extra}},
        download=_download,
        registry=registry,
    ), seen


def test_short_circuit_stops_downloading_after_hit():
    """命中即停：第 3 张命中 → 最多下载第一批（4 张），第 5 张不碰。"""
    hub, seen = _sc_hub({"short_circuit": True}, downloaded := [], hit_on={2})
    probes = [ImageProbe(url=f"https://cdn/{i}.png", index=i) for i in range(6)]
    asyncio.run(hub._screen_probes(probes, source="chat_record"))
    assert len(downloaded) == 4, f"第一批 4 张下完判完命中即停，实际下载 {len(downloaded)}"
    assert seen == [0, 1, 2, 3], "只判定第一批"


def test_short_circuit_first_image_hit_uses_single_batch_semantics():
    """命中在批次内靠前也照样：批内全下全判（并发 4 是下载效率的折中）。"""
    hub, seen = _sc_hub({"short_circuit": True}, downloaded := [], hit_on={0})
    probes = [ImageProbe(url=f"https://cdn/{i}.png", index=i) for i in range(6)]
    hits = asyncio.run(hub._screen_probes(probes, source="chat_record"))
    assert len(hits) == 1 and len(downloaded) == 4


def test_short_circuit_off_downloads_everything():
    """默认关闭：全量下载全量判定（原行为，审计清单完整）。"""
    hub, seen = _sc_hub({"short_circuit": False}, downloaded := [], hit_on={0})
    probes = [ImageProbe(url=f"https://cdn/{i}.png", index=i) for i in range(6)]
    hits = asyncio.run(hub._screen_probes(probes, source="chat_record"))
    assert len(downloaded) == 6 and len(seen) == 6
    assert len(hits) == 1


def test_short_circuit_no_hit_downloads_all():
    """开了短路但全程无命中 → 照样全量下全量判。"""
    hub, seen = _sc_hub({"short_circuit": True}, downloaded := [], hit_on=set())
    probes = [ImageProbe(url=f"https://cdn/{i}.png", index=i) for i in range(6)]
    hits = asyncio.run(hub._screen_probes(probes, source="chat_record"))
    assert len(downloaded) == 6 and len(seen) == 6 and hits == []


def test_nsfw_confirm_released_returns_manual_hit(monkeypatch):
    """低饱和图复核放行 → manual hit（发审计人工确认，不自动拦截）。"""
    import messaging.image_filter as mod

    class _FakeScorer:
        def score_bytes(self, data, params):
            return {
                "score": 0.9988, "verdict": "nsfw", "worst_view": "full",
                "confirm": {"sat": 0.0, "released": True,
                            "nsfwjs": {"drawings": 0.698, "hentai": 0.299, "porn": 0.001}},
            }

    monkeypatch.setattr(
        mod, "_get_nsfw_scorer",
        lambda path, threads, use_subprocess=True, *a: _FakeScorer(),
    )
    hit = mod.match_nsfw(_probe(), {"threshold": 0.5, "model_path": "D:/fake/m.onnx"})
    assert hit is not None and hit.manual is True
    assert "黑白图待人工" in hit.label
    assert "复核放行" in hit.detail


def test_nsfw_confirm_not_released_blocks(monkeypatch):
    """复核未放行（hentai+porn > drawings）→ 正常拦截，非 manual。"""
    import messaging.image_filter as mod

    class _FakeScorer:
        def score_bytes(self, data, params):
            return {
                "score": 0.676, "verdict": "nsfw", "worst_view": "full",
                "confirm": {"sat": 0.057, "released": False,
                            "nsfwjs": {"drawings": 0.324, "hentai": 0.675, "porn": 0.0}},
            }

    monkeypatch.setattr(
        mod, "_get_nsfw_scorer",
        lambda path, threads, use_subprocess=True, *a: _FakeScorer(),
    )
    hit = mod.match_nsfw(_probe(), {"threshold": 0.5, "model_path": "D:/fake/m.onnx"})
    assert hit is not None and hit.manual is False and hit.label == "色图"


def test_nsfw_confirm_released_returns_manual_hit(monkeypatch):
    """低饱和图复核放行 → manual hit（发审计人工确认，不自动拦截）。"""
    import messaging.image_filter as mod

    class _FakeScorer:
        def score_bytes(self, data, params):
            return {
                "score": 0.9988, "verdict": "nsfw", "worst_view": "full",
                "confirm": {"sat": 0.0, "released": True,
                            "nsfwjs": {"drawings": 0.698, "hentai": 0.299, "porn": 0.001}},
            }

    monkeypatch.setattr(
        mod, "_get_nsfw_scorer",
        lambda path, threads, use_subprocess=True, *a: _FakeScorer(),
    )
    hit = mod.match_nsfw(_probe(), {"threshold": 0.5, "model_path": "D:/fake/m.onnx"})
    assert hit is not None and hit.manual is True
    assert "黑白图待人工" in hit.label
    assert "复核放行" in hit.detail


def test_nsfw_confirm_not_released_blocks(monkeypatch):
    """复核未放行（hentai+porn > drawings）→ 正常拦截，非 manual。"""
    import messaging.image_filter as mod

    class _FakeScorer:
        def score_bytes(self, data, params):
            return {
                "score": 0.676, "verdict": "nsfw", "worst_view": "full",
                "confirm": {"sat": 0.057, "released": False,
                            "nsfwjs": {"drawings": 0.324, "hentai": 0.675, "porn": 0.0}},
            }

    monkeypatch.setattr(
        mod, "_get_nsfw_scorer",
        lambda path, threads, use_subprocess=True, *a: _FakeScorer(),
    )
    hit = mod.match_nsfw(_probe(), {"threshold": 0.5, "model_path": "D:/fake/m.onnx"})
    assert hit is not None and hit.manual is False and hit.label == "色图"
