"""`<thread>` 协议：解析不能漏、裁决默认值必须「留」。

两部分：

1. **解析**（``parse_agent_output``）—— 事故场景：模型把 ``<thread>`` 写在
   ``<output>`` 外面（小模型经常这么干），要是没先摘掉，它会跟着正文一起被当成
   消息发到群里。
2. **裁决**（``resolve_verdict``）—— 模型写了就听模型的，漏写用代码信号兜底。
   兜底默认是「留」：丢错不可逆（用户要细节时模型手上没东西，只能重翻或编）。
"""

import time

from agent.output import normalize_thread, parse_agent_output
from agent.thread_turns import Turn
from agent.thread_verdict import VERDICT_KEEP, VERDICT_NEW, resolve_verdict


# ══════════════════════════════════════════════════════════
# 一、解析：<thread> 绝不能变成发出去的消息
# ══════════════════════════════════════════════════════════
def test_thread_outside_output_is_not_sent():
    """标签写在 <output> 外面时，不能退化成「正文」发进群。

    这是第一版最容易出的事故：``_BLOCK_RE`` 只剥 status/think/output，
    ``<thread>keep</thread>好`` 会被整段当成一条回复。
    """
    parsed = parse_agent_output("<thread>keep</thread>好")

    assert parsed["thread"] == "keep"
    assert parsed["messages"] == ["好"]
    assert "thread" not in "".join(parsed["messages"])


def test_thread_inside_output_is_stripped():
    """写在 <output> 第一行（提示词现在的写法）也要能摘掉。"""
    parsed = parse_agent_output(
        "<output><thread>保留</thread><message>嗯</message></output>"
    )

    assert parsed["thread"] == "keep"
    assert parsed["messages"] == ["嗯"]


def test_thread_defaults_to_empty():
    """没写就是空串 —— 不在解析层替模型做主。"""
    parsed = parse_agent_output("<output><message>在呢</message></output>")

    assert parsed["thread"] == ""
    assert parsed["messages"] == ["在呢"]


def test_thread_value_variants():
    for text, expect in [
        ("新话题", "new"),
        ("换个话题", "new"),
        ("清空", "new"),
        ("保留", "keep"),
        ("继续", "keep"),
        ("接着", "keep"),
        ("嗯？", ""),
    ]:
        assert normalize_thread(text) == expect, text


def test_raw_keeps_original_text():
    """日志要看模型原话，raw 不能被剥干净。"""
    raw = "<thread>新话题</thread><output><message>x</message></output>"
    parsed = parse_agent_output(raw)

    assert parsed["raw"] == raw
    assert parsed["thread"] == "new"


# ══════════════════════════════════════════════════════════
# 二、裁决：模型说了算，漏写时代码兜底
# ══════════════════════════════════════════════════════════
def _turns(*, ago: float = 0.0, count: int = 1) -> list:
    at = time.time() - ago
    return [Turn(human=f"H{i}", ai=f"A{i}", at=at) for i in range(count)]


def test_model_keep_wins_even_after_long_gap():
    """模型明确说「保留」时，不按时间推翻它 —— 它读得到整段对话。"""
    verdict, source = resolve_verdict(
        VERDICT_KEEP,
        turns=_turns(ago=7200),
        user_text="换个话题吧",
        new_after=900,
    )
    assert (verdict, source) == (VERDICT_KEEP, "model")


def test_model_new_wins_even_with_reference_words():
    verdict, source = resolve_verdict(
        VERDICT_NEW, turns=_turns(), user_text="那它呢"
    )
    assert (verdict, source) == (VERDICT_NEW, "model")


def test_no_turns_means_new_segment():
    """本来就没东西可留 —— 走完整上下文那条路。"""
    assert resolve_verdict("", turns=[], user_text="随便") == (
        VERDICT_NEW,
        "default:empty",
    )


def test_long_gap_drops_even_without_model_verdict():
    verdict, source = resolve_verdict(
        "", turns=_turns(ago=1200), user_text="在吗", new_after=900
    )
    assert (verdict, source) == (VERDICT_NEW, "default:gap")


def test_gap_check_can_be_disabled():
    """new_after=0 表示不看时间，只信模型 + 话里的切换语。"""
    verdict, source = resolve_verdict(
        "", turns=_turns(ago=86400), user_text="在吗", new_after=0
    )
    assert verdict == VERDICT_KEEP


def test_explicit_topic_switch_marker_drops():
    for text in ["换个话题", "说点别的吧", "不提这个了", "算了不聊"]:
        verdict, source = resolve_verdict("", turns=_turns(), user_text=text)
        assert (verdict, source) == (VERDICT_NEW, "default:new-topic"), text


def test_followup_words_keep():
    """「挑几句 / 展开说说 / 具体点」—— 用户要的是上一轮的细节，绝不能丢。"""
    for text in ["挑几句", "展开说说", "具体点", "继续说", "给我看看那个"]:
        verdict, source = resolve_verdict("", turns=_turns(), user_text=text)
        assert verdict == VERDICT_KEEP, text
        assert source == "default:reference", text


def test_plain_chatter_keeps_by_default():
    """两可之间选择留：丢错是不可逆的（用户要细节时模型手里没东西）。"""
    assert resolve_verdict("", turns=_turns(), user_text="今天吃啥") == (
        VERDICT_KEEP,
        "default:keep",
    )


def test_unknown_model_value_falls_back_to_code_signal():
    """模型写歪了（比如 <thread>嗯</thread>）不能当成结论。"""
    verdict, source = resolve_verdict("嗯", turns=_turns(), user_text="换个话题")
    assert (verdict, source) == (VERDICT_NEW, "default:new-topic")
