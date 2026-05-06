"""飞书接入的共享业务核心：HTTP 模式和长连接模式都依赖这里的类和函数。

拆分原则：
- 本文件只依赖 stdlib 和 httpx；**不引入 fastapi / lark-oapi 等适配层专属依赖**，
  这样只装 `[ws]` extra（没有 fastapi）或 `[server]` extra 的部署都能 import。
- HTTP 适配层 `feishu_server.py` / 长连接适配层 `ws_server.py` 负责把
  各自框架的事件/请求翻译成调用 `handle_question` / `handle_feedback_click`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from pathlib import Path

import httpx
from cachetools import TTLCache

from .bot import AnswerResult, OpsQABot
from .feishu_format import markdown_to_feishu_post

logger = logging.getLogger("ops_qa_bot.feishu")
# 由 logging_config.setup_feedback_logger 配置专用 handler 写 logs/feedback.log
feedback_logger = logging.getLogger("ops_qa_bot.feedback")

FEISHU_BASE = "https://open.feishu.cn/open-apis"
POST_TITLE = "运维文档助手"
RESET_TRIGGERS = {"/reset", "/new", "新对话", "重置"}

# 图片输入：Anthropic vision 支持 png/jpeg/gif/webp；超过 5MB 大概率被 API 拒
# （像素 + 字节双重限制），让 bot 友好提示用户压缩，而不是甩 LLM 报错原文
_SUPPORTED_IMAGE_TYPES: set[str] = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# 用户只发图无文字时，bot 用这条默认 prompt 推动 agent："识别 → 找文档 → 答"
DEFAULT_IMAGE_PROMPT = (
    "用户发了一张运维相关的截图。请先识别图中的关键信息"
    "（报错文本、命令、指标值、配置项、UI 状态等），"
    "再按这些线索去文档里查解决办法。"
)


def _normalize_image_media_type(content_type: str, data: bytes) -> str:
    """归一化 image media_type 到 Anthropic vision 接受的集合内。

    优先看 HTTP 响应 Content-Type；不在白名单时按 magic bytes 嗅探；都识别
    不出来 fallback 到 image/jpeg（飞书截图绝大多数是 jpeg）。
    """
    if content_type in _SUPPORTED_IMAGE_TYPES:
        return content_type
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _placeholder_text(question: str, queued: bool) -> str:
    """生成带问题摘要的占位文本，让用户能区分多条并发问的占位。

    queued=True 表示当前 session 锁被前一条问题占着，本条还没开始跑，前缀用
    🕒 排队中；获取到锁开始跑时上层会再 update 一次置成 🔍 翻文档中。
    """
    excerpt = question.strip().replace("\n", " ")
    if len(excerpt) > 40:
        excerpt = excerpt[:40] + "…"
    icon = "🕒 排队中" if queued else "🔍 翻文档中"
    return f"{icon}：'{excerpt}'"

SessionKey = tuple[str, str]  # (chat_id, user_open_id)

# 升级机制：bot 答不上来时，按 prompt 输出 <<ESCALATE:ou_xxx>> 标记，
# handle_question 拦截 → 移除标记 → 在 post 末尾注入 @owner 提醒。
# 只接受形如 ou_xxxx 或字面量 none 两种值，防止注入。
_ESCALATE_RE = re.compile(r"<<ESCALATE:(ou_[A-Za-z0-9_-]+|none)>>")
_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")
# 同一 (chat, owner) 30 分钟内只 @ 一次，防止用户连环问把负责人刷烦
_escalate_cooldown: TTLCache = TTLCache(maxsize=10000, ttl=1800)

# 快捷追问机制：bot 答完后按 prompt 输出 <<FOLLOWUPS:k1|k2|k3>> 标记，
# handle_question 解析 → 在反馈卡上面挂对应按钮。点击 → 用预设 prompt
# 触发新一轮 handle_question，把用户自然带进下一轮。
# key 必须出自 _FOLLOWUP_LIBRARY；最多 3 个；不在白名单的 key 静默过滤。
# 抓取宽松（含数字/大小写都先收下），合法性靠 _FOLLOWUP_LIBRARY 白名单过滤
_FOLLOWUPS_RE = re.compile(r"<<FOLLOWUPS:([\w|]+)>>")

# 反问标记：LLM 检测到信息不足以准确答时输出 <<CLARIFY>>，把答案当成反问轮处理。
# 反问轮：不发反馈卡 / 追问按钮 / 升级 @ / 归档卡，让用户专注回答反问；
# 用户在同一 session 里答完，下一轮就按补充信息直接答。
_CLARIFY_RE = re.compile(r"<<CLARIFY>>")
_FOLLOWUP_LIBRARY: dict[str, tuple[str, str]] = {
    "troubleshoot": (
        "📋 排查步骤",
        "把刚才的内容整理成具体的排查步骤，按顺序列出每一步要查什么、用什么命令、看哪个指标判断。",
    ),
    "risks": (
        "⚠️ 风险点",
        "做这件事有哪些风险点和注意事项？特别是不可逆操作或可能影响其他业务的地方。",
    ),
    "rollback": (
        "↩️ 回滚方案",
        "如果按上面的方案做完发现有问题，怎么回滚？给出具体步骤和回滚后的检查清单。",
    ),
    "checklist": (
        "✅ Checklist",
        "把上面的内容总结成一个可勾选的 checklist，每条尽量短、可执行。",
    ),
    "commands": (
        "💻 示例命令",
        "给出可以直接复制运行的命令示例，每条带一行注释说明用途和参数含义。",
    ),
    "related": (
        "🔗 相关文档",
        "还有哪些相关的运维文档（INDEX 里登记的或没登记的）我可能需要看？给出文件路径。",
    ),
}

# 归档机制：bot 升级到负责人后，同时发一张表单卡（card v2 form）。
# 负责人在群里答完后填写卡片提交，内容写入 docs/<component>/qa-archive.md。
# qid → {chat_id, asker_id, question, owner_id, component_dir}。
# 24h 没人填写就过期，重启后清空（测试环境不持久化）。
_pending_archives: TTLCache = TTLCache(maxsize=1000, ttl=86400)
# INDEX.md 解析缓存：路径 → (mtime, {open_id: 目录名})
_archive_index_cache: dict[Path, tuple[float, dict[str, str]]] = {}
# 每个归档文件一把 asyncio.Lock，避免并发提交撕裂内容
_archive_locks: dict[Path, asyncio.Lock] = {}


class FeishuClient:
    """飞书 API 轻量客户端：缓存 tenant_access_token、发送文本/富文本/卡片消息。"""

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

    async def _send(
        self,
        chat_id: str,
        msg_type: str,
        content: dict,
        *,
        parent_id: str | None = None,
    ) -> str | None:
        """发消息。成功返回 message_id，失败返回 None（已打日志）。

        `parent_id` 给定时走 reply 端点（飞书"引用回复"），消息头部带原消息引用条；
        不给定时走普通 send 端点。reply 端点的 chat 默认就是父消息所在 chat，不再
        需要 receive_id。
        """
        async with httpx.AsyncClient(timeout=10) as client:
            token = await self._get_token(client)
            if parent_id:
                url = f"{FEISHU_BASE}/im/v1/messages/{parent_id}/reply"
                params = None
                payload = {
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                    "reply_in_thread": False,  # 引用回复，不开 thread 模式
                }
            else:
                url = f"{FEISHU_BASE}/im/v1/messages"
                params = {"receive_id_type": "chat_id"}
                payload = {
                    "receive_id": chat_id,
                    "msg_type": msg_type,
                    "content": json.dumps(content, ensure_ascii=False),
                }
            resp = await client.post(
                url,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
            body = resp.json() if resp.content else {}
            if resp.status_code != 200 or body.get("code") != 0:
                logger.error(
                    "feishu send(%s, parent=%s) failed: status=%s body=%s",
                    msg_type,
                    parent_id,
                    resp.status_code,
                    resp.text,
                )
                return None
            return (body.get("data") or {}).get("message_id")

    async def send_text(
        self, chat_id: str, text: str, *, parent_id: str | None = None
    ) -> str | None:
        return await self._send(chat_id, "text", {"text": text}, parent_id=parent_id)

    async def send_post(
        self,
        chat_id: str,
        post_content: dict,
        *,
        parent_id: str | None = None,
    ) -> str | None:
        """post_content 结构见 feishu_format.markdown_to_feishu_post。"""
        return await self._send(
            chat_id, "post", post_content, parent_id=parent_id
        )

    async def update_post(self, message_id: str, post_content: dict) -> bool:
        """编辑已发送的 post 消息。要求 im:message 权限。

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

    async def send_interactive(
        self, chat_id: str, card: dict, *, parent_id: str | None = None
    ) -> str | None:
        """发送 interactive 卡片消息，用于反馈收集等交互。"""
        return await self._send(
            chat_id, "interactive", card, parent_id=parent_id
        )

    async def download_message_resource(
        self, message_id: str, file_key: str, resource_type: str = "image"
    ) -> tuple[bytes, str]:
        """下载消息附件，返回 (bytes, media_type)。

        API: GET /open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=...
        要求飞书应用配置 `im:resource` scope，bot 是该消息所在群成员。

        media_type 优先取响应 Content-Type（去掉 charset 等参数），缺失或非
        Anthropic 支持的 image/* 类型时按 magic bytes 嗅探，再 fallback 到
        image/jpeg（最常见的截图格式）。
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            resp = await client.get(
                f"{FEISHU_BASE}/im/v1/messages/{message_id}/resources/{file_key}",
                params={"type": resource_type},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"download resource failed: status={resp.status_code} "
                    f"body={resp.text[:200]}"
                )
            data = resp.content
            ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            return data, _normalize_image_media_type(ct, data)


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

    async def queued(self, key: SessionKey) -> bool:
        """该 (chat, user) 当前是否有未完成的问题占着 session lock。

        用于占位文本判定：True 表示新进来的问题要排队，前缀用 🕒；False 直接 🔍。
        不创建 session，纯只读检查。
        """
        async with self._manager_lock:
            entry = self._sessions.get(key)
            if entry is None:
                return False
            return entry.lock.locked()

    def active_count(self) -> int:
        return len(self._sessions)

    @property
    def idle_ttl(self) -> float:
        return self._idle_ttl

    @property
    def docs_root(self) -> Path:
        return self._docs_root

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


def _mention_post(user_id: str, answer_markdown: str, title: str = POST_TITLE) -> dict:
    """在答案开头插入 `@用户` 提醒，让群里一眼看出回的是谁。"""
    post = markdown_to_feishu_post(answer_markdown, title)
    mention_paragraph = [
        {"tag": "at", "user_id": user_id},
        {"tag": "text", "text": " "},
    ]
    post["zh_cn"]["content"].insert(0, mention_paragraph)
    return post


async def handle_image_question(
    chat_id: str,
    user_id: str,
    image_key: str,
    parent_msg_id: str | None,
    feishu: "FeishuClient",
    session_mgr: "SessionManager",
) -> None:
    """处理 image 类型消息：下载 → 视觉答题。

    任何前置失败（下载报错 / 超大 / 内容为空）都用普通 post 回友好提示，不走
    答题流程，避免把 LLM 报错原文甩给用户。下载成功后用 DEFAULT_IMAGE_PROMPT
    作为引导问题，调用 `handle_question(images=...)` 复用占位 / 反馈卡 / 追问
    等所有现有逻辑。
    """
    if not parent_msg_id:
        # 没有原消息 id 拿不到资源端点（API 必须 message_id + file_key）
        logger.warning(
            "image without parent_msg_id, skip: chat=%s user=%s key=%s",
            chat_id,
            user_id,
            image_key,
        )
        return

    try:
        img_bytes, media_type = await feishu.download_message_resource(
            parent_msg_id, image_key, "image"
        )
    except Exception as e:
        logger.exception(
            "image download failed: chat=%s user=%s key=%s", chat_id, user_id, image_key
        )
        await feishu.send_post(
            chat_id,
            _mention_post(
                user_id,
                f"图片下载失败 🙏 {e}\n你可以把截图里的关键报错或现象用文字描述出来再发。",
            ),
            parent_id=parent_msg_id,
        )
        return

    if len(img_bytes) > MAX_IMAGE_BYTES:
        size_mb = len(img_bytes) / (1024 * 1024)
        await feishu.send_post(
            chat_id,
            _mention_post(
                user_id,
                f"图片太大（{size_mb:.1f}MB，上限 {MAX_IMAGE_BYTES // (1024 * 1024)}MB）"
                "🙏 请压缩后重发，或者把关键内容用文字描述出来。",
            ),
            parent_id=parent_msg_id,
        )
        return
    if not img_bytes:
        await feishu.send_post(
            chat_id,
            _mention_post(user_id, "图片内容为空，请重新发送 🙏"),
            parent_id=parent_msg_id,
        )
        return

    logger.info(
        "image question: chat=%s user=%s key=%s size=%dB type=%s",
        chat_id,
        user_id,
        image_key,
        len(img_bytes),
        media_type,
    )
    await handle_question(
        chat_id,
        user_id,
        DEFAULT_IMAGE_PROMPT,
        feishu,
        session_mgr,
        parent_msg_id=parent_msg_id,
        images=[(media_type, img_bytes)],
    )


async def handle_unsupported_message(
    chat_id: str,
    user_id: str,
    parent_msg_id: str | None,
    message_type: str,
    feishu: "FeishuClient",
) -> None:
    """收到非 text 消息（image / file / post / sticker / audio 等）时回一条友好提示。

    不真处理图片 / 文件，只让用户明确知道 bot 看到了但暂不支持文字以外的输入，
    避免静默丢弃让用户以为 bot 没看见。引用回复到原消息保持线程感。
    """
    hint = (
        "目前只支持文字提问 🙏\n"
        "图片 / 文件 / 截图请把关键报错或现象用文字描述出来再发，"
        "比如把截图里的报错关键句敲一遍。"
    )
    logger.info(
        "unsupported message hinted: type=%s chat=%s user=%s",
        message_type,
        chat_id,
        user_id,
    )
    await feishu.send_post(
        chat_id, _mention_post(user_id, hint), parent_id=parent_msg_id
    )


def _feedback_card(qid: str, user_id: str) -> dict:
    """问答结束后附带的反馈卡片：纯 👍 / 👎。

    追问按钮拆到独立的 `_followup_card`，避免点追问把整张反馈卡顶掉、用户失去打分入口。

    用 v2 schema：👎 后要替换成带 form 的原因表单（form 是 v2 才有），原卡和替换卡
    schema 不一致飞书侧渲染会失败。v2 不再支持 `tag:action` 容器，按钮直接放进
    column_set 并排，回调走 `behaviors:[{type:"callback", value:...}]`。
    """
    btn_up = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "👍 有帮助"},
        "type": "primary",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": "feedback",
                    "qid": qid,
                    "rating": "up",
                    "asker_id": user_id,
                },
            }
        ],
    }
    btn_down = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "👎 待改进"},
        "type": "default",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": "feedback",
                    "qid": qid,
                    "rating": "down",
                    "asker_id": user_id,
                },
            }
        ],
    }
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "这次回答是否有帮助？"},
                {
                    "tag": "column_set",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [btn_up],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [btn_down],
                        },
                    ],
                },
            ]
        },
    }


def _followup_card(
    qid: str,
    user_id: str,
    chat_id: str,
    parent_msg_id: str | None,
    followup_keys: list[str],
) -> dict:
    """问答结束后附带的追问按钮卡：独立卡片，与反馈卡解耦。

    `followup_keys` 来自 LLM 输出的 `<<FOLLOWUPS:...>>`，已过滤到白名单内、最多 3 个。
    每个 button value 自带 chat_id 和 parent_msg_id：parent_msg_id 是用户原始问题的
    message_id，回调时透传给新一轮 `handle_question`，让追问的占位/答案/卡片继续
    引用回到原问题，线程感不断。
    """
    btns: list[dict] = []
    for k in followup_keys:
        label, _ = _FOLLOWUP_LIBRARY[k]
        btns.append(
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": "default",
                "value": {
                    "action": "followup",
                    "qid": qid,
                    "key": k,
                    "asker_id": user_id,
                    "chat_id": chat_id,
                    "parent_msg_id": parent_msg_id,
                },
            }
        )
    return {
        "config": {"wide_screen_mode": True},
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**想再深入？**"},
            },
            {"tag": "action", "actions": btns},
        ],
    }


def _parse_followups(answer: str) -> tuple[str, list[str]]:
    """从答案抽 <<FOLLOWUPS:k1|k2|k3>> 标记，返回 (清理后的答案, 合法 key 列表)。

    最多保留 3 个；不在 _FOLLOWUP_LIBRARY 里的 key 静默过滤；去重。
    没有标记返回 (原文, [])。
    """
    m = _FOLLOWUPS_RE.search(answer)
    if not m:
        return answer, []
    cleaned = _FOLLOWUPS_RE.sub("", answer).strip()
    valid: list[str] = []
    for k in (k.strip() for k in m.group(1).split("|")):
        if k and k in _FOLLOWUP_LIBRARY and k not in valid:
            valid.append(k)
        if len(valid) >= 3:
            break
    return cleaned, valid


def _parse_clarify(answer: str) -> tuple[str, bool]:
    """抽 <<CLARIFY>> 标记，返回 (清理后的答案, 是否为反问轮)。"""
    if _CLARIFY_RE.search(answer):
        return _CLARIFY_RE.sub("", answer).strip(), True
    return answer, False


def _followup_ack_card(label: str) -> dict:
    """点完追问按钮后用来替换原反馈卡的状态卡（v2）。

    UI 层面：原卡（含 👍/👎 + 追问按钮）整张被替换；用户想反馈得在点追问前点。
    """
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": f"✅ 已发起追问：**{label}** —— 正在生成…",
                }
            ]
        },
    }


def _followup_error_card(message: str) -> dict:
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"⚠️ {message}"},
            ]
        },
    }


async def handle_followup_click(
    qid: str | None,
    key: str | None,
    chat_id: str | None,
    asker_id: str | None,
    clicker_id: str | None,
    feishu: "FeishuClient",
    session_mgr: "SessionManager",
    parent_msg_id: str | None = None,
) -> dict:
    """处理快捷追问按钮点击。后台触发新一轮 handle_question，立即返回 ack 卡。

    校验：key 在白名单 / chat_id+asker_id 都存在 / 点击者就是原提问者。
    任何校验失败都返回错误卡，不触发后续答题。

    `parent_msg_id` 来自按钮 value，是用户原始问题的 message_id；透传给新一轮
    `handle_question`，让追问的占位 / 答案 / 反馈卡继续引用回到原问题，线程不断。
    早期版本（按钮 value 没带 parent_msg_id）会落到 None，新一轮按 top-level 发送。
    """
    if not key or key not in _FOLLOWUP_LIBRARY:
        return _followup_error_card("追问类型无效，请重试。")
    if not chat_id or not asker_id:
        return _followup_error_card("追问参数缺失，请重试。")
    if clicker_id and clicker_id != asker_id:
        return _followup_error_card(
            f"只有 <at id={asker_id}></at> 能点这个追问，要追问请重新发一条消息。"
        )

    label, prompt_text = _FOLLOWUP_LIBRARY[key]
    # 后台跑新一轮答题：卡片回调要求秒级返回，不能等 5-15s 的 LLM 推理
    asyncio.create_task(
        handle_question(
            chat_id,
            asker_id,
            prompt_text,
            feishu,
            session_mgr,
            parent_msg_id=parent_msg_id,
        )
    )
    feedback_logger.info(
        json.dumps(
            {
                "event": "followup",
                "qid": qid,
                "key": key,
                "label": label,
                "asker_id": asker_id,
                "clicker_id": clicker_id,
            },
            ensure_ascii=False,
        )
    )
    logger.info(
        "followup triggered: qid=%s key=%s chat=%s asker=%s",
        qid,
        key,
        chat_id,
        asker_id,
    )
    return _followup_ack_card(label)


def _feedback_ack_card(rating: str, clicker_name: str | None = None) -> dict:
    """点击后用来替换原卡片的"已收到反馈"提示。

    v2 schema，对齐 `_feedback_card` / `_feedback_reason_form_card`。
    """
    msg = "✅ 感谢反馈！" if rating == "up" else "🙏 已收到，我们会持续改进。"
    if clicker_name:
        msg = f"{msg}（by {clicker_name}）"
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": msg},
            ]
        },
    }


# 👎 后弹出的原因枚举：覆盖最常见的几类可执行抓手（更新文档 / 调 prompt）
_FEEDBACK_REASONS: dict[str, str] = {
    "outdated": "文档过时",
    "incomplete": "步骤不完整",
    "incorrect": "事实错误",
    "verbose": "答案啰嗦 / 没重点",
    "other": "其他",
}


def _feedback_reason_form_card(qid: str, asker_id: str | None) -> dict:
    """👎 后替换原卡的原因收集表单（card v2 form）。

    select 选原因 + 多行 input 写备注（可选）+ 提交按钮，跳过按钮放 form 外（form 内
    放无 form_action_type 的纯 callback button 行为不明确，官方 demo 没这种写法）。
    submit 不挂 behaviors callback，仅靠 form_action_type:"submit" + button.value
    触发提交回调；事件里 action.value 带 payload，action.form_value 带字段值。
    qid / asker_id 透过按钮 value 带回，不依赖服务端状态。
    """
    options = [
        {"text": {"tag": "plain_text", "content": label}, "value": value}
        for value, label in _FEEDBACK_REASONS.items()
    ]
    btn_common = {"qid": qid, "asker_id": asker_id}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "想了解一下这次回答哪里需要改进，方便我们补文档 / 调 prompt："
                    ),
                },
                {
                    "tag": "form",
                    "name": "feedback_reason_form",
                    "elements": [
                        {
                            "tag": "select_static",
                            "name": "reason",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "请选择原因",
                            },
                            "required": True,
                            "options": options,
                        },
                        {
                            "tag": "input",
                            "name": "comment",
                            "input_type": "multiline_text",
                            "rows": 3,
                            "max_length": 500,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "可选：举例哪步错了 / 哪条步骤少了 / 哪段过时了",
                            },
                        },
                        {
                            "tag": "button",
                            "name": "submit_btn",
                            "text": {"tag": "plain_text", "content": "提交"},
                            "type": "primary",
                            "form_action_type": "submit",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "action": "feedback_reason_submit",
                                        **btn_common,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "tag": "button",
                    "name": "skip_btn",
                    "text": {"tag": "plain_text", "content": "跳过"},
                    "type": "default",
                    "behaviors": [
                        {
                            "type": "callback",
                            "value": {
                                "action": "feedback_reason_skip",
                                **btn_common,
                            },
                        }
                    ],
                },
            ]
        },
    }


def _excerpt(text: str, limit: int = 200) -> str:
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def _parse_escalate(answer: str) -> tuple[str, str | None]:
    """从答案里抽 <<ESCALATE:xxx>> 标记。

    返回 (清理后的答案文本, 要 @ 的 open_id 或 None)。
    none 视作 "不 @ 任何人"，与"未匹配到标记"等价。
    """
    m = _ESCALATE_RE.search(answer)
    if not m:
        return answer, None
    cleaned = _ESCALATE_RE.sub("", answer).strip()
    target = m.group(1)
    return cleaned, (target if target != "none" else None)


def _append_escalate_at(post: dict, owner_id: str) -> None:
    """在 post 内容末尾追加一段 "📣 已通知负责人 @xxx 协助回答"。

    用独立段落（一行）展示，不和答案正文挤在一起。
    """
    post["zh_cn"]["content"].append([{"tag": "text", "text": ""}])  # 空行隔开
    post["zh_cn"]["content"].append(
        [
            {"tag": "text", "text": "📣 已通知负责人 "},
            {"tag": "at", "user_id": owner_id},
            {"tag": "text", "text": " 协助回答 🙏"},
        ]
    )


def _index_owner_to_dir(docs_root: Path) -> dict[str, str]:
    """解析 docs_root/INDEX.md 的"组件目录"表 → {open_id: 目录名}。

    依赖 mtime 缓存：文件没改就直接返回上次的结果。
    解析容错：表头需要同时含"目录"和"open_id"两列；分隔行（|---|）跳过；
    open_id 列必须形如 ou_xxx 才录入，目录列允许 backtick / 末尾 `/`。
    """
    index_path = docs_root / "INDEX.md"
    try:
        mtime = index_path.stat().st_mtime
    except FileNotFoundError:
        return {}
    cached = _archive_index_cache.get(index_path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    mapping: dict[str, str] = {}
    in_table = False
    dir_idx = -1
    open_id_idx = -1
    try:
        with index_path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("|"):
                    if in_table:
                        break
                    continue
                cols = [c.strip() for c in line.strip("|").split("|")]
                if not in_table:
                    if "目录" in cols and "open_id" in cols:
                        dir_idx = cols.index("目录")
                        open_id_idx = cols.index("open_id")
                        in_table = True
                    continue
                # 分隔行 |---|---|
                if all(set(c) <= set("-: ") and c for c in cols):
                    continue
                if max(dir_idx, open_id_idx) >= len(cols):
                    continue
                dir_cell = cols[dir_idx].strip("`").strip().rstrip("/")
                open_id_cell = cols[open_id_idx].strip("`").strip()
                if not dir_cell or not _OPEN_ID_RE.match(open_id_cell):
                    continue
                mapping[open_id_cell] = dir_cell
    except OSError as e:
        logger.warning("read INDEX.md for archive mapping failed: %s", e)
        return _archive_index_cache.get(index_path, (0.0, {}))[1]

    _archive_index_cache[index_path] = (mtime, mapping)
    return mapping


def _archive_form_card(
    qid: str, question: str, owner_id: str, archive_path_repr: str
) -> dict:
    """归档表单卡（card v2 form）：展示问题 + 多行答案输入框 + 提交按钮。

    archive_path_repr：展示给 owner 的相对路径（如 "redis/qa-archive.md"），
    让他知道答案会落到哪个文件再决定写多详细。
    """
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "📝 问答归档"},
            "template": "blue",
        },
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"**Q:** {_excerpt(question, 300)}\n\n"
                        f"<at id={owner_id}></at> 答完后请把整理过的答案填进下方输入框，"
                        f"提交后会追加进 `{archive_path_repr}`。"
                    ),
                },
                {
                    "tag": "form",
                    "name": "archive_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "answer",
                            "input_type": "multiline_text",
                            "rows": 6,
                            "max_length": 10000,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "粘贴整理后的答案文本…",
                            },
                            "required": True,
                            "label": {"tag": "plain_text", "content": "答案"},
                            "label_position": "top",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "提交并归档"},
                            "type": "primary",
                            "action_type": "form_submit",
                            "name": "submit_btn",
                            "value": {"action": "archive_submit", "qid": qid},
                        },
                    ],
                },
            ]
        },
    }


def _archive_ack_card(icon: str, message: str) -> dict:
    """提交后用来替换原表单卡的提示卡（card v2，纯文本）。"""
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"{icon} {message}"},
            ]
        },
    }


async def _write_qa_archive(
    file_path: Path,
    qid: str,
    question: str,
    answer: str,
    owner_id: str,
    asker_id: str | None,
) -> bool:
    """append-only 写一条 Q&A。已存在 qid 跳过返回 False，写入返回 True。

    每个 file_path 一把 asyncio.Lock，并发归档不会撕裂；幂等键是 `qid: <id>`
    字符串在文件里的存在与否，省得维护单独索引。
    """
    lock = _archive_locks.setdefault(file_path, asyncio.Lock())
    async with lock:
        existing = ""
        if file_path.is_file():
            try:
                existing = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError as e:
                logger.warning("read qa-archive failed: path=%s err=%s", file_path, e)
        if f"qid: {qid}" in existing:
            return False
        file_path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime())
        meta_parts = [f"回答者：<@{owner_id}>", ts, f"qid: {qid}"]
        if asker_id:
            meta_parts.append(f"提问者：<@{asker_id}>")
        block = (
            f"\n## Q: {question.strip()}\n\n"
            f"*{' · '.join(meta_parts)}*\n\n"
            f"{answer.strip()}\n\n"
            f"---\n"
        )
        with file_path.open("a", encoding="utf-8") as f:
            f.write(block)
        return True


async def handle_archive_submit(
    qid: str | None,
    answer: str,
    clicker_id: str | None,
    docs_root: Path,
) -> dict:
    """处理归档表单提交。返回应替换原表单卡的 ack 卡片（card v2）。

    所有失败路径（参数缺失、过期、非负责人点击、空答案、写盘异常）都
    用 ack 卡片告诉点击者，原卡片被替换，避免重复提交困惑。
    """
    if not qid:
        return _archive_ack_card("⚠️", "归档参数缺失，请联系管理员。")
    ctx = _pending_archives.get(qid)
    if ctx is None:
        return _archive_ack_card(
            "⏰", "归档会话已过期或已处理，请联系管理员手动补记。"
        )

    expected_owner = ctx["owner_id"]
    if clicker_id and clicker_id != expected_owner:
        return _archive_ack_card(
            "🔒", f"只有 <at id={expected_owner}></at> 能归档此问答。"
        )

    answer_text = (answer or "").strip()
    if not answer_text:
        return _archive_ack_card("⚠️", "答案不能为空，请填写后再提交。")
    if len(answer_text) > 10_000:
        return _archive_ack_card("⚠️", "答案过长（>10KB），请精简后再提交。")

    component_dir = ctx.get("component_dir")
    if component_dir:
        file_path = docs_root / component_dir / "qa-archive.md"
    else:
        file_path = docs_root / "qa-archive.md"

    try:
        wrote = await _write_qa_archive(
            file_path=file_path,
            qid=qid,
            question=ctx["question"],
            answer=answer_text,
            owner_id=expected_owner,
            asker_id=ctx.get("asker_id"),
        )
    except Exception as e:
        logger.exception("archive write failed: qid=%s path=%s", qid, file_path)
        return _archive_ack_card("❌", f"归档写入失败：{e}")

    # 完成（写入或幂等命中）：从 pending 里清掉，避免重复处理
    _pending_archives.pop(qid, None)

    try:
        rel = file_path.relative_to(docs_root)
    except ValueError:
        rel = file_path
    feedback_logger.info(
        json.dumps(
            {
                "event": "archive",
                "qid": qid,
                "owner_id": expected_owner,
                "asker_id": ctx.get("asker_id"),
                "path": str(rel),
                "answer_excerpt": _excerpt(answer_text, 500),
                "duplicate": not wrote,
            },
            ensure_ascii=False,
        )
    )
    logger.info(
        "archive written: qid=%s path=%s duplicate=%s", qid, rel, not wrote
    )

    if wrote:
        return _archive_ack_card("✅", f"已归档至 `{rel}`，谢谢！")
    return _archive_ack_card(
        "ℹ️", f"该 qid 的归档已存在（`{rel}`），跳过。"
    )


async def handle_question(
    chat_id: str,
    user_id: str,
    question: str,
    feishu: FeishuClient,
    session_mgr: SessionManager,
    *,
    parent_msg_id: str | None = None,
    images: list[tuple[str, bytes]] | None = None,
) -> None:
    """处理单条用户提问（完整流程：重置 / 占位 / 答题 / 编辑 / 反馈卡片）。

    HTTP 模式和长连接模式都走这里，参数化 feishu 和 session_mgr 以便复用。

    `parent_msg_id` 给定时（来自原始用户消息），所有 bot 发出的消息（占位 / 答案 /
    反馈卡 / 追问卡 / 归档卡）都会"引用回复"这条原消息，飞书群里每条 bot 消息头部
    会显示原问题的引用条，连发多条问题时归属一目了然。followup-trigger 走来的不带
    parent_msg_id（无锚点消息），自然按 top-level 发送。

    `images` 给定时走视觉路径（image content block 喂给底层模型），占位文本切到
    "🖼️ 识别图片中…" 而不是问题摘要（用户发图时通常没有文字摘要可展示）。
    """
    key = (chat_id, user_id)

    # 重置指令：清掉该用户的会话（仅 text 输入有效；图片消息不走重置语义）
    if not images and question in RESET_TRIGGERS:
        existed = await session_mgr.reset(key)
        reply = (
            "已清空你的对话历史，下一个问题会开启新会话。"
            if existed
            else "你当前还没有活跃会话，下一个问题就是新会话。"
        )
        await feishu.send_post(
            chat_id, _mention_post(user_id, reply), parent_id=parent_msg_id
        )
        return

    # 1. 立即发占位消息：带问题摘要 + 排队/翻文档状态前缀，让用户能识别这条占位
    # 是为哪一句问题、是否在排队等前一条
    queued = await session_mgr.queued(key)
    if images:
        # 图片场景没有可摘要的"用户问题"，统一用图标占位；排队状态仍提示
        placeholder_init = (
            "🕒 排队中：识别图片" if queued else "🖼️ 识别图片中…"
        )
    else:
        placeholder_init = _placeholder_text(question, queued)
    placeholder_mid = await feishu.send_post(
        chat_id,
        _mention_post(user_id, placeholder_init),
        parent_id=parent_msg_id,
    )

    # 2. 生成答案
    result: AnswerResult | None = None
    try:
        entry = await session_mgr.get(key)
        async with entry.lock:
            # 拿到锁意味着前面的问题已答完，把占位从"排队中"刷成"翻文档中"
            if queued and placeholder_mid is not None:
                refresh = (
                    "🖼️ 识别图片中…" if images else _placeholder_text(question, False)
                )
                await feishu.update_post(
                    placeholder_mid,
                    _mention_post(user_id, refresh),
                )
            result = await entry.bot.answer(question, images=images)
            entry.last_used = time.time()
        answer = result.text
    except Exception as e:
        logger.exception("answer failed: chat=%s user=%s", chat_id, user_id)
        answer = f"抱歉，处理失败：{e}"
    answer = answer or "（无回答内容）"

    # 解析"找不到 → @ 负责人"标记。owner 为 None 表示不 @
    answer, escalate_owner = _parse_escalate(answer)
    # 解析快捷追问标记，挂在反馈卡上面让用户一键发起新一轮
    answer, followup_keys = _parse_followups(answer)
    # 解析反问标记：是否为"信息不足、需要用户补充"的反问轮
    answer, is_clarification = _parse_clarify(answer)
    # 反问轮防御性清空：prompt 已要求反问时不输出 ESCALATE/FOLLOWUPS，但 LLM
    # 偶尔会不严格遵守。强制清掉，避免反问轮还 @ 负责人 / 挂追问按钮把用户搞糊涂。
    if is_clarification:
        escalate_owner = None
        followup_keys = []
    final_post = _mention_post(user_id, answer)
    escalated_now = False
    if escalate_owner is not None:
        cooldown_key = (chat_id, escalate_owner)
        if cooldown_key in _escalate_cooldown:
            logger.info(
                "escalate cooldown hit: chat=%s owner=%s, suppress @",
                chat_id,
                escalate_owner,
            )
        else:
            _escalate_cooldown[cooldown_key] = True
            _append_escalate_at(final_post, escalate_owner)
            escalated_now = True
            logger.info(
                "escalated to owner: chat=%s owner=%s", chat_id, escalate_owner
            )

    # qid 提前生成：反馈卡 + 归档表单卡都需要它做关联
    qid = uuid.uuid4().hex[:12]

    # 3. 用最终答案替换占位；编辑失败则兜底发新消息（也走引用回复维持归属）
    if placeholder_mid is not None:
        if not await feishu.update_post(placeholder_mid, final_post):
            logger.warning(
                "update placeholder failed (mid=%s), sending new message",
                placeholder_mid,
            )
            await feishu.send_post(chat_id, final_post, parent_id=parent_msg_id)
    else:
        await feishu.send_post(chat_id, final_post, parent_id=parent_msg_id)

    # 4. 记录问答日志
    qa_record: dict[str, object] = {
        "event": "qa",
        "qid": qid,
        "chat_id": chat_id,
        "user_id": user_id,
        "question": _excerpt(question, 500),
        "answer_excerpt": _excerpt(answer, 500),
    }
    if escalate_owner is not None:
        qa_record["escalated_to"] = escalate_owner
    if is_clarification:
        qa_record["clarification"] = True
    # 模型用量：直接转发 SDK 给的字段，对接第三方 Claude 兼容代理时可以拿
    # input_tokens / output_tokens / cache_* 套自己的单价表算成本。
    if result is not None:
        qa_record["cost_usd"] = result.cost_usd
        qa_record["usage"] = result.usage
        qa_record["num_turns"] = result.num_turns
        qa_record["duration_ms"] = result.duration_ms
        qa_record["duration_api_ms"] = result.duration_api_ms
    feedback_logger.info(json.dumps(qa_record, ensure_ascii=False))

    # 5. 追问卡（如果有候选 key）+ 反馈卡（每次都发）。两张独立的 interactive
    # 消息，都引用回到 parent_msg_id：点追问只替换追问卡，反馈卡照样能 👍/👎。
    # 反问轮跳过：让用户专注答反问，下一轮按补充信息再答 + 那时再挂反馈/追问
    if not is_clarification:
        if followup_keys:
            await feishu.send_interactive(
                chat_id,
                _followup_card(qid, user_id, chat_id, parent_msg_id, followup_keys),
                parent_id=parent_msg_id,
            )
        await feishu.send_interactive(
            chat_id,
            _feedback_card(qid, user_id),
            parent_id=parent_msg_id,
        )

    # 6. 归档表单卡：仅在本次实际 @ 了负责人时发（cooldown 命中或 none 跳过）
    if escalated_now:
        component_dir = _index_owner_to_dir(session_mgr.docs_root).get(
            escalate_owner or ""
        )
        archive_path_repr = (
            f"{component_dir}/qa-archive.md" if component_dir else "qa-archive.md"
        )
        _pending_archives[qid] = {
            "chat_id": chat_id,
            "asker_id": user_id,
            "question": question,
            "owner_id": escalate_owner,
            "component_dir": component_dir,
        }
        await feishu.send_interactive(
            chat_id,
            _archive_form_card(qid, question, escalate_owner, archive_path_repr),
            parent_id=parent_msg_id,
        )
        logger.info(
            "archive form sent: qid=%s owner=%s target=%s",
            qid,
            escalate_owner,
            archive_path_repr,
        )


def handle_feedback_click(
    qid: str,
    rating: str,
    clicker_id: str | None,
    asker_id: str | None,
) -> dict:
    """记录反馈点击日志，返回应替换原卡片的卡片 JSON。

    👍：返回简单 ack 卡（v1，流程结束）。
    👎：先记 rating=down，再返回原因收集表单（v2 form）；用户填完按"提交"
    或"跳过"会触发第二次回调（feedback_reason_submit / _skip）记 reason 行。
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
    if rating == "down":
        return _feedback_reason_form_card(qid, asker_id)
    return _feedback_ack_card(rating)


def handle_feedback_reason_submit(
    qid: str | None,
    reason: str | None,
    comment: str | None,
    clicker_id: str | None,
    asker_id: str | None,
) -> dict:
    """处理 👎 后原因表单的提交，返回最终 ack 卡。

    reason 不在白名单（None / 注入 / SDK 字段名变了）会写一行 invalid 标记的
    日志，但仍返回 ack，避免 UI 卡住。grep `event=feedback_reason invalid=true`
    可发现这类异常。
    """
    valid = reason in _FEEDBACK_REASONS
    feedback_logger.info(
        json.dumps(
            {
                "event": "feedback_reason",
                "qid": qid,
                "reason": reason if valid else None,
                "reason_label": _FEEDBACK_REASONS.get(reason or "") if valid else None,
                "comment": _excerpt(comment, 500) if comment else None,
                "clicker_id": clicker_id,
                "asker_id": asker_id,
                "invalid": not valid,
            },
            ensure_ascii=False,
        )
    )
    logger.info(
        "feedback reason qid=%s reason=%s by=%s comment_len=%d",
        qid,
        reason,
        clicker_id,
        len(comment or ""),
    )
    return _feedback_ack_card("down")


def handle_feedback_reason_skip(
    qid: str | None,
    clicker_id: str | None,
    asker_id: str | None,
) -> dict:
    """用户在原因表单里点"跳过"：记一条 skipped 事件，返回最终 ack。

    skipped 计数能告诉我们"愿不愿意填原因"的整体比例；如果绝大多数都跳过，
    说明这个二次表单要么时机不对要么选项不对，需要再调。
    """
    feedback_logger.info(
        json.dumps(
            {
                "event": "feedback_reason_skipped",
                "qid": qid,
                "clicker_id": clicker_id,
                "asker_id": asker_id,
            },
            ensure_ascii=False,
        )
    )
    logger.info("feedback reason skipped: qid=%s by=%s", qid, clicker_id)
    return _feedback_ack_card("down")
