"""通过飞书 IM 跟 lark-copilot（B）做 agent-to-agent 协作。

背景：ops-qa-bot 部署在内网，bot 身份没有 user-scope 的飞书文档读写权限；
lark-copilot 跑在能访问飞书文档的机器上，user 身份接 DM。本模块把"读飞书
文档并回答问题"这件事封装成一个 SDK MCP 工具 `ask_feishu_doc`，agent 在
prompt 引导下决定何时调；调用时通过飞书 IM 给 B 发结构化 envelope，等 B
ack 后把 answer 返给 agent。

协议（与 lark-copilot/agent_collab.py 对端）:
    请求文本: {"op":"doc_qa","req_id":"<uuid>","doc":"<url>","q":"<question>"}
    回复文本: {"op":"doc_qa_ack","req_id":"<same>","ok":true,"answer":"..."}
           或 {"op":"doc_qa_ack","req_id":"<same>","ok":false,"error":"..."}

关键设计：
- 关联回复用 req_id；FeishuDocRPC 维护 dict[req_id, asyncio.Future]
- WS event handler 在线程池里被调，转发到 asyncio loop 用 call_soon_threadsafe
- 单进程一个 RPC 实例：FeishuDocRPC 在 ws_server 启动时 init，tool 通过模块
  级 getter 拿到，避免在 @tool 装饰器里捕获 self
- 超时降级：tool 会在 ASK_TIMEOUT_SEC 后给 agent 返一段失败提示，agent 按
  prompt 走 fallback（提示用户去看原 URL）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from .feishu_core import FeishuClient

logger = logging.getLogger("ops_qa_bot.feishu_doc_tool")

ASK_TIMEOUT_SEC = float(os.environ.get("FEISHU_DOC_RPC_TIMEOUT_SEC", "60"))

# 熔断器参数：连续 N 次"peer 不可达"后冷却 M 秒不再发请求，到期再放行一次试探。
# - 触发条件只算"peer 不可达"（DM 发送失败 / 等 ack 超时），不算 peer 在线但
#   ok:false / 空 answer——后者是 peer 工作正常只是答不出来，不该让其他正常
#   问题陪着挨 60s 超时。
# - 默认 3 次连续不可达 ≈ 3 * 60s = 3 分钟才打开熔断，避免单次抖动误判
_CB_FAIL_THRESHOLD = int(os.environ.get("FEISHU_DOC_CB_FAIL_THRESHOLD", "3"))
_CB_COOLDOWN_SEC = float(os.environ.get("FEISHU_DOC_CB_COOLDOWN_SEC", "60"))


class FeishuDocRPC:
    """通过飞书 IM 与 lark-copilot 协作的 RPC 客户端。

    生命周期：进程级单例。ws_server 启动时 init，事件循环里 ask()，
    `deliver()` 由收到 peer DM 时调用（线程安全）。
    """

    def __init__(
        self,
        feishu: FeishuClient,
        peer_open_id: str,
        loop: asyncio.AbstractEventLoop,
    ):
        self._feishu = feishu
        self._peer_open_id = peer_open_id
        self._loop = loop
        self._pending: dict[str, asyncio.Future[dict]] = {}
        # 熔断状态：单线程 asyncio，无需锁。
        # _opened_at == 0 表示 closed；>0 是 open 起始的 monotonic 时间戳。
        # cooldown 过期后 _is_open() 主动重置成 closed 并放行下一次请求做试探。
        self._consecutive_failures: int = 0
        self._opened_at: float = 0.0

    @property
    def peer_open_id(self) -> str:
        return self._peer_open_id

    def _is_open(self) -> bool:
        if self._opened_at == 0.0:
            return False
        if time.monotonic() - self._opened_at >= _CB_COOLDOWN_SEC:
            logger.info(
                "feishu_doc_rpc circuit cooldown elapsed, allowing probe request"
            )
            self._opened_at = 0.0
            self._consecutive_failures = 0
            return False
        return True

    def _record_failure(self, reason: str) -> None:
        """只在"peer 不可达"路径调用（DM 发送失败 / 超时）。peer 在线但答错/答空不算。"""
        self._consecutive_failures += 1
        if (
            self._consecutive_failures >= _CB_FAIL_THRESHOLD
            and self._opened_at == 0.0
        ):
            self._opened_at = time.monotonic()
            logger.warning(
                "feishu_doc_rpc circuit OPEN after %d consecutive failures "
                "(last: %s); cooling down %.0fs",
                self._consecutive_failures,
                reason,
                _CB_COOLDOWN_SEC,
            )

    def _record_success(self) -> None:
        if self._consecutive_failures > 0 or self._opened_at > 0.0:
            logger.info(
                "feishu_doc_rpc circuit CLOSED (was failures=%d)",
                self._consecutive_failures,
            )
        self._consecutive_failures = 0
        self._opened_at = 0.0

    async def ask(
        self,
        doc_url: str,
        question: str,
        *,
        timeout: float = ASK_TIMEOUT_SEC,
    ) -> str:
        """给 peer 发 doc_qa 请求并等回复。返回 answer 字符串。失败抛 RuntimeError。"""
        if self._is_open():
            # 熔断打开期间快速失败，不发 DM、不创建 future——这是熔断的全部价值：
            # peer 挂着的时候，每个带飞书链接的问题不用再陪 60s 超时。
            raise RuntimeError(
                f"peer unavailable (circuit breaker open, "
                f"{_CB_FAIL_THRESHOLD}+ consecutive failures)"
            )

        req_id = uuid.uuid4().hex[:12]
        envelope = {
            "op": "doc_qa",
            "req_id": req_id,
            "doc": doc_url,
            "q": question,
        }
        text = json.dumps(envelope, ensure_ascii=False)
        fut: asyncio.Future[dict] = self._loop.create_future()
        self._pending[req_id] = fut
        logger.info(
            "feishu_doc_rpc out: req_id=%s doc=%s q=%r",
            req_id,
            doc_url,
            question[:80],
        )
        try:
            sent = await self._feishu.send_text_to_open_id(self._peer_open_id, text)
            if not sent:
                self._record_failure("dm send failed")
                raise RuntimeError("peer DM send failed (see feishu logs)")
            try:
                ack = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError as e:
                self._record_failure("timeout")
                raise RuntimeError(f"peer timeout after {timeout:.0f}s") from e
        finally:
            self._pending.pop(req_id, None)

        # peer 在线、回了 ack——下面两个分支都属于"peer 工作正常但本次答不出来"，
        # 不该让其他问题陪着熔断，只往上抛错让本次 agent 走 fallback 即可。
        # 同时算作"链路通"的证据：把连续失败计数清零。
        self._record_success()
        if not ack.get("ok"):
            err = str(ack.get("error") or "unknown error")
            raise RuntimeError(f"peer reported error: {err}")
        answer = ack.get("answer") or ""
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("peer returned empty answer")
        return answer.strip()

    def try_deliver(self, text: str) -> bool:
        """尝试把一条文本当作 ack envelope 投递。

        从事件回调线程调用：解析成功且 req_id 匹配未完成请求时 set_result，
        返回 True 表示这条消息已被协作通道消费、调用方应停止后续路由。

        text 不是合法 ack（或 req_id 不在 pending）时返回 False —— 调用方
        把消息当成普通 DM 继续处理。
        """
        s = (text or "").strip()
        if not s or not s.startswith("{"):
            return False
        try:
            obj = json.loads(s)
        except Exception:
            return False
        if not isinstance(obj, dict):
            return False
        if obj.get("op") != "doc_qa_ack":
            return False
        req_id = obj.get("req_id")
        if not isinstance(req_id, str):
            return False
        fut = self._pending.get(req_id)
        if fut is None or fut.done():
            logger.info("feishu_doc_rpc ack with no pending: req_id=%s", req_id)
            return True  # 仍认作协作消息，不要让 QA 流程把 JSON 当成提问
        # set_result 必须在 fut 所属 loop 的线程里
        self._loop.call_soon_threadsafe(_safe_set_result, fut, obj)
        logger.info("feishu_doc_rpc ack: req_id=%s ok=%s", req_id, obj.get("ok"))
        return True


def _safe_set_result(fut: asyncio.Future, value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


# --- module-level singleton -------------------------------------------------

_rpc: FeishuDocRPC | None = None


def init_rpc(
    feishu: FeishuClient,
    peer_open_id: str,
    loop: asyncio.AbstractEventLoop,
) -> FeishuDocRPC:
    """ws_server 启动时调一次。重复 init 会覆盖（适合 SIGHUP 热重载将来扩展）。"""
    global _rpc
    _rpc = FeishuDocRPC(feishu=feishu, peer_open_id=peer_open_id, loop=loop)
    logger.info("feishu_doc_rpc initialized: peer=%s", peer_open_id)
    return _rpc


def get_rpc() -> FeishuDocRPC | None:
    return _rpc


# --- SDK MCP tool -----------------------------------------------------------

@tool(
    "ask_feishu_doc",
    (
        "通过协作机器人（lark-copilot）读取飞书文档并基于该文档回答一个问题。"
        "适用场景：问题需要参考的资料保存在飞书云文档里，本机不能直接访问。"
        "参数 doc 为飞书文档 URL（含 docx/wiki 链接），question 为针对该文档"
        "的具体问题（短句更稳）。返回协作机器人基于该文档给出的答案文本——"
        "把它当成另一个 agent 的回答，可以引用、二次加工，不要伪装成你自己读的。"
    ),
    {"doc": str, "question": str},
)
async def ask_feishu_doc(args: dict) -> dict:
    rpc = get_rpc()
    if rpc is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "协作通道未就绪（FeishuDocRPC 未初始化）。请把该飞书文档 URL 转给用户人工查阅。",
                }
            ],
            "is_error": True,
        }
    doc = (args.get("doc") or "").strip()
    question = (args.get("question") or "").strip()
    if not doc or not question:
        return {
            "content": [
                {"type": "text", "text": "参数缺失：doc 和 question 都不能为空。"}
            ],
            "is_error": True,
        }
    try:
        answer = await rpc.ask(doc, question)
    except Exception as ex:  # noqa: BLE001
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"协作机器人未返回答案（{type(ex).__name__}: {ex}）。"
                        f"请把这个飞书链接转给用户人工查阅：{doc}"
                    ),
                }
            ],
            "is_error": True,
        }
    return {"content": [{"type": "text", "text": answer}]}


def build_mcp_server() -> McpSdkServerConfig:
    """ws_server 启动 OpsQABot 时把这个 server 塞进 ClaudeAgentOptions.mcp_servers。"""
    return create_sdk_mcp_server(
        name="feishu_doc",
        version="0.1.0",
        tools=[ask_feishu_doc],
    )
