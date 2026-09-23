# -*- coding: utf-8 -*-
"""用排查日志把老指纹升级成新版 SimHash 算法。

背景：`forward_fingerprint` 里的 simhash / 分桶是**当时那版算法**算出来的。算法一改
（比如给每个词封顶权重，见 forward_dedup.TOKEN_WEIGHT_CAP），老行和新行就不能直接
比距离了，所以判定时只拿 `sim_algo` 相同的行来比。这个脚本把日志里能还原出原文的
老行原地重算一遍，让它们重新参与相似度判定。

日志里没有原文的行（这份日志只覆盖 bot 启动之后的判定）不会被升级，它们也不
会丢：content_hash 精确命中这条路照常走（weak 行另外还要图片确认），只是不参与
模糊相似度。想连它们一起清掉就加 `--purge-stale`（代价是这些内容可能被再搬一次）。

    python Minitor/forward_dedup_rebuild.py --dry-run
    python Minitor/forward_dedup_rebuild.py
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from forward_dedup import (  # noqa: E402
    DEFAULT_DB_PATH, DEFAULT_LOG_PATH, SIM_ALGO, _ensure_columns, fingerprint_of,
    tokenize,
)


def texts_by_content_hash(log_path: Path) -> dict[str, str]:
    """扫描日志，取出「内容哈希 -> 指纹原文」（同一哈希只留第一份）。"""
    found: dict[str, str] = {}
    if not log_path.exists():
        return found
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用日志把老 SimHash 指纹升级成新版算法")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--log", default=str(DEFAULT_LOG_PATH))
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写库")
    ap.add_argument("--purge-stale", action="store_true",
                    help="把日志里没有原文、升不了级的老行一并删掉（会丢它们的群标记）")
    args = ap.parse_args(argv)

    stats = rebuild_from_log(Path(args.db), Path(args.log),
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
