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
from lark_oapi.channel.types import CardActionEvent, InboundMessage

from .config import AppConfig
from .feishu_core import (
    FeishuClient,
    SessionManager,
    _archive_ack_card,
    _followup_error_card,
    card_form_value,
    dispatch_inbound,
    handle_archive_submit,
    handle_clarify_giveup_click,
    handle_db_change_confirm,
    handle_db_change_reject,
    handle_feedback_click,
    handle_feedback_reason_skip,
    handle_feedback_reason_submit,
    handle_followup_cancel_click,
    handle_followup_click,
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
    #   所以 require_mention=False 关掉 channel 这层重复检查。
    # - respond_to_mention_all=False：纯 @所有人 由 channel PolicyGate 直接拒
    #   （policy_mention_all_blocked），不会到 handler；"@所有人 + 同时
    #   单独 @bot" 仍放行答题（PolicyGate 视为定向求助）。
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
            respond_to_mention_all=False,
        ),
        safety=SafetyConfig(
            text_batch=TextBatchConfig(delay_ms=0),
            chat_queue=ChatQueueConfig(enabled=False),
        ),
    )

    feishu = FeishuClient(channel=webhook_channel)
    session_mgr = SessionManager(
        docs_root=docs_root,
        idle_ttl=idle_ttl,
        max_sessions=config.session_max_sessions,
        max_turns=config.agent_max_turns,
        doc_qa_config=config.doc_qa,
        gateway_trace_config=config.gateway_trace,
        database_config=config.database,
        scheduled_followup_config=config.scheduled_followup,
        # 参数变更审批要在工具里发确认卡，把 outbound client 传给 session 层
        feishu=feishu,
    )

    # channel 后台 loop 兜底全部 async 资源：session_mgr.start() 在 bg loop 上
    # 调，``_manager_lock`` 绑定 bg loop；channel.on(...) handler 也跑在 bg loop。
    # 这样消息/卡片 handler 直接 await 业务函数即可，不再需要跨 loop 桥（旧版
    # ``fastapi_loop_ref + run_coroutine_threadsafe`` 已删）。
    # fastapi 主 loop 只负责接 HTTP 请求 + 转发；/admin/sessions 想读 session_mgr
    # 状态时单独走 channel.schedule(...) + wrap_future 桥过去。

    async def _on_inbound(inbound: InboundMessage) -> None:
        """channel.on("message") handler — 直接在 channel bg loop 上跑业务。

        消息分发逻辑收在 ``feishu_core.dispatch_inbound``；本地只做 webhook
        路径特有的 rid 前缀。
        """
        rid = (inbound.id or uuid.uuid4().hex)[:8]
        request_id_var.set(rid)
        await dispatch_inbound(inbound, feishu, session_mgr)

    async def _on_card_action(event: CardActionEvent) -> None:
        """channel.on("cardAction") handler — 直接在 channel bg loop 上跑业务。

        channel 内置 dedup 按 ``card:{msg_id}:{operator}:{tag}:{value}``——
        重复点击直接 drop 不到这里。非 asker 的拒绝走业务函数自己的 reject
        路径（返回原卡视觉无变化）。
        """
        request_id_var.set("c" + uuid.uuid4().hex[:7])

        action = event.action
        value = action.value if isinstance(action.value, dict) else {}
        action_name = value.get("action")
        msg_id = event.message_id
        clicker_id = event.operator.open_id or None
        form_value = card_form_value(event)

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
                    ack_card = await handle_followup_click(
                        qid, key, chat_id_v, asker_id, clicker_id,
                        feishu, session_mgr,
                        parent_msg_id=parent_msg_id_v,
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
                    ack_card = await handle_clarify_giveup_click(
                        qid, chat_id_v, asker_id, clicker_id,
                        feishu, session_mgr,
                        parent_msg_id=parent_msg_id_v,
                    )
                except Exception:
                    logger.exception("clarify_giveup click failed: qid=%s", qid)
                    ack_card = _followup_error_card("触发失败，请重试。")

            elif action_name == "followup_cancel":
                record_id = value.get("record_id")
                chat_id_v = value.get("chat_id")
                asker_id = value.get("asker_id")
                try:
                    ack_card = handle_followup_cancel_click(
                        record_id, chat_id_v, asker_id, clicker_id, session_mgr,
                    )
                except Exception:
                    logger.exception(
                        "followup cancel failed: id=%s", record_id
                    )
                    ack_card = _followup_error_card("取消失败，请重试。")

            elif action_name == "archive_submit":
                qid = value.get("qid")
                answer = form_value.get("answer") or ""
                question = form_value.get("question") or ""
                try:
                    ack_card = await handle_archive_submit(
                        qid, question, answer, clicker_id,
                        docs_root, feishu=feishu,
                    )
                except Exception:
                    logger.exception("archive submit failed: qid=%s", qid)
                    ack_card = _archive_ack_card("❌", "归档失败，请联系管理员。")

            elif action_name == "db_change_confirm":
                change_id = value.get("change_id")
                try:
                    ack_card = await handle_db_change_confirm(
                        change_id, clicker_id, config.database, feishu=feishu,
                    )
                except Exception:
                    logger.exception("db change confirm failed: id=%s", change_id)
                    ack_card = _archive_ack_card("❌", "执行出错，请联系运维查日志。")

            elif action_name == "db_change_reject":
                change_id = value.get("change_id")
                try:
                    ack_card = await handle_db_change_reject(
                        change_id, clicker_id, config.database, feishu=feishu,
                    )
                except Exception:
                    logger.exception("db change reject failed: id=%s", change_id)
                    ack_card = _archive_ack_card("❌", "驳回出错，请联系运维查日志。")
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

    async def _bootstrap() -> None:
        """在 channel bg loop 上启动 session_mgr，确保其 asyncio.Lock 绑定 bg loop。"""
        await session_mgr.start()

    async def _teardown() -> None:
        await session_mgr.stop()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info(
            "ops-qa-bot feishu server starting, docs_root=%s idle_ttl=%ss",
            docs_root,
            idle_ttl,
        )
        # channel.connect() 在 webhook 模式下：起后台 loop + 同步 fetch bot
        # identity（10s 超时，失败会进 retry loop 不阻塞）+ 构建 dispatcher。
        await webhook_channel.connect()
        # SessionManager 必须在 channel bg loop 上 start——业务 handler 也跑在
        # 那里，asyncio.Lock 必须绑定同一个 loop。
        await asyncio.wrap_future(webhook_channel.schedule(_bootstrap()))
        try:
            yield
        finally:
            logger.info("closing all sessions ...")
            try:
                await asyncio.wrap_future(
                    webhook_channel.schedule(_teardown())
                )
            except Exception:
                logger.exception("teardown failed")
            await webhook_channel.disconnect()
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
        # active_count 是纯读 dict 的 sync 方法，跨 loop 读在 GIL 下是安全的
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
        # session_mgr.snapshot 会 acquire 绑定到 channel bg loop 的 _manager_lock，
        # 必须 schedule 回 bg loop 跑（fastapi 主 loop 上 await 会跨 loop 报错）。
        sessions = await asyncio.wrap_future(
            webhook_channel.schedule(session_mgr.snapshot())
        )
        return {
            "count": len(sessions),
            "idle_ttl_seconds": session_mgr.idle_ttl,
            "sessions": sessions,
        }

    return app
