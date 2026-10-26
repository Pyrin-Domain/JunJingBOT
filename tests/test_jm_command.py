"""``/jm`` 命令的参数解析与配置接线。

锁住这些边界：

1. 参数解析只认 ``/jm <source>``，脏输入直接拒绝；
2. 私聊没接下载服务 / 被配置关闭 / 缺参数时，回对应提示而不是抛异常；
3. 占位图缺失时 ``DownloadService.download_jm`` **不排队**，直接抛
   ``JmNotConfigured``（避免池子重试把原因埋成 DownloadError）；
4. ``jm`` 已注册到关键词动作和面板校验。
"""

import asyncio
import json
from pathlib import Path

import pytest

from bot.commands import CommandHandlers
from bot.commands import jm as jm_cmd
from download_system import DownloadService, DownloadStorage
from download_system.providers.jm import JmNotConfigured, normalize_source


def _run(coro):
    return asyncio.run(coro)


class FakeMP:
    """最小 MessageProcessor 替身：只记录发出去的文本。"""

    def __init__(self):
        self.sent = []
        self.images = None

    def get_clean_context(self, event, keepAt: bool = False):
        text = ""
        for item in event.get("message") or []:
            if item.get("type") == "text":
                text += (item.get("data") or {}).get("text", "")
        return text

    async def send_message(self, event, message):
        self.sent.append((event.get("message_type"), message))
        return {"message_id": 1}

    async def ensure_connected(self):
        return None


class FakeAPI:
    def __init__(self):
        self.private_files = []
        self.group_files = []
        self.calls = 0
        self.responses: list[dict] = []   # 依次返回；用完就回 ok
        self.timeouts = 0                 # >0 时接下来几次抛 TimeoutError
        self.timeouts_seen: list = []

    def _next(self, **kwargs):
        self.calls += 1
        self.timeouts_seen.append(kwargs.get("timeout"))
        if self.timeouts:
            self.timeouts -= 1
            raise TimeoutError("send_group_msg 在 300 秒内没有响应")
        if self.responses:
            return self.responses.pop(0)
        return {"status": "ok", "retcode": 0, "data": {"message_id": 2}}

    async def send_private_message(self, user_id, message, **kwargs):
        self.private_files.append((user_id, message))
        return self._next(**kwargs)

    async def send_group_message(self, group_id, message, **kwargs):
        self.group_files.append((group_id, message))
        return self._next(**kwargs)


class _FakeDownloads:
    """``DownloadService`` 的替身：直接返回一个已存在的本地文件。"""

    def __init__(self, path):
        self.path = path

    async def download_jm(self, source):
        result = type("R", (), {})()
        result.path = self.path
        result.meta = {"source": source}
        return result


def _handlers(settings=None, downloads=None):
    mp = FakeMP()
    api = FakeAPI()
    handlers = CommandHandlers(
        mp=mp,
        api=api,
        settings=settings if settings is not None else {"jm": {"enable": True}},
        downloads=downloads,
    )
    return handlers, mp, api


def _event(text, *, message_type="private", user_id=123456):
    event = {
        "message_id": 9,
        "message_type": message_type,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}],
    }
    if message_type == "group":
        event["group_id"] = 1000
    return event


class _Settings:
    """带 ``get('a.b')`` 的最小配置对象。"""

    def __init__(self, data):
        self.data = data

    def get(self, key_path, default=None):
        node = self.data
        for part in key_path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


# ─────────────────────── 参数解析 ───────────────────────
@pytest.mark.parametrize(
    "text,expected",
    [
        ("/jm 123456", "123456"),
        ("  /jm abc-1_2  ", "abc-1_2"),
        ("/JM 42", "42"),
        ("/jm", None),
        ("/jmp 123", None),
        ("随便说 /jm 123", None),
    ],
)
def test_parse_jm_arg(text, expected):
    assert jm_cmd.parse_jm_arg(text) == expected


def test_normalize_source_rejects_dirty_input():
    assert normalize_source(" 123456 ") == "123456"
    for bad in ["", "   ", "a b", "../../etc", "x" * 65]:
        with pytest.raises(ValueError):
            normalize_source(bad)


# ─────────────────────── 私聊链路 ───────────────────────
def test_private_without_downloads_reports_not_ready():
    handlers, mp, _ = _handlers(downloads=None)

    result = _run(handlers.jm(_event("/jm 123456")))

    assert result is False
    assert mp.sent == [("private", jm_cmd.NOT_READY)]


def test_private_disabled_by_config():
    handlers, mp, _ = _handlers(settings=_Settings({"jm": {"enable": False}}))

    result = _run(handlers.jm(_event("/jm 123456")))

    assert result is False
    assert mp.sent == [("private", jm_cmd.DISABLED)]


def test_private_missing_arg_returns_usage():
    handlers, mp, _ = _handlers()

    result = _run(handlers.jm(_event("/jm")))

    assert result is False
    assert mp.sent == [("private", jm_cmd.USAGE_HINT)]


def test_missing_placeholder_raises_instead_of_queueing(tmp_path, monkeypatch):
    monkeypatch.setenv("JM_PLACEHOLDER_FILE", str(tmp_path / "nope.jpg"))
    service = DownloadService(DownloadStorage(_tmpdir(), _tmpdir()))

    async def scenario():
        with pytest.raises(JmNotConfigured):
            await service.download_jm("123456")
        return service.stats()

    stats = _run(scenario())
    assert stats["queue_size"] == 0


# ─────────────────────── 发送：超时 / 重试 / 失败回执 ───────────────────────
def _pdf(tmp_path, name="25.pdf"):
    path = tmp_path / name
    path.write_bytes(b"%PDF-1.4\n% test\n")
    return path


def _reject(retcode=100, wording="group file upload failed: code=-134 msg=文件上传失败（-134）"):
    return {"status": "failed", "retcode": retcode, "data": None, "wording": wording}


def test_send_passes_long_timeout(tmp_path, monkeypatch):
    """带文件的发送要放宽超时：实测 8MB 就要 32 秒，默认 30 秒不够。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", ())
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))

    _run(handlers.jm(_event("/jm 25")))

    assert api.timeouts_seen == [jm_cmd.SEND_TIMEOUT]
    assert jm_cmd.SEND_TIMEOUT >= 120


def test_rejected_upload_is_retried_then_succeeds(tmp_path, monkeypatch):
    """-134 是间歇性的：明确拒绝要退避重试，第二次成功就不该打扰用户。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", (0, 0))
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))
    api.responses = [_reject()]

    result = _run(handlers.jm(_event("/jm 25")))

    assert result is True
    assert api.calls == 2          # 第一次被拒 + 重试一次
    assert mp.sent == []           # 成功不发任何提示


def test_rejections_end_with_reason(tmp_path, monkeypatch):
    """重试用尽仍失败：把 QQ 的原话回给用户，不能静默。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", (0, 0))
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))
    api.responses = [_reject() for _ in range(10)]

    result = _run(handlers.jm(_event("/jm 25")))

    assert result is False
    assert api.calls == 3          # 1 次 + len(RETRY_DELAYS) 次重试
    assert len(mp.sent) == 1
    assert "-134" in mp.sent[0][1]


def test_timeout_is_not_retried(tmp_path, monkeypatch):
    """超时不重试：协议端可能还在传，重试会多传一份。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", (0, 0, 0))
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))
    api.timeouts = 1

    result = _run(handlers.jm(_event("/jm 25")))

    assert result is False
    assert api.calls == 1          # 只发了一次
    assert "超时" in mp.sent[0][1]


# ─────────────────────── 配置接线 ───────────────────────
def test_command_is_registered_in_app_config():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config" / "bot_config.json").read_text(encoding="utf-8"))
    actions = [item.get("action") for item in config.get("keyword_actions") or []]
    assert "jm" in actions
    assert config["jm"]["enable"] is True


def test_action_registry_knows_jm():
    from bot.commands.registry import build_action_registry

    handlers, _, _ = _handlers()
    registry = build_action_registry(handlers)
    assert registry.get("jm") is not None


def test_panel_validation_accepts_jm_section():
    """面板保存时 ``jm`` 不能报「未知参数段」，类型错要报错。"""
    from web.validation import validate_config

    root = Path(__file__).resolve().parents[1]
    config = json.loads((root / "config" / "bot_config.json").read_text(encoding="utf-8"))

    errors, warnings = validate_config(config)
    assert not [item for item in errors if "jm" in item]
    assert not [item for item in warnings if "jm" in item]

    broken = dict(config, jm={"enable": "yes"})
    errors, _ = validate_config(broken)
    assert any("jm.enable" in item for item in errors)


def _tmpdir():
    import tempfile

    return tempfile.mkdtemp(prefix="jm-test-")
