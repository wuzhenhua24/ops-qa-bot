"""HTTP 模式的飞书接入层：FastAPI 统一 webhook（消息 + 卡片回调）。

长连接模式（见 `ws_server.py`）和 HTTP 模式共享 `feishu_core.py` 里的业务
核心（FeishuClient / SessionManager / handle_question / handle_feedback_click）。
本文件只负责"从 HTTP 请求解出事件 payload → 调用核心业务函数 → 返回 HTTP 响应"
这一层适配。

消息事件 + 卡片回调都通过同一个 `/feishu/webhook` 端点进入 FeishuChannel，
由 SDK 内置的 dispatcher 按 ``event_type`` 分流：``im.message.receive_v1``
走 ``on("message")``，``card.action.trigger`` 走 ``on("cardAction")``。AES
解密 / 签名校验 / verification_token / url_verification challenge / 事件
dedup 全在 channel 里跑——业务侧不再维护手写的加解密、token 校验、TTLCache
幂等。

飞书开放平台配置：参见 README。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from lark_oapi.channel import FeishuChannel
from lark_oapi.channel.config import (
    ChatQueueConfig,
    PolicyConfig,
    SafetyConfig,
    TextBatchConfig,
)
from lark_oapi.channel.types import (
    CardActionEvent,
    ImageContent,
    InboundMessage,
    PostContent,
    TextContent,
)

from .config import AppConfig
from .feishu_core import (
    FeishuClient,
    SessionManager,
    _archive_ack_card,
    _extract_image_caption,
    _followup_error_card,
    _parse_post_text,
    card_form_value,
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
    normalize_card_reasons,
)
from .logging_config import request_id_var

logger = logging.getLogger("ops_qa_bot.feishu.http")


def create_app(config: AppConfig) -> FastAPI:
    docs_root = config.docs_root
    if not (docs_root / "INDEX.md").is_file():
        raise RuntimeError(f"docs_root 缺少 INDEX.md: {docs_root}")

    admin_token = config.admin_token
    idle_ttl = config.session_idle_ttl

    # 消息事件 + 卡片回调都走 FeishuChannel——它内置：AES 解密、签名校验、
    # verification_token、url_verification challenge、event-id dedup、
    # cardAction dedup（按 ``card:{msg_id}:{operator}:{tag}:{value}``）、
    # @所有人 检测（mentioned_all 字段）。outbound 用同一个 channel 实例，
    # 避免重复的 token cache + bot identity 查询。
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

    # 业务函数（handle_question 等）跑在 fastapi 主 loop 上（session_mgr 也在主 loop
    # 启动）；channel 的 on(...) handler 跑在 channel 后台 loop 上，通过
    # run_coroutine_threadsafe 把业务 schedule 到主 loop，避免跨 loop
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

        # SDK 的 InboundPipeline 已经把 content JSON 解析成 typed dataclass，
        # image_key / post AST 直接从 inbound.content 拿；post 里的 image
        # 走 inbound.resources（converter 抽完了）。文本走 content.raw（原始
        # 未解析 @ 的形式），保留按 mentions[].key 整段剥 @ 的语义。
        content = inbound.content

        if isinstance(content, ImageContent):
            if not content.image_key or not msg_id:
                logger.info(
                    "image message missing key/msg_id: chat=%s user=%s",
                    chat_id,
                    sender_id,
                )
                return
            caption = _extract_image_caption(content.raw)
            asyncio.run_coroutine_threadsafe(
                handle_image_question(
                    chat_id,
                    sender_id,
                    content.image_key,
                    msg_id,
                    feishu,
                    session_mgr,
                    caption=caption,
                ),
                fastapi_loop,
            )
            return

        if isinstance(content, PostContent):
            text = _parse_post_text(content.post)
            image_keys = [
                r.file_key for r in inbound.resources if r.type == "image"
            ]
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

        if not isinstance(content, TextContent):
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

        # text 消息：用 content.raw 里的原始文本（@_user_N 占位符未替换），
        # 配合 mentions[].key 整段剥 @ 占位。inbound.content.text 已被 SDK
        # resolve_mentions 成 @Name，再按 m.key replace 会 miss——所以走 raw。
        question = (content.raw.get("text") or "").strip()
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

    async def _on_card_action(event: CardActionEvent) -> None:
        """channel.on("cardAction") handler — 在 channel bg loop 上跑。

        sync handler（feedback 三个）直接调（只访问 logger / 内存 dict，
        不绑 loop）；async handler（followup / clarify_giveup / archive_submit）
        访问 session_mgr / feishu 等 fastapi loop 上的资源，通过
        ``run_coroutine_threadsafe`` 桥到主 loop 等结果。

        channel 内置 dedup 按 ``card:{msg_id}:{operator}:{tag}:{value}``——
        重复点击直接 drop 不到这里，旧版的 TTLCache 幂等不再需要。非 asker
        的拒绝走业务函数自己的 reject 路径（返回原卡视觉无变化）。
        """
        request_id_var.set("c" + uuid.uuid4().hex[:7])

        action = event.action
        value = action.value if isinstance(action.value, dict) else {}
        action_name = value.get("action")
        msg_id = event.message_id
        clicker_id = event.operator.open_id or None
        form_value = card_form_value(event)

        fastapi_loop = fastapi_loop_ref["loop"]
        if fastapi_loop is None:
            logger.error("fastapi loop not ready, drop card action")
            return

        async def _bridge(coro):
            """把 coroutine schedule 到 fastapi loop 并 await 结果。"""
            return await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, fastapi_loop)
            )

        ack_card: dict | None = None
        try:
            if action_name == "feedback":
                qid = value.get("qid")
                rating = value.get("rating")
                asker_id = value.get("asker_id")
                if not qid or rating not in ("up", "down"):
                    return
                ack_card = handle_feedback_click(
                    qid=qid, rating=rating,
                    clicker_id=clicker_id, asker_id=asker_id,
                )

            elif action_name == "feedback_reason_submit":
                qid = value.get("qid")
                asker_id = value.get("asker_id")
                reasons = normalize_card_reasons(form_value)
                comment = form_value.get("comment") or None
                ack_card = handle_feedback_reason_submit(
                    qid, reasons, comment, clicker_id, asker_id
                )

            elif action_name == "feedback_reason_skip":
                qid = value.get("qid")
                asker_id = value.get("asker_id")
                ack_card = handle_feedback_reason_skip(qid, clicker_id, asker_id)

            elif action_name == "followup":
                qid = value.get("qid")
                key = value.get("key")
                chat_id_v = value.get("chat_id")
                asker_id = value.get("asker_id")
                parent_msg_id_v = value.get("parent_msg_id")
                try:
                    ack_card = await _bridge(
                        handle_followup_click(
                            qid, key, chat_id_v, asker_id, clicker_id,
                            feishu, session_mgr,
                            parent_msg_id=parent_msg_id_v,
                        )
                    )
                except Exception:
                    logger.exception(
                        "followup click failed: qid=%s key=%s", qid, key
                    )
                    ack_card = _followup_error_card("追问触发失败，请重试。")

            elif action_name == "clarify_giveup":
                qid = value.get("qid")
                chat_id_v = value.get("chat_id")
                asker_id = value.get("asker_id")
                parent_msg_id_v = value.get("parent_msg_id")
                try:
                    ack_card = await _bridge(
                        handle_clarify_giveup_click(
                            qid, chat_id_v, asker_id, clicker_id,
                            feishu, session_mgr,
                            parent_msg_id=parent_msg_id_v,
                        )
                    )
                except Exception:
                    logger.exception("clarify_giveup click failed: qid=%s", qid)
                    ack_card = _followup_error_card("触发失败，请重试。")

            elif action_name == "archive_submit":
                qid = value.get("qid")
                answer = form_value.get("answer") or ""
                question = form_value.get("question") or ""
                try:
                    ack_card = await _bridge(
                        handle_archive_submit(
                            qid, question, answer, clicker_id,
                            docs_root, feishu=feishu,
                        )
                    )
                except Exception:
                    logger.exception("archive submit failed: qid=%s", qid)
                    ack_card = _archive_ack_card("❌", "归档失败，请联系管理员。")
            else:
                return  # 其他类型的按钮暂不处理
        except Exception:
            logger.exception("cardAction handler failed: action=%s", action_name)
            return

        if ack_card is None or not msg_id:
            return
        result = await webhook_channel.update_card(msg_id, ack_card)
        if not result.success:
            logger.error(
                "update_card failed: msg_id=%s action=%s err=%s",
                msg_id, action_name, result.error,
            )

    webhook_channel.on("message", _on_inbound)
    webhook_channel.on("cardAction", _on_card_action)

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

    @app.post("/feishu/webhook")
    async def webhook(req: Request) -> Response:
        """飞书统一 webhook 入口（消息事件 + 卡片回调）。

        全权委托给 FeishuChannel：AES 解密、签名校验、verification_token、
        url_verification challenge、event-id dedup、cardAction dedup、@所有人
        检测、sender_type 过滤都在 channel 里跑完。channel dispatcher 按
        event_type 把事件分流到 ``on("message", _on_inbound)`` /
        ``on("cardAction", _on_card_action)``，handler 在 channel 后台 loop
        上跑，通过 run_coroutine_threadsafe 把业务 schedule 到 fastapi 主 loop。

        飞书后台"消息卡片请求网址"和"事件订阅请求网址"都填同一个 URL 即可。
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
