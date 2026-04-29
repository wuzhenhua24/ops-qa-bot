"""HTTP 模式的飞书接入层：FastAPI webhook + 卡片回调。

长连接模式（见 `ws_server.py`）和 HTTP 模式共享 `feishu_core.py` 里的业务
核心（FeishuClient / SessionManager / handle_question / handle_feedback_click）。
本文件只负责"从 HTTP 请求解出事件 payload → 调用核心业务函数 → 返回 HTTP 响应"
这一层适配。

飞书开放平台配置：参见 README。
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager

from cachetools import TTLCache
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .config import AppConfig
from .feishu_core import (
    FeishuClient,
    SessionManager,
    _archive_ack_card,
    _feedback_ack_card,
    handle_archive_submit,
    handle_feedback_click,
    handle_question,
)
from .feishu_crypto import FeishuCrypto
from .logging_config import request_id_var

logger = logging.getLogger("ops_qa_bot.feishu.http")


def _extract_event(event: dict) -> tuple[str | None, str | None, str | None]:
    """从飞书事件抽出 (chat_id, sender_open_id, question)。不是有效文本消息返回全 None。"""
    msg = event.get("message") or {}
    if msg.get("message_type") != "text":
        return None, None, None
    chat_id = msg.get("chat_id")

    sender = event.get("sender") or {}
    # 只处理真人发送的消息：过滤 sender_type != "user"，避免其他 bot 转发、
    # 应用广播、甚至多 bot 群里互相 @ 形成的消息环路触发答题。
    if sender.get("sender_type") != "user":
        return None, None, None
    sender_id = (sender.get("sender_id") or {}).get("open_id")
    if not chat_id or not sender_id:
        return None, None, None

    try:
        content = json.loads(msg.get("content") or "{}")
    except json.JSONDecodeError:
        return None, None, None
    question = (content.get("text") or "").strip()
    # 群聊里去掉 @bot 的提及占位符
    for mention in msg.get("mentions") or []:
        key = mention.get("key")
        if key:
            question = question.replace(key, "").strip()
    return chat_id, sender_id, (question or None)


def create_app(config: AppConfig) -> FastAPI:
    docs_root = config.docs_root
    if not (docs_root / "INDEX.md").is_file():
        raise RuntimeError(f"docs_root 缺少 INDEX.md: {docs_root}")

    verify_token = config.feishu.verify_token
    card_verify_token = config.feishu.card_verify_token
    admin_token = config.admin_token
    idle_ttl = config.session_idle_ttl

    feishu = FeishuClient(config.feishu.app_id, config.feishu.app_secret)
    session_mgr = SessionManager(docs_root=docs_root, idle_ttl=idle_ttl)

    # 配置了 encrypt_key 就启用签名校验 + AES 解密，否则维持原行为（依赖 verify_token + IP 白名单）
    crypto: FeishuCrypto | None = (
        FeishuCrypto(config.feishu.encrypt_key) if config.feishu.encrypt_key else None
    )

    # 飞书事件/卡片回调在超时或网络抖动时会重试，用 TTLCache 按 event_id
    # 和 (message_id, qid, rating) 做幂等，10 分钟窗口覆盖飞书重试周期。
    # asyncio 单线程下，TTLCache 的 __contains__ / __setitem__ 都不 await，
    # 查-写之间不会被抢占，无须额外加锁。
    seen_events: TTLCache = TTLCache(maxsize=10000, ttl=600)
    seen_clicks: TTLCache = TTLCache(maxsize=10000, ttl=600)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "ops-qa-bot feishu server starting, docs_root=%s idle_ttl=%ss",
            docs_root,
            idle_ttl,
        )
        await session_mgr.start()
        try:
            yield
        finally:
            logger.info("closing all sessions ...")
            await session_mgr.stop()
            logger.info("ops-qa-bot feishu server stopped")

    app = FastAPI(lifespan=lifespan)

    async def process_question(chat_id: str, user_id: str, question: str) -> None:
        await handle_question(chat_id, user_id, question, feishu, session_mgr)

    def _check_verify_token(payload: dict) -> None:
        if not verify_token:
            return
        token = (
            (payload.get("header") or {}).get("token")  # v2
            or payload.get("token")  # v1 / url_verification
        )
        if token != verify_token:
            raise HTTPException(status_code=403, detail="invalid verify token")

    async def _read_and_decode(req: Request) -> dict:
        """读 body → 签名校验（可选）→ AES 解密（可选）→ 返回原始 payload dict。

        encrypt_key 未配置时走老路径，只 json 解析 body。配置后全流程都走。
        """
        body = await req.body()
        if crypto is not None:
            ts = req.headers.get("X-Lark-Request-Timestamp", "")
            nonce = req.headers.get("X-Lark-Request-Nonce", "")
            sig = req.headers.get("X-Lark-Signature", "")
            if not crypto.verify_sig(ts, nonce, body, sig):
                logger.warning("signature verification failed")
                raise HTTPException(status_code=401, detail="invalid signature")
        wrapped = json.loads(body)
        if crypto is not None:
            return crypto.unwrap(wrapped)
        return wrapped

    @app.post("/feishu/webhook")
    async def webhook(req: Request, background: BackgroundTasks):
        payload = await _read_and_decode(req)

        # 每个请求生成 correlation id：优先用飞书 event_id 前 8 位（方便对照飞书后台），
        # 没有就随机。同一请求链路（含 BackgroundTasks 里的 process_question）所有日志都会带。
        event_id = (payload.get("header") or {}).get("event_id") or ""
        rid = event_id[:8] if event_id else uuid.uuid4().hex[:8]
        request_id_var.set(rid)

        # 1. URL 校验（配置 webhook 时飞书会打一次 challenge）
        if payload.get("type") == "url_verification":
            _check_verify_token(payload)
            return {"challenge": payload["challenge"]}

        # 2. 事件校验
        _check_verify_token(payload)

        # 3. 去重：飞书重试时 event_id 不变；第一条请求会 add_task 立即返 200，
        # 重试打到这里就直接跳过，避免重复答题（也顺带防用户恶意重放）。
        if event_id:
            if event_id in seen_events:
                logger.info("duplicate event, skip: event_id=%s", event_id)
                return {"code": 0}
            seen_events[event_id] = True

        # 4. 解析消息事件（v2 格式）
        event = payload.get("event") or {}
        chat_id, sender_id, question = _extract_event(event)
        if not chat_id or not sender_id or not question:
            return {"code": 0}

        logger.info(
            "webhook received: chat=%s user=%s q=%r",
            chat_id,
            sender_id,
            question[:80],
        )
        # 5. 后台处理，立即返回（飞书要求 3 秒内响应）
        background.add_task(process_question, chat_id, sender_id, question)
        return {"code": 0}

    @app.post("/feishu/card")
    async def card_callback(req: Request):
        """飞书交互卡片回调。

        在飞书开放平台：功能 → 机器人 → 消息卡片请求网址，配置为
        `https://<host>/feishu/card`。首次保存时飞书会打 url_verification。
        """
        payload = await _read_and_decode(req)

        # 卡片回调也分配一个 rid，便于和问答链路关联查问题
        request_id_var.set("c" + uuid.uuid4().hex[:7])

        # URL 校验（和 webhook 同逻辑）
        if payload.get("type") == "url_verification":
            if card_verify_token and payload.get("token") != card_verify_token:
                raise HTTPException(status_code=403, detail="invalid verify token")
            return {"challenge": payload["challenge"]}

        # token 校验（卡片回调的 token 在 payload 顶层）
        if card_verify_token and payload.get("token") != card_verify_token:
            raise HTTPException(status_code=403, detail="invalid verify token")

        action = payload.get("action") or {}
        value = action.get("value") or {}
        action_name = value.get("action")
        clicker_id = payload.get("open_id") or payload.get("user_id")
        msg_id = payload.get("open_message_id") or ""

        if action_name == "feedback":
            qid = value.get("qid")
            rating = value.get("rating")
            if not qid or rating not in ("up", "down"):
                return {}
            # 去重：卡片回调同样会重试（无 event_id，用 message + qid + 点击人 + 方向作组合键）。
            click_key = f"{msg_id}|{qid}|{clicker_id}|{rating}"
            if click_key in seen_clicks:
                logger.info("duplicate card click, skip: key=%s", click_key)
                return {"card": _feedback_ack_card(rating)}
            seen_clicks[click_key] = True
            ack_card = handle_feedback_click(
                qid=qid,
                rating=rating,
                clicker_id=clicker_id,
                asker_id=value.get("asker_id"),
            )
            # 返回新卡片替换原按钮卡片（防止重复点击）
            return {"card": ack_card}

        if action_name == "archive_submit":
            qid = value.get("qid")
            form_value = action.get("form_value") or {}
            answer = form_value.get("answer") or ""
            click_key = f"{msg_id}|archive|{qid}|{clicker_id}"
            if click_key in seen_clicks:
                logger.info("duplicate archive submit, skip: key=%s", click_key)
                return {
                    "card": {
                        "type": "raw",
                        "data": _archive_ack_card("ℹ️", "已处理。"),
                    }
                }
            seen_clicks[click_key] = True
            ack_card = await handle_archive_submit(
                qid, answer, clicker_id, docs_root
            )
            # v2 卡片用 type:raw 包一层，确保飞书按 v2 渲染
            return {"card": {"type": "raw", "data": ack_card}}

        return {}  # 其他类型的按钮暂不处理

    @app.get("/healthz")
    async def healthz():
        return {
            "ok": True,
            "active_sessions": session_mgr.active_count(),
        }

    def _check_admin(req: Request) -> None:
        if admin_token is None:
            return
        provided = req.headers.get("X-Admin-Token") or req.query_params.get("token")
        if provided != admin_token:
            raise HTTPException(status_code=403, detail="forbidden")

    @app.get("/admin/sessions")
    async def list_sessions(req: Request):
        """列出当前活跃会话。配置 ADMIN_TOKEN 环境变量后需带 X-Admin-Token 请求头。"""
        _check_admin(req)
        sessions = await session_mgr.snapshot()
        return {
            "count": len(sessions),
            "idle_ttl_seconds": session_mgr.idle_ttl,
            "sessions": sessions,
        }

    return app
