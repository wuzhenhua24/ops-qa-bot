"""工单类升级 <<ESCALATE_TICKET:>> 的回归测试。

覆盖 _parse_escalate_ticket 解析、_append_escalate_at 的 is_ticket 分支
（@ 行措辞 / 不追加归档路径行）、以及与 _parse_escalate 的优先级关系。

跑法：
    .venv/bin/python -m tests.test_escalate_ticket
"""

from __future__ import annotations

from ops_qa_bot import feishu_core as fc


# ---------------------------------------------------------------------------
# _parse_escalate_ticket
# ---------------------------------------------------------------------------

def test_ticket_marker_extracts_owner():
    cleaned, owner = fc._parse_escalate_ticket(
        "这是工单请求，已转给 Redis 负责人。\n\n<<ESCALATE_TICKET:ou_abc123>>"
    )
    assert owner == "ou_abc123"
    assert "<<ESCALATE_TICKET" not in cleaned


def test_ticket_marker_none_means_no_at():
    cleaned, owner = fc._parse_escalate_ticket("xx <<ESCALATE_TICKET:none>>")
    assert owner is None
    assert "<<ESCALATE_TICKET" not in cleaned


def test_ticket_marker_missing():
    cleaned, owner = fc._parse_escalate_ticket("普通答案，没有标记。")
    assert owner is None
    assert cleaned == "普通答案，没有标记。"


def test_ticket_does_not_match_plain_escalate():
    # 不能把 <<ESCALATE:ou_xxx>> 误识别成 ticket（前缀严格匹配 ESCALATE_TICKET:）
    cleaned, owner = fc._parse_escalate_ticket("<<ESCALATE:ou_abc123:redis>>")
    assert owner is None
    assert "<<ESCALATE:" in cleaned  # 普通 ESCALATE 不该被 ticket parser 剥


def test_plain_escalate_parser_ignores_ticket_marker():
    # 反向：普通 ESCALATE parser 也不能误吃 ticket marker
    cleaned, owner, dir_hint = fc._parse_escalate("<<ESCALATE_TICKET:ou_abc123>>")
    assert owner is None
    assert dir_hint is None
    assert "<<ESCALATE_TICKET" in cleaned


# ---------------------------------------------------------------------------
# _append_escalate_at: is_ticket 分支
# ---------------------------------------------------------------------------

def _empty_post():
    return {"zh_cn": {"title": "x", "content": []}}


def _post_text(post):
    """把 post 的所有文本段拼起来，便于断言。"""
    chunks = []
    for line in post["zh_cn"]["content"]:
        for seg in line:
            if seg.get("tag") == "text":
                chunks.append(seg.get("text", ""))
            elif seg.get("tag") == "at":
                chunks.append(f"@{seg.get('user_id')}")
    return "\n".join(chunks)


def test_append_escalate_at_normal_has_archive_line():
    post = _empty_post()
    fc._append_escalate_at(post, "ou_owner", "redis/qa-archive.md")
    text = _post_text(post)
    assert "已通知负责人" in text
    assert "@ou_owner" in text
    assert "协助回答" in text  # 知识答疑用"回答"
    assert "redis/qa-archive.md" in text
    assert "归档到" in text


def test_append_escalate_at_ticket_skips_archive_line():
    post = _empty_post()
    fc._append_escalate_at(post, "ou_owner", "", is_ticket=True)
    text = _post_text(post)
    assert "已通知负责人" in text
    assert "@ou_owner" in text
    assert "协助处理" in text  # 工单用"处理"
    assert "归档" not in text  # 关键：工单不提归档
    assert "qa-archive" not in text


def test_append_escalate_at_ticket_uses_at_tag():
    # 确认 @ 用的是飞书 at tag（user_id=ou_xxx），不是普通文本——这样才能真的推送
    post = _empty_post()
    fc._append_escalate_at(post, "ou_target", "", is_ticket=True)
    at_tags = [
        seg for line in post["zh_cn"]["content"]
        for seg in line if seg.get("tag") == "at"
    ]
    assert len(at_tags) == 1
    assert at_tags[0]["user_id"] == "ou_target"


# ---------------------------------------------------------------------------

def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
