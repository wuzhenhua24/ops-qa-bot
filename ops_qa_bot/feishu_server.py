"""HTTP 模式的飞书接入层：FastAPI webhook + 卡片回调。

长连接模式（见 `ws_server.py`）和 HTTP 模式共享 `feishu_core.py` 里的业务
核心（FeishuClient / SessionManager / handle_question / handle_feedback_click）。
本文件只负责"从 HTTP 请求解出事件 payload → 调用核心业务函数 → 返回 HTTP 响应"
这一层适配。

飞书开放平台配置：参见 README。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager

from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Request, Response
from lark_oapi.channel import FeishuChannel
from lark_oapi.channel.config import (
    ChatQueueConfig,
    PolicyConfig,
    SafetyConfig,
    TextBatchConfig,
)
from lark_oapi.channel.types import InboundMessage

from .config import AppConfig
from .feishu_core import (
    FeishuClient,
    SessionManager,
    _archive_ack_card,
    _clarify_giveup_ack_card,
    _extract_image_caption,
    _feedback_ack_card,
    _feedback_reason_form_card,
    _FOLLOWUP_LIBRARY,
    _followup_ack_card,
    _followup_error_card,
    _parse_post_content,
    get_archive_expected_owner,
    handle_archive_submit,
    handle_clarify_giveup_click,
    handle_feedback_click,
    handle_feedback_reason_skip,
    handle_feedback_reason_submit,
    handle_followup_click,
    handle_image_question,
    handle_post_question,
    handle_question,
    handle_unsupported_message,
)
from .feishu_crypto import FeishuCrypto
from .logging_config import request_id_var

logger = logging.getLogger("ops_qa_bot.feishu.http")


def create_app(config: AppConfig) -> FastAPI:
    docs_root = config.docs_root
    if not (docs_root / "INDEX.md").is_file():
        raise RuntimeError(f"docs_root 缺少 INDEX.md: {docs_root}")

    card_verify_token = config.feishu.card_verify_token
    admin_token = config.admin_token
    idle_ttl = config.session_idle_ttl

    # 消息事件 webhook 走 FeishuChannel——它内置：AES 解密、签名校验、verify_token、
    # url_verification challenge、event-id dedup、@所有人 检测（mentioned_all 字段）。
    # outbound 用同一个 channel 实例，避免重复的 token cache + bot identity 查询。
    #
    # safety/policy 配置说明：
    # - 飞书事件订阅 im.message.receive_v1 已经在订阅层过滤了"群里 @bot 才推送"，
    #   所以 require_mention=False 关掉 channel 这层重复检查；
    #   respond_to_mention_all=True 把 mentioned_all 决策让给我们 handler。
    # - 不批合并消息（delay_ms=0）、不做 per-chat 串行（chat_queue.enabled=False），
    #   保持现有"逐条处理"语义；并发安全由 SessionManager 的 per-(chat,user) lock 兜底。
    webhook_channel = FeishuChannel(
        app_id=config.feishu.app_id,
        app_secret=config.feishu.app_secret,
        encrypt_key=config.feishu.encrypt_key or None,
        verification_token=config.feishu.verify_token or None,
        transport="webhook",
        policy=PolicyConfig(
            require_mention=False,
            respond_to_mention_all=True,
        ),
        safety=SafetyConfig(
            text_batch=TextBatchConfig(delay_ms=0),
            chat_queue=ChatQueueConfig(enabled=False),
        ),
    )

    feishu = FeishuClient(channel=webhook_channel)
    session_mgr = SessionManager(
        docs_root=docs_root, idle_ttl=idle_ttl, doc_qa_config=config.doc_qa
    )

    # 卡片回调路径还保留旧的同步响应模式（飞书要求 3s 内同步返回新卡顶替原卡），
    # channel.on("cardAction") 是异步 schedule 模式对不上，所以 /feishu/card 路由
    # 沿用 FeishuCrypto + 手写校验 + 同步分发的老逻辑。
    crypto: FeishuCrypto | None = (
        FeishuCrypto(config.feishu.encrypt_key) if config.feishu.encrypt_key else None
    )

    # 卡片回调的幂等键：(message_id, qid, rating) 组合，10 分钟窗口覆盖飞书重试。
    # 消息事件的 event-id dedup 已经被 channel.SafetyPipeline 接管。
    seen_clicks: TTLCache = TTLCache(maxsize=10000, ttl=600)

    # 业务函数（handle_question 等）跑在 fastapi 主 loop 上（session_mgr 也在主 loop
    # 启动）；channel 的 on("message") handler 跑在 channel 的后台 loop 上，
    # 通过 run_coroutine_threadsafe 把业务 schedule 到主 loop，避免跨 loop
    # asyncio.Lock 协调问题。
    fastapi_loop_ref: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}

    async def _on_inbound(inbound: InboundMessage) -> None:
        """channel.on("message") handler — 在 channel bg loop 上跑。

        从 InboundMessage 解出业务参数，fire-and-forget 调度到 fastapi 主 loop。
        重复事件、url_verification、签名校验、@所有人 过滤、bot 自己发的消息全部
        由 channel 上游处理，这里只负责"事件 → 业务函数"的路由。
        """
        rid = (inbound.id or uuid.uuid4().hex)[:8]
        request_id_var.set(rid)

        chat_id = inbound.chat_id
        sender_id = inbound.sender_id
        msg_id = inbound.message_id
        message_type = inbound.raw_content_type

        if not chat_id or not sender_id or not message_type:
            return
        if inbound.sender.is_bot:
            # 其他 bot 转发 / 应用广播 / 多 bot 群里互相 @ 形成的消息环路：不答题
            return
        if inbound.mentioned_all:
            logger.info(
                "skip: @所有人 broadcast chat=%s user=%s type=%s",
                chat_id,
                sender_id,
                message_type,
            )
            return

        fastapi_loop = fastapi_loop_ref["loop"]
        if fastapi_loop is None:
            logger.error("fastapi loop not ready, drop event")
            return

        # image_key / post AST / caption 这些飞书原始 wire 字段还得从 raw 里挖；
        # InboundMessage.raw 就是飞书 message dict（已经从 v2 envelope 剥出来）。
        raw_content_str = (inbound.raw or {}).get("content") or "{}"
        try:
            content_dict = json.loads(raw_content_str)
        except json.JSONDecodeError:
            content_dict = {}

        if message_type == "image":
            image_key = content_dict.get("image_key")
            if not image_key or not msg_id:
                logger.info(
                    "image message missing key/msg_id: chat=%s user=%s",
                    chat_id,
                    sender_id,
                )
                return
            caption = _extract_image_caption(content_dict)
            asyncio.run_coroutine_threadsafe(
                handle_image_question(
                    chat_id,
                    sender_id,
                    image_key,
                    msg_id,
                    feishu,
                    session_mgr,
                    caption=caption,
                ),
                fastapi_loop,
            )
            return

        if message_type == "post":
            text, image_keys = _parse_post_content(content_dict)
            asyncio.run_coroutine_threadsafe(
                handle_post_question(
                    chat_id,
                    sender_id,
                    text,
                    image_keys,
                    msg_id,
                    feishu,
                    session_mgr,
                ),
                fastapi_loop,
            )
            return

        if message_type != "text":
            logger.info(
                "non-text message: type=%s chat=%s user=%s",
                message_type,
                chat_id,
                sender_id,
            )
            asyncio.run_coroutine_threadsafe(
                handle_unsupported_message(
                    chat_id, sender_id, msg_id, message_type, feishu
                ),
                fastapi_loop,
            )
            return

        # text 消息：从原 content.text 抽问题，按 mentions[].key 去掉 @bot 占位符
        question = (content_dict.get("text") or "").strip()
        for m in inbound.mentions:
            if m.key:
                question = question.replace(m.key, "").strip()
        if not question:
            return

        logger.info(
            "webhook received: chat=%s user=%s q=%r",
            chat_id,
            sender_id,
            question[:80],
        )
        asyncio.run_coroutine_threadsafe(
            handle_question(
                chat_id,
                sender_id,
                question,
                feishu,
                session_mgr,
                parent_msg_id=msg_id,
            ),
            fastapi_loop,
        )

    webhook_channel.on("message", _on_inbound)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "ops-qa-bot feishu server starting, docs_root=%s idle_ttl=%ss",
            docs_root,
            idle_ttl,
        )
        fastapi_loop_ref["loop"] = asyncio.get_running_loop()
        await session_mgr.start()
        # channel.connect() 在 webhook 模式下：起后台 loop + 同步 fetch bot
        # identity（10s 超时，失败会进 retry loop 不阻塞）+ 构建 dispatcher。
        await webhook_channel.connect()
        try:
            yield
        finally:
            logger.info("closing all sessions ...")
            await webhook_channel.disconnect()
            await session_mgr.stop()
            fastapi_loop_ref["loop"] = None
            logger.info("ops-qa-bot feishu server stopped")

    app = FastAPI(lifespan=lifespan)

    async def _read_and_decode(req: Request) -> dict:
        """读 body → 签名校验（可选）→ AES 解密（可选）→ 返回原始 payload dict。

        卡片回调专用：飞书要求同步返回新卡顶替原卡，channel 的 cardAction
        路径是异步 schedule 模式对不上，所以这条路径保留手写解密 + 校验。
        消息事件 webhook 已经委托给 FeishuChannel，不再走这里。
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
    async def webhook(req: Request) -> Response:
        """消息事件 webhook 入口。

        全权委托给 FeishuChannel：AES 解密、签名校验、verification_token、
        url_verification challenge、event-id dedup、@所有人 检测、sender_type
        过滤都在 channel 里跑完，业务路由由 channel.on("message", _on_inbound)
        在 channel 后台 loop 上 fire-and-forget 到 fastapi 主 loop。
        """
        body = await req.body()
        status, content = await webhook_channel.handle_webhook_request(
            dict(req.headers), body
        )
        return Response(
            content=content,
            status_code=status,
            media_type="application/json",
        )

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
            asker_id = value.get("asker_id")
            if not qid or rating not in ("up", "down"):
                return {}
            # 非 asker 点击不进 dedup 缓存：缓存了之后第二次点击会被认作"重试"走 replay
            # 路径返回 form/ack（错的），把 asker 的原卡顶掉。直接走 handler 走拒绝路径，
            # 它会记 feedback_rejected 日志 + 返回原反馈卡（视觉无变化），asker 的按钮还在。
            if clicker_id and asker_id and clicker_id != asker_id:
                ack_card = handle_feedback_click(
                    qid=qid,
                    rating=rating,
                    clicker_id=clicker_id,
                    asker_id=asker_id,
                )
                return {"card": {"type": "raw", "data": ack_card}}
            # 去重：卡片回调同样会重试（无 event_id，用 message + qid + 点击人 + 方向作组合键）。
            click_key = f"{msg_id}|{qid}|{clicker_id}|{rating}"
            if click_key in seen_clicks:
                logger.info("duplicate card click, skip: key=%s", click_key)
                # 重试要返回和首次一致的卡片，否则 👎 的原因表单会被 ack 顶掉
                replay = (
                    _feedback_reason_form_card(qid, asker_id)
                    if rating == "down"
                    else _feedback_ack_card(rating)
                )
                return {"card": {"type": "raw", "data": replay}}
            seen_clicks[click_key] = True
            ack_card = handle_feedback_click(
                qid=qid,
                rating=rating,
                clicker_id=clicker_id,
                asker_id=asker_id,
            )
            # 用 raw 包一层兼容 v1（👍 ack）与 v2（👎 表单）两种 schema
            return {"card": {"type": "raw", "data": ack_card}}

        if action_name in ("feedback_reason_submit", "feedback_reason_skip"):
            qid = value.get("qid")
            asker_id = value.get("asker_id")
            # 非 asker 不进 dedup 缓存：见 feedback 分支同样的注释。reason 表单这边
            # 风险更高——缓存了之后 replay 直接返回 _feedback_ack_card("down")，会把
            # asker 还在填的 reason 表单顶成 ack。
            if clicker_id and asker_id and clicker_id != asker_id:
                if action_name == "feedback_reason_skip":
                    ack_card = handle_feedback_reason_skip(qid, clicker_id, asker_id)
                else:
                    form_value = action.get("form_value") or {}
                    raw_reasons = form_value.get("reasons")
                    if isinstance(raw_reasons, str):
                        reasons = [raw_reasons]
                    elif isinstance(raw_reasons, list):
                        reasons = [r for r in raw_reasons if isinstance(r, str)]
                    else:
                        reasons = None
                    comment = form_value.get("comment") or None
                    ack_card = handle_feedback_reason_submit(
                        qid, reasons, comment, clicker_id, asker_id
                    )
                return {"card": {"type": "raw", "data": ack_card}}
            click_key = f"{msg_id}|reason|{qid}|{clicker_id}|{action_name}"
            if click_key in seen_clicks:
                logger.info("duplicate reason click, skip: key=%s", click_key)
                return {
                    "card": {"type": "raw", "data": _feedback_ack_card("down")}
                }
            seen_clicks[click_key] = True
            if action_name == "feedback_reason_skip":
                ack_card = handle_feedback_reason_skip(qid, clicker_id, asker_id)
            else:
                form_value = action.get("form_value") or {}
                # multi_select_static 返回 list[str]；防御兼容历史 str 值（飞书 SDK
                # 调整或回放旧事件时可能塞单字符串），统一归并成 list 喂给下游
                raw_reasons = form_value.get("reasons")
                if isinstance(raw_reasons, str):
                    reasons = [raw_reasons]
                elif isinstance(raw_reasons, list):
                    reasons = [r for r in raw_reasons if isinstance(r, str)]
                else:
                    reasons = None
                comment = form_value.get("comment") or None
                ack_card = handle_feedback_reason_submit(
                    qid, reasons, comment, clicker_id, asker_id
                )
            return {"card": {"type": "raw", "data": ack_card}}

        if action_name == "followup":
            qid = value.get("qid")
            key = value.get("key")
            chat_id_v = value.get("chat_id")
            asker_id = value.get("asker_id")
            parent_msg_id_v = value.get("parent_msg_id")
            # 非 asker 不进 dedup 缓存：见 feedback 分支同样的注释。
            if clicker_id and asker_id and clicker_id != asker_id:
                ack_card = await handle_followup_click(
                    qid,
                    key,
                    chat_id_v,
                    asker_id,
                    clicker_id,
                    feishu,
                    session_mgr,
                    parent_msg_id=parent_msg_id_v,
                )
                return {"card": {"type": "raw", "data": ack_card}}
            click_key = f"{msg_id}|followup|{qid}|{key}|{clicker_id}"
            if click_key in seen_clicks:
                logger.info("duplicate followup click, skip: key=%s", click_key)
                # 重放：用同样 label 的 ack（key 失效就报错卡）
                if key in _FOLLOWUP_LIBRARY:
                    replay = _followup_ack_card(_FOLLOWUP_LIBRARY[key][0])
                else:
                    replay = _followup_error_card("追问类型无效。")
                return {"card": {"type": "raw", "data": replay}}
            seen_clicks[click_key] = True
            ack_card = await handle_followup_click(
                qid,
                key,
                chat_id_v,
                asker_id,
                clicker_id,
                feishu,
                session_mgr,
                parent_msg_id=parent_msg_id_v,
            )
            return {"card": {"type": "raw", "data": ack_card}}

        if action_name == "clarify_giveup":
            qid = value.get("qid")
            chat_id_v = value.get("chat_id")
            asker_id = value.get("asker_id")
            parent_msg_id_v = value.get("parent_msg_id")
            # 非 asker 不进 dedup 缓存：见 feedback 分支同样的注释。
            if clicker_id and asker_id and clicker_id != asker_id:
                ack_card = await handle_clarify_giveup_click(
                    qid,
                    chat_id_v,
                    asker_id,
                    clicker_id,
                    feishu,
                    session_mgr,
                    parent_msg_id=parent_msg_id_v,
                )
                return {"card": {"type": "raw", "data": ack_card}}
            click_key = f"{msg_id}|clarify_giveup|{qid}|{clicker_id}"
            if click_key in seen_clicks:
                logger.info("duplicate clarify_giveup click, skip: key=%s", click_key)
                return {
                    "card": {"type": "raw", "data": _clarify_giveup_ack_card()}
                }
            seen_clicks[click_key] = True
            ack_card = await handle_clarify_giveup_click(
                qid,
                chat_id_v,
                asker_id,
                clicker_id,
                feishu,
                session_mgr,
                parent_msg_id=parent_msg_id_v,
            )
            return {"card": {"type": "raw", "data": ack_card}}

        if action_name == "archive_submit":
            qid = value.get("qid")
            form_value = action.get("form_value") or {}
            answer = form_value.get("answer") or ""
            question = form_value.get("question") or ""
            # 非 owner 提交不进 dedup 缓存：见 feedback 分支同样的注释。这里风险更高
            # ——dedup 命中后 replay 直接返回 _archive_ack_card("已处理")，会把 owner
            # 还在填的归档表单顶成 ack。
            expected_owner = get_archive_expected_owner(qid)
            if (
                clicker_id
                and expected_owner
                and clicker_id != expected_owner
            ):
                ack_card = await handle_archive_submit(
                    qid, question, answer, clicker_id, docs_root
                )
                return {"card": {"type": "raw", "data": ack_card}}
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
                qid, question, answer, clicker_id, docs_root, feishu=feishu
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
