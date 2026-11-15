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

from AbstractionLayer.kernal import NAPCAT, SNOWLUMA
from bot.commands import CommandHandlers
from bot.commands import jm as jm_cmd
from download_system import DownloadService, DownloadStorage
from download_system.providers.jm import JmNotConfigured, normalize_source
from transport.api import ensure_ok


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
    """按真实 ``OneBotAPI`` 的行为回包：明确失败**抛异常**，不返回失败 dict。

    ``send_group_msg`` / ``send_private_msg`` 在能力表的 ``strict_actions``
    里，真实实现会用 ``ensure_ok`` 把 ``retcode != 0`` 转成
    ``OneBotAPIError``。替身如果只返回失败 dict，会把「重试死代码」这种
    问题放过去，所以这里也走同一个校验函数。
    """

    def __init__(self):
        self.private_files = []
        self.group_files = []
        self.calls = 0
        self.responses: list[dict] = []   # 依次返回；用完就回 ok
        self.timeouts = 0                 # >0 时接下来几次抛 TimeoutError
        self.timeouts_seen: list = []
        self.flash_tasks: list = []       # (files, name)
        self.flash_msgs: list = []        # (fileset_id, group_id, user_id)
        self.flash_response: dict = {
            "status": "ok", "retcode": 0, "data": {"fileset_id": "set-1"}
        }
        self.flash_send_response: dict = {
            "status": "ok", "retcode": 0, "data": {"message_id": 0}
        }

    def _next(self, action, **kwargs):
        self.calls += 1
        self.timeouts_seen.append(kwargs.get("timeout"))
        if self.timeouts:
            self.timeouts -= 1
            raise TimeoutError("send_group_msg 在 300 秒内没有响应")
        if self.responses:
            return ensure_ok(action, self.responses.pop(0))
        return {"status": "ok", "retcode": 0, "data": {"message_id": 2}}

    async def send_private_message(self, user_id, message, **kwargs):
        self.private_files.append((user_id, message))
        return self._next("send_private_msg", **kwargs)

    async def send_group_message(self, group_id, message, **kwargs):
        self.group_files.append((group_id, message))
        return self._next("send_group_msg", **kwargs)

    async def create_flash_task(self, files, name=None, timeout=None):
        self.flash_tasks.append((files, name))
        return ensure_ok("create_flash_task", self.flash_response)

    async def send_flash_msg(self, fileset_id, group_id=None, user_id=None, timeout=None):
        self.flash_msgs.append((fileset_id, group_id, user_id))
        return ensure_ok("send_flash_msg", self.flash_send_response)


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


def test_group_rejection_is_retried(tmp_path, monkeypatch):
    """实测故障发生在群聊：``ensure_ok`` 抛出的 OneBotAPIError 同样要重试。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", (0,))
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))
    api.responses = [_reject()]

    result = _run(handlers.jm(_event("/jm 25", message_type="group")))

    assert result is True
    assert api.calls == 2
    assert mp.sent == []
    segment = api.group_files[0][1][0]
    assert segment["type"] == "file"
    # name 由抽象层统一补（OneBotAPI._call_api），命令层只负责给路径。
    assert "name" not in segment["data"]


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


# ─────────────────────── 发送：闪传兜底 ───────────────────────
def _use_caps(api, caps):
    """给 FakeAPI 挂一份能力表（真实实现里在 ``api.kernel.capabilities``）。"""
    api.kernel = type("K", (), {"capabilities": caps})()


def test_file_rejection_falls_back_to_flash(tmp_path, monkeypatch):
    """群文件链路被 -134 拒绝后，改走闪传（独立上传服务），用户仍能收到文件。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", ())
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))
    _use_caps(api, SNOWLUMA)
    api.responses = [_reject()]

    result = _run(handlers.jm(_event("/jm 25", message_type="group")))

    assert result is True
    target = jm_cmd._to_napcat(handlers, tmp_path / "25.pdf")
    assert api.flash_tasks == [([target], "25.pdf")]
    assert api.flash_msgs == [("set-1", 1000, None)]
    assert mp.sent == []           # 兜底成功，不打扰用户


def test_flash_fileset_path_differs_per_backend(tmp_path, monkeypatch):
    """NapCat 的 fileset id 在内核结构里，SnowLuma 在顶层，不能写死。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", ())
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))
    _use_caps(api, NAPCAT)
    api.responses = [_reject()]
    api.flash_response = {
        "status": "ok",
        "retcode": 0,
        "data": {"result": 0, "createFlashTransferResult": {"fileSetId": "set-nc"}},
    }

    result = _run(handlers.jm(_event("/jm 25")))

    assert result is True
    assert api.flash_msgs[0][0] == "set-nc"


def test_flash_failure_reports_both_channels(tmp_path, monkeypatch):
    """两条通道都失败：把文件通道的原因和闪传的原因一起回给用户。"""
    monkeypatch.setattr(jm_cmd, "RETRY_DELAYS", ())
    handlers, mp, api = _handlers(downloads=_FakeDownloads(_pdf(tmp_path)))
    _use_caps(api, SNOWLUMA)
    api.responses = [_reject()]
    api.flash_response = {
        "status": "failed", "retcode": 100, "wording": "flash upload failed"
    }

    result = _run(handlers.jm(_event("/jm 25")))

    assert result is False
    assert "-134" in mp.sent[0][1]
    assert "闪传" in mp.sent[0][1]


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
