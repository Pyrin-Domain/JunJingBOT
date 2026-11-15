# -*- coding: utf-8 -*-
"""从群历史把「聊天记录」重新入库 —— 只登记，不转发。

为什么需要它
------------
入库清洗（丢装扮卡片 / 末尾广告）是后加的，它会改写 ``content_hash``。清洗上线
**之前**入库的行存的是「带广告」的哈希，而这批行的原文已经找不回来了（日志只覆盖
bot 启动之后的判定，更早的只留在库里）。唯一能把它们对齐的办法，是拿真实聊天记录
按今天的口径重算一遍。

数据源
------
协议端的 ``get_group_msg_history``。SnowLuma 的游标是 ``message_id``、单页最多 200；
NapCat 的游标是 ``message_seq``、单页别超过 100（超时返回空）。这些差异在
:class:`~AbstractionLayer.kernal.OneBotKernel` 的能力档案里，这里只翻页。

口径必须一致
------------
入库的指纹**必须和搬运闸门算得一模一样**，否则这条记录将来还是匹配不上。所以这里
不另写一套，直接复用 :class:`~bot.services.forward.ForwardService` 的私有流程：

    _expand_forward_for_dedup() → _clean_for_dedup() → build_fingerprint()
    → weak 时补图片双哈希 / 视频指纹

用法
----
    # 先看会写什么（不落库）
    python -m bot.services.dedup_ingest --days 60 --dry-run

    # 真写
    python -m bot.services.dedup_ingest --days 60

    # 历史覆盖确认完整之后，再删掉「没有原文」的老行（默认不动）
    python -m bot.services.dedup_ingest --days 60 --delete-legacy

注意：``--delete-legacy`` 删的是**还原不出原文**的行。历史没覆盖到的内容一旦删掉，
下次出现就会被当成新内容再搬一次 —— 所以这个开关要和「历史深度确认过」一起用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from AbstractionLayer.kernal import OneBotKernel

from .dedup import ForwardDedupStore, build_fingerprint, fingerprint_of, tokenize
from .forward import ForwardService

__all__ = ["HistoryIngestor", "main"]

#: 默认协议端（用户另开的那个号：uin 1013098110，历史比 bot 那个号完整）
DEFAULT_URL = "http://127.0.0.1:3600/"
#: 监听的来源群（见 config/bot_config.json 的 auto_forward.groupid_list）
DEFAULT_GROUPS = (
    1054955587,
    973208344,
    949950852,
    933109193,
    1126988159,
    740726382,
    1079845768,
)
#: 写进库的群标记（target_groupid）—— 这几个群「已经发过了」
DEFAULT_TARGETS = (1079845768, 1054955587, 933109193)
#: bot 自己（3122793026）搬出去的卡片不用再入库：内容一样，但会白花展开的 API 调用
DEFAULT_BOT_UIN = 3122793026
#: SnowLuma 单页上限 200；NapCat 超过 100 会超时，交给 kernel 的能力档案夹
PAGE_SIZE = 200


def _log(message: str) -> None:
    print(message, flush=True)


class HistoryIngestor:
    """把一段群历史里的聊天记录写进指纹库。"""

    def __init__(
        self,
        *,
        kernel: OneBotKernel,
        store: ForwardDedupStore,
        service: ForwardService,
        targets: tuple[str, ...] | list[str] = DEFAULT_TARGETS,
        bot_uin: int = DEFAULT_BOT_UIN,
        concurrency: int = 3,
        dry_run: bool = False,
    ) -> None:
        self.kernel = kernel
        self.store = store
        self.service = service
        self.targets = [str(g) for g in targets]
        self.bot_uin = int(bot_uin or 0)
        self.dry_run = dry_run
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        #: 展开过的 forward id：同一份内容常被多个人、多个群重复转发，展开一次就够
        self._seen_ids: set[str] = set()
        #: 库里已有的 content_hash。重跑（幂等补存档）时用它跳过图片/视频哈希 ——
        #: 那些哈希早就存在 forward_image / forward_video 里了，再算一遍纯浪费时间，
        #: 而下载过期图片正是整段入库最慢的一环。
        self._known_hashes: set[str] = self._load_known_hashes()
        self.stats: dict = {
            "messages": 0,
            "cards": 0,
            "recorded": 0,
            "dup_card": 0,
            "expand_fail": 0,
            "empty_fp": 0,
            "no_media": 0,
            "self_skip": 0,
            "errors": 0,
            "image_ok": 0,
            "image_fail": 0,
            "media_skipped": 0,
            "video_ok": 0,
            "video_fail": 0,
            "card_kinds": {},
            "media_errors": {},
        }

    def _load_known_hashes(self) -> set[str]:
        try:
            conn = self.store._ensure_conn()
            return {str(row[0]) for row in conn.execute(
                "SELECT content_hash FROM forward_fingerprint"
            ) if row[0]}
        except Exception:
            return set()

    # ── 统计小工具 ──
    def _bump(self, key: str, amount: int = 1) -> None:
        self.stats[key] = int(self.stats.get(key, 0)) + amount

    def _bump_reason(self, bucket: str, reason: str, amount: int = 1) -> None:
        table = self.stats.setdefault(bucket, {})
        table[reason or "unknown"] = int(table.get(reason or "unknown", 0)) + amount

    # ── 单张卡 ──
    async def ingest_card(self, event: dict, kind: str) -> str:
        """把一张聊天记录卡片写进库，返回分类结果（用于统计 / 排查）。"""
        forward_id = ""
        for seg in event.get("message") or []:
            if isinstance(seg, dict) and seg.get("type") == "forward":
                forward_id = str((seg.get("data") or {}).get("id") or "")
                break
        if forward_id and forward_id in self._seen_ids:
            return "dup_card"
        if forward_id:
            self._seen_ids.add(forward_id)
        async with self._semaphore:
            return await self._ingest_card_inner(event, kind)

    async def _ingest_card_inner(self, event: dict, kind: str) -> str:
        messages, _info = await self.service._expand_forward_for_dedup(event, kind)
        if not messages:
            return "expand_fail"
        # 清洗前留一份：存档要的是「原始内容」，这样以后清洗规则再变，
        # 拿它重算就行，不用回头再爬一遍群（老库就吃过这个亏）。
        raw_messages = messages
        messages, _clean = await self.service._clean_for_dedup(messages, event)
        fingerprint = build_fingerprint(event, kind, forward_messages=messages)
        if not fingerprint["text"]:
            return "empty_fp"

        images: list[str] = []
        videos: list[dict] = []
        image_cnt, video_cnt = self.service._count_media_segments(messages)
        text_hash = ""
        try:
            text_hash = fingerprint_of(tokenize(fingerprint["text"]))["content_hash"]
        except Exception:
            text_hash = ""
        known = bool(text_hash) and text_hash in self._known_hashes
        if known and fingerprint.get("weak"):
            # 这份内容已经在库里了：媒体哈希存在 forward_image / forward_video，
            # 重算一遍只会多下几次图（过期图还要等超时）。直接跳过，只补存档。
            self._bump("media_skipped")
        elif fingerprint.get("weak"):
            if image_cnt > 0 or video_cnt == 0:
                images, report = await self.service._fetch_forward_image_hashes(
                    event, kind, messages=messages
                )
                if images:
                    self._bump("image_ok")
                else:
                    self._bump("image_fail")
                    for item in (report or {}).get("items") or []:
                        self._bump_reason(
                            "media_errors", f"image:{(item or {}).get('reason') or '?'}"
                        )
                    return "no_media"
            if video_cnt > 0:
                videos, report = await self.service._fetch_forward_video_hashes(
                    event, kind, messages=messages
                )
                if videos:
                    self._bump("video_ok")
                else:
                    self._bump("video_fail")
                    for item in (report or {}).get("items") or []:
                        self._bump_reason(
                            "media_errors", f"video:{(item or {}).get('reason') or '?'}"
                        )
                    return "no_media"

        if self.dry_run:
            return "recorded"  # dry-run 也记成"会写"，好让数字可比

        await self.store.record(
            fingerprint["text"],
            kind=kind,
            group_ids=self.targets,
            preview=fingerprint.get("preview", ""),
            weak=bool(fingerprint.get("weak")),
            images=images,
            videos=videos,
            at=int(event.get("time") or 0) or None,
        )
        # 原始内容存档（按 content_hash 去重，同一份内容只占一份空间）。
        # 失败了不影响入库本身 —— 存档是「以后能重算」的保险，不是必需条件。
        try:
            if await self.store.save_corpus(
                fingerprint["text"],
                kind,
                raw_messages,
                at=int(event.get("time") or 0) or None,
            ):
                self._bump("corpus_new")
            else:
                self._bump("corpus_dup")
        except Exception as exc:  # noqa: BLE001
            self._bump("corpus_fail")
            _log(f"[入库] 存档失败（message_id={event.get('message_id')}）：{exc}")
        return "recorded"

    # ── 翻页 ──
    async def scan_group(
        self,
        group_id: int,
        *,
        days: int = 60,
        max_pages: int = 2000,
        on_page=None,
    ) -> dict:
        """从最新一条往回翻，直到 ``days`` 天前或历史到头。"""
        cutoff = int(time.time()) - int(days) * 86400
        caps = self.kernel.capabilities
        cursor = None
        pages = 0
        oldest = 0
        started = time.time()
        while pages < max_pages:
            params: dict = {
                "group_id": group_id,
                "count": min(PAGE_SIZE, caps.history_max_count),
                "reverse_order": True,
            }
            if cursor:
                params[caps.history_cursor] = cursor
            result = await self.kernel.call("get_group_msg_history", params)
            if not result.ok:
                _log(f"[入库] {group_id} 翻页失败：{result.error or result.status}")
                break
            batch = result.messages
            if not batch:
                if pages == 0:
                    _log(f"[入库] {group_id} 拿不到历史（账号可能不在群里）")
                break
            pages += 1
            # 锚点会被原样带回，按 message_id 剔掉自己
            fresh = [m for m in batch if m.get("message_id") != cursor]
            if not fresh:
                break
            oldest = int(fresh[0].get("time") or 0)
            self._bump("messages", len(fresh))
            await self._consume(fresh)
            if oldest and oldest < cutoff:
                break
            cursor = fresh[0].get("message_id")
            if on_page is not None:
                on_page(group_id, pages, oldest, dict(self.stats))
            if time.time() - started > 3600:
                _log(f"[入库] {group_id} 超过 1 小时，先停在这里")
                break
        return {
            "group_id": group_id,
            "pages": pages,
            "oldest": oldest,
            "seconds": round(time.time() - started, 1),
        }

    async def _consume(self, messages: list[dict]) -> None:
        tasks = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if self.bot_uin and str(message.get("user_id")) == str(self.bot_uin):
                self._bump("self_skip")
                continue
            kind = self.service._forward_kind(message)
            if kind not in ("old_forward", "multimsg"):
                continue
            self._bump("cards")
            self._bump_reason("card_kinds", kind)
            tasks.append(self._guarded(message, kind))
        if tasks:
            for outcome in await asyncio.gather(*tasks):
                if outcome and outcome in self.stats:
                    self._bump(outcome)

    async def _guarded(self, message: dict, kind: str) -> str:
        try:
            return await self.ingest_card(message, kind)
        except Exception as exc:  # noqa: BLE001 - 单条失败不影响整段历史
            self._bump("errors")
            _log(f"[入库] 处理失败（message_id={message.get('message_id')}）：{exc}")
            return "errors"


# ---------------------------------------------------------------- 老行清理 ----
def delete_legacy_rows(
    store: ForwardDedupStore, known_hashes: set[str], *, dry_run: bool = False
) -> dict:
    """删掉「还原不出原文」的老行（``kind`` 只有 old_forward / multimsg 受影响）。

    :param known_hashes: 能从日志还原原文的 content_hash 集合 —— 这些行是干净的，
        不在删除范围里。
    """
    conn = store._ensure_conn()
    rows = conn.execute(
        "SELECT id, content_hash, kind FROM forward_fingerprint"
    ).fetchall()
    doomed = [
        int(row["id"])
        for row in rows
        if str(row["kind"]) in ("old_forward", "multimsg")
        and row["content_hash"] not in known_hashes
    ]
    if not doomed or dry_run:
        return {"candidates": len(doomed), "deleted": 0, "dry_run": dry_run}
    marks = ",".join("?" for _ in doomed)
    # 图片 / 视频指纹跟着指纹走，先删子表再删主表
    conn.execute(f"DELETE FROM forward_image WHERE fp_id IN ({marks})", doomed)
    conn.execute(f"DELETE FROM forward_video WHERE fp_id IN ({marks})", doomed)
    conn.executemany(
        "DELETE FROM forward_fingerprint WHERE id = ?", [(i,) for i in doomed]
    )
    conn.commit()
    return {"candidates": len(doomed), "deleted": len(doomed), "dry_run": False}


def _known_hashes(logs: list[Path]) -> set[str]:
    from .dedup import fingerprint_of, tokenize

    found: set[str] = set()
    for path in logs:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                text = (entry.get("fingerprint") or {}).get("text") or ""
                if not text:
                    continue
                try:
                    found.add(fingerprint_of(tokenize(text))["content_hash"])
                except Exception:
                    continue
    return found


# ---------------------------------------------------------------- 组装 / CLI --
def build_ingestor(
    *,
    url: str = DEFAULT_URL,
    token: str = "",
    targets=DEFAULT_TARGETS,
    bot_uin: int = DEFAULT_BOT_UIN,
    concurrency: int = 3,
    dry_run: bool = False,
    backend: str | None = "snowluma",
) -> HistoryIngestor:
    kernel = OneBotKernel.http(url, token, backend=backend, timeout=60.0)
    store = ForwardDedupStore()
    service = ForwardService(
        mp=SimpleNamespace(),
        api=SimpleNamespace(kernel=kernel),
        settings=SimpleNamespace(reply=lambda key, default="": default),
        dedup=store,
    )
    return HistoryIngestor(
        kernel=kernel,
        store=store,
        service=service,
        targets=targets,
        bot_uin=bot_uin,
        concurrency=concurrency,
        dry_run=dry_run,
    )


async def _run(args) -> int:
    ingestor = build_ingestor(
        url=args.url,
        token=args.token,
        targets=args.target or DEFAULT_TARGETS,
        bot_uin=args.bot_uin,
        concurrency=args.concurrency,
        dry_run=args.dry_run,
    )
    groups = args.group or list(DEFAULT_GROUPS)
    _log(
        f"[入库] 目标协议端 {args.url}，群 {groups}，回溯 {args.days} 天，"
        f"写标记 {list(ingestor.targets)}，{'演练' if args.dry_run else '写库'}"
    )
    started = time.time()
    summaries = []
    for group_id in groups:
        summary = await ingestor.scan_group(group_id, days=args.days)
        summaries.append(summary)
        _log(
            f"[入库] {group_id}: {summary['pages']} 页，最早 "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(summary['oldest'])) if summary['oldest'] else '-'}"
            f"，{summary['seconds']}s"
        )
    _log(f"[入库] 总计 {time.time() - started:.0f}s")
    _log("[入库] 统计：" + json.dumps(ingestor.stats, ensure_ascii=False, sort_keys=True))

    if args.delete_legacy:
        logs = [Path(p) for p in args.log] if args.log else []
        known = _known_hashes(logs)
        outcome = delete_legacy_rows(ingestor.store, known, dry_run=args.dry_run)
        _log(f"[入库] 老行清理：{outcome}")
    ingestor.store.close()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="从群历史重新入库聊天记录指纹（只登记，不转发）"
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--token", default="")
    parser.add_argument("--group", type=int, action="append", default=None)
    parser.add_argument("--target", type=int, action="append", default=None)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--bot-uin", type=int, default=DEFAULT_BOT_UIN)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--delete-legacy",
        action="store_true",
        help="删掉还原不出原文的 old_forward / multimsg 老行（历史没覆盖到时别开）",
    )
    parser.add_argument("--log", action="append", default=None, help="判定「有原文」用的日志")
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
