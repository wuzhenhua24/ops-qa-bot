"""长连接模式：通过 lark-oapi SDK 的 WebSocket 客户端接收飞书事件。

相比 HTTP 模式（`feishu_server.py`）的差异：
- 不需要开公网 HTTPS 入站端口、TLS 证书、反向代理、IP 白名单
- 不需要 encrypt_key / verify_token（SDK 自己处理鉴权）
- 事件和卡片按钮点击统一走 `card.action.trigger` 事件（v2）
- 单进程只能服务一个 app_id

业务逻辑（OpsQABot、SessionManager、反馈日志、占位消息等）完全复用
`feishu_server.py` 里抽出来的 `handle_question` / `handle_feedback_click`。

飞书开放平台配置：
- 事件订阅方式选 "长连接"（不配 Request URL）
- 订阅事件：`im.message.receive_v1`、`card.action.trigger`
- 消息卡片的"接收方式"选 "事件订阅"（对应 card.action.trigger）
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path

import lark_oapi as lark
from cachetools import TTLCache
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from .config import AppConfig
from .feishu_server import (
    FeishuClient,
    SessionManager,
    handle_feedback_click,
    handle_question,
)
from .logging_config import request_id_var

logger = logging.getLogger("ops_qa_bot.ws")


class WsRunner:
    """长连接模式的运行主体。

    - SDK 的事件回调是同步的（线程池里跑）→ 用 `run_coroutine_threadsafe` 桥接到 asyncio
    - asyncio 主循环负责 SessionManager、业务流程、对外 httpx 调用
    - WS 客户端在后台线程里阻塞 `start()`，asyncio 主循环永远 sleep
    """

    def __init__(self, config: AppConfig):
        docs_root = config.docs_root
        if not (docs_root / "INDEX.md").is_file():
            raise RuntimeError(f"docs_root 缺少 INDEX.md: {docs_root}")

        self._config = config
        self._feishu = FeishuClient(config.feishu.app_id, config.feishu.app_secret)
        self._session_mgr = SessionManager(
            docs_root=docs_root, idle_ttl=config.session_idle_ttl
        )
        # WS 下 SDK 一般自己去重，但保险起见我们也按 event_id/click key 兜一层
        self._seen_events: TTLCache = TTLCache(maxsize=10000, ttl=600)
        self._seen_clicks: TTLCache = TTLCache(maxsize=10000, ttl=600)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 事件回调（SDK 从线程池调用，同步接口）
    # ------------------------------------------------------------------

    def _on_message(self, event: P2ImMessageReceiveV1) -> None:
        header = getattr(event, "header", None)
        event_id = getattr(header, "event_id", None) if header else None

        import uuid as _uuid
        rid = event_id[:8] if event_id else "ws" + _uuid.uuid4().hex[:6]
        request_id_var.set(rid)

        # 去重
        if event_id:
            if event_id in self._seen_events:
                logger.info("duplicate event, skip: event_id=%s", event_id)
                return
            self._seen_events[event_id] = True

        data = event.event
        msg = data.message if data else None
        sender = data.sender if data else None
        if not msg or not sender:
            return
        if msg.message_type != "text":
            return
        if sender.sender_type != "user":
            return

        chat_id = msg.chat_id
        sender_id = sender.sender_id.open_id if sender.sender_id else None
        if not chat_id or not sender_id:
            return

        try:
            content = json.loads(msg.content or "{}")
        except json.JSONDecodeError:
            return
        question = (content.get("text") or "").strip()
        for mention in msg.mentions or []:
            key = getattr(mention, "key", None)
            if key:
                question = question.replace(key, "").strip()
        if not question:
            return

        logger.info(
            "ws message: chat=%s user=%s q=%r", chat_id, sender_id, question[:80]
        )

        # 交给 asyncio 主循环跑业务
        if self._loop is None:
            logger.error("asyncio loop not initialized, drop event")
            return
        asyncio.run_coroutine_threadsafe(
            handle_question(
                chat_id, sender_id, question, self._feishu, self._session_mgr
            ),
            self._loop,
        )

    def _on_card_action(
        self, event: P2CardActionTrigger
    ) -> P2CardActionTriggerResponse:
        import uuid as _uuid
        event_id = (
            event.header.event_id
            if getattr(event, "header", None) and event.header.event_id
            else None
        )
        rid = "c" + (event_id[:7] if event_id else _uuid.uuid4().hex[:7])
        request_id_var.set(rid)

        data = event.event
        if not data or not data.action or not data.action.value:
            return P2CardActionTriggerResponse({})
        value = data.action.value
        if value.get("action") != "feedback":
            return P2CardActionTriggerResponse({})

        qid = value.get("qid")
        rating = value.get("rating")
        clicker_id = data.operator.open_id if data.operator else None
        if not qid or rating not in ("up", "down"):
            return P2CardActionTriggerResponse({})

        # 去重：点击重试场景
        click_key = f"{data.context.open_message_id if data.context else ''}|{qid}|{clicker_id}|{rating}"
        if click_key in self._seen_clicks:
            logger.info("duplicate card click, skip: key=%s", click_key)
            # 即使重复也返回 ack 卡片，保证 UI 一致
            from .feishu_server import _feedback_ack_card
            return P2CardActionTriggerResponse(
                {"card": {"type": "raw", "data": _feedback_ack_card(rating)}}
            )
        self._seen_clicks[click_key] = True

        ack_card = handle_feedback_click(
            qid=qid,
            rating=rating,
            clicker_id=clicker_id,
            asker_id=value.get("asker_id"),
        )
        # v2 卡片回调响应格式：{"type": "raw", "data": <card json>}
        return P2CardActionTriggerResponse(
            {"card": {"type": "raw", "data": ack_card}}
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _build_ws_client(self) -> lark.ws.Client:
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .register_p2_card_action_trigger(self._on_card_action)
            .build()
        )
        return lark.ws.Client(
            self._config.feishu.app_id,
            self._config.feishu.app_secret,
            event_handler=handler,
        )

    async def run(self) -> None:
        """主入口：启动 asyncio 主循环 + 后台 WS 线程，阻塞直到 Ctrl+C。"""
        self._loop = asyncio.get_running_loop()
        await self._session_mgr.start()

        ws_client = self._build_ws_client()
        self._ws_thread = threading.Thread(
            target=ws_client.start, daemon=True, name="lark-ws-client"
        )
        self._ws_thread.start()
        logger.info(
            "ws server started: app_id=%s docs_root=%s idle_ttl=%ss",
            self._config.feishu.app_id,
            self._config.docs_root,
            self._config.session_idle_ttl,
        )

        try:
            # 主协程永远等着，让 SDK 线程和业务协程跑起来
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("ws server stopping, closing sessions ...")
            await self._session_mgr.stop()
            logger.info("ws server stopped")
