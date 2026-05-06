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
from typing import Any

import httpx
from cachetools import LRUCache, TTLCache

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


# 进度占位限频参数：飞书 update_post 是 PUT API，短时间高频会触发 999911404 类
# 限流；中间一连串同目录 Read 折叠成累计计数靠节流压平闪烁。
# - _PROGRESS_MIN_INTERVAL：非强制刷新的最小间隔（秒）。强制档（跨工具/跨目录）
#   立刻刷，不受这个间隔限制。
# - _PROGRESS_MAX_UPDATES：一次回答最多刷几次占位，避免 LLM 跑 20 个工具时
#   占位被刷 20 次让用户晕。超出后停刷，等最终答案 update_post。
_PROGRESS_MIN_INTERVAL = 1.5
_PROGRESS_MAX_UPDATES = 5


# 错误归类：把底层异常翻译成"用户能看懂、知道下一步怎么做"的提示，避免把
# traceback / API code / SDK 内部消息泄漏到飞书。判定靠"异常类名 + str(e)"
# 关键词嗅探，httpx / SDK / 第三方 Claude 兼容代理的异常类型各异，硬绑类型
# 反而会漏；未命中归通用兜底。raw exception 仍由 logger.exception 进日志。
_ERROR_PATTERNS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("rate limit", "rate_limit", "ratelimit", "429", "overloaded", "too many requests"),
        "🙇 模型当前繁忙，请稍等几秒再试一次。",
    ),
    (
        ("timeout", "timed out", "deadline exceeded"),
        "⏱️ 处理超时，请稍后重试；如反复出现请把问题简化或拆成几条问。",
    ),
    (
        ("payload too large", "request entity too large", "413"),
        "📦 请求过大，请压缩附件或简化问题后重试。",
    ),
    (
        ("unauthorized", "401", "permission denied", "forbidden", "403"),
        "🔒 权限或鉴权出错，请联系机器人管理员核对配置。",
    ),
    (
        # 上游连接 / 网关错误。注意 "connection" 关键词放在 401/403 之后，避免
        # "Connection refused / 401" 之类混合消息被错归到网络档
        ("connection refused", "connect error", "network is unreachable",
         "bad gateway", "502", "503", "504", "gateway timeout"),
        "🌐 网络或上游服务不稳定，稍后重试一次。",
    ),
)


def _friendly_error(
    e: BaseException, *, context: str = "处理", suggest_reset: bool = False
) -> str:
    """归类异常 → 用户友好提示。原异常仍走 logger.exception 上报。

    suggest_reset：仅 catch-all 分支生效——预分类（rate limit / timeout / 网络等）的
    根因明确不是会话状态，重试即可，不混入 reset 建议。catch-all 里恰好包含"session
    被 LLM 输出搞拧了 / SDK 内部状态污染"这类只能靠重启 session 恢复的场景，把已有的
    /reset 通道在错误时机教给用户，先自救一步再走"联系管理员"。仅 QA 流程的调用处
    传 True，图片下载 / 归档写入这种无 session 状态的上下文不传，避免误导。
    """
    blob = (type(e).__name__ + " " + str(e)).lower()
    for keywords, friendly in _ERROR_PATTERNS:
        if any(k in blob for k in keywords):
            return friendly
    if suggest_reset:
        return (
            f"❗ {context}临时出错，请稍后重试；"
            "如反复出现可发 /reset 清空当前会话再试，仍不行请联系管理员。"
        )
    return f"❗ {context}临时出错，请稍后重试；如持续出现请联系管理员。"


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


class ProgressTracker:
    """吃 bot.ask() 流式工具事件，给出占位文案更新决策。

    三档文案：
    - Glob 命中组件目录 → "🔍 已定位 redis/，正在列文档…"
    - 首个 Read（跨目录/跨工具） → "📖 正在查看 redis/troubleshooting.md…"
    - 后续同目录 Read → "📖 正在查看 redis/ 下 N 个文件…"（计数累加）
    - Grep → "🔎 正在搜索 'pattern'…"
    - INDEX.md → "📖 正在读路由表…"

    返回 (新文案, force_update) 或 None。force_update 用于跨阶段/跨目录这种
    用户感知强的关键节点，调用方可绕过节流限频立刻 update；同档内的累计更新
    是 force=False，靠时间窗口限频。
    """

    def __init__(self, docs_root: Path):
        self._docs_root = str(docs_root.resolve())
        self._last_dir: str | None = None
        self._last_phase: str | None = None  # "glob" | "read" | "grep" | "index"
        self._read_count_in_dir: int = 0

    def _extract_top_dir(self, path_or_pattern: str) -> str | None:
        """从相对路径 / glob pattern / 绝对路径里抽出 docs_root 下的一级目录名。

        - "redis/troubleshooting.md"            → "redis"
        - "redis/*.md"                          → "redis"
        - "/abs/.../docs/redis/x.md"            → "redis"
        - "INDEX.md" / "*.md"                   → None（顶层无组件归属）
        - "redis/images/x.png"                  → "redis"（嵌套不影响）
        """
        if not path_or_pattern:
            return None
        p = path_or_pattern
        # 绝对路径相对化：如果在 docs_root 下，剥前缀
        if p.startswith(self._docs_root):
            p = p[len(self._docs_root):]
        p = p.lstrip("/")
        if "/" not in p:
            return None
        head = p.split("/", 1)[0]
        # 通配符开头的"目录"不算（罕见但避免误判）
        if any(c in head for c in "*?["):
            return None
        return head

    def consume(self, tool_name: str, tool_input: dict) -> tuple[str, bool] | None:
        """处理一个工具调用事件，返回 (placeholder_text, force_update) 或 None。"""
        if tool_name == "Glob":
            pattern = str(tool_input.get("pattern", ""))
            d = self._extract_top_dir(pattern)
            if d:
                # 跨目录强制刷新，同目录重复 Glob 不抖动
                if d != self._last_dir or self._last_phase != "glob":
                    self._last_dir = d
                    self._last_phase = "glob"
                    self._read_count_in_dir = 0
                    return f"🔍 已定位 {d}/，正在列文档…", True
            return None

        if tool_name == "Read":
            file_path = str(tool_input.get("file_path", ""))
            base = file_path.rsplit("/", 1)[-1]
            d = self._extract_top_dir(file_path)
            if not d:
                # 顶层文件：INDEX.md 单独标识，其它顶层 md 不展示
                if base.lower() == "index.md" and self._last_phase != "index":
                    self._last_phase = "index"
                    self._last_dir = None
                    self._read_count_in_dir = 0
                    return "📖 正在读路由表…", True
                return None
            if d != self._last_dir or self._last_phase != "read":
                # 跨组件 / 从 Glob/Grep 切到 Read：展示具体文件名
                self._last_dir = d
                self._last_phase = "read"
                self._read_count_in_dir = 1
                return f"📖 正在查看 {d}/{base}…", True
            # 同目录连读：折叠成计数，靠节流防抖动
            self._read_count_in_dir += 1
            return f"📖 正在查看 {d}/ 下 {self._read_count_in_dir} 个文件…", False

        if tool_name == "Grep":
            pattern = str(tool_input.get("pattern", ""))
            short = pattern[:30] + ("…" if len(pattern) > 30 else "")
            if self._last_phase != "grep":
                self._last_phase = "grep"
                self._last_dir = None
                self._read_count_in_dir = 0
                return f"🔎 正在搜索 '{short}'…", True
            return None

        return None

SessionKey = tuple[str, str]  # (chat_id, user_open_id)

# 升级机制：bot 答不上来时，按 prompt 输出 <<ESCALATE:ou_xxx:component_dir>>
# 标记，handle_question 拦截 → 移除标记 → 在 post 末尾注入 @owner 提醒。
# owner 接受 ou_xxx 或 none；后缀目录可选，由 LLM 基于"问题归属哪个组件"给出，
# 用于归档卡选目录。owner / dir 都做白名单校验防注入和路径穿越。
_ESCALATE_RE = re.compile(
    r"<<ESCALATE:(?P<who>ou_[A-Za-z0-9_-]+|none)"
    r"(?::(?P<dir>[A-Za-z0-9._/-]+))?>>"
)
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

# 答案内嵌图机制：步骤截图 / 标注图 / 强相关故障截图，文字转述不如直接展示。
# LLM 在答案里独立一行写 <<IMG:redis/images/step1.png>>（路径相对 docs_root），
# bot 校验路径 → 上传飞书拿 image_key → 把标记换成 <<IMG_KEY:img_xxx>>，渲染层
# 把这种行渲染为飞书 post 的 img 段。每条最多 5 张，超限剥除 + 末尾告知用户。
# 路径正则放宽到非控制字符以兼容中文/带空格的文件名；安全校验在 _validate 里做。
_IMG_RE = re.compile(r"<<IMG:([^<>\n\r]+?)>>")
_IMG_ALLOWED_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_IMG_MAX_BYTES = 5 * 1024 * 1024  # 飞书消息图官方上限 10MB，留点余量
_IMG_MAX_PER_ANSWER = 5
# 同一 docs 图在多轮答案里会被反复引用，缓存 (绝对路径, mtime) → image_key 避免
# 重复上传；mtime 变化（图被替换）天然失效。LRU 500 条对常规文档量够用。
_image_key_cache: LRUCache = LRUCache(maxsize=500)
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
_archive_index_cache: dict[Path, tuple[float, dict[str, list[str]]]] = {}
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

    async def upload_image(self, image_bytes: bytes) -> str | None:
        """上传图片到飞书拿 image_key（用于 post 消息内嵌 img 段），失败返回 None。

        API: POST /open-apis/im/v1/images，image_type=message。要求 `im:resource:upload`
        scope（实际作用域名以飞书后台为准）。image_key 在飞书侧长期有效，调用方可放
        心做内存级缓存，重启后再 lazy 重传即可。
        """
        async with httpx.AsyncClient(timeout=30) as client:
            token = await self._get_token(client)
            files = {
                "image_type": (None, "message"),
                "image": ("image", image_bytes),
            }
            resp = await client.post(
                f"{FEISHU_BASE}/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                files=files,
            )
            body = resp.json() if resp.content else {}
            if resp.status_code != 200 or body.get("code") != 0:
                logger.error(
                    "feishu upload_image failed: status=%s body=%s",
                    resp.status_code,
                    resp.text,
                )
                return None
            return (body.get("data") or {}).get("image_key")

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

    回收是静默的，但需要给"过期回来追问"的用户显式提示。`_last_seen` 表独立
    于 session 生命周期保留 24h，记录每个 (chat, user) 上次活跃的 unix 时间戳；
    `take_expired_notice` 用 last_seen + session 缺失 + 距今 ≥ idle_ttl 三联
    判定"上一轮上下文已被回收"，调用方据此在新答案里挂一行提示。`/reset`
    主动清掉 last_seen 不算过期，避免给主动重置的用户再补一条提示。
    """

    def __init__(self, docs_root: Path, idle_ttl: float = 1800.0):
        self._docs_root = docs_root
        self._idle_ttl = idle_ttl
        self._sessions: dict[SessionKey, _SessionEntry] = {}
        self._manager_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None
        # 24h TTL：再往前的回访就当全新用户，不再提示"上下文已过期"——
        # 隔一天才回来一般是新场景，提示反而显得啰嗦
        self._last_seen: TTLCache = TTLCache(maxsize=10000, ttl=86400)

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
            self._last_seen[key] = entry.last_used
            return entry

    async def take_expired_notice(self, key: SessionKey) -> bool:
        """若上一次见过该用户但 session 已被空闲回收，返回 True 并消费一次性
        标记。**必须在 get(key) 之前调用**——get 会刷新 last_seen，调用顺序错
        了就检测不到。

        判定三条件：(1) last_seen 存在 (2) 当前 session 不在 (3) 距今 ≥ idle_ttl。
        命中后 pop 掉 last_seen，本轮答完 get 会写回 now，下一轮自然不再触发。
        """
        async with self._manager_lock:
            if key in self._sessions:
                return False
            last = self._last_seen.get(key)
            if last is None:
                return False
            if time.time() - last < self._idle_ttl:
                return False
            self._last_seen.pop(key, None)
            return True

    async def reset(self, key: SessionKey) -> bool:
        """关闭并移除指定 session。返回 True 表示之前存在。

        同时清 last_seen：用户主动 /reset 不算"过期回收"，下一轮提问按全新会话
        处理，不要再追加"上下文已过期"提示徒增噪音。
        """
        async with self._manager_lock:
            entry = self._sessions.pop(key, None)
            self._last_seen.pop(key, None)
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
                f"📎 附件读取失败：{_friendly_error(e, context='图片下载')}\n"
                "你可以把截图里的关键报错或现象用文字描述出来再发。",
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


def _validate_image_path(rel: str, docs_root: Path) -> Path | None:
    """校验 LLM 在 <<IMG:path>> 里给的路径：必须 docs_root 子目录下真实图片文件，
    扩展名在白名单内，大小 ≤ _IMG_MAX_BYTES。

    LLM 偶尔会写错路径 / 写绝对路径 / 写 ../ 跳出 docs_root，全部拒绝；上传飞书
    的图必须保证是 docs_root 下的合法运维文档插图，不让 LLM 借此把任意本地文件
    上传到飞书。
    """
    cleaned = (rel or "").strip().strip("/").rstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        return None
    try:
        target = (docs_root / cleaned).resolve()
        root = docs_root.resolve()
    except OSError:
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None
    if not target.is_file():
        return None
    if target.suffix.lower() not in _IMG_ALLOWED_EXT:
        return None
    try:
        if target.stat().st_size > _IMG_MAX_BYTES:
            return None
    except OSError:
        return None
    return target


async def _upload_doc_image(
    target: Path, feishu: "FeishuClient"
) -> str | None:
    """读 docs 下图片字节并上传飞书（带 LRU 缓存），返回 image_key 或 None。"""
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return None
    cache_key = (str(target), mtime)
    cached = _image_key_cache.get(cache_key)
    if cached:
        return cached
    try:
        with target.open("rb") as f:
            blob = f.read()
    except OSError as e:
        logger.warning("read doc image failed: path=%s err=%s", target, e)
        return None
    image_key = await feishu.upload_image(blob)
    if image_key:
        _image_key_cache[cache_key] = image_key
    return image_key


async def _resolve_image_markers(
    answer: str, docs_root: Path, feishu: "FeishuClient"
) -> tuple[str, list[str], list[str]]:
    """剥答案里的 <<IMG:rel_path>>：校验路径 → 上传 → 把成功的标记换成
    <<IMG_KEY:img_xxx>>，失败/超限的剥除。

    返回 (改写后的答案, 已展示的相对路径列表, 因 cap 被截断的相对路径列表)。
    `attached` 供日志统计展示 ROI；`truncated` 让调用方在答案末尾告知用户
    "另有 N 张图未展示"，避免出现"按下图操作"但底下没图的脱节体验。
    保序去重：同一路径在答案里重复出现只上传一次、只展示一次（LLM 偶尔会把图
    放在多个段落里），按出现顺序保留，超过 _IMG_MAX_PER_ANSWER 截断。
    """
    paths_in_order = _IMG_RE.findall(answer)
    if not paths_in_order:
        return answer, [], []
    seen: set[str] = set()
    uniq: list[str] = []
    for p in paths_in_order:
        cleaned = p.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            uniq.append(cleaned)

    # 严格按 cap 切：只有"超出 cap 被吞掉"的算 truncated；前 N 张里因路径无效 /
    # 上传失败丢的算"失败"（已有 warning 日志），不汇总成用户可见提示——失败是
    # 偶发/可疑事件，混进"另有 N 张图未展示"会让用户以为是 cap 截断。
    truncated = uniq[_IMG_MAX_PER_ANSWER:]

    path_to_key: dict[str, str] = {}
    attached: list[str] = []
    for rel in uniq[:_IMG_MAX_PER_ANSWER]:
        target = _validate_image_path(rel, docs_root)
        if target is None:
            logger.warning("invalid IMG marker, dropped: rel=%s", rel)
            continue
        image_key = await _upload_doc_image(target, feishu)
        if not image_key:
            logger.warning("upload IMG failed, dropped: rel=%s", rel)
            continue
        path_to_key[rel] = image_key
        attached.append(rel)

    def _replace(m: re.Match) -> str:
        rel = m.group(1).strip()
        key = path_to_key.get(rel)
        return f"<<IMG_KEY:{key}>>" if key else ""

    new_answer = _IMG_RE.sub(_replace, answer)
    # 剥除标记后可能留多余空行，归并相邻空行减少视觉噪音
    new_answer = re.sub(r"\n{3,}", "\n\n", new_answer).strip()
    return new_answer, attached, truncated


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


def _parse_escalate(answer: str) -> tuple[str, str | None, str | None]:
    """从答案里抽 <<ESCALATE:owner[:component_dir]>> 标记。

    返回 (清理后的答案文本, 要 @ 的 open_id 或 None, LLM 给的组件目录 hint 或 None)。
    none 视作"不 @ 任何人"，与"未匹配到标记"等价。component_dir 是 LLM 基于答案
    路由判断给出的归档目录提示，调用方需先 `_resolve_component_dir` 校验落地。
    """
    m = _ESCALATE_RE.search(answer)
    if not m:
        return answer, None, None
    cleaned = _ESCALATE_RE.sub("", answer).strip()
    who = m.group("who")
    dir_hint = m.group("dir")
    return cleaned, (who if who != "none" else None), dir_hint


def _resolve_component_dir(dir_hint: str | None, docs_root: Path) -> str | None:
    """把 LLM 给的目录 hint 校验成"docs_root 下真实存在的相对目录"，否则返回 None。

    LLM 输出可能写错目录名 / 写绝对路径 / 写 ../../etc，必须把真实写盘路径限制在
    docs_root 子目录下，否则归档会落到任意位置。匹配失败的 hint 直接丢弃，调用方
    自行 fallback 到按 owner 反查 INDEX。
    """
    if not dir_hint:
        return None
    cleaned = dir_hint.strip().strip("/").rstrip("/")
    if not cleaned or ".." in cleaned.split("/"):
        return None
    try:
        target = (docs_root / cleaned).resolve()
        root = docs_root.resolve()
    except OSError:
        return None
    if not target.is_dir():
        return None
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return cleaned


def _append_escalate_at(post: dict, owner_id: str, archive_path: str) -> None:
    """在 post 末尾追加 "📣 已通知负责人 @xxx" + "📁 归档去向" 两行。

    archive_path 是相对 docs_root 的路径（如 "redis/qa-archive.md"），与紧随其后
    发出的归档表单卡 (_archive_form_card) 一致。告诉 asker 答案最终会落到哪、下次
    类似问题 bot 能从哪里直接答，避免"通知完就没下文"的预期空白。归档依赖 owner
    填表单，措辞用条件式（"填写后会归档"）不要说死。
    """
    post["zh_cn"]["content"].append([{"tag": "text", "text": ""}])  # 空行隔开
    post["zh_cn"]["content"].append(
        [
            {"tag": "text", "text": "📣 已通知负责人 "},
            {"tag": "at", "user_id": owner_id},
            {"tag": "text", "text": " 协助回答 🙏"},
        ]
    )
    post["zh_cn"]["content"].append(
        [
            {
                "tag": "text",
                "text": (
                    f"📁 负责人填写后会归档到 {archive_path}，"
                    "下次类似问题我能直接从这里答。"
                ),
            }
        ]
    )


def _index_owner_to_dirs(docs_root: Path) -> dict[str, list[str]]:
    """解析 docs_root/INDEX.md 的"组件目录"表 → {open_id: [目录1, 目录2, ...]}。

    一个 owner 在表里挂多个组件时所有目录都保留（保序去重），不再后写覆盖前写。
    调用方在 owner 反查兜底时只接受唯一映射，多目录场景下让 LLM 给的 hint 决定，
    避免归档静默落到"该 owner 名下另一个组件"。

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

    mapping: dict[str, list[str]] = {}
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
                dirs = mapping.setdefault(open_id_cell, [])
                if dir_cell not in dirs:
                    dirs.append(dir_cell)
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
        "config": {"update_multi": True},
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
                            "max_length": 1000,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "粘贴整理后的答案文本（最多 1000 字）…",
                            },
                            "required": True,
                        },
                        {
                            "tag": "button",
                            "name": "submit_btn",
                            "text": {"tag": "plain_text", "content": "提交并归档"},
                            "type": "primary",
                            "form_action_type": "submit",
                            "behaviors": [
                                {
                                    "type": "callback",
                                    "value": {
                                        "action": "archive_submit",
                                        "qid": qid,
                                    },
                                }
                            ],
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
        return _archive_ack_card(
            "❌", _friendly_error(e, context="归档写入")
        )

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

    # 上一轮 session 已被空闲回收时一次性消费这个标记，等会儿挂在答案最前面
    # 让用户知道"那一句『接着上面的』bot 不会按追问处理"。必须在 get(key) 前
    # 调用，get 会刷新 last_seen 让判定失效。
    session_expired = await session_mgr.take_expired_notice(key)

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

    # 2. 生成答案：直接消费 bot.ask() 流式事件，边攒文本边按节流刷占位文案，
    # 让用户从"翻文档中"看到具体进度（已定位 redis/、正在查 troubleshooting.md…）
    result: AnswerResult | None = None
    answer = ""
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

            # 流式消费 + 节流占位更新。视觉路径不挂进度（图通常一两步就出答案，
            # 且占位文案是"识别图片中"语义更直观）；文字路径才挂。
            tracker = (
                ProgressTracker(session_mgr.docs_root) if not images else None
            )
            text_chunks: list[str] = []
            done_meta: dict[str, Any] = {}
            last_update_at = time.time()
            update_count = 0
            async for event in entry.bot.ask(question, images=images):
                etype = event.get("type")
                if etype == "tool":
                    if (
                        tracker is not None
                        and placeholder_mid is not None
                        and update_count < _PROGRESS_MAX_UPDATES
                    ):
                        decision = tracker.consume(
                            event.get("name", ""), event.get("input") or {}
                        )
                        if decision is not None:
                            new_text, force = decision
                            now = time.time()
                            if force or (
                                now - last_update_at >= _PROGRESS_MIN_INTERVAL
                            ):
                                ok = await feishu.update_post(
                                    placeholder_mid,
                                    _mention_post(user_id, new_text),
                                )
                                if ok:
                                    last_update_at = now
                                    update_count += 1
                elif etype == "text":
                    text_chunks.append(event.get("text", ""))
                elif etype == "done":
                    done_meta = {
                        "cost_usd": event.get("cost_usd"),
                        "usage": event.get("usage"),
                        "num_turns": event.get("num_turns"),
                        "duration_ms": event.get("duration_ms"),
                        "duration_api_ms": event.get("duration_api_ms"),
                    }
            result = AnswerResult(text="".join(text_chunks).strip(), **done_meta)
            entry.last_used = time.time()
        answer = result.text
    except Exception as e:
        logger.exception("answer failed: chat=%s user=%s", chat_id, user_id)
        answer = _friendly_error(e, context="问答", suggest_reset=True)
    answer = answer or "（无回答内容）"

    # 解析"找不到 → @ 负责人"标记。owner 为 None 表示不 @
    # escalate_dir_hint 是 LLM 直接给的归档目录（基于答案命中的组件，准确性高于
    # 按 owner 反查；同一负责人挂多组件时只有 LLM 自己知道这次答的是哪个组件）。
    answer, escalate_owner, escalate_dir_hint = _parse_escalate(answer)
    # 解析快捷追问标记，挂在反馈卡上面让用户一键发起新一轮
    answer, followup_keys = _parse_followups(answer)
    # 解析反问标记：是否为"信息不足、需要用户补充"的反问轮
    answer, is_clarification = _parse_clarify(answer)
    # 反问轮防御性清空：prompt 已要求反问时不输出 ESCALATE/FOLLOWUPS/IMG，但 LLM
    # 偶尔会不严格遵守。强制清掉，避免反问轮还 @ 负责人 / 挂追问按钮把用户搞糊涂。
    if is_clarification:
        escalate_owner = None
        escalate_dir_hint = None
        followup_keys = []
        # 反问轮里 LLM 不应该塞图，但如果塞了直接剥掉标记不上传
        answer = _IMG_RE.sub("", answer).strip()
        attached_images: list[str] = []
        truncated_images: list[str] = []
    else:
        # 解析答案内嵌图：校验路径 → 上传飞书 → 把 <<IMG:path>> 换成 <<IMG_KEY:img_xxx>>
        # 失败/超 3 张静默剥除（带 warning 日志），不阻塞答题主链路
        answer, attached_images, truncated_images = await _resolve_image_markers(
            answer, session_mgr.docs_root, feishu
        )
        # 超 cap 截断时在答案末尾告知用户少了几张、是哪些（取 basename，不暴露
        # 完整文档路径）。用 set 去重 basename 在不同目录重名的极端情况；列举
        # 5 张内全列，更多只列前 5 张 + 省略号，避免提示行本身刷屏。
        if truncated_images:
            names_unique: list[str] = []
            seen_names: set[str] = set()
            for p in truncated_images:
                base = p.rsplit("/", 1)[-1]
                if base not in seen_names:
                    seen_names.add(base)
                    names_unique.append(base)
            shown = "、".join(names_unique[:5])
            if len(names_unique) > 5:
                shown += "…"
            answer = (
                answer.rstrip()
                + f"\n\n（另有 {len(truncated_images)} 张图未展示：{shown}）"
            )
    # 上一轮上下文已过期时在答案最前面加一行提示，让用户立刻知道"那句『接着
    # 上面的』bot 没拿到上下文，本次按全新问题答的"。注入放在嵌图标记解析之后、
    # _mention_post 之前，提示成为飞书 post 的第一段，最显眼。
    if session_expired:
        idle_minutes = max(1, int(session_mgr.idle_ttl // 60))
        answer = (
            f"⏱️ 上一轮上下文已过期（{idle_minutes} 分钟未活跃自动重置），"
            "本次按新问题处理。\n\n"
            + answer
        )
    final_post = _mention_post(user_id, answer)
    escalated_now = False
    # component_dir / archive_path_repr 在 cooldown 不命中时需要喂给 _append_escalate_at
    # （让 asker 知道答案会归档到哪），escalated_now=True 时还要复用给后面的归档表单卡，
    # 保证两边路径完全一致。cooldown 命中走不到，留默认值即可。
    component_dir: str | None = None
    archive_path_repr = "qa-archive.md"
    if escalate_owner is not None:
        cooldown_key = (chat_id, escalate_owner)
        if cooldown_key in _escalate_cooldown:
            logger.info(
                "escalate cooldown hit: chat=%s owner=%s, suppress @",
                chat_id,
                escalate_owner,
            )
        else:
            # 优先用 LLM 给的 component_dir hint（与读文档时的判断一致）；hint 无效或
            # 缺失时按 owner 反查 INDEX，**且仅在 owner 唯一对应一个目录时才 fallback**
            # ——多对一情况下反查会猜错（同一负责人挂多个组件，只能押一个），不如直接
            # 落公共 docs_root/qa-archive.md 让人事后挪，比静默落到错的组件目录强。
            component_dir = _resolve_component_dir(
                escalate_dir_hint, session_mgr.docs_root
            )
            if not component_dir:
                owner_dirs = _index_owner_to_dirs(session_mgr.docs_root).get(
                    escalate_owner, []
                )
                if len(owner_dirs) == 1:
                    component_dir = owner_dirs[0]
                elif len(owner_dirs) > 1:
                    logger.info(
                        "owner has multiple dirs, falling back to public archive: "
                        "owner=%s dirs=%s",
                        escalate_owner,
                        owner_dirs,
                    )
            archive_path_repr = (
                f"{component_dir}/qa-archive.md" if component_dir else "qa-archive.md"
            )

            _escalate_cooldown[cooldown_key] = True
            _append_escalate_at(final_post, escalate_owner, archive_path_repr)
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
    if attached_images:
        # 用相对路径而不是 image_key，事后能直接定位到具体哪些图被引用
        qa_record["images_attached"] = attached_images
    if truncated_images:
        # 监控 cap=3 命中频率：grep `images_truncated` 看 LLM 多频繁超量、
        # 是不是该把上限从 3 调高
        qa_record["images_truncated"] = truncated_images
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

    # 6. 归档表单卡：仅在本次实际 @ 了负责人时发（cooldown 命中或 none 跳过）。
    # component_dir / archive_path_repr 已在 _append_escalate_at 之前算好，这里直接复用，
    # 与答案末尾告知 asker 的归档路径保持一致。
    if escalated_now:
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

    非提问者点击会被拒绝（群里反馈卡保持开放，否则任何人都能给打分污染 rating）。
    返回原反馈卡保持按钮可用，让真正的 asker 仍能投票。
    """
    if clicker_id and asker_id and clicker_id != asker_id:
        feedback_logger.info(
            json.dumps(
                {
                    "event": "feedback_rejected",
                    "qid": qid,
                    "rating": rating,
                    "clicker_id": clicker_id,
                    "asker_id": asker_id,
                },
                ensure_ascii=False,
            )
        )
        logger.info(
            "feedback rejected (not asker): qid=%s rating=%s by=%s asker=%s",
            qid,
            rating,
            clicker_id,
            asker_id,
        )
        return _feedback_card(qid, asker_id)
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

    非提问者提交会被拒绝并返回原表单卡，避免污染 reason 数据 / 把 asker 的表单顶掉。
    """
    if clicker_id and asker_id and clicker_id != asker_id:
        feedback_logger.info(
            json.dumps(
                {
                    "event": "feedback_reason_rejected",
                    "qid": qid,
                    "clicker_id": clicker_id,
                    "asker_id": asker_id,
                },
                ensure_ascii=False,
            )
        )
        logger.info(
            "feedback reason rejected (not asker): qid=%s by=%s asker=%s",
            qid,
            clicker_id,
            asker_id,
        )
        return _feedback_reason_form_card(qid, asker_id)
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

    非提问者跳过会被拒绝并返回原表单卡，避免污染 skipped 计数 / 把 asker 的表单顶掉。
    """
    if clicker_id and asker_id and clicker_id != asker_id:
        feedback_logger.info(
            json.dumps(
                {
                    "event": "feedback_reason_rejected",
                    "qid": qid,
                    "clicker_id": clicker_id,
                    "asker_id": asker_id,
                },
                ensure_ascii=False,
            )
        )
        logger.info(
            "feedback reason skip rejected (not asker): qid=%s by=%s asker=%s",
            qid,
            clicker_id,
            asker_id,
        )
        return _feedback_reason_form_card(qid, asker_id)
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
