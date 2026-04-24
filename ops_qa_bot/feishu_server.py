"""飞书 webhook 接入层。

部署前需要在飞书开放平台创建自建应用，获取 app_id / app_secret，
并在"事件与回调"中配置 webhook URL 指向 `/feishu/webhook`。

配置统一通过 `ops_qa_bot.config.AppConfig` 加载（config.toml + 环境变量）。

会话隔离：
- session key = (chat_id, sender_open_id)
- 同一群里每个用户有独立上下文；A 的追问不会带入 B 的问题
- 空闲超过 session_idle_ttl 自动回收
- 用户发送 `/reset` / `/new` / `新对话` / `重置` 可手动清空自己的上下文

注意：
- webhook 立即返回 200，问答在 BackgroundTasks 里异步跑，完成后主动推回飞书。
- 未实现 encrypt_key 签名校验；生产环境请在飞书开放平台配置 IP 白名单。
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from cachetools import TTLCache
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from .bot import OpsQABot
from .config import AppConfig
from .feishu_crypto import FeishuCrypto
from .feishu_format import markdown_to_feishu_post
from .logging_config import request_id_var

logger = logging.getLogger("ops_qa_bot.feishu")
feedback_logger = logging.getLogger("ops_qa_bot.feedback")  # 由 logging_config.setup_feedback_logger 配置

FEISHU_BASE = "https://open.feishu.cn/open-apis"
POST_TITLE = "运维文档助手"
RESET_TRIGGERS = {"/reset", "/new", "新对话", "重置"}
PLACEHOLDER_MARKDOWN = "🔍 正在翻文档，请稍候..."

SessionKey = tuple[str, str]  # (chat_id, user_open_id)


class FeishuClient:
    """飞书 API 轻量客户端：缓存 tenant_access_token、发送文本/富文本消息。"""

    def __init__(self, app_id: str, app_secret: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _get_token(self, client: httpx.AsyncClient) -> str:
        async with self._lock:
            now = time.time()
            if self._token and self._token_expires_at > now + 60:
                return self._token
            resp = await client.post(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                raise RuntimeError(f"get tenant_access_token failed: {data}")
            self._token = data["tenant_access_token"]
            self._token_expires_at = now + int(data.get("expire", 7200))
            return self._token

    async def _send(self, chat_id: str, msg_type: str, content: dict) -> str | None:
        """发消息。成功返回 message_id，失败返回 None（已打日志）。"""
        async with httpx.AsyncClient(timeout=10) as client:
            token = await self._get_token(client)
            resp = await client.post(
                f"{FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "chat_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": chat_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                },
            )
            body = resp.json() if resp.content else {}
            if resp.status_code != 200 or body.get("code") != 0:
                logger.error(
                    "feishu send(%s) failed: status=%s body=%s",
                    msg_type,
                    resp.status_code,
                    resp.text,
                )
                return None
            return (body.get("data") or {}).get("message_id")

    async def send_text(self, chat_id: str, text: str) -> str | None:
        return await self._send(chat_id, "text", {"text": text})

    async def send_post(self, chat_id: str, post_content: dict) -> str | None:
        """post_content 结构见 feishu_format.markdown_to_feishu_post。"""
        return await self._send(chat_id, "post", post_content)

    async def update_post(self, message_id: str, post_content: dict) -> bool:
        """编辑已发送的 post 消息。要求应用权限 `im:message.update_msg`。

        API: PUT /open-apis/im/v1/messages/{message_id}
        只有 text / post 类型消息可编辑，且只能由发送方（bot 自己）编辑。
        """
        async with httpx.AsyncClient(timeout=10) as client:
            token = await self._get_token(client)
            resp = await client.put(
                f"{FEISHU_BASE}/im/v1/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "msg_type": "post",
                    "content": json.dumps(post_content, ensure_ascii=False),
                },
            )
            body = resp.json() if resp.content else {}
            if resp.status_code != 200 or body.get("code") != 0:
                logger.error(
                    "feishu update_post failed: status=%s body=%s",
                    resp.status_code,
                    resp.text,
                )
                return False
            return True

    async def send_interactive(self, chat_id: str, card: dict) -> str | None:
        """发送 interactive 卡片消息，用于反馈收集等交互。"""
        return await self._send(chat_id, "interactive", card)


class _SessionEntry:
    __slots__ = ("bot", "lock", "last_used")

    def __init__(self, bot: OpsQABot):
        self.bot = bot
        self.lock = asyncio.Lock()
        self.last_used = time.time()


class SessionManager:
    """按 (chat_id, user_id) 维护独立 OpsQABot 会话。

    - 首次提问时 lazy 创建 bot
    - 同一 key 内的提问串行（per-session lock）
    - 空闲超 idle_ttl 秒的会话由后台任务回收
    """

    def __init__(self, docs_root: Path, idle_ttl: float = 1800.0):
        self._docs_root = docs_root
        self._idle_ttl = idle_ttl
        self._sessions: dict[SessionKey, _SessionEntry] = {}
        self._manager_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        async with self._manager_lock:
            entries = list(self._sessions.items())
            self._sessions.clear()
        for key, entry in entries:
            await self._close_entry(key, entry)

    async def get(self, key: SessionKey) -> _SessionEntry:
        async with self._manager_lock:
            entry = self._sessions.get(key)
            if entry is None:
                bot = OpsQABot(docs_root=self._docs_root)
                await bot.__aenter__()
                entry = _SessionEntry(bot)
                self._sessions[key] = entry
                logger.info("session created: chat=%s user=%s", *key)
            entry.last_used = time.time()
            return entry

    async def reset(self, key: SessionKey) -> bool:
        """关闭并移除指定 session。返回 True 表示之前存在。"""
        async with self._manager_lock:
            entry = self._sessions.pop(key, None)
        if entry is None:
            return False
        await self._close_entry(key, entry)
        return True

    async def _close_entry(self, key: SessionKey, entry: _SessionEntry) -> None:
        try:
            # 等正在处理的问题完成再关
            async with entry.lock:
                await entry.bot.__aexit__(None, None, None)
            logger.info("session closed: chat=%s user=%s", *key)
        except Exception:
            logger.exception("session close failed: chat=%s user=%s", *key)

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self._evict_idle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cleanup loop error")

    async def _evict_idle(self) -> None:
        cutoff = time.time() - self._idle_ttl
        to_close: list[tuple[SessionKey, _SessionEntry]] = []
        async with self._manager_lock:
            for key, entry in list(self._sessions.items()):
                if entry.last_used < cutoff:
                    to_close.append((key, entry))
            for key, _ in to_close:
                self._sessions.pop(key, None)
        for key, entry in to_close:
            logger.info("evicting idle session: chat=%s user=%s", *key)
            await self._close_entry(key, entry)

    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def idle_ttl(self) -> float:
        return self._idle_ttl

    async def snapshot(self) -> list[dict]:
        """当前活跃 session 的只读快照，按空闲时长升序（最新活跃在前）。"""
        now = time.time()
        async with self._manager_lock:
            items = [
                {
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "last_used": time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(entry.last_used)
                    ),
                    "idle_seconds": round(now - entry.last_used, 1),
                }
                for (chat_id, user_id), entry in self._sessions.items()
            ]
        items.sort(key=lambda x: x["idle_seconds"])
        return items


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


def _mention_post(user_id: str, answer_markdown: str, title: str = POST_TITLE) -> dict:
    """在答案开头插入 `@用户` 提醒，让群里一眼看出回的是谁。"""
    post = markdown_to_feishu_post(answer_markdown, title)
    mention_paragraph = [
        {"tag": "at", "user_id": user_id},
        {"tag": "text", "text": " "},
    ]
    post["zh_cn"]["content"].insert(0, mention_paragraph)
    return post


def _feedback_card(qid: str, user_id: str) -> dict:
    """问答结束后附带的反馈卡片：👍 / 👎 两个按钮。"""
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "这次回答是否有帮助？"},
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "👍 有帮助"},
                        "type": "primary",
                        "value": {
                            "action": "feedback",
                            "qid": qid,
                            "rating": "up",
                            "asker_id": user_id,
                        },
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "👎 待改进"},
                        "type": "default",
                        "value": {
                            "action": "feedback",
                            "qid": qid,
                            "rating": "down",
                            "asker_id": user_id,
                        },
                    },
                ],
            }
        ],
    }


def _feedback_ack_card(rating: str, clicker_name: str | None = None) -> dict:
    """点击后用来替换原卡片的"已收到反馈"提示。"""
    msg = "✅ 感谢反馈！" if rating == "up" else "🙏 已收到，我们会持续改进。"
    if clicker_name:
        msg = f"{msg}（by {clicker_name}）"
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": msg}},
        ],
    }


def _excerpt(text: str, limit: int = 200) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


async def handle_question(
    chat_id: str,
    user_id: str,
    question: str,
    feishu: "FeishuClient",
    session_mgr: "SessionManager",
) -> None:
    """处理单条用户提问（完整流程：重置 / 占位 / 答题 / 编辑 / 反馈卡片）。

    抽成模块级函数的原因：HTTP 模式和长连接模式都要走同一套业务逻辑，
    参数化 feishu 和 session_mgr 之后两边可以共享。
    """
    key = (chat_id, user_id)

    # 重置指令：清掉该用户的会话
    if question in RESET_TRIGGERS:
        existed = await session_mgr.reset(key)
        reply = (
            "已清空你的对话历史，下一个问题会开启新会话。"
            if existed
            else "你当前还没有活跃会话，下一个问题就是新会话。"
        )
        await feishu.send_post(chat_id, _mention_post(user_id, reply))
        return

    # 1. 立即发占位消息，让用户感知 bot 已收到（问答生成要 5-15 秒）
    placeholder_mid = await feishu.send_post(
        chat_id, _mention_post(user_id, PLACEHOLDER_MARKDOWN)
    )

    # 2. 生成答案
    try:
        entry = await session_mgr.get(key)
        async with entry.lock:
            answer = await entry.bot.answer(question)
            entry.last_used = time.time()
    except Exception as e:
        logger.exception("answer failed: chat=%s user=%s", chat_id, user_id)
        answer = f"抱歉，处理失败：{e}"
    answer = answer or "（无回答内容）"
    final_post = _mention_post(user_id, answer)

    # 3. 用最终答案替换占位；编辑失败则兜底发新消息
    if placeholder_mid is not None:
        if not await feishu.update_post(placeholder_mid, final_post):
            logger.warning(
                "update placeholder failed (mid=%s), sending new message",
                placeholder_mid,
            )
            await feishu.send_post(chat_id, final_post)
    else:
        await feishu.send_post(chat_id, final_post)

    # 4. 发反馈卡片并记录问答（qid 用来关联后续的反馈事件）
    qid = uuid.uuid4().hex[:12]
    feedback_logger.info(
        json.dumps(
            {
                "event": "qa",
                "qid": qid,
                "chat_id": chat_id,
                "user_id": user_id,
                "question": _excerpt(question, 500),
                "answer_excerpt": _excerpt(answer, 500),
            },
            ensure_ascii=False,
        )
    )
    await feishu.send_interactive(chat_id, _feedback_card(qid, user_id))


def handle_feedback_click(
    qid: str,
    rating: str,
    clicker_id: str | None,
    asker_id: str | None,
) -> dict:
    """记录反馈点击日志，返回应替换原卡片的 ack 卡片 JSON。

    两种模式通用：HTTP 模式把这个 dict 包成 `{"card": <ack>}` 返回；
    WS 模式把它塞进 `P2CardActionTriggerResponse`。
    """
    feedback_logger.info(
        json.dumps(
            {
                "event": "feedback",
                "qid": qid,
                "rating": rating,
                "clicker_id": clicker_id,
                "asker_id": asker_id,
            },
            ensure_ascii=False,
        )
    )
    logger.info("feedback qid=%s rating=%s by=%s", qid, rating, clicker_id)
    return _feedback_ack_card(rating)


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
        if value.get("action") != "feedback":
            return {}  # 其他类型的按钮暂不处理

        qid = value.get("qid")
        rating = value.get("rating")
        clicker_id = payload.get("open_id") or payload.get("user_id")
        if not qid or rating not in ("up", "down"):
            return {}

        # 去重：卡片回调同样会重试（无 event_id，用 message + qid + 点击人 + 方向作组合键）。
        # 同一用户对同一问答只算一次反馈，重复点击或飞书重试都被挡掉。
        click_key = f"{payload.get('open_message_id')}|{qid}|{clicker_id}|{rating}"
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
