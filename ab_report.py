"""按实验臂统计主模型的缓存命中：``python ab_report.py [日志路径]``

只认带标记的行：

    [LLM][group_583261953|on] prompt 15482 tokens，缓存命中 0（0%）

- ``group_…|on``  = 实验组（今天开了多轮留存）
- ``group_…|off`` = 对照组（今天没开）
- ``judge|``      = 插话判断器（GLM），**不是主模型**，统计时剔除

为什么要按天交替
----------------
网关的缓存热不热是日级噪声（同一群一天里能从 0% 跳到 92%，见 2026-11-14 的日志）。
按天翻转能把这种噪声平均分到两组；按群分组做不到，因为群和群本来就差很多。

看什么
------
``加权命中`` 按 token 算，最接近账单；``≥50% 的请求`` 看的是「有多少次真的吃到了」。
两边样本少于 ~100 次时，别急着下结论。
"""

from __future__ import annotations

import collections
import re
import statistics
import sys

LINE_RE = re.compile(
    r"\[LLM\]\[(?P<sess>[^|\]]*)\|(?P<arm>[^\]]*)\] "
    r"prompt (?P<tok>\d+) tokens，缓存命中 (?P<hit>\d+)（(?P<pct>\d+)%）"
)


def load(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path, encoding="utf-8", errors="ignore") as handle:
        for line in handle:
            match = LINE_RE.search(line)
            if not match:
                continue
            item = match.groupdict()
            if item["sess"] == "judge":
                continue
            item["tok"] = int(item["tok"])
            item["hit"] = int(item["hit"])
            item["pct"] = int(item["pct"])
            rows.append(item)
    return rows


def report(name: str, rows: list[dict]) -> None:
    if not rows:
        return
    tokens = sum(r["tok"] for r in rows)
    hit = sum(r["hit"] for r in rows)
    pcts = [r["pct"] for r in rows]
    good = sum(1 for p in pcts if p >= 50)
    print(
        f"{name:<18} {len(rows):>4} 次 | 加权 {hit / max(1, tokens) * 100:>5.1f}%"
        f" | 中位 {statistics.median(pcts):>5.1f}%"
        f" | ≥50% {good:>3}/{len(rows):<4} | prompt 合计 {tokens:>9,}"
    )


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "logs/bot.log"
    rows = load(path)
    if not rows:
        print(
            f"{path} 里没有带臂标记的 [LLM][会话|arm] 行。\n"
            "说明这份日志是加标记之前跑的，或者 Bot 还没重启。"
        )
        return

    print(f"来源：{path}（已剔除 judge 调用）\n")
    print(f"{'分组':<18} {'样本':>4}    {'命中':>6}      {'中位':>6}   {'命中过半':>9}")
    report("实验组 arm=on", [r for r in rows if r["arm"] == "on"])
    report("对照组 arm=off", [r for r in rows if r["arm"] == "off"])

    print("\n按会话：")
    by_session: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        by_session[f"{row['sess']}|{row['arm']}"].append(row)
    for key in sorted(by_session, key=lambda k: -len(by_session[k])):
        report(key, by_session[key])


if __name__ == "__main__":
    main()
