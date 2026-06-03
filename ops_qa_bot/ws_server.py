"""长连接模式：通过 lark-oapi 的 FeishuChannel(transport="ws") 接收飞书事件。

相比 HTTP 模式（`feishu_server.py`）的差异：
- 不需要开公网 HTTPS 入站端口、TLS 证书、反向代理、IP 白名单
- 不需要 encrypt_key / verify_token（SDK 自己处理鉴权）
- 事件和卡片按钮点击统一走 `card.action.trigger` 事件（v2）
- 单进程只能服务一个 app_id

业务逻辑（OpsQABot、SessionManager、反馈日志、占位消息等）完全复用
`feishu_core.py` 里抽出来的 `handle_question` / `handle_feedback_click` 等。

飞书开放平台配置：
- 事件订阅方式选 "长连接"（不配 Request URL）
- 订阅事件：`im.message.receive_v1`、`card.action.trigger`
- 消息卡片的"接收方式"选 "事件订阅"（对应 card.action.trigger）

架构：
- FeishuChannel 内部托管 WS 连接、自动重连、签名/token 校验、event-id
  与 cardAction tag+value+operator 维度的 dedup、@所有人 检测
- 消息事件走 channel.on("message", _on_inbound)，handler 拿规范化的
  InboundMessage，直接在 channel 后台 loop 上 await 业务函数
- 卡片回调走 channel.on("cardAction", _on_card_action)，handler 内部主动调
  channel.update_card() 完成卡片顶替——channel 的 cardAction processor
  是异步 schedule + 空响应模式，不支持飞书 v2 卡片回调的"同步 return 顶替"
- SessionManager + HealthServer 都启动在 channel 后台 loop，所有业务
  同一个 loop 跑，避免跨 loop asyncio.Lock 协调问题
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

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
    handle_followup_click,
    normalize_card_reasons,
)
from .health_server import HealthServer
from .logging_config import request_id_var

logger = logging.getLogger("ops_qa_bot.ws")


class WsRunner:
    """长连接模式的运行主体。

    构造时建一个 FeishuChannel(transport="ws") 并注册 message / cardAction
    handler。run() 启动 channel + SessionManager + HealthServer，全在 channel
    的后台 loop 上跑。主 loop（run() 所在协程）只做"等关闭信号"的 sleep。
    """

    def __init__(self, config: AppConfig):
        docs_root = config.docs_root
        if not (docs_root / "INDEX.md").is_file():
            raise RuntimeError(f"docs_root 缺少 INDEX.md: {docs_root}")

        self._config = config

        # channel policy / safety 配置跟 HTTP 路径对齐：
        # - require_mention=False：飞书订阅 im.message.receive_v1 本身只在
        #   群里 @bot 时推送，channel 这层重复检查关掉
        # - respond_to_mention_all=False：纯 @所有人 由 channel PolicyGate 直接拒
        #   （policy_mention_all_blocked），不会到 handler；"@所有人 + 同时
        #   单独 @bot" 仍放行答题（PolicyGate 视为定向求助）
        # - text_batch.delay_ms=0：不批合并连发消息，保持"逐条处理"
        # - chat_queue.enabled=False：不做 per-chat 串行；并发安全由
        #   SessionManager 的 per-(chat,user) lock 兜底
        self._channel = FeishuChannel(
            app_id=config.feishu.app_id,
            app_secret=config.feishu.app_secret,
            transport="ws",
            policy=PolicyConfig(
                require_mention=False,
                respond_to_mention_all=False,
            ),
            safety=SafetyConfig(
                text_batch=TextBatchConfig(delay_ms=0),
                chat_queue=ChatQueueConfig(enabled=False),
            ),
        )
        # outbound 复用同一个 channel，避免双 token cache + 双 bot identity 查询
        self._feishu = FeishuClient(channel=self._channel)
        self._session_mgr = SessionManager(
            docs_root=docs_root,
            idle_ttl=config.session_idle_ttl,
            doc_qa_config=config.doc_qa,
            gateway_trace_config=config.gateway_trace,
            database_config=config.database,
            scheduled_followup_config=config.scheduled_followup,
            # 参数变更审批要在工具里发确认卡，把 outbound client 传给 session 层
            feishu=self._feishu,
        )

        self._health: HealthServer | None = None

        # 健康检查 / 观测用的状态
        self._started_at: float = 0.0
        self._last_event_at: float = 0.0  # 最近一次成功收到事件
        self._event_count: int = 0
        self._click_count: int = 0

        self._register_handlers()

    # ------------------------------------------------------------------
    # channel 事件 handler
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        self._channel.on("message", self._on_inbound)
        self._channel.on("cardAction", self._on_card_action)
        self._channel.on(
            "reconnecting", lambda: logger.warning("ws reconnecting ...")
        )
        self._channel.on("reconnected", lambda: logger.info("ws reconnected"))

    async def _on_inbound(self, inbound: InboundMessage) -> None:
        """消息事件 handler — 在 channel 后台 loop 上跑。

        消息分发逻辑收在 ``feishu_core.dispatch_inbound``；本地只做 ws 路径
        特有的：rid 前缀、事件计数。无 schedule 参数 = 直接 await（业务跟
        handler 同 loop）。
        """
        event_id = inbound.id or uuid.uuid4().hex
        request_id_var.set("ws" + event_id[:6])

        self._last_event_at = time.time()
        self._event_count += 1

        await dispatch_inbound(inbound, self._feishu, self._session_mgr)

    async def _on_card_action(self, event: CardActionEvent) -> None:
        """卡片回调 handler — 业务结果通过 channel.update_card 顶替原卡。

        channel 内置 dedup key 是 ``card:{msg_id}:{operator.open_id}:{tag}:{value}``，
        按 operator 分桶 —— 不同点击人有不同 dedup key，"非 asker 错点击闭锁 asker"
        在这里不存在。dedup hit 时 channel 直接 drop 不顶替原卡，行为反而比旧的
        "重试返回 replay 卡可能顶掉新状态"更稳。

        权限校验失败（非 asker / 非 owner）由各 handler 内部走 reject 路径处理，
        返回视觉等同原卡的 ack；外层统一 update_card 顶替一次，UX 上无差异。
        """
        request_id_var.set("c" + uuid.uuid4().hex[:7])

        self._last_event_at = time.time()
        self._click_count += 1

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
                        self._feishu, self._session_mgr,
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
                        self._feishu, self._session_mgr,
                        parent_msg_id=parent_msg_id_v,
                    )
                except Exception:
                    logger.exception("clarify_giveup click failed: qid=%s", qid)
                    ack_card = _followup_error_card("触发失败，请重试。")

            elif action_name == "archive_submit":
                qid = value.get("qid")
                answer = form_value.get("answer") or ""
                question = form_value.get("question") or ""
                try:
                    ack_card = await handle_archive_submit(
                        qid, question, answer, clicker_id,
                        self._config.docs_root, feishu=self._feishu,
                    )
                except Exception:
                    logger.exception("archive submit failed: qid=%s", qid)
                    ack_card = _archive_ack_card("❌", "归档失败，请联系管理员。")

            elif action_name == "db_change_confirm":
                change_id = value.get("change_id")
                try:
                    ack_card = await handle_db_change_confirm(
                        change_id, clicker_id,
                        self._config.database, feishu=self._feishu,
                    )
                except Exception:
                    logger.exception("db change confirm failed: id=%s", change_id)
                    ack_card = _archive_ack_card("❌", "执行出错，请联系运维查日志。")

            elif action_name == "db_change_reject":
                change_id = value.get("change_id")
                try:
                    ack_card = await handle_db_change_reject(
                        change_id, clicker_id,
                        self._config.database, feishu=self._feishu,
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
        # 异步 patch 顶替原卡。channel safety pipeline 已经在 handler 跑之前
        # mark dedup（finally 路径），update_card 失败不会自动重试。日志报警足够。
        result = await self._channel.update_card(msg_id, ack_card)
        if not result.success:
            logger.error(
                "update_card failed: msg_id=%s action=%s err=%s",
                msg_id, action_name, result.error,
            )

    # ------------------------------------------------------------------
    # 健康检查端点
    # ------------------------------------------------------------------

    async def _h_healthz(self) -> tuple[int, dict[str, Any]]:
        """liveness：进程响应即 200。能跑到这里说明 asyncio loop 没卡死。"""
        return 200, {
            "ok": True,
            "active_sessions": self._session_mgr.active_count(),
            "uptime_seconds": round(time.time() - self._started_at, 1),
        }

    async def _h_readyz(self) -> tuple[int, dict[str, Any]]:
        """readiness：channel.is_ready 为 True 即算 ready。

        channel.is_ready 在 WS 自动重连期间保持 True（SDK 内部处理重连，
        业务无感）。真断连退不出 → systemd 拉起兜底。
        事件计数 / 距上次事件时间仅作为观测，不参与 ready 判定 —— 低流量时
        会误报，真断连时又迟报。
        """
        now = time.time()
        uptime = now - self._started_at
        last = self._last_event_at
        idle = (now - last) if last > 0 else None

        ready = self._channel.is_ready
        reason = "ok" if ready else "channel not ready"

        return (200 if ready else 503), {
            "ready": ready,
            "reason": reason,
            "channel_ready": ready,
            "last_event_age_seconds": round(idle, 1) if idle is not None else None,
            "uptime_seconds": round(uptime, 1),
            "event_count": self._event_count,
            "click_count": self._click_count,
        }

    async def _h_admin_sessions(self) -> tuple[int, dict[str, Any]]:
        sessions = await self._session_mgr.snapshot()
        return 200, {
            "count": len(sessions),
            "idle_ttl_seconds": self._session_mgr.idle_ttl,
            "sessions": sessions,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def _bootstrap(self) -> None:
        """在 channel 后台 loop 上启动 SessionManager + HealthServer。

        都跑在同一个 loop 上 —— session_mgr.snapshot() 跨 loop 调会因为
        asyncio.Lock 的 Future loop 绑定踩坑。
        """
        await self._session_mgr.start()
        if self._config.health.enabled:
            self._health = HealthServer(
                host=self._config.health.host,
                port=self._config.health.port,
                routes={
                    "/healthz": self._h_healthz,
                    "/readyz": self._h_readyz,
                    "/admin/sessions": self._h_admin_sessions,
                },
            )
            await self._health.start()

    async def _teardown(self) -> None:
        if self._health is not None:
            await self._health.stop()
            self._health = None
        await self._session_mgr.stop()

    async def run(self) -> None:
        """主入口：启动 channel + SessionManager + HealthServer。"""
        self._started_at = time.time()

        # channel.connect() 在 ws 模式下：建后台 loop+thread、同步 fetch bot
        # identity（10s 超时；失败进 retry loop 不阻塞）、建 dispatcher、
        # 启 WSClient（连上飞书 + 后台跑 ws 收发循环）。
        await self._channel.connect()

        # 把 SessionManager + HealthServer 启动到 channel 后台 loop。
        # 业务 handler 也在那个 loop 跑，全局单 loop 简化锁语义。
        fut = self._channel.schedule(self._bootstrap())
        await asyncio.wrap_future(fut)

        logger.info(
            "ws server started: app_id=%s docs_root=%s idle_ttl=%ss",
            self._config.feishu.app_id,
            self._config.docs_root,
            self._config.session_idle_ttl,
        )

        try:
            # 主协程永远等着，让 channel 后台 loop + handler 协程跑业务
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("ws server stopping ...")
            try:
                fut = self._channel.schedule(self._teardown())
                await asyncio.wrap_future(fut)
            except Exception:
                logger.exception("teardown failed")
            await self._channel.disconnect()
            logger.info("ws server stopped")
