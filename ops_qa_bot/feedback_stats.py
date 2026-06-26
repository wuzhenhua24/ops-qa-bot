"""feedback.log 离线统计：一条命令出"满意率 / 被踩问题 / 升级率 / 成本"报表。

把 README「反馈日志分析」一节里那组 jq 命令固化下来，降低真正去看数据的摩擦。
纯 stdlib、只读日志文件，不依赖服务进程在跑。将来若要做"周报推群"，cron +
本模块 + 一次 send_post 即可，聚合逻辑直接复用。

用法：
    uv run python -m ops_qa_bot.feedback_stats                # 近 7 天
    uv run python -m ops_qa_bot.feedback_stats --days 30      # 近 30 天
    uv run python -m ops_qa_bot.feedback_stats --days 0       # 全量
    uv run python -m ops_qa_bot.feedback_stats --log /path/to/feedback.log

用量口径以**纯 token 数**为主（订阅 / coding plan 套餐没有严格的 token 单价，
美元数没意义）；只有显式传了 --price-*（$/1M tokens）才追加美元估算行。
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

# 默认单价（$/1M tokens），与 README 示例一致；按实际代理价格用 --price-* 覆盖
DEFAULT_PRICES = {
    "input": 3.0,
    "output": 15.0,
    "cache_read": 0.3,
    "cache_write": 3.75,
}


def parse_log(path: Path) -> list[tuple[str, dict]]:
    """逐行解析 feedback.log → [(日期 'YYYY-MM-DD', 事件 dict), ...]。

    每行格式是 "时间戳前缀 + JSON"；前缀里取前 10 个字符当日期。脏行
    （无 JSON / 解析失败 / 没有 event 字段）静默跳过，统计工具不该因为
    一行损坏就罢工。
    """
    events: list[tuple[str, dict]] = []
    try:
        f = path.open("r", encoding="utf-8")
    except OSError as e:
        raise SystemExit(f"读不到日志文件 {path}: {e}")
    with f:
        for line in f:
            brace = line.find("{")
            if brace < 0:
                continue
            try:
                obj = json.loads(line[brace:])
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or "event" not in obj:
                continue
            events.append((line[:10], obj))
    return events


def filter_days(
    events: list[tuple[str, dict]], days: int, today: date
) -> list[tuple[str, dict]]:
    """保留近 N 天（含今天）的事件；days=0 表示全量。日期前缀解析不了的行
    仅在全量模式下保留。"""
    if days <= 0:
        return events
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    return [(d, e) for d, e in events if d >= cutoff]


def aggregate(events: list[tuple[str, dict]], prices: dict) -> dict:
    """聚合所有事件 → 报表数据 dict（render 与未来的周报推送共用）。"""
    qa_by_qid: dict[str, dict] = {}
    days_counter: Counter = Counter()
    s = {
        "qa_total": 0,
        "clarifications": 0,
        "cancelled": 0,
        "escalated_qa": 0,
        "escalated_ticket": 0,
        "drift": 0,
        # open_id 泄漏兜底（[[project-open-id-leak-in-body-scrub]]）：LLM 把负责人
        # open_id 当文字写进正文。scrub_rounds = 被剥过的轮次（含正常答完只清理的）；
        # promoted = 其中"答不上来 + 点名在册负责人"被救回成正经升级 @ 的轮次（⊆ scrub_rounds，
        # 也 ⊆ escalated_qa）。两者都高说明 prompt 那条"别在正文写 open_id"没拦住。
        "open_id_scrub_rounds": 0,
        "open_id_promoted": 0,
        "max_turns_hit": 0,
        "images_answers": 0,
        "up": 0,
        "down": 0,
        "down_items": [],          # [{qid, question, reasons}]
        "reason_counter": Counter(),
        "archives": 0,
        "archive_paths": Counter(),
        "archive_had_draft": 0,
        "archive_edited": 0,
        "followup_clicks": Counter(),
        "followup_cancels": 0,
        "cost_sdk": 0.0,
        "tokens": Counter(),       # input/output/cache_read/cache_write
        "active_users": set(),
        "days": days_counter,
    }
    down_reasons: dict[str, list[str]] = {}

    for day, e in events:
        ev = e.get("event")
        if ev == "qa":
            s["qa_total"] += 1
            days_counter[day] += 1
            if qid := e.get("qid"):
                qa_by_qid[qid] = e
            if uid := e.get("user_id"):
                s["active_users"].add(uid)
            if e.get("clarification"):
                s["clarifications"] += 1
            if e.get("escalated_to"):
                if e.get("escalation_kind") == "ticket":
                    s["escalated_ticket"] += 1
                else:
                    s["escalated_qa"] += 1
            if e.get("escalate_drift_fallback"):
                s["drift"] += 1
            if e.get("open_ids_scrubbed"):
                s["open_id_scrub_rounds"] += 1
            if e.get("escalate_from_leaked_open_id"):
                s["open_id_promoted"] += 1
            if e.get("max_turns_hit"):
                s["max_turns_hit"] += 1
            if e.get("images_attached"):
                s["images_answers"] += 1
            if isinstance(e.get("cost_usd"), (int, float)):
                s["cost_sdk"] += e["cost_usd"]
            usage = e.get("usage") or {}
            s["tokens"]["input"] += usage.get("input_tokens") or 0
            s["tokens"]["output"] += usage.get("output_tokens") or 0
            s["tokens"]["cache_read"] += usage.get("cache_read_input_tokens") or 0
            s["tokens"]["cache_write"] += (
                usage.get("cache_creation_input_tokens") or 0
            )
        elif ev == "feedback":
            if e.get("rating") == "up":
                s["up"] += 1
            elif e.get("rating") == "down":
                s["down"] += 1
                s["down_items"].append({"qid": e.get("qid"), "question": None})
        elif ev == "feedback_reason":
            if not e.get("invalid"):
                for label in e.get("reason_labels") or []:
                    s["reason_counter"][label] += 1
                if qid := e.get("qid"):
                    down_reasons.setdefault(qid, []).extend(
                        e.get("reason_labels") or []
                    )
        elif ev == "archive":
            if e.get("duplicate"):
                continue
            s["archives"] += 1
            if p := e.get("path"):
                s["archive_paths"][p] += 1
            if e.get("had_draft"):
                s["archive_had_draft"] += 1
            if e.get("question_edited"):
                s["archive_edited"] += 1
        elif ev == "followup":
            s["followup_clicks"][e.get("label") or e.get("key") or "?"] += 1
        elif ev == "followup_cancel":
            if e.get("status") == "cancelled":
                s["followup_cancels"] += 1
        elif ev == "cancelled":
            s["cancelled"] += 1

    # 被踩问题回填原题 + 原因
    for item in s["down_items"]:
        qa = qa_by_qid.get(item["qid"] or "")
        item["question"] = (qa or {}).get("question") or "（找不到对应 qa 记录）"
        item["reasons"] = down_reasons.get(item["qid"] or "", [])

    t = s["tokens"]
    s["cost_priced"] = (
        t["input"] * prices["input"]
        + t["output"] * prices["output"]
        + t["cache_read"] * prices["cache_read"]
        + t["cache_write"] * prices["cache_write"]
    ) / 1_000_000
    return s


def render(s: dict, *, days: int, today: date, show_priced: bool = False) -> str:
    """聚合结果 → 人读的纯文本报表。

    `show_priced`：是否展示美元估算行。默认关——订阅 / coding plan 套餐没有
    严格的 token 单价，报表只给纯用量；显式传了 --price-* 才认为部署方有
    真实单价表、把估算行打开。
    """
    span = f"近 {days} 天" if days > 0 else "全量"
    lines = [f"📊 ops-qa-bot 反馈统计（{span}，截至 {today.isoformat()}）", ""]

    lines.append("【问答】")
    n = s["qa_total"]
    lines.append(
        f"- 问答 {n} 次，活跃用户 {len(s['active_users'])} 人"
        + (f"，日均 {n / max(1, len(s['days'])):.1f} 次" if n else "")
    )
    if n:
        lines.append(
            f"- 反问轮 {s['clarifications']}；用户取消 {s['cancelled']}；"
            f"嵌图答案 {s['images_answers']}"
        )
        lines.append(
            f"- 升级 @ 负责人 {s['escalated_qa'] + s['escalated_ticket']}"
            f"（知识 {s['escalated_qa']} / 工单 {s['escalated_ticket']}）；"
            f"drift 兜底 {s['drift']}；max_turns 命中 {s['max_turns_hit']}"
        )
        # open_id 泄漏只在发生过时才打一行，避免零值噪音
        if s["open_id_scrub_rounds"] or s["open_id_promoted"]:
            lines.append(
                f"- open_id 泄漏：剥 {s['open_id_scrub_rounds']} 轮，"
                f"其中救回升级 @ {s['open_id_promoted']} 轮"
            )

    lines.append("")
    lines.append("【满意度】")
    rated = s["up"] + s["down"]
    if rated:
        lines.append(
            f"- 👍 {s['up']} / 👎 {s['down']}（满意率 {s['up'] / rated:.0%}，"
            f"评价率 {rated / max(1, s['qa_total']):.0%}）"
        )
    else:
        lines.append("- 暂无评价")
    if s["reason_counter"]:
        dist = "、".join(f"{k}×{v}" for k, v in s["reason_counter"].most_common())
        lines.append(f"- 差评原因：{dist}")
    for item in s["down_items"][:10]:
        reasons = f"（{'、'.join(item['reasons'])}）" if item["reasons"] else ""
        lines.append(f"  👎 {item['question']}{reasons}")

    lines.append("")
    lines.append("【归档沉淀】")
    if s["archives"]:
        lines.append(
            f"- 归档 {s['archives']} 条；LLM 标题草稿命中 {s['archive_had_draft']}，"
            f"负责人改标题 {s['archive_edited']}"
        )
        for p, c in s["archive_paths"].most_common(5):
            lines.append(f"  📁 {p} ×{c}")
    else:
        lines.append("- 本期无归档")

    if s["followup_clicks"] or s["followup_cancels"]:
        lines.append("")
        lines.append("【追问 / 跟进】")
        if s["followup_clicks"]:
            dist = "、".join(
                f"{k}×{v}" for k, v in s["followup_clicks"].most_common()
            )
            lines.append(f"- 追问点击：{dist}")
        if s["followup_cancels"]:
            lines.append(f"- 定时跟进取消 {s['followup_cancels']} 次")

    lines.append("")
    lines.append("【模型用量】")
    t = s["tokens"]
    total = t["input"] + t["output"] + t["cache_read"] + t["cache_write"]
    lines.append(
        f"- 总用量 {total:,} tokens："
        f"输入 {t['input']:,} / 输出 {t['output']:,} / "
        f"缓存读 {t['cache_read']:,} / 缓存写 {t['cache_write']:,}"
    )
    if s["qa_total"]:
        lines.append(f"- 平均每题 {total // s['qa_total']:,} tokens")
    cacheable = t["input"] + t["cache_read"]
    if cacheable:
        lines.append(
            f"- 缓存命中率 {t['cache_read'] / cacheable:.0%}"
            "（命中部分约为正价输入的 1/10，命中率越高同样问答越省）"
        )
    # 美元估算只在显式给了单价时展示：订阅/coding plan 套餐没有严格的
    # token 单价，硬给美元数反而误导，纯用量才是可靠口径。
    if show_priced:
        lines.append(
            f"- 按单价表估算 ${s['cost_priced']:.4f}"
            f"（SDK 官方价参考 ${s['cost_sdk']:.4f}）"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="feedback.log 离线统计报表")
    ap.add_argument(
        "--log", default="./logs/feedback.log", help="feedback.log 路径"
    )
    ap.add_argument(
        "--days", type=int, default=7, help="统计近 N 天（0 = 全量，默认 7）"
    )
    for key, default in DEFAULT_PRICES.items():
        ap.add_argument(
            f"--price-{key.replace('_', '-')}",
            type=float,
            default=None,
            dest=f"price_{key}",
            help=(
                f"{key} 单价（$/1M tokens，缺省 {default}）。"
                "传了任意 --price-* 才会在报表里展示美元估算；"
                "订阅/coding plan 套餐不传，报表只给纯 token 用量"
            ),
        )
    args = ap.parse_args(argv)

    overrides = {
        k: v
        for k in DEFAULT_PRICES
        if (v := getattr(args, f"price_{k}")) is not None
    }
    prices = {**DEFAULT_PRICES, **overrides}
    today = date.today()
    events = filter_days(parse_log(Path(args.log)), args.days, today)
    print(
        render(
            aggregate(events, prices),
            days=args.days,
            today=today,
            show_priced=bool(overrides),
        )
    )


if __name__ == "__main__":
    main()
