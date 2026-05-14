"""_placeholder_text 初始占位文案启发的回归测试。

覆盖两档分流：含 IP 默认走"🔧 诊断中"；操作/工单关键词命中时**压制** IP 启发回
"🔍 翻文档中"（agent 先查文档判断自助还是手动，不会 ssh，贴诊断中会误导）。

跑法：
    .venv/bin/python -m tests.test_placeholder_text
"""

from __future__ import annotations

from ops_qa_bot import feishu_core as fc


# ---------------------------------------------------------------------------
# 基线分流
# ---------------------------------------------------------------------------

def test_default_doc_lookup_icon():
    assert fc._placeholder_text("redis 怎么备份", queued=False).startswith("🔍 翻文档中")


def test_ip_only_triggers_diagnosis_icon():
    assert fc._placeholder_text("看下 172.28.4.40 内存", queued=False).startswith("🔧 诊断中")


def test_queued_overrides_everything():
    # 排队中阶段不区分类型，统一一个图标，等拿到锁后再 update。
    txt = fc._placeholder_text("帮我加 172.28.2.24 的权限", queued=True)
    assert txt.startswith("🕒 排队中")


# ---------------------------------------------------------------------------
# 操作/工单请求：压制 IP 启发，回到"翻文档中"（先读文档判定自助/手动）
# ---------------------------------------------------------------------------

def test_op_request_with_ip_suppresses_diagnosis():
    # 关键回归：句子里有 IP，但实质是"帮我加权限"——agent 不会 ssh，会读权限文档。
    txt = fc._placeholder_text("帮我加 172.28.2.24 这台机器的权限", queued=False)
    assert txt.startswith("🔍 翻文档中")


def test_apply_account_request_without_ip():
    # 没 IP 的话本来就走默认"翻文档中"——也确认 OP_REQUEST 正则识别得到。
    assert fc._placeholder_text("申请下 mysql 的只读账号", queued=False).startswith("🔍 翻文档中")


def test_mafan_open_request_without_ip():
    assert fc._placeholder_text("麻烦给我开通一下访问权限", queued=False).startswith("🔍 翻文档中")


def test_bang_kai_request_without_ip():
    assert fc._placeholder_text("帮开一下 redis-test-01 的访问", queued=False).startswith("🔍 翻文档中")


def test_kai_tong_whitelist_with_ip_suppresses_diagnosis():
    # "172.28.x.x 给我开通下白名单"——含 IP，但"开通白名单"是 OP_REQUEST，
    # 压制掉诊断中。
    txt = fc._placeholder_text("172.28.x.x 给我开通下白名单", queued=False)
    assert txt.startswith("🔍 翻文档中")


# ---------------------------------------------------------------------------
# 知识类问题不要被工单正则误伤
# ---------------------------------------------------------------------------

def test_knowledge_how_to_add_node_not_ticket():
    # "怎么加节点" 是知识问题，不带"帮/麻烦/给我/申请/开通"委托语义。
    txt = fc._placeholder_text("redis 集群怎么加节点", queued=False)
    assert txt.startswith("🔍 翻文档中")


def test_knowledge_encryption_word_not_ticket():
    # "加密"含"加"字，但不是"加权限/加白名单"句式。
    txt = fc._placeholder_text("redis 加密访问怎么配", queued=False)
    assert txt.startswith("🔍 翻文档中")


def test_knowledge_how_to_enable_binlog_not_ticket():
    # "如何开启" 是知识问题。
    txt = fc._placeholder_text("mysql 如何开启 binlog", queued=False)
    assert txt.startswith("🔍 翻文档中")


def test_diagnosis_with_error_not_ticket():
    # 报错诊断句不是工单。
    txt = fc._placeholder_text("172.28.4.40 redis 报错 OOM 怎么办", queued=False)
    assert txt.startswith("🔧 诊断中")


# ---------------------------------------------------------------------------
# 摘要截断
# ---------------------------------------------------------------------------

def test_excerpt_truncation():
    long_q = "redis " + "性能优化" * 30
    txt = fc._placeholder_text(long_q, queued=False)
    assert "…" in txt


def test_excerpt_normalizes_newline():
    txt = fc._placeholder_text("多行\n问题\n内容", queued=False)
    assert "\n" not in txt


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
