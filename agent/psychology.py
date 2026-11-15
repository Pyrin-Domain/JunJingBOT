"""心理分析用的独立语言模型。

和主对话（``agent.runtime.QQBotAgent``）完全分开：

* **模型可选** —— 面板里可以引用 ``agent.models`` 的某一条，也可以单独填
  base_url / api_key / model；都没填就当作「未配置」，调用方给一句提示即可，
  不会拖累主流程，也不会偷偷用主对话的模型。
* **不建 Agent、不带工具** —— 只是一次（或几次）纯文本 / 多模态调用，
  不参与群聊输出协议，不写任何记忆。
* **图片三档**（配置 ``psychology.image_mode``）：

  ==========  ==========================================================
  ``image``   图片原样发给分析模型（要求它是多模态模型）
  ``ocr``     不发送图片，只把 OCR 文字并进聊天记录
  ``vision``  先用视觉模型逐张写客观描述（「辅助视觉分析」），再把
              描述和分析任务一起交给分析模型——两个模型可以不同
  ==========  ==========================================================

为什么要单独一层：主对话的模型往往是小模型 / 纯文本模型，让它看群里的
长截图既贵又不准；心理分析要的是「看懂了再说话」，模型和图片处理方式
都应该跟主对话解耦。

对外入口：

    from agent.psychology import PsychologyAnalyzer, PsychImage, PsychMaterial
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Optional

from core.logging import get_logger

from .config import find_model_entry, resolve_llm_config, resolve_model_entry
from .prompts import persona_voice_excerpt

log = get_logger("agent.psychology")

try:  # 缺 Pillow 时退回「原样发图」，只是不压缩
    from PIL import Image

    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - 环境没装 Pillow
    Image = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

try:  # 和 runtime 一样：没装依赖时给出可读错误，而不是导入期崩掉
    from langchain_openai import ChatOpenAI
except Exception:  # pragma: no cover - 测试环境可能没有 langchain
    ChatOpenAI = None  # type: ignore[assignment]

try:
    from langchain_core.messages import HumanMessage, SystemMessage
except Exception:  # pragma: no cover
    HumanMessage = SystemMessage = None  # type: ignore[assignment]


__all__ = [
    "IMAGE_MODES",
    "MODE_LABELS",
    "NodeDraft",
    "PsychologyAnalyzer",
    "PsychologyError",
    "PsychologyUnavailable",
    "PsychEntry",
    "PsychImage",
    "PsychMaterial",
    "default_analysis_prompt",
    "default_nodes_prompt",
    "default_vision_prompt",
    "parse_node_lines",
]


#: 图片处理方式（顺序就是面板上的顺序）
IMAGE_MODES = ("image", "ocr", "vision")

MODE_LABELS = {
    "image": "原样发送图片",
    "ocr": "不发送图片（仅 OCR）",
    "vision": "辅助视觉分析",
}


class PsychologyError(RuntimeError):
    """调用模型失败。``str(exc)`` 是能直接回给群里的文案。"""


class PsychologyUnavailable(PsychologyError):
    """没配置模型 / 配置不完整——这类错误不需要重试，也不该刷日志。"""


# ══════════════════════════════════════════════════════════
# 素材
# ══════════════════════════════════════════════════════════
@dataclass
class PsychImage:
    """一张待分析的图片。

    ``data`` 在过滤门下载后才有；``blocked=True`` 表示被图片过滤门命中，
    不发给模型（在结果里只提一句「跳过 N 张」）。
    """

    label: str = ""          # 展示用编号，如 "#2"
    data: bytes | None = None
    url: str = ""
    mime: str = ""
    sender: str = ""
    caption: str = ""        # 图片所在那一行的文本（帮助模型对齐）
    blocked: bool = False
    note: str = ""           # 跳过的原因（过滤命中 / 太大 / 下载失败）


@dataclass
class PsychEntry:
    """聊天记录里的一条消息（构造发送节点用）。

    ``text`` 是**原文渲染**（不含「昵称：」前缀，节点本身会显示发送者），
    图片占位是 ``[图片N]``，回复前缀是 ``<回复消息<...>>``。
    """

    index: int = 0                    # 从 1 开始，和模型输出里的 #编号 对齐
    sender_id: str = ""
    sender_name: str = ""
    time: int = 0
    text: str = ""
    image_indexes: list[int] = field(default_factory=list)  # 指向 material.images（1-based）


@dataclass
class NodeDraft:
    """模型逐条写完的一条：``text`` 可以直接作为发送节点正文。"""

    index: int
    text: str
    from_model: bool = False


@dataclass
class PsychMaterial:
    """一次分析的全部输入：文本记录 + 图片 + 来源说明。"""

    source: str = ""         # reply_record / reply_single / current
    title: str = ""          # 给人看的来源描述（日志 / 提示用）
    transcript: str = ""     # 已渲染的聊天文本（图片写成 [图片 1]）
    images: list[PsychImage] = field(default_factory=list)
    entries: list[PsychEntry] = field(default_factory=list)  # 逐条原文（聊天记录模式）
    skipped: int = 0         # 没进模型的图片数（过滤 / 超限）
    truncated: bool = False  # 记录是否被条数上限截断

    @property
    def usable_images(self) -> list[PsychImage]:
        return [image for image in self.images if not image.blocked and image.data]

    def summary(self) -> str:
        return (
            f"{self.title or self.source}：{len(self.transcript)} 字，"
            f"{len(self.entries)} 条消息，"
            f"图片 {len(self.usable_images)}/{len(self.images)} 张"
            + (f"，另有 {self.skipped} 张未纳入" if self.skipped else "")
        )


# ══════════════════════════════════════════════════════════
# 提示词
# ══════════════════════════════════════════════════════════
def default_analysis_prompt(settings=None) -> str:
    """内置分析提示词。

    「说话方式」从人设文件里抽（和去复读改写复用同一个函数），所以改人设
    会一起影响心理分析的口气；规则部分留在这里，保证输出稳定。
    """
    voice = ""
    try:
        voice = persona_voice_excerpt(settings, max_chars=600)
    except Exception:  # noqa: BLE001 - 人设读不到不影响分析
        voice = ""
    voice_block = f"\n你的说话方式（照这个口气，不要照抄例句）：\n{voice}\n" if voice.strip() else ""
    return f"""你是「君景」，在 QQ 群里待了很久，看人看得挺准。现在有人把一段聊天内容丢给你，让你做一次心理侧写。
{voice_block}
请只根据下面给的材料推断，输出四段，不要用 Markdown 标题、不要用列表符号堆砌：

第一行：一句结论（不超过 40 字，直接说这个人现在是什么状态）。
第二段：情绪与动机——他/她此刻在意的到底是什么；引用材料里的原话作为依据。
第三段：潜台词——字面之外还想让对方听懂什么；如果有明显的防御、试探、示弱或撒娇，指出来。
第四段：怎么接——给两三条具体的回应方向（说人话，不要「多沟通」这种空话）。

边界（必须守住）：
- 只分析这段内容里的表达，不编造对方的身份、职业、现实经历等隐私；
- 不做临床诊断，不给任何精神疾病贴标签；用「可能」「像是在」而不是断言；
- 材料不足时直接说「信息太少」，不要硬编；对方明显在开玩笑就按玩笑读；
- 不复述整段材料，不要输出「根据分析」这类套话。
- 总长度控制在 300~500 字，像在群里说话，不要客服腔。
"""


def default_vision_prompt() -> str:
    """视觉模型的客观描述提示词：只描述，不分析、不评价。"""
    return """你在帮一个心理分析模型「看图」。请客观描述这张图片，供后续分析使用：

1. 图片类型（聊天截图 / 自拍 / 表情包 / 风景 / 其他）与整体构图；
2. 如果是聊天截图：逐条转写可见的对话（谁说的、说了什么、有没有时间或状态栏信息），保留语气词和标点；
3. 画面里人物的表情、姿态、视线方向（如果有）；
4. 任何可能影响理解的细节（红包 / 转账 / 撤回提示 / 特殊状态、深夜时间、备注名等）。

只描述你真实看到的内容，不要推测人物心理、不要评价、不要编造看不清的文字。
看不清的地方写「不清晰」。控制在 300 字以内。"""


def default_nodes_prompt(settings=None) -> str:
    """聊天记录模式：逐条复刻原话，并在后面用 ``<...>`` 补心理侧写。

    和 ``default_analysis_prompt`` 的区别：那个写一段总结，这个**逐条**写，
    输出会直接变成一条聊天记录的节点正文。所以格式约束必须硬：
    行首 ``#编号``、原文保留、分析只写在尖括号里。
    """
    return """你在逐条解读一段 QQ 群聊。输出会直接变成一条聊天记录：每条消息保留原话，后面用 <...> 补上这个人此刻的心理 / 潜台词。

输出格式（必须严格遵守）：
- 一条消息一行，行首写 #编号（编号就是材料里每条前面的编号，不能改、不能少）；
- 有文字的消息：**原样保留文字**，可以保留 @某人 和 <回复消息<...>> 前缀；只在后面追加 <...>；
- 纯图片 / 表情：先用一句短话转述内容（例如「飞机杯照片」「可爱的表情包」），再追加 <...>；
- 分析写在尖括号里，一条最多两句，像看懂了的人在旁边小声点评；
- 不要输出 JSON、不要 Markdown、不要空行、不要解释规则；
- 材料不足或看不懂就写 <没看懂>，不要编造；同一个人不要反复贴同一个标签。

例子：
#1 @chihaya anon 你买的？<想知道是不是对方买的>
#2 不是<否定>
#3 我买杯子干什么 <人家是好男孩，不会一天到晚想着这些事情的>
#4 干牛子咯 <类似于「晚上吃什么」「吃晚饭」的附和>
#5 <回复消息<我买杯子干什么>>
有道理
<开玩笑，附和>

只输出这些行，不要别的内容。"""


# ══════════════════════════════════════════════════════════
# 模型配置
# ══════════════════════════════════════════════════════════
@dataclass
class _ModelConfig:
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    model: str = ""
    temperature: float = 0.5
    max_tokens: int = 900
    source: str = ""  # 面板 / 日志用的来源说明

    @property
    def ready(self) -> bool:
        return bool(self.model.strip() and self.base_url.strip() and self.api_key.strip())

    def key(self) -> tuple:
        return (self.base_url, self.model, self.api_key, self.temperature, self.max_tokens)

    def describe(self) -> str:
        return f"{self.model or '未命名'}" + (f"（{self.source}）" if self.source else "")


def _as_text(value: Any) -> str:
    return str(value or "").strip()


def _as_float(value: Any, default: float, minimum: float = 0.0, maximum: float = 2.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(minimum, min(maximum, number))


def _as_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _resolve_model(section: dict, agent_section: dict, *, defaults: dict) -> _ModelConfig:
    """解析一份模型配置。

    优先级：``model_id`` 指向 ``agent.models`` 的条目 → 否则用本节自己的
    base_url / api_key / model。两者都没有 model 时返回「未配置」，由调用方
    决定是提示还是静默。
    """
    models = agent_section.get("models") if isinstance(agent_section.get("models"), list) else []
    base = resolve_llm_config(agent_section)
    model_id = _as_text(section.get("model_id"))
    entry = find_model_entry(models, model_id)

    if entry is not None:
        resolved = resolve_model_entry(entry, base)
        name = _as_text(entry.get("name"))
        config = _ModelConfig(
            base_url=_as_text(resolved.get("base_url")),
            api_key=_as_text(resolved.get("api_key")),
            model=_as_text(resolved.get("model")),
            api_key_env=_as_text(resolved.get("api_key_env")),
            temperature=_as_float(section.get("temperature"), defaults["temperature"]),
            max_tokens=_as_int(section.get("max_tokens"), defaults["max_tokens"]),
            source=f"agent.models/{model_id}" + (f"「{name}」" if name else ""),
        )
        # 本节的 temperature / max_tokens 优先；没写才用条目里的
        if section.get("temperature") is None:
            config.temperature = _as_float(
                resolved.get("temperature"), defaults["temperature"]
            )
        if section.get("max_tokens") is None:
            config.max_tokens = _as_int(resolved.get("max_tokens"), defaults["max_tokens"])
        return config

    base_url = _as_text(section.get("base_url")) or _as_text(base.get("base_url"))
    api_key_env = _as_text(section.get("api_key_env")) or _as_text(base.get("api_key_env"))
    api_key = _as_text(section.get("api_key"))
    if not api_key and api_key_env:
        api_key = _as_text(os.environ.get(api_key_env))
    if not api_key and base_url == _as_text(base.get("base_url")):
        # 同一家服务商：允许沿用主对话的密钥（换地址不借，和模型分级一致）
        api_key = _as_text(base.get("api_key"))
    return _ModelConfig(
        base_url=base_url,
        api_key=api_key,
        api_key_env=api_key_env,
        model=_as_text(section.get("model")),
        temperature=_as_float(section.get("temperature"), defaults["temperature"]),
        max_tokens=_as_int(section.get("max_tokens"), defaults["max_tokens"]),
        source="独立配置",
    )


# ══════════════════════════════════════════════════════════
# 图片编码
# ══════════════════════════════════════════════════════════
def _mime_of(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _encode_image(data: bytes, *, max_side: int, max_bytes: int) -> tuple[str, bytes] | None:
    """把图片压到「视觉模型够用、请求别太大」的尺寸。

    :return: ``(mime, bytes)``；压缩后仍超限返回 ``None``（这张图不发给模型）。
    """
    if not data:
        return None
    mime = _mime_of(data)
    if not _PIL_AVAILABLE:
        return (mime, data) if len(data) <= max_bytes else None
    try:
        image = Image.open(io.BytesIO(data))
        image.seek(0)  # GIF 只取首帧
        image = image.convert("RGB")
        if max_side > 0 and max(image.size) > max_side:
            image.thumbnail((max_side, max_side))
        for quality in (85, 70, 55):
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality, optimize=True)
            payload = buffer.getvalue()
            if len(payload) <= max_bytes:
                return "image/jpeg", payload
        # 还大：缩到一半再来一轮
        if max(image.size) > 768:
            image.thumbnail((768, 768))
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=70, optimize=True)
            payload = buffer.getvalue()
            if len(payload) <= max_bytes:
                return "image/jpeg", payload
    except Exception as exc:  # noqa: BLE001 - 坏图 / 不支持的格式
        log.warning("[心理分析] 图片编码失败: %s", exc)
        return None
    return None


def _data_url(mime: str, payload: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def _message_text(result: Any) -> str:
    """AIMessage → 纯文本；兼容 content 是 blocks 数组的新版返回。"""
    content = getattr(result, "content", result)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts).strip()
    return str(content or "").strip()


_BAD_OCR_PREFIX = "[OCR"


def _clean_ocr(text: str) -> str:
    text = _as_text(text)
    if not text or text.startswith(_BAD_OCR_PREFIX):
        return ""
    return re.sub(r"\n{3,}", "\n\n", text)


#: 模型按提示词输出时的格式：``#12 正文``
_NODE_STRICT_RE = re.compile(r"^\s*#\s*(\d{1,4})\s*(.*)$")
#: 小模型省掉 # 时的退路：``12. 正文`` / ``12：正文``（必须带分隔符，避免正文里的数字被误判）
_NODE_LOOSE_RE = re.compile(r"^\s*(\d{1,4})\s*[.、:：|｜]\s*(.*)$")


def _format_time(value: Any) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        return ""
    if stamp > 1e12:  # 毫秒
        stamp /= 1000.0
    try:
        return datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_node_lines(raw: str, entries: list[PsychEntry]) -> list[NodeDraft]:
    """把模型的「#编号 正文」解析成每条消息的节点正文。

    * 严格模式只认 ``#编号``；一条都没认出时退回 ``1.`` / ``1：`` 的宽松模式；
    * 非编号行当作上一条的续行（一条消息里可能有换行）；
    * 模型漏掉的条目用原话兜底，保证节点条数与原始记录一致。
    """
    indexes = {entry.index for entry in entries}

    def scan(pattern: re.Pattern[str]) -> tuple[dict[int, list[str]], int]:
        blocks: dict[int, list[str]] = {}
        current: int | None = None
        hits = 0
        for line in str(raw or "").splitlines():
            text = line.rstrip()
            if not text.strip():
                continue
            matched = pattern.match(text)
            if matched is not None:
                index = int(matched.group(1))
                if index not in indexes:
                    # 编号对不上（模型多写了 / 数错）：丢掉这一条及其续行，
                    # 不能当成上一条的续行，否则会把无关内容贴到别人身上
                    current = None
                    continue
                current = index
                blocks.setdefault(index, [])
                body = matched.group(2).strip()
                if body:
                    blocks[index].append(body)
                hits += 1
                continue
            if current is not None:
                blocks[current].append(text.strip())
        return blocks, hits

    blocks, hits = scan(_NODE_STRICT_RE)
    if not hits:
        blocks, hits = scan(_NODE_LOOSE_RE)

    drafts: list[NodeDraft] = []
    for entry in entries:
        body = "\n".join(blocks.get(entry.index) or []).strip()
        drafts.append(NodeDraft(entry.index, body or entry.text, bool(body)))
    return drafts


# ══════════════════════════════════════════════════════════
# 分析器
# ══════════════════════════════════════════════════════════
class PsychologyAnalyzer:
    """心理分析的模型层。

    :param settings: BotSettings（读 ``psychology`` 段与 ``agent.models``）
    :param ocr_text: ``async (url) -> str``；``ocr`` 模式用。缺省时该模式降级
        成「没有图片文字」，不会崩。
    :param chat_factory: 测试注入：``(config_dict) -> 有 ainvoke 的对象``。
    """

    def __init__(
        self,
        settings=None,
        *,
        ocr_text: Optional[Callable[[str], Awaitable[str]]] = None,
        chat_factory: Optional[Callable[[dict], Any]] = None,
    ) -> None:
        self.settings = settings
        self.ocr_text = ocr_text
        self._chat_factory = chat_factory
        self._clients: dict[tuple, Any] = {}
        self._config_cache: Optional[dict] = None

    # ── 配置 ──
    def _section(self) -> dict:
        if self.settings is None:
            return {}
        try:
            section = self.settings.section("psychology")
        except Exception:  # noqa: BLE001 - 老配置 / 测试替身
            section = {}
        return section if isinstance(section, dict) else {}

    def _agent_section(self) -> dict:
        if self.settings is None:
            return {}
        try:
            section = self.settings.section("agent")
        except Exception:  # noqa: BLE001
            return {}
        return section if isinstance(section, dict) else {}

    def reload(self) -> None:
        """配置保存 / 热加载后清掉客户端缓存。"""
        self._clients.clear()
        self._config_cache = None

    @property
    def config(self) -> dict:
        if self._config_cache is not None:
            return self._config_cache
        section = self._section()
        defaults = {"temperature": 0.5, "max_tokens": 900}
        analysis = _resolve_model(section, self._agent_section(), defaults=defaults)
        vision_section = section.get("vision") if isinstance(section.get("vision"), dict) else {}
        vision = _resolve_model(vision_section, self._agent_section(), defaults=defaults)
        mode = _as_text(section.get("image_mode")).lower()
        if mode not in IMAGE_MODES:
            mode = "vision"
        self._config_cache = {
            "enable": bool(section.get("enable")),
            "image_mode": mode,
            "analysis": analysis,
            "vision": vision,
            "max_images": _as_int(section.get("max_images"), 6, 0),
            "image_max_bytes": _as_int(section.get("image_max_bytes"), 4 * 1024 * 1024, 65536),
            "image_max_side": _as_int(section.get("image_max_side"), 1568, 256),
            "analysis_prompt": _as_text(section.get("prompt")) or default_analysis_prompt(self.settings),
            "vision_prompt": _as_text(section.get("vision_prompt")) or default_vision_prompt(),
            "nodes_prompt": _as_text(section.get("nodes_prompt")) or default_nodes_prompt(self.settings),
            "forward_max_tokens": _as_int(section.get("forward_max_tokens"), 2000, 128),
        }
        return self._config_cache

    @property
    def available(self) -> bool:
        return bool(self.config["analysis"].ready)

    def status(self) -> str:
        """一行状态，日志 / 面板排查用。"""
        config = self.config
        analysis = config["analysis"]
        vision = config["vision"]
        mode = config["image_mode"]
        if not analysis.ready:
            missing = [
                name
                for name, value in (
                    ("base_url", analysis.base_url),
                    ("api_key", analysis.api_key),
                    ("model", analysis.model),
                )
                if not value
            ]
            return f"未配置（缺 {'/'.join(missing)}）"
        text = f"{analysis.describe()} · 图片模式 {MODE_LABELS[mode]}"
        if mode == "vision":
            text += f" · 视觉模型 {vision.describe() if vision.ready else analysis.describe()}"
        return text

    # ── 对外 ──
    async def analyze(self, material: PsychMaterial) -> str:
        """跑一次分析，返回一段纯文本侧写。

        抛出 :class:`PsychologyUnavailable`（没配模型）或
        :class:`PsychologyError`（调用失败），文案可直接回群。
        """
        self._require_ready()
        self._require_content(material)
        config = self.config
        parts = await self._content_parts(material, numbered=False)
        return await self._invoke(config["analysis"], config["analysis_prompt"], parts)

    async def analyze_nodes(self, material: PsychMaterial) -> list[NodeDraft]:
        """逐条侧写：返回与 ``material.entries`` 一一对应的节点正文。

        输出会直接变成一条聊天记录的节点，所以失败（模型没按 ``#编号`` 回）
        要抛出可读错误，由业务层退回纯文本，而不是发一条没有分析的记录。
        """
        self._require_ready()
        self._require_content(material)
        if not material.entries:
            raise PsychologyError("这段内容里没有可逐条侧写的消息。")
        config = self.config
        parts = await self._content_parts(material, numbered=True)
        answer = await self._invoke(
            config["analysis"],
            config["nodes_prompt"],
            parts,
            max_tokens=config["forward_max_tokens"],
        )
        drafts = parse_node_lines(answer, material.entries)
        return drafts

    # ── 内部：校验 / 素材 → parts ──
    def _require_ready(self) -> None:
        if not self.config["analysis"].ready:
            raise PsychologyUnavailable(f"心理分析模型还没配好：{self.status()}")

    @staticmethod
    def _require_content(material: PsychMaterial) -> None:
        if not _as_text(material.transcript) and not material.usable_images:
            raise PsychologyError("这段内容里没有可分析的文字或图片。")

    async def _content_parts(self, material: PsychMaterial, *, numbered: bool) -> list[Any]:
        """三档图片策略的唯一入口：文本模式 / 逐条模式共用同一套素材处理。"""
        config = self.config
        limit = config["max_images"]
        images = material.usable_images[:limit] if limit > 0 else []
        transcript = self._numbered_transcript(material) if numbered else material.transcript
        mode = config["image_mode"]
        parts: list[Any] = [
            {
                "type": "text",
                "text": self._user_prompt(
                    material, transcript, with_image_hint=(mode == "image")
                ),
            }
        ]

        if mode == "ocr":
            block = await self._ocr_block(images)
            if block:
                parts[0]["text"] += f"\n\n【图片里的文字（OCR）】\n{block}"
            return parts
        if mode == "vision":
            described = await self._describe_images(images)
            if described:
                parts[0]["text"] += f"\n\n【图片内容描述（辅助视觉分析）】\n{described}"
            return parts

        # image：图片作为 image_url 附在文本后面
        attached: list[PsychImage] = []
        for image in images:
            encoded = _encode_image(
                image.data or b"",
                max_side=config["image_max_side"],
                max_bytes=config["image_max_bytes"],
            )
            if encoded is None:
                continue
            mime, payload = encoded
            parts.append(
                {"type": "image_url", "image_url": {"url": _data_url(mime, payload)}}
            )
            attached.append(image)
        if attached:
            labels = "、".join(
                f"{image.label}（{image.sender or '未知'}）" for image in attached
            )
            parts[0]["text"] += (
                f"\n随消息附上 {len(attached)} 张图片，顺序为：{labels}，"
                "请按编号对应材料里的 [图片N]。"
            )
        return parts

    @staticmethod
    def _numbered_transcript(material: PsychMaterial) -> str:
        """给逐条模式用的「#编号 + 发送者 + 原文」清单。"""
        if not material.entries:
            return material.transcript
        lines: list[str] = []
        for entry in material.entries:
            when = _format_time(entry.time)
            prefix = f"[{when}] " if when else ""
            lines.append(
                f"#{entry.index} {prefix}{entry.sender_name or '未知'}：{entry.text}"
            )
        return "\n".join(lines)

    # ── 内部：素材 → 提示词 ──
    def _user_prompt(self, material: PsychMaterial, transcript: str, *, with_image_hint: bool = False) -> str:
        head = material.title or "群聊内容"
        lines = [f"材料来源：{head}"]
        if material.truncated:
            lines.append("（内容较长，只截取了前面一部分）")
        if material.skipped:
            lines.append(f"（有 {material.skipped} 张图片没有纳入分析）")
        lines.append("")
        lines.append("材料：")
        lines.append(transcript or "（没有文字）")
        if with_image_hint and material.usable_images:
            lines.append("")
            lines.append(
                "图片顺序：" + "、".join(
                    f"{image.label}来自{image.sender or '未知'}" for image in material.usable_images
                )
            )
        return "\n".join(lines)

    async def _ocr_block(self, images: list[PsychImage]) -> str:
        if not images or self.ocr_text is None:
            return ""
        texts: list[str] = []
        for image in images:
            if not image.url:
                continue
            try:
                text = _clean_ocr(await self.ocr_text(image.url))
            except Exception as exc:  # noqa: BLE001 - OCR 失败不能挡住分析
                log.warning("[心理分析] OCR 失败（%s）: %s", image.label, exc)
                continue
            if text:
                texts.append(f"{image.label}（{image.sender or '未知'}）：{text}")
        return "\n".join(texts)

    async def _describe_images(self, images: list[PsychImage]) -> str:
        config = self.config
        vision = config["vision"] if config["vision"].ready else config["analysis"]
        if not images:
            return ""
        semaphore = asyncio.Semaphore(2)

        async def one(image: PsychImage) -> str:
            encoded = _encode_image(
                image.data or b"",
                max_side=config["image_max_side"],
                max_bytes=config["image_max_bytes"],
            )
            if encoded is None:
                return ""
            mime, payload = encoded
            async with semaphore:
                try:
                    text = await self._invoke(
                        vision,
                        config["vision_prompt"],
                        [
                            {
                                "type": "text",
                                "text": f"这是第 {image.label} 张图（来自 {image.sender or '未知'}）。"
                                + (f"同一行的文字：{image.caption}" if image.caption else ""),
                            },
                            {"type": "image_url", "image_url": {"url": _data_url(mime, payload)}},
                        ],
                        max_tokens=500,
                    )
                except PsychologyError as exc:
                    log.warning("[心理分析] 图片描述失败（%s）: %s", image.label, exc)
                    return ""
            return f"{image.label}（{image.sender or '未知'}）：{text}" if text else ""

        results = await asyncio.gather(*(one(image) for image in images))
        return "\n\n".join(text for text in results if text)

    # ── 内部：调用 ──
    def _build_client(self, config: _ModelConfig) -> Any:
        if self._chat_factory is not None:
            return self._chat_factory(
                {
                    "base_url": config.base_url,
                    "api_key": config.api_key,
                    "model": config.model,
                    "temperature": config.temperature,
                    "max_tokens": config.max_tokens,
                }
            )
        if ChatOpenAI is None:
            raise PsychologyUnavailable("未安装 langchain-openai，无法调用心理分析模型")
        key = config.key()
        client = self._clients.get(key)
        if client is None:
            client = ChatOpenAI(
                model=config.model,
                api_key=config.api_key,
                base_url=config.base_url,
                temperature=float(config.temperature),
                max_tokens=int(config.max_tokens),
            )
            self._clients[key] = client
        return client

    async def _invoke(
        self,
        config: _ModelConfig,
        system_prompt: str,
        parts: list[Any],
        *,
        max_tokens: int | None = None,
    ) -> str:
        if not config.ready:
            raise PsychologyUnavailable(f"心理分析模型还没配好：{self.status()}")
        client = self._build_client(config)
        if max_tokens is not None:
            # 逐条侧写的输出比一段总结长得多：按调用方给的上限临时压/放
            try:
                client = client.bind(max_tokens=int(max_tokens))
            except Exception:  # noqa: BLE001 - 不支持 bind 的替身对象
                pass
        content: Any = parts
        if len(parts) == 1 and isinstance(parts[0], dict) and parts[0].get("type") == "text":
            content = parts[0]["text"]
        messages = [SystemMessage(content=system_prompt), HumanMessage(content=content)]
        try:
            result = await client.ainvoke(messages)
        except Exception as exc:  # noqa: BLE001 - 全部转成可读文案
            from .router import is_rate_limit_error

            if is_rate_limit_error(exc):
                raise PsychologyError(
                    "心理分析模型被限流了（429），过一会儿再试或者换一条模型。"
                ) from exc
            text = str(exc)
            if len(text) > 180:
                text = text[:180] + "…"
            raise PsychologyError(f"心理分析模型调用失败：{text}") from exc
        answer = _message_text(result)
        if not answer:
            raise PsychologyError("心理分析模型没有返回内容。")
        return answer
