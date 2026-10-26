"""模型分级 / 429 降级：判断器与回答模型各走各的，主模型限流时换备用模型。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime import QQBotAgent, _INTERJECT_JUDGE_SYSTEM
from agent.config import find_model_entry, model_label, resolve_model_entry
from agent.router import (
    ModelRoute,
    ModelRouter,
    is_quota_error,
    is_rate_limit_error,
)
from config import BotSettings
from web.validation import validate_config


def BotConfig(*, path=None, overrides=None):
    """旧测试构造 BotConfig(path=..., overrides=...) 的兼容包装。"""
    root = Path(path).parent if path is not None else None
    return BotSettings(root=root, overrides=overrides, auto_create=False)


# ── resolve_model_entry ────────────────────────────────────
def test_resolve_model_entry_inherits_base_and_normalizes():
    base = {
        "base_url": "https://main/v1",
        "api_key": "main-key",
        "api_key_env": "MAIN_KEY",
        "model": "main-model",
        "temperature": 0.7,
        "max_tokens": 10240,
    }
    cfg = resolve_model_entry(
        {
            "base_url": " https://judge/v1 ",
            "model": " tiny ",
            "temperature": "0.1",
            "max_tokens": "512",
        },
        base,
    )
    assert cfg["base_url"] == "https://judge/v1"
    assert cfg["model"] == "tiny"
    assert cfg["temperature"] == 0.1
    assert cfg["max_tokens"] == 512
    # 换了服务地址又没填密钥 → 不会把主模型的密钥发到别家
    assert cfg["api_key"] == ""


def test_resolve_model_entry_keeps_base_key_for_same_provider():
    """只换模型名、不换地址：沿用主模型密钥（最常见的小模型分级场景）。"""
    cfg = resolve_model_entry(
        {"model": "tiny"},
        {"api_key": "main-key", "api_key_env": "MAIN_KEY", "model": "main-model",
         "base_url": "https://main/v1", "temperature": 0.7, "max_tokens": 100},
    )
    assert cfg["api_key"] == "main-key"
    assert cfg["base_url"] == "https://main/v1"
    assert cfg["model"] == "tiny"


def test_resolve_model_entry_uses_own_env(monkeypatch):
    monkeypatch.setenv("JUDGE_KEY", "judge-secret")
    cfg = resolve_model_entry(
        {"api_key_env": "JUDGE_KEY", "api_key": ""},
        {"api_key": "main-key", "api_key_env": "MAIN_KEY", "model": "m",
         "base_url": "https://x/v1", "temperature": 0.7, "max_tokens": 100},
    )
    assert cfg["api_key"] == "judge-secret"
    assert cfg["api_key_env"] == "JUDGE_KEY"


def test_model_label_and_find_model_entry():
    models = [
        {"id": "m1", "name": "小模型", "model": "tiny"},
        {"id": "m2", "name": "", "model": "big-model"},
        {"id": "m3", "name": "", "model": "", "base_url": "https://x/v1"},
    ]
    assert model_label(models[0]) == "小模型"
    assert model_label(models[1]) == "big-model"
    assert model_label(models[2]) == "https://x/v1"
    assert find_model_entry(models, "m2")["model"] == "big-model"
    assert find_model_entry(models, "m9") is None
    assert find_model_entry(models, "") is None


# ── 429 判定 ────────────────────────────────────────────────
class _HTTPError(Exception):
    def __init__(self, status_code, message="boom"):
        super().__init__(message)
        self.status_code = status_code


def test_is_rate_limit_error_detects_common_shapes():
    assert is_rate_limit_error(_HTTPError(429))
    assert is_rate_limit_error(Exception("Error code: 429 - rate limit exceeded"))
    assert is_rate_limit_error(Exception("insufficient_quota: You exceeded your quota"))
    assert not is_rate_limit_error(_HTTPError(500))
    assert not is_rate_limit_error(ValueError("bad request"))


def test_is_rate_limit_error_ignores_429_inside_other_numbers():
    """message_id 429123 / 端口 14290 之类只是恰好含 429，不是限流。"""
    assert not is_rate_limit_error(ValueError("invalid message_id 429123"))
    assert not is_rate_limit_error(ValueError("connect failed on port 14290"))


def test_is_quota_error_distinguishes_daily_quota_from_burst_limit():
    """日额度 / 余额用完要冷却很久；普通「这一分钟太挤」只冷却 60 秒。"""
    daily = Exception(
        "Error code: 429 - Rate limit exceeded: free-models-per-day. "
        "Add 10 credits to unlock 1000 free model requests per day"
    )
    burst = Exception("Error code: 429 - rate limit exceeded, please retry shortly")
    assert is_rate_limit_error(daily) and is_quota_error(daily)
    assert is_rate_limit_error(burst) and not is_quota_error(burst)


def test_quota_error_uses_long_cooldown():
    """日额度类 429 冷却到下一个额度周期，避免每分钟白撞一次。"""
    primary = _FakeModel("primary", fail_with=Exception(
        "Error code: 429 - Rate limit exceeded: free-models-per-day"
    ))
    backup = _FakeModel("backup")
    router = _router(primary, backup)
    asyncio.run(router.ainvoke({}))
    # 默认 60s 的 burst 冷却远小于额度冷却
    assert router.cooldown_left(router.routes[0]) > router.cooldown_seconds



# ── ModelRouter ─────────────────────────────────────────────
class _FakeModel:
    def __init__(self, name, *, fail_with=None, content="ok"):
        self.name = name
        self.fail_with = fail_with
        self.content = content
        self.calls = 0

    async def ainvoke(self, _payload):
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return SimpleNamespace(content=self.content, name=self.name)


def _router(*models, **kwargs):
    routes = [ModelRoute(key=f"k{i}", label=m.name, llm=m) for i, m in enumerate(models)]
    return ModelRouter(routes, **kwargs)


def test_router_uses_primary_when_healthy():
    primary, backup = _FakeModel("primary"), _FakeModel("backup")
    router = _router(primary, backup)
    result = asyncio.run(router.ainvoke({}))
    assert result.content == "ok" and primary.calls == 1 and backup.calls == 0


def test_router_falls_back_on_429_and_cools_down():
    primary = _FakeModel("primary", fail_with=_HTTPError(429))
    backup = _FakeModel("backup", content="来自备用")
    router = _router(primary, backup)

    assert asyncio.run(router.ainvoke({})).content == "来自备用"
    assert primary.calls == 1 and backup.calls == 1

    # 冷却期内直接走备用模型，不再先撞一次 429
    assert asyncio.run(router.ainvoke({})).content == "来自备用"
    assert primary.calls == 1 and backup.calls == 2


def test_router_falls_back_on_quota_text_without_status_code():
    primary = _FakeModel("primary", fail_with=Exception("insufficient_quota"))
    backup = _FakeModel("backup")
    assert asyncio.run(_router(primary, backup).ainvoke({})).content == "ok"


def test_router_reraises_non_rate_limit_errors():
    primary = _FakeModel("primary", fail_with=ValueError("bad prompt"))
    backup = _FakeModel("backup")
    with pytest.raises(ValueError):
        asyncio.run(_router(primary, backup).ainvoke({}))
    assert backup.calls == 0


def test_router_raises_last_429_when_all_models_limited():
    primary = _FakeModel("primary", fail_with=_HTTPError(429))
    backup = _FakeModel("backup", fail_with=_HTTPError(429, "still limited"))
    with pytest.raises(_HTTPError):
        asyncio.run(_router(primary, backup).ainvoke({}))
    assert primary.calls == 1 and backup.calls == 1


def test_router_retries_primary_after_cooldown(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("agent.router.time.monotonic", lambda: clock["now"])
    primary = _FakeModel("primary", fail_with=_HTTPError(429))
    backup = _FakeModel("backup")
    router = _router(primary, backup, cooldown_seconds=10)
    asyncio.run(router.ainvoke({}))
    clock["now"] += 11
    asyncio.run(router.ainvoke({}))
    assert primary.calls == 2      # 冷却结束，重新优先试主模型


def test_router_skips_fallback_when_side_effects_happened():
    """工具已经发过消息后遇到 429：不换模型重跑，避免重复发送。"""
    primary = _FakeModel("primary", fail_with=_HTTPError(429))
    backup = _FakeModel("backup")
    router = _router(primary, backup)
    with pytest.raises(_HTTPError):
        asyncio.run(router.ainvoke({}, can_fallback=lambda: False))
    assert backup.calls == 0
    # 但限流状态仍然记录，下一轮会直接走备用
    assert asyncio.run(router.ainvoke({})).content == "ok"
    assert backup.calls == 1


def test_router_uses_agent_target_when_requested():
    llm = _FakeModel("llm")
    agent = _FakeModel("agent", content="带工具")
    route = ModelRoute(key="k", label="primary", llm=llm, agent=agent)
    router = ModelRouter([route])
    assert asyncio.run(router.ainvoke({}, use_agent=True)).content == "带工具"
    assert asyncio.run(router.ainvoke({}, use_agent=False)).content == "ok"
    assert agent.calls == 1 and llm.calls == 1


# ── BotConfig ───────────────────────────────────────────────
def test_agent_routing_defaults(tmp_path):
    cfg = BotConfig(path=tmp_path / "bot_config.json")
    assert cfg.agent_judge_model == ""
    assert cfg.agent_fallback_models == []
    assert cfg.agent_models == []
    assert cfg.agent_model_routing == {
        "models": [],
        "active_model": "",
        "judge_model": "",
        "fallback_models": [],
    }


def test_agent_routing_reads_configured_ids(tmp_path):
    cfg = BotConfig(
        path=tmp_path / "bot_config.json",
        overrides={
            "agent": {
                "models": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}],
                "active_model": "m1",
                "judge_model": "m2",
                "fallback_models": ["m3"],
            }
        },
    )
    assert cfg.agent_judge_model == "m2"
    assert cfg.agent_fallback_models == ["m3"]
    assert [entry["id"] for entry in cfg.agent_models] == ["m1", "m2", "m3"]
    routing = cfg.agent_model_routing
    assert routing["judge_model"] == "m2"
    assert routing["fallback_models"] == ["m3"]


# ── 网页面板校验 ────────────────────────────────────────────
def _base(**agent):
    return {
        "master_qq": ["10001"],
        "heartbeat": {"min_minutes": 10, "max_minutes": 20},
        "agent": {"enable": True, **agent},
    }


def _model(**entry):
    return {"id": "m1", "base_url": "https://api.example.com/v1", "model": "x", **entry}


def test_validate_accepts_judge_and_fallback_models():
    errors, warnings = validate_config(
        _base(
            models=[_model(), _model(id="m2", model="y")],
            active_model="m1",
            judge_model="m2",
            fallback_models=["m2"],
        )
    )
    assert errors == []
    assert not any("judge_model" in item or "fallback_models" in item for item in warnings)


def test_validate_warns_unknown_judge_and_fallback_ids():
    errors, warnings = validate_config(
        _base(models=[_model()], active_model="m1", judge_model="m9", fallback_models=["m8"])
    )
    assert errors == []
    assert any("judge_model" in item for item in warnings)
    assert any("fallback_models" in item for item in warnings)


def test_validate_rejects_bad_judge_and_fallback_types():
    errors, _ = validate_config(_base(models=[_model()], judge_model=1))
    assert errors
    errors, _ = validate_config(_base(models=[_model()], fallback_models="m1"))
    assert errors
    errors, _ = validate_config(_base(models=[_model()], fallback_models=[""]))
    assert errors


def test_validate_accepts_per_model_generation_params():
    errors, _ = validate_config(
        _base(models=[_model(temperature=0.2, max_tokens=512)])
    )
    assert errors == []


def test_validate_rejects_bad_per_model_generation_params():
    errors, _ = validate_config(_base(models=[_model(temperature=3)]))
    assert errors
    errors, _ = validate_config(_base(models=[_model(max_tokens=0)]))
    assert errors


# ── QQBotAgent 接线 ─────────────────────────────────────────
class _StubLLM:
    def __init__(self, verdict="<judge>yes</judge>", label="stub"):
        self.verdict = verdict
        self.label = label
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content=self.verdict)


def test_maybe_interject_uses_judge_router():
    """插话判断走 judge_router，而不是主回答模型。"""
    agent = QQBotAgent.__new__(QQBotAgent)
    judge = _StubLLM("<judge>yes</judge>", "judge")
    agent.judge_router = ModelRouter(
        [ModelRoute(key="judge", label="judge", llm=judge)], role="插话判断器"
    )
    agent.llm = _StubLLM("<judge>no</judge>", "main")   # 不应被调用
    calls = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        return ["插一句"]

    agent.chat = fake_chat
    ctx = SimpleNamespace(group_id="1", render=lambda: "x")
    plan = asyncio.run(agent.maybe_interject(ctx))
    assert plan.act == "join"
    assert plan.messages == ["插一句"]
    assert len(judge.calls) == 1
    assert agent.llm.calls == []
    assert calls[0]["action_check"] is False


def test_judge_falls_back_to_primary_on_429():
    judge = _StubLLM("", "judge")
    primary = _StubLLM("<judge>no</judge>", "primary")

    async def limited(_messages):
        raise _HTTPError(429)

    judge.ainvoke = limited
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.judge_router = ModelRouter(
        [
            ModelRoute(key="judge", label="judge", llm=judge),
            ModelRoute(key="primary", label="primary", llm=primary),
        ],
        role="插话判断器",
    )
    agent.chat = lambda **kwargs: None
    ctx = SimpleNamespace(group_id="1", render=lambda: "x")
    assert asyncio.run(agent.maybe_interject(ctx)) is None   # 备用模型判 no
    assert len(primary.calls) == 1


def test_judge_prompt_is_the_lightweight_one():
    agent = QQBotAgent.__new__(QQBotAgent)
    judge = _StubLLM("<judge>no</judge>")
    agent.judge_router = ModelRouter([ModelRoute(key="j", label="j", llm=judge)])
    ctx = SimpleNamespace(group_id="1", render=lambda: "群聊记录")
    asyncio.run(agent.maybe_interject(ctx))
    system, human = judge.calls[0]
    assert system.content == _INTERJECT_JUDGE_SYSTEM
    assert human.content == "群聊记录"


def test_chat_uses_reply_router_and_falls_back():
    """chat() 走 reply_router：主模型 429 时换备用模型，回复照常返回。"""
    from agent.router import ModelRoute, ModelRouter

    class _AgentModel:
        def __init__(self, content=None, fail=None):
            self.content = content
            self.fail = fail
            self.calls = 0

        async def ainvoke(self, _payload):
            self.calls += 1
            if self.fail is not None:
                raise self.fail
            return {"messages": [{"role": "assistant", "content": self.content}]}

    primary = _AgentModel(fail=_HTTPError(429))
    backup = _AgentModel(content="<output><message>备用模型回答</message></output>")
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.rw_tool = None
    agent._recent_replies = {}
    agent.reply_router = ModelRouter(
        [
            ModelRoute(key="primary", label="primary", llm=primary, agent=primary),
            ModelRoute(key="backup", label="backup", llm=backup, agent=backup),
        ],
        role="回答模型",
    )
    assert asyncio.run(agent.chat("在吗")) == ["备用模型回答"]
    assert primary.calls == 1 and backup.calls == 1


def test_invoke_agent_refuses_fallback_after_side_effect_tool():
    """chat() 里已经发过消息（send_dress 等）再遇到 429：不降级重跑。"""
    import agent.runtime as LanguageModule
    from agent.router import ModelRoute, ModelRouter

    class _AgentModel:
        def __init__(self, fail=None):
            self.fail = fail
            self.calls = 0

        async def ainvoke(self, _payload):
            self.calls += 1
            if self.fail is not None:
                raise self.fail
            return {"messages": [{"role": "assistant", "content": "<output><message>好</message></output>"}]}

    primary = _AgentModel(fail=_HTTPError(429))
    backup = _AgentModel()
    agent = QQBotAgent.__new__(QQBotAgent)
    agent.reply_router = ModelRouter(
        [
            ModelRoute(key="primary", label="primary", llm=primary, agent=primary),
            ModelRoute(key="backup", label="backup", llm=backup, agent=backup),
        ]
    )
    LanguageModule._current_tool_calls_var.set([{"tool": "send_dress", "args": {}}])
    with pytest.raises(_HTTPError):
        asyncio.run(agent._invoke_agent({"messages": []}))
    assert backup.calls == 0

    # 没有副作用工具时照常降级
    LanguageModule._current_tool_calls_var.set([])
    assert asyncio.run(agent._invoke_agent({"messages": []}))["messages"][0]["content"]
    assert backup.calls == 1


# ── 真实初始化：三条模型各就各位 ────────────────────────────
def _routing_config(**overrides):
    return {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-main",
        "model": "main-model",
        "temperature": 0.7,
        "max_tokens": 4096,
        "models": [
            {"id": "m1", "name": "主模型", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-main", "model": "main-model"},
            {"id": "m2", "name": "判断器", "base_url": "https://open.bigmodel.cn/api/paas/v4",
             "api_key": "sk-judge", "model": "glm-4-flash"},
            {"id": "m3", "name": "备用", "base_url": "https://openrouter.ai/api/v1",
             "api_key": "sk-backup", "model": "free"},
        ],
        "active_model": "m1",
        "judge_model": "m2",
        "fallback_models": ["m3"],
        **overrides,
    }


def test_agent_builds_separate_judge_and_reply_routes():
    agent = QQBotAgent(llm_config=_routing_config())
    assert agent.reply_router.labels == ["主模型", "备用"]
    assert agent.judge_router.labels == ["判断器", "主模型"]
    assert agent.llm.model_name == "main-model"
    assert agent.judge_llm.model_name == "glm-4-flash"
    # 主模型仍然带完整工具
    assert agent.agent is not None


def test_agent_judge_follows_primary_when_not_configured():
    agent = QQBotAgent(llm_config=_routing_config(judge_model=""))
    assert agent.judge_router.labels == ["主模型"]
    assert agent.judge_llm is agent.llm


def test_agent_skips_broken_fallback_without_failing():
    """备用模型密钥缺失（ChatOpenAI 初始化报错）时跳过它，主模型照常可用。"""
    config = _routing_config(
        fallback_models=["m3", "m4"],
        models=[
            {"id": "m1", "name": "主模型", "base_url": "https://api.deepseek.com/v1",
             "api_key": "sk-main", "model": "main-model"},
            # 环境变量读不到、api_key 也留空 → ChatOpenAI 初始化会抛错
            {"id": "m4", "name": "坏配置", "base_url": "https://x/v1",
             "api_key_env": "TOTALLY_MISSING_KEY_FOR_TEST", "api_key": "", "model": "x"},
        ],
    )
    agent = QQBotAgent(llm_config=config)
    assert agent.reply_router.labels == ["主模型"]


def test_agent_legacy_config_has_single_route():
    agent = QQBotAgent(llm_config={"model": "deepseek-v4-flash"})
    assert agent.reply_router.labels == ["deepseek-v4-flash"]
    assert agent.judge_router.labels == ["deepseek-v4-flash"]


def test_botmain_passes_routing_to_agent(tmp_path, monkeypatch):
    """BotApp 把 models / judge_model / fallback_models 一起下发给 QQBotAgent。"""
    import agent
    import agent.campus as campus_module
    from bot.app import BotApp

    class _FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    monkeypatch.setattr(agent, "QQBotAgent", _FakeAgent)
    monkeypatch.setattr(campus_module, "CampusAssistant", _FakeAgent)

    bot = BotApp.__new__(BotApp)
    bot.settings = BotConfig(
        path=tmp_path / "bot_config.json",
        overrides={"agent": {
            "models": [{"id": "m1", "model": "a"}, {"id": "m2", "model": "b"}],
            "active_model": "m1",
            "judge_model": "m2",
            "fallback_models": ["m2"],
        }},
    )
    bot.api = bot.mp = bot.media = bot.images = None
    bot._agent = None
    bot.campus_assistant = None
    bot._agent_generation = 0
    import threading
    bot._agent_ready = threading.Event()

    bot._init_agent_bg(generation=0)

    cfg = bot._agent.kwargs["llm_config"]
    assert cfg["judge_model"] == "m2"
    assert cfg["fallback_models"] == ["m2"]
    assert [entry["id"] for entry in cfg["models"]] == ["m1", "m2"]
    assert bot._agent_ready.is_set()


def test_botmain_agent_init_failure_does_not_hang(tmp_path, monkeypatch):
    """模型配置写错时 agent_ready 仍然要置位，否则 @ 消息会一直等。"""
    import agent
    import agent.campus as campus_module
    from bot.app import BotApp
    import threading

    def _boom(**_kwargs):
        raise RuntimeError("missing credentials")

    monkeypatch.setattr(agent, "QQBotAgent", _boom)
    monkeypatch.setattr(campus_module, "CampusAssistant", _boom)

    bot = BotApp.__new__(BotApp)
    bot.settings = BotConfig(path=tmp_path / "bot_config.json")
    bot.api = bot.mp = bot.media = bot.images = None
    bot._agent = None
    bot.campus_assistant = None
    bot._agent_generation = 0
    bot._agent_ready = threading.Event()

    bot._init_agent_bg(generation=0)

    assert bot._agent is None
    assert bot._agent_ready.is_set()
