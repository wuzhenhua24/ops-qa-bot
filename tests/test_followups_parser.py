"""_parse_followups / _FOLLOWUPS_RE 的回归测试。

主因：实测 LLM 偶尔会在 marker 里多打空格——
  - `<<FOLLOWUPS: troubleshoot|commands|related>>` 冒号后空格
  - `<<FOLLOWUPS:troubleshoot | commands | related>>` 竖线两侧空格
原正则 [\\w|]+ 不容空格，漏匹配 → marker 字面糊到答案末尾 + 追问卡不发。
修复后用 [^<>\\n\\r]+? 宽松匹配，下游 strip + 白名单过滤兜底。

跑法：
    .venv/bin/python -m tests.test_followups_parser
"""

from __future__ import annotations

from ops_qa_bot import feishu_core as fc


# ---------------------------------------------------------------------------
# 空格容忍：核心回归
# ---------------------------------------------------------------------------

def test_canonical_no_whitespace():
    cleaned, keys = fc._parse_followups(
        "答案\n<<FOLLOWUPS:troubleshoot|commands|related>>"
    )
    assert keys == ["troubleshoot", "commands", "related"]
    assert "<<FOLLOWUPS" not in cleaned


def test_leading_space_after_colon():
    # 关键回归：实际线上踩到的 case
    cleaned, keys = fc._parse_followups(
        "答案\n<<FOLLOWUPS: troubleshoot|commands|related>>"
    )
    assert keys == ["troubleshoot", "commands", "related"]
    assert "<<FOLLOWUPS" not in cleaned


def test_spaces_around_pipes():
    cleaned, keys = fc._parse_followups(
        "答案\n<<FOLLOWUPS:troubleshoot | commands | related>>"
    )
    assert keys == ["troubleshoot", "commands", "related"]
    assert "<<FOLLOWUPS" not in cleaned


def test_all_whitespace_combos():
    cleaned, keys = fc._parse_followups(
        "答案\n<<FOLLOWUPS: troubleshoot | commands | related >>"
    )
    assert keys == ["troubleshoot", "commands", "related"]
    assert "<<FOLLOWUPS" not in cleaned


# ---------------------------------------------------------------------------
# 白名单过滤、去重、上限——原本就有的不变性
# ---------------------------------------------------------------------------

def test_unknown_key_filtered_out():
    # bogus 不在 _FOLLOWUP_LIBRARY 白名单里 → 静默过滤
    _, keys = fc._parse_followups("a <<FOLLOWUPS:troubleshoot|bogus|commands>>")
    assert keys == ["troubleshoot", "commands"]


def test_duplicate_keys_deduped():
    _, keys = fc._parse_followups(
        "a <<FOLLOWUPS:troubleshoot|troubleshoot|commands>>"
    )
    assert keys == ["troubleshoot", "commands"]


def test_max_three_keys():
    _, keys = fc._parse_followups(
        "a <<FOLLOWUPS:troubleshoot|risks|rollback|checklist|commands>>"
    )
    assert len(keys) == 3
    assert keys == ["troubleshoot", "risks", "rollback"]


def test_no_marker_returns_empty():
    cleaned, keys = fc._parse_followups("普通答案，没有标记")
    assert keys == []
    assert cleaned == "普通答案，没有标记"


def test_empty_marker_yields_no_keys():
    # `<<FOLLOWUPS:>>` 现在能被新正则识别为"空 marker"（旧正则不匹配），剥掉但
    # 没有合法 key。允许这种边界情况，避免空 marker 字面糊到答案。
    cleaned, keys = fc._parse_followups("a <<FOLLOWUPS: >>")
    assert keys == []
    assert "<<FOLLOWUPS" not in cleaned


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
