"""网关链路排查接入的回归测试：logview 客户端 + 工具错误映射。

覆盖：
- GatewayTraceClient.fetch：gzip 解压 / 明文 fallback / HTML 转义还原 /
  messageId 参数透传 / 超长截断 / 非 200 / 空体 / 超时 / 连接错误。
- query_gateway_trace handler：成功透传文本；缺 hi_trace_id / 超长 id /
  上游失败都返回 is_error 文字结果而不抛。

跑法：
    .venv/bin/python -m tests.test_gateway_trace
"""

from __future__ import annotations

import asyncio
import gzip

import httpx

from ops_qa_bot.config import GatewayTraceConfig
from ops_qa_bot.gateway_trace import (
    GatewayTraceClient,
    GatewayTraceError,
    _LOGVIEW_PATH,
    _MAX_RESULT_CHARS,
    make_query_gateway_trace_handler,
)

_TRACE_ID = "unified-access-server-0aa4c5db-479090-103"


def _run(coro):
    return asyncio.run(coro)


def _client_with_response(handler) -> GatewayTraceClient:
    """造一个 MockTransport 注入的 GatewayTraceClient。"""
    transport = httpx.MockTransport(handler)
    cfg = GatewayTraceConfig(base_url="http://gw-trace.test:8080", timeout=5)
    return GatewayTraceClient(cfg, transport=transport)


# ---------------------------------------------------------------------------
# GatewayTraceClient.fetch
# ---------------------------------------------------------------------------

def test_client_gzip_body_decompressed():
    body = "<table class=\"logview\"><tr><td>status=404</td></tr></table>"

    def h(req: httpx.Request) -> httpx.Response:
        # 校验路径 + messageId 参数透传
        assert req.url.path == _LOGVIEW_PATH
        assert req.url.params["messageId"] == _TRACE_ID
        return httpx.Response(200, content=gzip.compress(body.encode("utf-8")))

    client = _client_with_response(h)
    out = _run(client.fetch(_TRACE_ID))
    assert "status=404" in out
    assert "logview" in out


def test_client_plain_body_fallback():
    # 服务端没压缩时（非 gzip magic）当明文处理
    def h(req):
        return httpx.Response(200, content=b"<table>plain</table>")

    client = _client_with_response(h)
    assert "plain" in _run(client.fetch(_TRACE_ID))


def test_client_html_entities_unescaped():
    body = "host=gh-appserver-p2.haierrj.cn&amp;method=GET&amp;status=404"

    def h(req):
        return httpx.Response(200, content=gzip.compress(body.encode("utf-8")))

    client = _client_with_response(h)
    out = _run(client.fetch(_TRACE_ID))
    assert "&amp;" not in out
    assert "method=GET&status=404" in out


def test_client_truncates_oversized_body():
    big = "x" * (_MAX_RESULT_CHARS + 5000)

    def h(req):
        return httpx.Response(200, content=gzip.compress(big.encode("utf-8")))

    client = _client_with_response(h)
    out = _run(client.fetch(_TRACE_ID))
    assert len(out) <= _MAX_RESULT_CHARS + 100  # 截断 + 提示语
    assert "已截断" in out


def test_client_non_200_raises():
    def h(req):
        return httpx.Response(500, content=b"oops")

    client = _client_with_response(h)
    try:
        _run(client.fetch(_TRACE_ID))
    except GatewayTraceError as e:
        assert e.agent_hint
    else:
        raise AssertionError("非 200 应抛 GatewayTraceError")


def test_client_empty_body_raises():
    def h(req):
        return httpx.Response(200, content=gzip.compress(b"   "))

    client = _client_with_response(h)
    try:
        _run(client.fetch(_TRACE_ID))
    except GatewayTraceError as e:
        assert "Hi-Trace-Id" in e.agent_hint
    else:
        raise AssertionError("空体应抛 GatewayTraceError")


def test_client_timeout_raises():
    def h(req):
        raise httpx.TimeoutException("timed out", request=req)

    client = _client_with_response(h)
    try:
        _run(client.fetch(_TRACE_ID))
    except GatewayTraceError as e:
        assert e.agent_hint
    else:
        raise AssertionError("超时应抛 GatewayTraceError")


def test_client_connect_error_raises():
    def h(req):
        raise httpx.ConnectError("refused", request=req)

    client = _client_with_response(h)
    try:
        _run(client.fetch(_TRACE_ID))
    except GatewayTraceError:
        pass
    else:
        raise AssertionError("连接错误应抛 GatewayTraceError")


# ---------------------------------------------------------------------------
# query_gateway_trace handler
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc
        self.calls: list[str] = []

    async def fetch(self, hi_trace_id):
        self.calls.append(hi_trace_id)
        if self._exc is not None:
            raise self._exc
        return self._text


def test_handler_success_passes_text():
    fake = _FakeClient(text="链路表内容")
    handler = make_query_gateway_trace_handler(fake)
    res = _run(handler({"hi_trace_id": _TRACE_ID}))
    assert res["content"][0]["text"] == "链路表内容"
    assert "is_error" not in res
    assert fake.calls == [_TRACE_ID]


def test_handler_missing_id_is_error():
    fake = _FakeClient(text="x")
    handler = make_query_gateway_trace_handler(fake)
    res = _run(handler({"hi_trace_id": "  "}))
    assert res.get("is_error") is True
    assert "Hi-Trace-Id" in res["content"][0]["text"]
    assert not fake.calls  # 没真去调上游


def test_handler_oversized_id_is_error():
    fake = _FakeClient(text="x")
    handler = make_query_gateway_trace_handler(fake)
    res = _run(handler({"hi_trace_id": "a" * 300}))
    assert res.get("is_error") is True
    assert not fake.calls


def test_handler_upstream_failure_returns_hint_not_raises():
    fake = _FakeClient(exc=GatewayTraceError("boom", "请让用户重取 Hi-Trace-Id"))
    handler = make_query_gateway_trace_handler(fake)
    res = _run(handler({"hi_trace_id": _TRACE_ID}))
    assert res.get("is_error") is True
    assert "Hi-Trace-Id" in res["content"][0]["text"]


def test_handler_strips_whitespace_in_id():
    fake = _FakeClient(text="ok")
    handler = make_query_gateway_trace_handler(fake)
    _run(handler({"hi_trace_id": f"  {_TRACE_ID}  "}))
    assert fake.calls == [_TRACE_ID]


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
