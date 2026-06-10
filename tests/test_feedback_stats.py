"""feedback_stats 离线统计的回归测试。

覆盖：parse_log 容忍脏行；filter_days 时间窗；aggregate 各事件类型的计数
（qa/feedback/feedback_reason/archive/followup/followup_cancel/cancelled、
duplicate 归档不重复计、token 成本按单价表算）；render 关键数字出现在报表里。

跑法：
    .venv/bin/python -m pytest tests/test_feedback_stats.py -q
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ops_qa_bot.feedback_stats import (
    DEFAULT_PRICES,
    aggregate,
    filter_days,
    parse_log,
    render,
)

TODAY = date(2026, 6, 10)


def _line(day: str, obj: dict) -> str:
    return f"{day} 12:00:00,000 INFO " + json.dumps(obj, ensure_ascii=False)


def _sample_log(tmp_path: Path) -> Path:
    rows = [
        "garbage line without json",
        _line("2026-06-09", {"no_event_field": 1}),
        _line(
            "2026-06-09",
            {
                "event": "qa",
                "qid": "q1",
                "user_id": "ou_a",
                "question": "redis 内存满了怎么办",
                "cost_usd": 0.01,
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 2000,
                    "cache_read_input_tokens": 10000,
                    "cache_creation_input_tokens": 500,
                },
                "escalated_to": "ou_x",
                "escalation_kind": "qa",
            },
        ),
        _line(
            "2026-06-10",
            {
                "event": "qa",
                "qid": "q2",
                "user_id": "ou_b",
                "question": "开个 jumpserver 权限",
                "clarification": True,
            },
        ),
        _line("2026-06-10", {"event": "feedback", "qid": "q1", "rating": "down"}),
        _line("2026-06-10", {"event": "feedback", "qid": "q2", "rating": "up"}),
        _line(
            "2026-06-10",
            {
                "event": "feedback_reason",
                "qid": "q1",
                "reason_labels": ["文档过时", "步骤不完整"],
                "invalid": False,
            },
        ),
        _line(
            "2026-06-10",
            {
                "event": "archive",
                "qid": "q1",
                "path": "redis/qa-archive.md",
                "had_draft": True,
                "question_edited": False,
            },
        ),
        _line(
            "2026-06-10",
            {"event": "archive", "qid": "q1", "duplicate": True, "path": "x"},
        ),
        _line(
            "2026-06-10",
            {"event": "followup", "qid": "q1", "key": "risks", "label": "⚠️ 风险点"},
        ),
        _line(
            "2026-06-10",
            {"event": "followup_cancel", "record_id": "r1", "status": "cancelled"},
        ),
        _line("2026-06-10", {"event": "cancelled", "question": "发错的问题"}),
        # 窗口外的老事件：--days 7 不该统计进来
        _line("2026-05-01", {"event": "qa", "qid": "old", "question": "老问题"}),
    ]
    p = tmp_path / "feedback.log"
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return p


def _stats(tmp_path: Path, days: int = 7) -> dict:
    events = filter_days(parse_log(_sample_log(tmp_path)), days, TODAY)
    return aggregate(events, DEFAULT_PRICES)


def test_parse_log_skips_garbage(tmp_path: Path):
    events = parse_log(_sample_log(tmp_path))
    assert all("event" in e for _, e in events)
    assert len(events) == 11  # 两行脏数据被跳过


def test_filter_days_window(tmp_path: Path):
    events = parse_log(_sample_log(tmp_path))
    week = filter_days(events, 7, TODAY)
    assert all(d >= "2026-06-04" for d, _ in week)
    assert len(filter_days(events, 0, TODAY)) == len(events)  # 0 = 全量


def test_aggregate_counts(tmp_path: Path):
    s = _stats(tmp_path)
    assert s["qa_total"] == 2  # 窗口外的 old 不算
    assert s["active_users"] == {"ou_a", "ou_b"}
    assert s["clarifications"] == 1
    assert s["escalated_qa"] == 1 and s["escalated_ticket"] == 0
    assert s["up"] == 1 and s["down"] == 1
    assert s["reason_counter"]["文档过时"] == 1
    # 被踩问题回填了原题和原因
    assert s["down_items"][0]["question"] == "redis 内存满了怎么办"
    assert "文档过时" in s["down_items"][0]["reasons"]
    # duplicate 归档不重复计
    assert s["archives"] == 1
    assert s["archive_paths"]["redis/qa-archive.md"] == 1
    assert s["archive_had_draft"] == 1 and s["archive_edited"] == 0
    assert s["followup_clicks"]["⚠️ 风险点"] == 1
    assert s["followup_cancels"] == 1
    assert s["cancelled"] == 1


def test_aggregate_cost(tmp_path: Path):
    s = _stats(tmp_path)
    assert s["cost_sdk"] == 0.01
    expected = (1000 * 3.0 + 2000 * 15.0 + 10000 * 0.3 + 500 * 3.75) / 1_000_000
    assert abs(s["cost_priced"] - expected) < 1e-9
    assert s["tokens"]["input"] == 1000 and s["tokens"]["cache_read"] == 10000


def test_render_contains_key_numbers(tmp_path: Path):
    s = _stats(tmp_path)
    text = render(s, days=7, today=TODAY)
    assert "问答 2 次" in text
    assert "👍 1 / 👎 1" in text
    assert "满意率 50%" in text
    assert "redis 内存满了怎么办" in text  # 被踩问题列出来
    assert "redis/qa-archive.md" in text
    assert "max_turns 命中 0" in text
    # 用量口径：纯 token 为主（13,500 = 1000+2000+10000+500）
    assert "总用量 13,500 tokens" in text
    assert "平均每题 6,750 tokens" in text
    # 缓存命中率 = 10000 / (1000 + 10000) ≈ 91%
    assert "缓存命中率 91%" in text
    # 默认不展示美元估算（订阅/coding plan 没有严格单价）
    assert "$" not in text


def test_render_priced_only_when_requested(tmp_path: Path):
    s = _stats(tmp_path)
    text = render(s, days=7, today=TODAY, show_priced=True)
    assert "按单价表估算 $" in text
    assert "SDK 官方价参考 $0.0100" in text


def test_render_empty_log(tmp_path: Path):
    p = tmp_path / "empty.log"
    p.write_text("", encoding="utf-8")
    s = aggregate(filter_days(parse_log(p), 7, TODAY), DEFAULT_PRICES)
    text = render(s, days=7, today=TODAY)
    assert "问答 0 次" in text
    assert "暂无评价" in text