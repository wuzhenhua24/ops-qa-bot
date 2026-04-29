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
PLACEHOLDER_MARKDOWN = "🔍 正在翻文档，请稍候..."

SessionKey = tuple[str, str]  # (chat_id, user_open_id)

# 升级机制：bot 答不上来时，按 prompt 输出 <<ESCALATE:ou_xxx>> 标记，
# handle_question 拦截 → 移除标记 → 在 post 末尾注入 @owner 提醒。
# 只接受形如 ou_xxxx 或字面量 none 两种值，防止注入。
_ESCALATE_RE = re.compile(r"<<ESCALATE:(ou_[A-Za-z0-9_-]+|none)>>")
_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")
# 同一 (chat, owner) 30 分钟内只 @ 一次，防止用户连环问把负责人刷烦
_escalate_cooldown: TTLCache = TTLCache(maxsize=10000, ttl=1800)

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
) -> None:
    """处理单条用户提问（完整流程：重置 / 占位 / 答题 / 编辑 / 反馈卡片）。

    HTTP 模式和长连接模式都走这里，参数化 feishu 和 session_mgr 以便复用。
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
    result: AnswerResult | None = None
    try:
        entry = await session_mgr.get(key)
        async with entry.lock:
            result = await entry.bot.answer(question)
            entry.last_used = time.time()
        answer = result.text
    except Exception as e:
        logger.exception("answer failed: chat=%s user=%s", chat_id, user_id)
        answer = f"抱歉，处理失败：{e}"
    answer = answer or "（无回答内容）"

    # 解析"找不到 → @ 负责人"标记。owner 为 None 表示不 @
    answer, escalate_owner = _parse_escalate(answer)
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
    # 模型用量：直接转发 SDK 给的字段，对接第三方 Claude 兼容代理时可以拿
    # input_tokens / output_tokens / cache_* 套自己的单价表算成本。
    if result is not None:
        qa_record["cost_usd"] = result.cost_usd
        qa_record["usage"] = result.usage
        qa_record["num_turns"] = result.num_turns
        qa_record["duration_ms"] = result.duration_ms
        qa_record["duration_api_ms"] = result.duration_api_ms
    feedback_logger.info(json.dumps(qa_record, ensure_ascii=False))

    # 5. 反馈卡（每次都发）
    await feishu.send_interactive(chat_id, _feedback_card(qid, user_id))

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
    """记录反馈点击日志，返回应替换原卡片的 ack 卡片 JSON。

    两种模式通用：HTTP 模式把返回值包成 `{"card": <ack>}`；
    WS 模式塞进 `P2CardActionTriggerResponse`。
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
