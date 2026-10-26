"""协议差异调试器：一个命令跑遍两个后端，看同一件事各自返回什么。

    python -m AbstractionLayer.cli probe   --backend napcat
    python -m AbstractionLayer.cli history --backend snowluma --group 20010 --count 5
    python -m AbstractionLayer.cli recover --backend napcat --group 20010 --from seq:184
    python -m AbstractionLayer.cli forward --backend napcat --id 7700868210009142942
    python -m AbstractionLayer.cli forward --backend snowluma --message 1753375808
    python -m AbstractionLayer.cli diff    --group 20010
    python -m AbstractionLayer.cli call    --backend napcat --action get_login_info
    python -m AbstractionLayer.cli sweep   --group 20010

``diff`` 会把两个后端拉到的同一批消息按全局序号对齐，逐字段标出「一致 / 不一致」，
是排查「为什么换个后端行为就变了」最快的手段。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .cursor import MessageCursor
from .forward import ForwardExpander, extract_forward_id
from .history import HistoryPager
from .kernal import (
    DEFAULT_ENDPOINTS,
    OneBotKernel,
    default_token,
)

# ══════════════════════════════════════════════════════════
# 打印工具
# ══════════════════════════════════════════════════════════
def _short(value: Any, width: int = 22) -> str:
    text = str(value if value is not None else "-")
    text = text.replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "…"


def _seg_types(message: dict) -> str:
    segs = message.get("message") or message.get("content") or []
    return ",".join(str(s.get("type")) for s in segs if isinstance(s, dict)) or "-"


def print_messages(messages: list[dict], *, title: str = "") -> None:
    if title:
        print(title)
    if not messages:
        print("  <空>")
        return
    print(f"  {'#':<3} {'real_seq':>9} {'message_id':>12} {'message_seq':>12}  {'time':<11} {'uid':<11} segs")
    for index, message in enumerate(messages):
        cursor = MessageCursor.from_message(message)
        print(
            f"  {index:<3} {_short(cursor.real_seq, 9):>9} "
            f"{_short(cursor.message_id, 12):>12} {_short(cursor.message_seq, 12):>12}  "
            f"{_short(message.get('time'), 11):<11} {_short(message.get('user_id'), 11):<11} "
            f"{_seg_types(message)}  {_short(message.get('raw_message'), 40)}"
        )


def print_capabilities(kernel: OneBotKernel) -> None:
    caps = kernel.capabilities
    print(f"[{kernel.label}] 后端能力（实测）→ {caps.label} ({caps.id})")
    for key, value in caps.to_dict().items():
        if key in {"id", "label"}:
            continue
        print(f"  {key:<30} {value}")
    if kernel.self_id:
        print(f"  {'self_id':<30} {kernel.self_id}")
    for note in caps.notes:
        print(f"  · {note}")


# ══════════════════════════════════════════════════════════
# 组装 kernel
# ══════════════════════════════════════════════════════════
def build_kernel(args) -> OneBotKernel:
    backend = args.backend
    url = getattr(args, "url", None) or DEFAULT_ENDPOINTS.get(backend, "")
    if not url:
        raise SystemExit(f"未知后端 {backend}，请用 --url 指定地址")
    token = getattr(args, "token", "") or default_token()
    return OneBotKernel.http(
        url,
        token,
        backend=backend,
        timeout=getattr(args, "timeout", 20.0),
    )


def both_kernels(args) -> dict[str, OneBotKernel]:
    kernels = {}
    for name in (args.backends or [name for name in DEFAULT_ENDPOINTS]):
        url = DEFAULT_ENDPOINTS.get(name, "")
        if not url:
            continue
        kernels[name] = OneBotKernel.http(
            url, args.token or default_token(), backend=name, timeout=args.timeout
        )
    return kernels


# ══════════════════════════════════════════════════════════
# 子命令
# ══════════════════════════════════════════════════════════
async def cmd_probe(args) -> int:
    kernel = build_kernel(args)
    caps = await kernel.detect(group_id=args.group, verify_cursor=not args.no_verify)
    print_capabilities(kernel)
    if args.json:
        print(json.dumps(caps.to_dict(), ensure_ascii=False, indent=2))
    return 0


async def cmd_history(args) -> int:
    kernel = build_kernel(args)
    if not args.no_detect:
        await kernel.detect(group_id=args.group)
    pager = HistoryPager(kernel)

    cursor = MessageCursor.parse(args.cursor) if args.cursor else None
    page = await pager.page(
        args.group,
        cursor=cursor,
        backward=not args.forward,
        count=args.count,
    )
    print(f"[{kernel.label}] group={args.group} count={args.count} backward={not args.forward} "
          f"cursor={cursor.describe() if cursor else '-'}")
    print(f"  请求={page.requested} 原始返回={page.raw_count} 有效={len(page.messages)} "
          f"{'截断 ' if page.truncated else ''}{'到头 ' if page.exhausted else ''}")
    if page.error:
        print(f"  !! {page.error}")
    print_messages(page.messages)
    if page.next_cursor:
        print(f"  下一页游标：{page.next_cursor.describe()}")
    return 0


async def cmd_recover(args) -> int:
    """模拟断点恢复：从最新往旧翻，直到命中 --from。"""
    kernel = build_kernel(args)
    if not args.no_detect:
        await kernel.detect(group_id=args.group)
    pager = HistoryPager(kernel)
    stop_at = MessageCursor.parse(args.from_) if args.from_ else None

    print(f"[{kernel.label}] group={args.group} 断点={stop_at.describe() if stop_at else '无'}")
    walk = await pager.walk(
        args.group,
        stop_at=stop_at,
        count=args.count,
        max_pages=args.max_pages,
    )
    print(
        f"  翻了 {walk.pages} 页，命中断点={walk.found} 到头={walk.reached_start} "
        f"待回放={len(walk.messages)}"
        + (f" 原因：{walk.reason}" if walk.reason else "")
    )
    print_messages(walk.messages, title="  断点之后（升序，回放顺序）")
    return 0


async def cmd_forward(args) -> int:
    kernel = build_kernel(args)
    if not args.no_detect:
        await kernel.detect(group_id=args.group)
    expander = ForwardExpander(kernel, max_depth=args.max_depth, max_items=args.max_items)

    if args.message:
        fetched = await kernel.call("get_msg", {"message_id": args.message})
        if not fetched.ok:
            print(f"get_msg({args.message}) 失败：{fetched.error}")
            return 1
        event = fetched.data or {}
        fid = extract_forward_id(event)
        if not fid:
            print(f"消息 {args.message} 不是转发；message 段：{_seg_types(event)}")
            return 1
    else:
        fid = args.id
        event = {}

    print(f"[{kernel.label}] 展开 {fid[:32]}…（depth<={args.max_depth}）")
    expansion = await expander.expand_event(event) if event else await expander.expand(fid)
    print(f"  {expansion.describe()}")
    if expansion.errors:
        for err in expansion.errors[:5]:
            print(f"  !! {err}")
    print(f"  {'#':<3} {'depth':<5} {'via':<7} {'real_seq':>8}  nick / 内容")
    for index, entry in enumerate(expansion.entries):
        message = entry.message
        nick = (message.get("sender") or {}).get("nickname") or message.get("user_id")
        print(
            f"  {index:<3} {entry.depth:<5} {entry.via:<7} "
            f"{_short(MessageCursor.from_message(message).real_seq, 8):>8}  "
            f"{_short(nick, 14)} {_short(message.get('raw_message'), 50)}"
        )
    if args.json:
        print(json.dumps(
            [{"depth": e.depth, "via": e.via, "message_id": e.message.get("message_id")}
             for e in expansion.entries],
            ensure_ascii=False, indent=2,
        ))
    return 0


async def cmd_diff(args) -> int:
    """两个后端拉同一批消息，按全局序号对齐逐字段比对。"""
    kernels = both_kernels(args)
    if not kernels:
        print("没有可用的后端地址")
        return 1

    data: dict[str, list[dict]] = {}
    caps: dict[str, Any] = {}
    for name, kernel in kernels.items():
        detected = await kernel.detect(group_id=args.group)
        caps[name] = detected.to_dict()
        pager = HistoryPager(kernel)
        collected = []
        async for message in pager.iter_messages(
            args.group, count=args.count, limit=args.count
        ):
            collected.append(message)
        data[name] = list(reversed(collected))  # 升序
        print(f"[{name}] 拉到 {len(collected)} 条")

    print("\n── 能力差异 ──")
    keys = sorted({k for item in caps.values() for k in item})
    names = list(caps)
    for key in keys:
        values = [caps[name].get(key) for name in names]
        flag = "  " if len(set(map(str, values))) == 1 else "≠ "
        print(f"  {flag}{key:<32} " + "  ".join(f"{_short(v, 24):<24}" for v in values))

    print("\n── 消息对齐（按全局序号）──")
    by_seq: dict[int, dict[str, dict]] = {}
    for name, messages in data.items():
        for message in messages:
            cursor = MessageCursor.from_message(message)
            if cursor.real_seq is None:
                continue
            by_seq.setdefault(cursor.real_seq, {})[name] = message

    if not by_seq:
        print("  两边都没有全局序号，无法对齐")
        return 0

    print(f"  {'real_seq':>9} " + "  ".join(f"{n+' message_id':>18}" for n in names))
    for seq in sorted(by_seq):
        row = by_seq[seq]
        cells = []
        for name in names:
            message = row.get(name)
            cells.append(
                f"{_short(message.get('message_id'), 18):>18}" if message else f"{'<缺>':>18}"
            )
        print(f"  {seq:>9} " + "  ".join(cells))

    only_in = {name: len([1 for row in by_seq.values() if name in row]) for name in names}
    print(f"\n  各后端命中的全局序号数：{only_in}")
    common = [seq for seq, row in by_seq.items() if len(row) == len(names)]
    print(f"  两边都有的：{len(common)} 条")

    if common:
        print("\n── 字段一致性（两边都有的消息）──")
        fields = ["message_id", "message_seq", "real_seq", "time", "user_id", "raw_message"]
        for field in fields:
            same = sum(
                1
                for seq in common
                if str(by_seq[seq][names[0]].get(field)) == str(by_seq[seq][names[1]].get(field))
            )
            print(f"  {'一致' if same == len(common) else '不同'} {field:<14} {same}/{len(common)}")

        same = sum(
            1
            for seq in common
            if MessageCursor.from_message(by_seq[seq][names[0]]).real_seq
            == MessageCursor.from_message(by_seq[seq][names[1]]).real_seq
        )
        print(f"  {'一致' if same == len(common) else '不同'} "
              f"{'归一化 real_seq':<14} {same}/{len(common)}   ← 抽象层给你的可比字段")
    return 0


async def cmd_sweep(args) -> int:
    """扫参数矩阵：同一组参数分别打到两个后端，看返回条数与锚点位置。"""
    kernels = both_kernels(args)
    print(f"group={args.group}")
    for name, kernel in kernels.items():
        await kernel.detect(group_id=args.group)
        caps = kernel.capabilities
        pager = HistoryPager(kernel)
        print(f"\n[{name}] cursor={caps.history_cursor} max_count={caps.history_max_count}")

        base = await pager.page(args.group, count=8)
        if not base.messages:
            print("  取不到样本消息")
            continue
        anchor = base.messages[len(base.messages) // 2]
        anchor_cursor = MessageCursor.from_message(anchor)
        print(f"  锚点 {anchor_cursor.describe()}")

        print(f"  {'字段':<14}{'rev':<7}{'条数':<6}{'锚点位置':<10}结果")
        for field_name in ("message_seq", "message_id", "real_seq"):
            value = anchor_cursor.native_value(field_name)
            if value in (None, "", 0):
                continue
            for reverse in (True, False):
                params = {
                    "group_id": args.group,
                    "count": 6,
                    "reverse_order": reverse,
                    field_name: value,
                }
                result = await kernel.call("get_group_msg_history", params)
                page = [MessageCursor.from_message(m) for m in result.messages]
                position = next(
                    (i for i, cur in enumerate(page) if cur.matches(anchor_cursor)), -1
                )
                if result.messages:
                    verdict = "✅ 正常翻页" if (
                        position == len(page) - 1 if reverse else position == 0
                    ) else ("⚠️ 被忽略/居中窗口" if position >= 0 else "❌ 锚点不在返回里")
                else:
                    verdict = f"❌ 空返回（{result.error or result.status}）"
                print(
                    f"  {field_name:<14}{str(reverse):<7}{len(result.messages):<6}"
                    f"{position:<10}{verdict}"
                )
    return 0


async def cmd_call(args) -> int:
    kernel = build_kernel(args)
    params = json.loads(args.params) if args.params else {}
    result = await kernel.call(args.action, params)
    print(f"[{kernel.label}] {args.action} {params}")
    print(f"  ok={result.ok} status={result.status!r} retcode={result.retcode} "
          f"error={result.error!r}")
    print(json.dumps(result.data, ensure_ascii=False, indent=2)[: args.limit])
    return 0 if result.ok else 1


# ══════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="AbstractionLayer.cli",
        description="NapCat / SnowLuma 协议差异调试器",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(sp, *, needs_backend: bool = True) -> None:
        if needs_backend:
            sp.add_argument("--backend", default="napcat", help="napcat / snowluma")
        sp.add_argument("--url", default="", help="覆盖默认地址")
        sp.add_argument("--token", default="", help="覆盖默认 token")
        sp.add_argument("--timeout", type=float, default=20.0)
        sp.add_argument("--group", type=int, default=20010, help="群号")
        sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("probe", help="实测后端能力")
    add_common(sp)
    sp.add_argument("--no-verify", action="store_true", help="只按字段形状判定，不发翻页验证")
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("history", help="拉一页历史")
    add_common(sp)
    sp.add_argument("--count", type=int, default=10)
    sp.add_argument("--cursor", default="", help="seq:184 / id:501493501 / raw:184")
    sp.add_argument("--forward", action="store_true", help="往新的方向翻")
    sp.add_argument("--no-detect", action="store_true")
    sp.set_defaults(func=cmd_history)

    sp = sub.add_parser("recover", help="模拟断点恢复")
    add_common(sp)
    sp.add_argument("--from", dest="from_", required=True, help="断点游标：seq:184 / id:501493501")
    sp.add_argument("--count", type=int, default=20)
    sp.add_argument("--max-pages", type=int, default=80)
    sp.add_argument("--no-detect", action="store_true")
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("forward", help="展开合并转发")
    add_common(sp)
    sp.add_argument("--id", default="", help="forward id（顶层）")
    sp.add_argument("--message", default="", help="从这条 message_id 里找 forward id")
    sp.add_argument("--max-depth", type=int, default=3)
    sp.add_argument("--max-items", type=int, default=200)
    sp.add_argument("--no-detect", action="store_true")
    sp.set_defaults(func=cmd_forward)

    sp = sub.add_parser("diff", help="两个后端逐字段对比")
    add_common(sp, needs_backend=False)
    sp.add_argument("--backends", nargs="*", default=list(DEFAULT_ENDPOINTS))
    sp.add_argument("--count", type=int, default=10)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("sweep", help="参数矩阵：哪个字段才真的能翻页")
    add_common(sp, needs_backend=False)
    sp.add_argument("--backends", nargs="*", default=list(DEFAULT_ENDPOINTS))
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("call", help="裸发一个 action")
    add_common(sp)
    sp.add_argument("--action", required=True)
    sp.add_argument("--params", default="", help="JSON")
    sp.add_argument("--limit", type=int, default=4000)
    sp.set_defaults(func=cmd_call)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
