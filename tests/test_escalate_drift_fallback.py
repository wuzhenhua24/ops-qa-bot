"""Escalate marker drift 兜底的回归测试。

场景：LLM 答出"文档中未找到"但忘了输出 ESCALATE marker（[[project-escalate-
trigger-probabilistic]]）。bot 端要识别这种漂移、追加 asker-facing 提示、
打 warning 日志、qa_record 加 escalate_drift_fallback=True 字段做分析。

覆盖 _NOT_FOUND_RE 句式识别 + _is_escalate_drift 四档判定（非反问轮 / 无 owner /
raw_answer 无 marker / 文本命中找不到句式）。handle_question 是 async + IO 重，
单测只验纯函数。

跑法：
    .venv/bin/python -m tests.test_escalate_drift_fallback
"""

from __future__ import annotations

from ops_qa_bot import feishu_core as fc


# ---------------------------------------------------------------------------
# _NOT_FOUND_RE：识别 LLM 委婉拒答的常见句式
# ---------------------------------------------------------------------------

def test_not_found_re_matches_canonical_phrases():
    cases = [
        "文档中未找到相关内容。",
        "文档未找到相关说明。",
        "文档里未找到对应步骤。",
        "未找到相关文档。",
        "找不到相关说明。",
        "文档中没有这方面的内容。",
        "文档里没有相关介绍。",
    ]
    for ans in cases:
        assert fc._NOT_FOUND_RE.search(ans), f"should match: {ans!r}"


def test_not_found_re_misses_unrelated_text():
    # 包含"找不到"但不是"找不到相关"的搭配——避免把知识答案误判成 drift
    misses = [
        "redis 怎么备份：先 BGSAVE，找不到 rdb 文件位置可以看 dir 配置。",
        "节点状态显示 disconnected，连不上找不到上游了。",
        "已通知负责人，请协助处理。",
    ]
    for ans in misses:
        assert not fc._NOT_FOUND_RE.search(ans), f"should NOT match: {ans!r}"


# ---------------------------------------------------------------------------
# _is_escalate_drift：四档判定
# ---------------------------------------------------------------------------

def test_drift_true_when_all_conditions_met():
    raw = "文档中未找到相关内容，建议联系运维。"
    stripped = raw  # 没有 marker 要剥，剥前剥后一样
    assert fc._is_escalate_drift(
        raw_answer=raw,
        stripped_answer=stripped,
        escalate_owner=None,
        is_clarification=False,
    ) is True


def test_drift_false_when_clarification():
    # 反问轮 LLM 本来就不该 escalate，不算漂移
    raw = "为了准确回答需要先确认一点：你用的是 redis 6 还是 7？\n<<CLARIFY>>"
    stripped = "为了准确回答需要先确认一点：你用的是 redis 6 还是 7？"
    assert fc._is_escalate_drift(
        raw_answer=raw,
        stripped_answer=stripped,
        escalate_owner=None,
        is_clarification=True,
    ) is False


def test_drift_false_when_owner_already_set():
    # 已经识别到 owner（LLM 输出了 ESCALATE:ou_xxx），不需要兜底
    raw = "文档中未找到，已转给负责人。\n<<ESCALATE:ou_xxx:redis>>"
    stripped = "文档中未找到，已转给负责人。"
    assert fc._is_escalate_drift(
        raw_answer=raw,
        stripped_answer=stripped,
        escalate_owner="ou_xxx",
        is_clarification=False,
    ) is False


def test_drift_false_when_escalate_none_marker_present():
    # LLM 明确输出了 ESCALATE:none（"找不到但也不知道找谁"），按其意图办不兜底
    raw = "文档中未找到，建议联系对应组件负责人。\n<<ESCALATE:none>>"
    stripped = "文档中未找到，建议联系对应组件负责人。"
    assert fc._is_escalate_drift(
        raw_answer=raw,
        stripped_answer=stripped,
        escalate_owner=None,  # parser 把 none 解析成 None
        is_clarification=False,
    ) is False


def test_drift_false_when_escalate_ticket_marker_present():
    # 工单类升级 marker 也算 LLM 明确表态了
    raw = "这是工单请求。\n<<ESCALATE_TICKET:ou_xxx>>"
    stripped = "这是工单请求。"
    assert fc._is_escalate_drift(
        raw_answer=raw,
        stripped_answer=stripped,
        escalate_owner="ou_xxx",  # ticket owner 也算 owner
        is_clarification=False,
    ) is False


def test_drift_false_when_answer_does_not_say_not_found():
    # 正常知识问答，没说"找不到"——不该误判
    raw = "redis 备份用 BGSAVE：\n1. 连上 redis-cli\n2. 执行 BGSAVE\n3. 文件落在 dir 配置的目录"
    stripped = raw
    assert fc._is_escalate_drift(
        raw_answer=raw,
        stripped_answer=stripped,
        escalate_owner=None,
        is_clarification=False,
    ) is False


def test_drift_true_with_raw_having_unrelated_lt_chars():
    # raw_answer 含 "<<" 但不是 ESCALATE/CLARIFY（极少见但要鲁棒），仍然算 drift
    raw = "文档中未找到。提示：<<IMG_KEY:abc>>"  # IMG_KEY 不影响 drift 判断
    stripped = "文档中未找到。提示：<<IMG_KEY:abc>>"
    assert fc._is_escalate_drift(
        raw_answer=raw,
        stripped_answer=stripped,
        escalate_owner=None,
        is_clarification=False,
    ) is True


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
