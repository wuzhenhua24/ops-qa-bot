"""网关链路排查接口接入：cat logview HTTP 客户端 + SDK MCP 工具。

网关组件的本地文档里记录了一套**人工**排查流程：拿失败响应头里的 `Hi-Trace-Id`
去 cat 平台页面查这次请求的链路日志。本模块把那个页面背后的后端接口
（`GET /cat/r/model/logview/unified-access-server?messageId=<Hi-Trace-Id>`，返回
应用层 gzip 压缩的 logview 表格）包成主 `OpsQABot` 的一个进程内工具
`query_gateway_trace`，让 agent 在用户报告"访问域名/接口失败 + 给了 Hi-Trace-Id"
时**确定性地**取到链路数据，而不是靠读散文 runbook 现拼 curl（触发不稳）。

设计要点（与 doc_qa.py 同姿态）：
- **只依赖 stdlib + httpx + claude_agent_sdk**，不引入 bot / feishu_core（避免循环）。
- **工具只对固定端点发 GET**：唯一来自 LLM 的输入是 `hi_trace_id`，作为 query 参数
  由 httpx 负责 urlencode——工具天生只能读、改不成写操作，不需要写命令拦截那一层。
- 端点 base_url 走 config（`GatewayTraceConfig`），不写死在代码里：真实内网地址只落
  部署侧 config.toml（不提交），符合内部安全要求。
- **不解析字段，原样返回**：返回 gzip 解压 + HTML 转义还原后的链路表全文，交给主 agent
  解读（内容不大）。工具的价值在"可靠取到"，不在"替 agent 理解"。
- 失败**返回文字结果而不是抛异常**：抛异常会打断 agent 这一轮；返回一段引导文字让
  agent 自己决定让用户重取 Hi-Trace-Id 或按升级规则通知负责人。
"""

from __future__ import annotations

import gzip
import html
import logging

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from .config import GatewayTraceConfig

logger = logging.getLogger("ops_qa_bot.gateway_trace")

# 工具名（allowed_tools 引用）。agent 侧全名是 mcp__<server_key>__<tool_name>。
TOOL_NAME = "query_gateway_trace"
SERVER_KEY = "gwtrace"
FULL_TOOL_NAME = f"mcp__{SERVER_KEY}__{TOOL_NAME}"

# logview 接口的固定路径段（网关 app 名固定为 unified-access-server，已与使用方确认）。
_LOGVIEW_PATH = "/cat/r/model/logview/unified-access-server"

# Hi-Trace-Id 长度上限：正常形如 unified-access-server-0aa4c5db-479090-103，
# 远不到这个量级。超长直接当非法，防被塞进奇怪内容。
_MAX_TRACE_ID_LEN = 256

# 返回体上限：内容一般不大，但仍设防御性截断，避免异常大返回灌爆 agent 上下文。
_MAX_RESULT_CHARS = 20000


class GatewayTraceError(Exception):
    """logview 接口调用失败。`agent_hint` 是给主 agent 看的引导文字。"""

    def __init__(self, log_detail: str, agent_hint: str):
        super().__init__(log_detail)
        self.agent_hint = agent_hint


# 给 agent 的兜底引导：取不到链路数据时让它先让用户核对/重取 Hi-Trace-Id，
# 仍不行再按升级规则。措辞 agent-facing，不是给终端用户看的。
_FETCH_HINT = (
    "未能取得该 Hi-Trace-Id 的网关链路数据。可能原因：Hi-Trace-Id 填错或已过期，"
    "或链路服务暂时不可达。请让用户确认 Hi-Trace-Id（取自失败响应的响应头 "
    "`Hi-Trace-Id`），或稍后重试；多次取不到再按「升级规则」通知网关负责人，"
    "不要凭常识编造链路结论。"
)


class GatewayTraceClient:
    """cat logview 接口的轻量 httpx 客户端。

    `transport` 仅供测试注入 httpx.MockTransport；生产留 None 走默认网络。
    """

    def __init__(
        self,
        config: GatewayTraceConfig,
        transport: "httpx.AsyncBaseTransport | None" = None,
    ):
        if not config.base_url:
            raise ValueError("GatewayTraceClient 需要非空 base_url")
        self._url = f"{config.base_url}{_LOGVIEW_PATH}"
        self._timeout = config.timeout
        self._transport = transport

    async def fetch(self, hi_trace_id: str) -> str:
        """按 Hi-Trace-Id 取链路日志，返回解压 + HTML 转义还原后的文本。

        失败抛 GatewayTraceError（带 agent_hint）。空内容也当失败抛（多半是
        id 错/过期）。
        """
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                resp = await client.get(
                    self._url, params={"messageId": hi_trace_id}
                )
        except httpx.TimeoutException as e:
            raise GatewayTraceError(
                f"gateway trace timeout: {e!r}", _FETCH_HINT
            ) from e
        except httpx.HTTPError as e:
            raise GatewayTraceError(
                f"gateway trace connect error: {e!r}", _FETCH_HINT
            ) from e

        if resp.status_code != 200:
            raise GatewayTraceError(
                f"gateway trace failed: status={resp.status_code}", _FETCH_HINT
            )

        raw = resp.content or b""
        # 应用层 gzip：服务端没设 Content-Encoding（所以原命令要 | gunzip），
        # httpx 不会自动解。按 gzip magic（1f 8b）判断，是则解，否则当明文。
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError) as e:
                raise GatewayTraceError(
                    f"gateway trace gunzip failed: {e!r}", _FETCH_HINT
                ) from e

        text = html.unescape(raw.decode("utf-8", errors="replace")).strip()
        if not text:
            raise GatewayTraceError(
                "gateway trace empty body (likely bad/expired Hi-Trace-Id)",
                _FETCH_HINT,
            )
        if len(text) > _MAX_RESULT_CHARS:
            text = (
                text[:_MAX_RESULT_CHARS]
                + "\n…（链路内容过长，已截断；如需更早记录请用 cat 页面工具查看）"
            )
        return text


_TOOL_DESC = (
    "查询经过网关的某次请求的链路日志，用于排查「访问域名/接口失败」类问题。"
    "触发条件：用户报告经网关的请求失败、并提供了 Hi-Trace-Id（取自失败响应的"
    "响应头 `Hi-Trace-Id`，形如 unified-access-server-0aa4c5db-479090-103）。"
    "hi_trace_id 传该值原文。返回这次请求的网关链路表（命中的路由、网关调用的后端"
    "服务与 IP、客户端真实 IP（realIP，需要按白名单放行域名的场景尤其要看并展示给"
    "用户）、请求 method/path/host、收到的状态码与异常类型、给客户端的响应码与耗时"
    "等），据此判断失败原因，例如 URL_NOT_MATCHED/_no_url_matched=网关没匹配到路由、"
    "UPSTREAM_NO_HOSTS=后端集群无可用实例、5xx=后端服务异常。"
    "用户没给 Hi-Trace-Id 时**不要**调本工具，先让他从失败响应的响应头里取；"
    "取不到数据时本工具会返回引导提示。"
)


def _text_result(text: str, *, is_error: bool = False) -> dict:
    res: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        res["is_error"] = True
    return res


def make_query_gateway_trace_handler(client: "GatewayTraceClient"):
    """造 query_gateway_trace 的 async handler（与 SDK 解耦，便于单测直接 await）。

    失败一律返回 is_error 文字结果而不抛——抛会打断 agent 这一轮，返回提示则让
    agent 自己按引导处理（让用户重取 Hi-Trace-Id 或走升级规则）。
    """

    async def handler(args: dict) -> dict:
        hi_trace_id = str(args.get("hi_trace_id") or "").strip()
        if not hi_trace_id:
            return _text_result(
                "调用缺少 hi_trace_id 参数。请让用户提供失败响应头里的 "
                "`Hi-Trace-Id` 后再查。",
                is_error=True,
            )
        if len(hi_trace_id) > _MAX_TRACE_ID_LEN:
            return _text_result(
                "hi_trace_id 不像合法的 Hi-Trace-Id（过长）。请核对后重试。",
                is_error=True,
            )

        logger.info("gateway trace call: hi_trace_id=%s", hi_trace_id)
        try:
            text = await client.fetch(hi_trace_id)
        except GatewayTraceError as e:
            logger.warning("gateway trace failed: %s", e)
            return _text_result(e.agent_hint, is_error=True)
        logger.info(
            "gateway trace ok: hi_trace_id=%s len=%d", hi_trace_id, len(text)
        )
        return _text_result(text)

    return handler


def build_gateway_trace_server(config: GatewayTraceConfig) -> McpSdkServerConfig:
    """构建挂网关链路排查工具的进程内 MCP server。

    工具 `query_gateway_trace(hi_trace_id)`：调 cat logview 接口取该请求的链路
    日志，解压 + HTML 转义还原后原样返给主 agent 解读。
    """
    handler = make_query_gateway_trace_handler(GatewayTraceClient(config))
    decorated = tool(TOOL_NAME, _TOOL_DESC, {"hi_trace_id": str})(handler)
    return create_sdk_mcp_server(
        name="ops-qa-gateway-trace", version="1.0.0", tools=[decorated]
    )
