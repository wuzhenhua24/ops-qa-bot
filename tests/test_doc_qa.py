"""飞书文档问答接入的回归测试：INDEX.md 注册表解析 + 工具错误映射。

覆盖：
- parse_feishu_registry：只收 feishu 行、按组件名/目录名建别名、token 分隔、
  local 行忽略、缺列容错、mtime 缓存随文件变更失效。
- DocQAClient.ask：200 成功 / 200 空答案 / 504 / 500 / 401 / 422 / 超时 /
  连接错误 各自的映射（成功返回 answer，失败抛 DocQAError 带 agent_hint）。
- query_feishu_doc handler：成功透传答案；未登记组件 / 缺 question / 上游失败
  都返回 is_error 文字结果而不抛。

跑法：
    .venv/bin/python -m tests.test_doc_qa
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import httpx

from ops_qa_bot.config import DocQAConfig
from ops_qa_bot.doc_qa import (
    DocQAClient,
    DocQAError,
    make_query_feishu_doc_handler,
    parse_feishu_registry,
)

# ---------------------------------------------------------------------------
# 测试夹具：临时 docs_root + INDEX.md
# ---------------------------------------------------------------------------

_INDEX_WITH_FEISHU = """# 索引

| 组件 | 来源 | 目录 | 飞书文档 | 覆盖内容 | 负责人 | open_id |
|------|------|------|----------|----------|--------|---------|
| Redis | local | `redis/` | - | 缓存 | 张三 | ou_aaa |
| Nginx | feishu | `nginx/` | docx_ABC, docx_DEF | 网关 | 赵六 | ou_bbb |
| Gateway | feishu | `gw/` | `docx_XYZ` | 网关2 | 钱七 | ou_ccc |
"""

_INDEX_LOCAL_ONLY = """# 索引

| 组件 | 目录 | 覆盖内容 | 负责人 | open_id |
|------|------|----------|--------|---------|
| Redis | `redis/` | 缓存 | 张三 | ou_aaa |
"""


def _make_docs_root(index_body: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="docqa_test_"))
    (d / "INDEX.md").write_text(index_body, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# 注册表解析
# ---------------------------------------------------------------------------

def test_registry_picks_only_feishu_rows():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    reg = parse_feishu_registry(root)
    # Redis 是 local，不进注册表；Nginx / Gateway 进
    names = {c.name for c in reg.values()}
    assert names == {"Nginx", "Gateway"}, names
    assert "redis" not in reg


def test_registry_alias_by_name_and_dir():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    reg = parse_feishu_registry(root)
    # 组件名（小写）和目录名（去 backtick/斜杠）都能查到同一条
    assert reg["nginx"].name == "Nginx"
    assert reg["gw"].name == "Gateway"
    assert reg["nginx"] is reg["nginx"]


def test_registry_token_split():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    reg = parse_feishu_registry(root)
    assert reg["nginx"].docs == ("docx_ABC", "docx_DEF")
    assert reg["gateway"].docs == ("docx_XYZ",)


def test_registry_local_only_index_empty():
    root = _make_docs_root(_INDEX_LOCAL_ONLY)
    # 没有「来源」「飞书文档」列 → 视作没有飞书组件
    assert parse_feishu_registry(root) == {}


def test_registry_missing_index_empty():
    d = Path(tempfile.mkdtemp(prefix="docqa_test_"))
    assert parse_feishu_registry(d) == {}


def test_registry_cache_invalidates_on_change():
    root = _make_docs_root(_INDEX_LOCAL_ONLY)
    assert parse_feishu_registry(root) == {}
    # 改写 INDEX.md 加飞书行，mtime 变 → 缓存失效，重新解析
    import os
    import time

    (root / "INDEX.md").write_text(_INDEX_WITH_FEISHU, encoding="utf-8")
    # 某些文件系统 mtime 粒度较粗，强制推进一下避免同秒未刷新
    future = time.time() + 2
    os.utime(root / "INDEX.md", (future, future))
    reg = parse_feishu_registry(root)
    assert {c.name for c in reg.values()} == {"Nginx", "Gateway"}


# ---------------------------------------------------------------------------
# DocQAClient.ask 错误映射
# ---------------------------------------------------------------------------

def _client_with_response(handler) -> DocQAClient:
    """造一个 MockTransport 注入的 DocQAClient。handler(request)->httpx.Response。"""
    transport = httpx.MockTransport(handler)
    cfg = DocQAConfig(base_url="http://doc-qa.test", token="t0ken", timeout=5)
    return DocQAClient(cfg, transport=transport)


def _run(coro):
    return asyncio.run(coro)


def test_client_success_returns_answer():
    def h(req: httpx.Request) -> httpx.Response:
        # 校验请求体 + 鉴权头
        import json

        body = json.loads(req.content)
        assert body["q"] == "怎么配限流"
        assert body["docs"] == ["docx_ABC"]
        assert req.headers["Authorization"] == "Bearer t0ken"
        return httpx.Response(
            200, json={"ok": True, "req_id": "r1", "answer": "用 limit_req"}
        )

    client = _client_with_response(h)
    out = _run(client.ask(["docx_ABC"], "怎么配限流"))
    assert out == "用 limit_req"


def test_client_200_empty_answer_raises():
    def h(req):
        return httpx.Response(200, json={"ok": True, "req_id": "r", "answer": "  "})

    client = _client_with_response(h)
    try:
        _run(client.ask(["d"], "q"))
    except DocQAError as e:
        assert e.agent_hint  # 有给 agent 的引导
    else:
        raise AssertionError("空答案应抛 DocQAError")


def test_client_504_500_401_422_all_raise():
    for status in (504, 500, 401, 422):
        def h(req, _s=status):
            return httpx.Response(_s, json={"ok": False, "error": "x", "req_id": "r"})

        client = _client_with_response(h)
        try:
            _run(client.ask(["d"], "q"))
        except DocQAError as e:
            assert "升级规则" in e.agent_hint
        else:
            raise AssertionError(f"status={status} 应抛 DocQAError")


def test_client_timeout_raises():
    def h(req):
        raise httpx.TimeoutException("timed out", request=req)

    client = _client_with_response(h)
    try:
        _run(client.ask(["d"], "q"))
    except DocQAError as e:
        assert e.agent_hint
    else:
        raise AssertionError("超时应抛 DocQAError")


def test_client_connect_error_raises():
    def h(req):
        raise httpx.ConnectError("refused", request=req)

    client = _client_with_response(h)
    try:
        _run(client.ask(["d"], "q"))
    except DocQAError:
        pass
    else:
        raise AssertionError("连接错误应抛 DocQAError")


def test_client_no_token_omits_auth_header():
    def h(req):
        assert "Authorization" not in req.headers
        return httpx.Response(200, json={"ok": True, "req_id": "r", "answer": "a"})

    transport = httpx.MockTransport(h)
    cfg = DocQAConfig(base_url="http://doc-qa.test", token=None, timeout=5)
    client = DocQAClient(cfg, transport=transport)
    assert _run(client.ask(["d"], "q")) == "a"


# ---------------------------------------------------------------------------
# query_feishu_doc handler
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, answer=None, exc=None):
        self._answer = answer
        self._exc = exc
        self.calls: list[tuple] = []

    async def ask(self, docs, question, req_id=None):
        self.calls.append((tuple(docs), question, req_id))
        if self._exc is not None:
            raise self._exc
        return self._answer


def test_handler_success_passes_answer_and_docs():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    fake = _FakeClient(answer="网关答案")
    handler = make_query_feishu_doc_handler(root, fake)
    res = _run(handler({"component": "Nginx", "question": "怎么配"}))
    assert res["content"][0]["text"] == "网关答案"
    assert "is_error" not in res
    # 服务端按组件名解析出 token，传给 client
    assert fake.calls[0][0] == ("docx_ABC", "docx_DEF")


def test_handler_unknown_component_is_error():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    fake = _FakeClient(answer="x")
    handler = make_query_feishu_doc_handler(root, fake)
    res = _run(handler({"component": "Redis", "question": "q"}))  # Redis 是 local
    assert res.get("is_error") is True
    assert "未登记" in res["content"][0]["text"]
    assert not fake.calls  # 没真去调上游


def test_handler_missing_question_is_error():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    fake = _FakeClient(answer="x")
    handler = make_query_feishu_doc_handler(root, fake)
    res = _run(handler({"component": "Nginx", "question": "  "}))
    assert res.get("is_error") is True
    assert not fake.calls


def test_handler_upstream_failure_returns_hint_not_raises():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    fake = _FakeClient(exc=DocQAError("boom", "请按升级规则通知负责人"))
    handler = make_query_feishu_doc_handler(root, fake)
    res = _run(handler({"component": "Nginx", "question": "怎么配"}))
    assert res.get("is_error") is True
    assert "升级规则" in res["content"][0]["text"]


def test_handler_lookup_by_dir_name():
    root = _make_docs_root(_INDEX_WITH_FEISHU)
    fake = _FakeClient(answer="ok")
    handler = make_query_feishu_doc_handler(root, fake)
    # 传目录名 gw 也能命中 Gateway
    res = _run(handler({"component": "gw", "question": "q"}))
    assert res["content"][0]["text"] == "ok"
    assert fake.calls[0][0] == ("docx_XYZ",)


# ---------------------------------------------------------------------------
# 飞书来源组件升级措辞：不承诺本地回读，引导维护飞书文档
# ---------------------------------------------------------------------------

from ops_qa_bot.feishu_core import (  # noqa: E402
    _append_escalate_at,
    _archive_form_card,
    _feishu_reference_links,
)
from ops_qa_bot.feishu_format import markdown_to_feishu_post  # noqa: E402


def _fresh_post() -> dict:
    return {"zh_cn": {"title": "t", "content": [[{"tag": "text", "text": "答案"}]]}}


def test_escalate_at_local_promises_readback():
    post = _fresh_post()
    _append_escalate_at(post, "ou_x", "redis/qa-archive.md", is_feishu=False)
    txt = str(post["zh_cn"]["content"])
    assert "下次类似问题我能直接从这里答" in txt
    assert "redis/qa-archive.md" in txt


def test_escalate_at_feishu_no_readback_promise():
    post = _fresh_post()
    _append_escalate_at(post, "ou_x", "nginx/qa-archive.md", is_feishu=True)
    txt = str(post["zh_cn"]["content"])
    # 不撒"会自学"的谎，也不暴露本地路径
    assert "下次类似问题我能直接从这里答" not in txt
    assert "qa-archive.md" not in txt
    assert "维护在飞书文档" in txt
    # @ 负责人这件事仍保留
    assert any(
        seg.get("tag") == "at" and seg.get("user_id") == "ou_x"
        for para in post["zh_cn"]["content"]
        for seg in para
    )


def test_escalate_at_ticket_unaffected_by_feishu_flag():
    # ticket 模式不提归档，is_feishu 不应改变它的行为
    post = _fresh_post()
    _append_escalate_at(post, "ou_x", "", is_ticket=True, is_feishu=True)
    txt = str(post["zh_cn"]["content"])
    assert "协助处理" in txt
    assert "飞书文档" not in txt  # ticket 不走知识归档措辞


def test_archive_card_local_mentions_path_and_keywords():
    card = _archive_form_card("q1", "问题标题", "ou_x", "redis/qa-archive.md")
    s = str(card)
    assert "检索关键词" in s
    assert "redis/qa-archive.md" in s


def test_archive_card_feishu_redirects_to_feishu_doc():
    card = _archive_form_card(
        "q1", "问题标题", "ou_x", "nginx/qa-archive.md", is_feishu=True
    )
    s = str(card)
    assert "检索关键词" not in s
    assert "qa-archive.md" not in s
    assert "飞书文档" in s
    # 表单本体（问题/答案输入框 + 提交）照常在，机制不变
    assert "archive_form" in s and "archive_submit" in s


# ---------------------------------------------------------------------------
# 参考文档链接：本轮查过的飞书组件 → 末尾可点链接
# ---------------------------------------------------------------------------

_INDEX_REF = """# i
| 组件 | 来源 | 目录 | 飞书文档 | 覆盖内容 | 负责人 | open_id |
|---|---|---|---|---|---|---|
| Nginx | feishu | `nginx/` | https://abc.feishu.cn/docx/AAA | 网关 | 赵六 | ou_b |
| Gw | feishu | `gw/` | https://abc.feishu.cn/docx/B1, https://abc.feishu.cn/docx/B2 | x | 钱七 | ou_c |
| Tok | feishu | `t/` | docx_TOKEN_ONLY | x | 孙八 | ou_d |
"""


def test_ref_links_single_url():
    root = _make_docs_root(_INDEX_REF)
    out = _feishu_reference_links(["Nginx"], root)
    assert out == "📄 参考文档：\n- [Nginx 飞书文档](https://abc.feishu.cn/docx/AAA)"


def test_ref_links_multi_url_numbered():
    root = _make_docs_root(_INDEX_REF)
    out = _feishu_reference_links(["Gw"], root)
    assert "[Gw 飞书文档 1](https://abc.feishu.cn/docx/B1)" in out
    assert "[Gw 飞书文档 2](https://abc.feishu.cn/docx/B2)" in out


def test_ref_links_dedup_by_component():
    root = _make_docs_root(_INDEX_REF)
    out = _feishu_reference_links(["Nginx", "nginx", "Nginx"], root)
    assert out.count("Nginx 飞书文档") == 1


def test_ref_links_token_only_skipped():
    root = _make_docs_root(_INDEX_REF)
    # 纯 token 拼不出链接 → 该组件无可链 → 整体 None
    assert _feishu_reference_links(["Tok"], root) is None


def test_ref_links_unknown_component_none():
    root = _make_docs_root(_INDEX_REF)
    assert _feishu_reference_links(["Redis"], root) is None
    assert _feishu_reference_links([], root) is None


def test_ref_links_render_to_clickable_a_tag():
    root = _make_docs_root(_INDEX_REF)
    block = _feishu_reference_links(["Nginx"], root)
    post = markdown_to_feishu_post("答案正文\n\n" + block)
    assert any(
        seg.get("tag") == "a" and seg.get("href") == "https://abc.feishu.cn/docx/AAA"
        for para in post["zh_cn"]["content"]
        for seg in para
    )


# ---------------------------------------------------------------------------

def _run_all():
    fns = [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
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
