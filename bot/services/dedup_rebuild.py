# -*- coding: utf-8 -*-
"""用排查日志把老指纹升级：SimHash 换算法 / 内容重新清洗入库。

背景：`forward_fingerprint` 里的 simhash / 分桶是**当时那版算法**算出来的。算法一改
（比如给每个词封顶权重，见 forward_dedup.TOKEN_WEIGHT_CAP），老行和新行就不能直接
比距离了，所以判定时只拿 `sim_algo` 相同的行来比。这个脚本把日志里能还原出原文的
老行原地重算一遍，让它们重新参与相似度判定。

日志里没有原文的行（这份日志只覆盖 bot 启动之后的判定）不会被升级，它们也不
会丢：content_hash 精确命中这条路照常走（weak 行另外还要图片确认），只是不参与
模糊相似度。想连它们一起清掉就加 `--purge-stale`（代价是这些内容可能被再搬一次）。

    python -m bot.services.dedup_rebuild --dry-run
    python -m bot.services.dedup_rebuild

另外支持「按新规则重新入库」（``--clean``）：把日志里能还原原文的行，按
:func:`bot.services.dedup.clean_fingerprint_text` 去掉末尾广告 / 占位卡片后重算
content_hash + simhash。同一个哈希撞到一起的两行会合并（群标记取并集、时间取
最早/最晚、命中次数取较大值）。

    python -m bot.services.dedup_rebuild --clean --dry-run
    python -m bot.services.dedup_rebuild --clean
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import re  # noqa: E402

from bot.services.dedup import (  # noqa: E402
    DEFAULT_DB_PATH, DEFAULT_LOG_PATH, SIM_ALGO, ForwardDedupStore, _ensure_columns,
    _line_body, build_fingerprint, clean_fingerprint_text, clean_group_ids,
    fingerprint_of, is_text_sparse_bodies, tokenize,
)

_MEDIA_RE = re.compile(r"\[图片\]|\[视频\]")


def texts_by_content_hash(log_path) -> dict[str, str]:
    """扫描日志，取出「内容哈希 -> 指纹原文」（同一哈希只留第一份）。

    :param log_path: 单个路径，或**多个路径**的列表。多份日志要一起给：这份项目
        自己的日志只覆盖它启动之后的判定，更早的行（以及迁移过来的老行）只在老项目
        的日志里 —— 少给一份就等于那批行永远还原不出原文（实测：老项目的
        ``data/forward_dedup_log.jsonl`` 独家覆盖着 128 行）。
    """
    paths = log_path if isinstance(log_path, (list, tuple, set)) else [log_path]
    found: dict[str, str] = {}
    for path in paths:
        path = Path(path)
        if not path.exists():
            print(f"[重建] 跳过不存在的日志：{path}")
            continue
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # 半行（正好在写）忽略
                text = (entry.get("fingerprint") or {}).get("text") or ""
                if not text:
                    continue
                try:
                    content_hash = fingerprint_of(tokenize(text))["content_hash"]
                except Exception:
                    continue
                found.setdefault(content_hash, text)
    return found


def rebuild_from_log(
    db_path: Path = DEFAULT_DB_PATH,
    log_path: Path = DEFAULT_LOG_PATH,
    dry_run: bool = False,
    purge_stale: bool = False,
) -> dict:
    texts = texts_by_content_hash(log_path)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_columns(conn)
        rows = conn.execute(
            "SELECT id, content_hash, simhash, sim_algo, token_cnt, preview "
            "FROM forward_fingerprint"
        ).fetchall()
        upgraded, current, stale = 0, 0, []
        for row in rows:
            if row["sim_algo"] == SIM_ALGO:
                current += 1
                continue
            text = texts.get(row["content_hash"])
            if not text:
                stale.append(row)
                continue
            fp = fingerprint_of(tokenize(text))
            if not dry_run:
                conn.execute(
                    "UPDATE forward_fingerprint SET simhash = ?, b0 = ?, b1 = ?, "
                    "b2 = ?, b3 = ?, sim_algo = ? WHERE id = ?",
                    (fp["simhash"], fp["b0"], fp["b1"], fp["b2"], fp["b3"],
                     SIM_ALGO, row["id"]),
                )
            upgraded += 1
        purged = 0
        if purge_stale and stale and not dry_run:
            ids = [r["id"] for r in stale]
            conn.executemany("DELETE FROM forward_fingerprint WHERE id = ?",
                             [(i,) for i in ids])
            conn.execute(
                "DELETE FROM forward_image WHERE fp_id NOT IN "
                "(SELECT id FROM forward_fingerprint)"
            )
            purged = len(ids)
        if not dry_run:
            conn.commit()
        return {
            "log_texts": len(texts),
            "rows": len(rows),
            "upgraded": upgraded,
            "already_current": current,
            "stale": len(stale),
            "purged": purged,
            "dry_run": dry_run,
        }
    finally:
        conn.close()


def _split_lines(text: str) -> list[str]:
    """指纹文本里「一条消息一行」的部分（去掉 kind / tsum / source 头）。"""
    parts = str(text or "").split("|")
    idx = 1
    if idx < len(parts) and re.fullmatch(r"tsum\d+", parts[idx] or ""):
        idx += 1
    lines: list[str] = []
    source_seen = False
    for part in parts[idx:]:
        if not part:
            continue
        if part.startswith("[") and part.endswith("]"):
            lines.append(part)
        elif ": " in part or part.endswith(":"):
            lines.append(part)
        elif not source_seen:
            source_seen = True  # 第一个不像行的段是 source
        else:
            lines.append(part)
    return lines


def _merge_into(conn, target_id: int, row, dry_run: bool) -> None:
    """把 row 并进 target_id：群并集、时间取边界、命中取大、媒体挂过去。"""
    if dry_run:
        return
    target = conn.execute(
        "SELECT group_ids, first_seen, last_seen, hit_count FROM forward_fingerprint "
        "WHERE id = ?",
        (target_id,),
    ).fetchone()
    groups = clean_group_ids(
        clean_group_ids(target["group_ids"]) + clean_group_ids(row["group_ids"])
    )
    first_seen = min(
        [v for v in (target["first_seen"], row["first_seen"]) if v] or [0]
    )
    last_seen = max(target["last_seen"] or 0, row["last_seen"] or 0)
    hit_count = max(target["hit_count"] or 0, row["hit_count"] or 0)
    conn.execute(
        "UPDATE forward_fingerprint SET group_ids = ?, first_seen = ?, last_seen = ?, "
        "hit_count = ? WHERE id = ?",
        (",".join(groups), first_seen, last_seen, hit_count, target_id),
    )
    conn.execute("UPDATE forward_image SET fp_id = ? WHERE fp_id = ?", (target_id, row["id"]))
    conn.execute("UPDATE forward_video SET fp_id = ? WHERE fp_id = ?", (target_id, row["id"]))
    conn.execute("DELETE FROM forward_fingerprint WHERE id = ?", (row["id"],))


def rebuild_from_corpus(
    db_path: Path = DEFAULT_DB_PATH, dry_run: bool = False, no_qr: bool = False
) -> dict:
    """用 ``forward_corpus`` 里的原始内容，按**当前**清洗规则重算指纹。

    和 :func:`rebuild_cleaned` 的区别：

    * ``--clean`` 靠日志里的指纹**原文**，只能重跑文本级清洗（去链接、去空行）；
    * 本函数靠入库时存下的**原始 messages**，能重跑整套消息级清洗
      （气泡卡片、二维码海报、群卡片、纯链接），还能顺带重算 weak 判定。

    代价：只有「开始存存档之后」入库的行能重算。老库没有存档，还是得靠
    ``--clean`` + 判定时的别名查询兜底。
    """
    from messaging.forward_filter import clean_forward_messages

    store = ForwardDedupStore(db_path=db_path, settings={"forward_dedup": {}})
    conn = store._ensure_conn()
    conn.row_factory = sqlite3.Row
    # 默认跟线上走同一套规则（含二维码检测），否则重算结果会和库里已有的行对不上：
    # 实测 467 条里有 27 条差异，全是「入库时识别出二维码海报删掉了、重算没删」。
    # 二维码要下载图片，嫌慢可以传 no_qr（代价是那 27 条会退化成带图版本）。
    opts = {k: v for k, v in (store.clean_options or {}).items() if k != "enable"}
    if no_qr:
        opts["drop_qr"] = False
    stats = {
        "corpus": 0, "recalled": 0, "updated": 0, "merged": 0,
        "unchanged": 0, "missing": 0, "skipped_kind": 0, "dry_run": dry_run,
    }
    try:
        rows = conn.execute(
            "SELECT id, content_hash, group_ids, first_seen, last_seen, hit_count "
            "FROM forward_fingerprint"
        ).fetchall()
        by_hash = {row["content_hash"]: int(row["id"]) for row in rows}
        for old_hash, kind, messages in store.iter_corpus():
            stats["corpus"] += 1
            if kind not in ("old_forward", "multimsg"):
                stats["skipped_kind"] += 1
                continue
            try:
                kept, _removed = clean_forward_messages(messages, **opts)
            except Exception:
                kept = messages
            if not kept:
                kept = messages
            fp = build_fingerprint({}, kind, forward_messages=kept)
            if not fp.get("text"):
                stats["unchanged"] += 1
                continue
            new_hash = fingerprint_of(tokenize(fp["text"]))["content_hash"]
            if new_hash == old_hash:
                stats["unchanged"] += 1
                continue
            row_id = by_hash.get(old_hash)
            if row_id is None:
                stats["missing"] += 1  # 这行可能已经被删了（--purge-stale）
                continue
            stats["recalled"] += 1
            target = by_hash.get(new_hash)
            if target is not None and target != row_id:
                if not dry_run:
                    row = conn.execute(
                        "SELECT * FROM forward_fingerprint WHERE id = ?", (row_id,)
                    ).fetchone()
                    _merge_into(conn, target, row, dry_run)
                stats["merged"] += 1
                by_hash.pop(old_hash, None)
                continue
            if not dry_run:
                fresh = fingerprint_of(tokenize(fp["text"]))
                conn.execute(
                    "UPDATE forward_fingerprint SET content_hash = ?, simhash = ?, "
                    "b0 = ?, b1 = ?, b2 = ?, b3 = ?, sim_algo = ?, weak = ?, "
                    "token_cnt = ?, preview = ? WHERE id = ?",
                    (
                        new_hash, fresh["simhash"], fresh["b0"], fresh["b1"],
                        fresh["b2"], fresh["b3"], SIM_ALGO,
                        1 if fp.get("weak") else 0, fresh["token_cnt"],
                        str(fp["text"])[:120], row_id,
                    ),
                )
            stats["updated"] += 1
            by_hash.pop(old_hash, None)
            by_hash[new_hash] = row_id
        if not dry_run:
            conn.commit()
    finally:
        store.close()
    return stats


def rebuild_cleaned(
    db_path: Path = DEFAULT_DB_PATH,
    log_path: Path = DEFAULT_LOG_PATH,
    dry_run: bool = False,
) -> dict:
    """按当前的清洗规则重新入库（只处理日志里能还原原文的行）。"""
    texts = texts_by_content_hash(log_path)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        _ensure_columns(conn)
        rows = conn.execute("SELECT * FROM forward_fingerprint").fetchall()
        by_hash = {row["content_hash"]: row["id"] for row in rows}
        stats = {
            "log_texts": len(texts),
            "rows": len(rows),
            "cleaned": 0,
            "updated": 0,
            "merged": 0,
            "unchanged": 0,
            "no_text": 0,
            "dry_run": dry_run,
        }
        for row in rows:
            old_text = texts.get(row["content_hash"])
            if not old_text:
                stats["no_text"] += 1
                continue
            new_text = clean_fingerprint_text(old_text)
            if new_text == old_text:
                stats["unchanged"] += 1
                continue
            stats["cleaned"] += 1
            fp = fingerprint_of(tokenize(new_text))
            lines = _split_lines(new_text)
            bodies = [_line_body(line) for line in lines]
            media = len(_MEDIA_RE.findall("|".join(lines)))
            weak = is_text_sparse_bodies(bodies, media)
            target_id = by_hash.get(fp["content_hash"])
            if target_id is not None and target_id != row["id"]:
                _merge_into(conn, target_id, row, dry_run)
                stats["merged"] += 1
                continue
            if not dry_run:
                conn.execute(
                    "UPDATE forward_fingerprint SET content_hash = ?, simhash = ?, "
                    "b0 = ?, b1 = ?, b2 = ?, b3 = ?, sim_algo = ?, weak = ?, "
                    "token_cnt = ?, preview = ? WHERE id = ?",
                    (
                        fp["content_hash"],
                        fp["simhash"],
                        fp["b0"],
                        fp["b1"],
                        fp["b2"],
                        fp["b3"],
                        SIM_ALGO,
                        1 if weak else 0,
                        len(tokenize(new_text)),
                        new_text[:120],
                        row["id"],
                    ),
                )
            by_hash[fp["content_hash"]] = row["id"]
            stats["updated"] += 1
        if not dry_run:
            conn.commit()
        return stats
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用日志把老 SimHash 指纹升级成新版算法")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument(
        "--log",
        action="append",
        default=None,
        help="日志路径，可重复给（默认本项目日志；老项目的日志也一起给才能还原更早的行）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--purge-stale", action="store_true",
                    help="把日志里没有原文、升不了级的老行一并删掉（会丢它们的群标记）")
    ap.add_argument("--clean", action="store_true",
                    help="按新的清洗规则重新入库（去掉末尾广告/占位卡片后重算指纹）")
    ap.add_argument("--from-corpus", action="store_true",
                    help="用入库存档（forward_corpus）里的原始内容按当前规则重算，"
                         "能重跑消息级清洗；只覆盖存过存档的行")
    ap.add_argument("--no-qr", action="store_true",
                    help="重算时不下载图片查二维码（快，但末尾二维码海报不会被去掉，"
                         "结果和线上可能有差异）")
    args = ap.parse_args(argv)
    # 本项目日志永远参与（它就是这一个库的历史），--log 只用来**追加**别处的日志
    logs = [Path(str(DEFAULT_LOG_PATH))]
    for extra in args.log or []:
        candidate = Path(extra)
        if all(candidate.resolve() != seen.resolve() for seen in logs):
            logs.append(candidate)

    if args.from_corpus:
        corpus_stats = rebuild_from_corpus(
            Path(args.db), dry_run=args.dry_run, no_qr=args.no_qr
        )
        print(f"存档里有原始内容的指纹：{corpus_stats['corpus']} 份")
        print(
            f"重算结果：改写 {corpus_stats['updated']}，合并 {corpus_stats['merged']}，"
            f"无变化 {corpus_stats['unchanged']}，库里找不到对应行 {corpus_stats['missing']}，"
            f"非转发类跳过 {corpus_stats['skipped_kind']}"
        )
        print("--dry-run：没有写库" if corpus_stats["dry_run"] else "存档重算完成")
        return 0

    if args.clean:
        clean_stats = rebuild_cleaned(Path(args.db), logs, dry_run=args.dry_run)
        print(f"日志里能还原原文的指纹：{clean_stats['log_texts']} 份")
        print(
            f"库里共 {clean_stats['rows']} 行：需要清洗 {clean_stats['cleaned']}，"
            f"改写 {clean_stats['updated']}，合并 {clean_stats['merged']}，"
            f"无变化 {clean_stats['unchanged']}，日志里没有原文 {clean_stats['no_text']}"
        )
        if clean_stats["dry_run"]:
            print("--dry-run：没有写库")
        else:
            print("清洗后重新入库完成")
        return 0

    stats = rebuild_from_log(Path(args.db), logs,
                             dry_run=args.dry_run, purge_stale=args.purge_stale)
    print(f"日志里能还原原文的指纹：{stats['log_texts']} 份")
    print(f"库里共 {stats['rows']} 行：可升级 {stats['upgraded']}，"
          f"已是新版 {stats['already_current']}，日志里没有原文 {stats['stale']}")
    if stats["purged"]:
        print(f"已清掉老算法残留 {stats['purged']} 行")
    elif stats["stale"]:
        print("（这些行仍可用 content_hash / 图片判定，只是不参与模糊相似度；"
              "要清掉加 --purge-stale）")
    if stats["dry_run"]:
        print("--dry-run：没有写库")
    else:
        print(f"完成（新的 sim_algo = {SIM_ALGO}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
