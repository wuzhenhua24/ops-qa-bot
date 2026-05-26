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
from .config import DocQAConfig
from .doc_qa import FULL_TOOL_NAME, parse_feishu_registry
from .doc_qa import _norm_key as _feishu_norm_key
from .feishu_format import markdown_to_feishu_post

logger = logging.getLogger("ops_qa_bot.feishu")
# 由 logging_config.setup_feedback_logger 配置专用 handler 写 logs/feedback.log
feedback_logger = logging.getLogger("ops_qa_bot.feedback")

FEISHU_BASE = "https://open.feishu.cn/open-apis"
POST_TITLE = "测试环境助手"
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
# post 消息一次最多读取的图片张数。飞书移动端"@bot + 多张截图"会打成 post，
# 单条问题塞多图既贵也违反 LLM 视觉上下文人体工学，5 张是经验上限——超出截断
# 走日志告警，不报给用户（用户不会期望"我发了 8 张但 bot 只答了 5 张"这种细节）。
_POST_MAX_IMAGES = 5


def _extract_image_caption(content_dict: dict) -> str | None:
    """从 image 消息 content 里抽 caption-like 字段。

    飞书标准 image 消息 schema 只有 `{"image_key": "..."}`，没有 caption 字段；
    但转发 / 富文本中转 / 部分第三方客户端会把说明文字塞到 caption / text /
    description 之一。全部尝试，第一个非空字符串就用，都没有返回 None。
    防御性读取——多数场景这是 no-op，少数场景能多兜住一段用户语义。
    """
    for k in ("caption", "text", "description"):
        v = content_dict.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _parse_post_content(content_dict: dict) -> tuple[str, list[str]]:
    """从 post 消息 content 抽出 (text, image_keys)。

    post 是飞书富文本：`content` 是 list[list[element]]，外层是段落，内层是
    段内元素（text / at / img / a / code / emotion ...）。这里只关心：

    - tag:text → 文本拼接
    - tag:a    → 取链接显示文本（href 不暴露给 LLM，简化注入面）
    - tag:img  → 收集 image_key（按出现顺序保序）
    - tag:at   → **整段跳过**：@ 是路由标记不是用户输入；多数场景就是 @bot 自己

    其它 tag（emotion / hr / code_inline 等）暂不抽取——常见场景里这些不是
    问题主语；将来发现需要再补。

    段内拼接不加分隔符（at 被剥后留下的空白由 strip 收掉），段间用 \\n。
    """
    paragraphs = content_dict.get("content") or []
    if not isinstance(paragraphs, list):
        return "", []
    text_lines: list[str] = []
    image_keys: list[str] = []
    for para in paragraphs:
        if not isinstance(para, list):
            continue
        line_parts: list[str] = []
        for el in para:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag == "text":
                t = el.get("text")
                if isinstance(t, str):
                    line_parts.append(t)
            elif tag == "a":
                t = el.get("text")
                if isinstance(t, str):
                    line_parts.append(t)
            elif tag == "img":
                k = el.get("image_key")
                if isinstance(k, str) and k:
                    image_keys.append(k)
            # at / emotion / hr / code_inline / code 等暂不抽取
        line = "".join(line_parts).strip()
        if line:
            text_lines.append(line)
    return "\n".join(text_lines).strip(), image_keys


_AT_ALL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])@_all(?![A-Za-z0-9_])")


def is_at_all_broadcast(
    mentions: list | None, text: str | None = None
) -> bool:
    """识别 @所有人 全员通知。

    群里有人 @所有人 会把 bot 也唤醒（飞书 @_all mention 所有人，含 bot），
    全员通知不应该触发答题。先看 mentions 结构有没有 @_all 项；实测 WS SDK
    有时不把 @_all 放进 mentions、只在 message content 文本里留 `@_all` 字面，
    需要 text 兜底（带词边界防止误伤 `@_allowed` 之类正常 token）。
    mentions 兼容 SDK 对象（WS，带 .key / .id.open_id）和 dict（HTTP webhook）。
    """
    for m in mentions or []:
        if m is None:
            continue
        key = getattr(m, "key", None)
        if key is None and isinstance(m, dict):
            key = m.get("key")
        if key == "@_all":
            return True
        mid = getattr(m, "id", None)
        if mid is None and isinstance(m, dict):
            mid = m.get("id")
        if mid is not None:
            open_id = getattr(mid, "open_id", None)
            if open_id is None and isinstance(mid, dict):
                open_id = mid.get("open_id")
            if open_id == "all":
                return True
    if text and _AT_ALL_TOKEN_RE.search(text):
        return True
    return False


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


# 简单启发：问题里含 IPv4 地址多半是诊断类（"看下 172.28.4.40 内存"），
# 用"🔧 诊断中"作为初始占位文案比"🔍 翻文档中"贴合实际走向。
# 误判代价低（占位文案不准，最终答案不受影响），不做更复杂的语义判断。
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

# 操作/工单请求关键词：用户让 bot/管理员代为执行变更（加权限/开账号/申请资源/
# 开通访问）。prompt 侧要求 agent **先读文档**判断是自助流程还是手动分配，所以
# 占位文案就用"🔍 翻文档中"——比"🔧 诊断中"准（agent 不会 ssh）、也比"📨
# 转交负责人中"诚实（最后不一定 @）。这里识别只为压制 IP 启发，避免"帮我加
# 172.x 权限"被误标成诊断中。
# 要求带"帮我/帮/麻烦/给我/申请/开通"这种委托语义的前缀——单看"加/开"会和
# "怎么加节点"这种知识句撞，宁可漏判（fallback 到默认占位）也不要把知识问题
# 误标成工单。
_OP_REQUEST_RE = re.compile(
    r"帮(我|忙)?(加|开|改|配|搞|弄|执行|批准|开通)|"
    r"麻烦(帮|加|开|改|配|执行|批准|开通)|"
    r"给我(加|开|改|配)|"
    # 申请 / 开通 之后允许夹 0-15 个非分句字符再到目标名词，覆盖
    # "申请下 mysql 的只读账号"、"开通一下 redis 的访问权限" 这种带宾语前置的写法。
    r"申请[^\n。！？!?]{0,15}(权限|账号|账户|资源|白名单|访问)|"
    r"开通[^\n。！？!?]{0,15}(权限|账号|账户|访问|白名单)"
)


def _placeholder_text(question: str, queued: bool) -> str:
    """生成带问题摘要的占位文本，让用户能区分多条并发问的占位。

    queued=True 表示当前 session 锁被前一条问题占着，本条还没开始跑，前缀用
    🕒 排队中；获取到锁开始跑时上层会再 update 一次置成 🔍 翻文档中 / 🔧 诊断中。

    含 IP 默认走"🔧 诊断中"；但如果同时命中操作/工单请求关键词（"帮我加权限"），
    强制切回"🔍 翻文档中"——这种 agent 会先读文档判断自助/手动，不会 ssh，
    贴"诊断中"会误导用户以为 bot 在 ssh 跑命令。
    """
    excerpt = question.strip().replace("\n", " ")
    if len(excerpt) > 40:
        excerpt = excerpt[:40] + "…"
    if queued:
        icon = "🕒 排队中"
    elif _IP_RE.search(question) and not _OP_REQUEST_RE.search(question):
        icon = "🔧 诊断中"
    else:
        icon = "🔍 翻文档中"
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

        if tool_name == "Bash":
            # Bash 诊断（ssh 远端跑只读命令）。从命令字符串里抽 IP + 内层
            # 命令片段做展示，让用户看到"在 X 机器上跑 Y"的进度感。
            # 嵌套 ssh 写法形如：ssh jumphost "ssh 172.28.4.40 'free -h'"
            # 取最后一个 IPv4 作为 target（跳过 jumphost），单引号里第一段作为命令。
            command = str(tool_input.get("command", ""))
            ips = _IP_RE.findall(command)
            target = ips[-1] if ips else "目标机"
            inner_m = re.search(r"'([^']+)'", command)
            inner = inner_m.group(1).strip() if inner_m else ""
            if len(inner) > 30:
                inner = inner[:30] + "…"
            text = (
                f"🔧 在 {target} 上跑 `{inner}`…" if inner
                else f"🔧 在 {target} 上诊断…"
            )
            # 跨阶段第一次 / 多次 Bash（agent 连跑多条命令）都展示，
            # force=True 让用户立刻看到每条诊断在哪台机器跑什么。
            phase_change = self._last_phase != "bash"
            self._last_phase = "bash"
            self._last_dir = None
            self._read_count_in_dir = 0
            return text, phase_change

        if tool_name == FULL_TOOL_NAME:
            # 飞书文档问答工具：单步就出结果，跨阶段第一次 force 刷一次"查飞书
            # 文档中"，重复调用（多组件）不抖动。component 入参带上让用户看到查的是谁。
            comp = str(tool_input.get("component", "")).strip()
            text = (
                f"📄 查飞书文档中（{comp}）…" if comp else "📄 查飞书文档中…"
            )
            phase_change = self._last_phase != "docqa"
            self._last_phase = "docqa"
            self._last_dir = None
            self._read_count_in_dir = 0
            return text, phase_change

        return None

SessionKey = tuple[str, str]  # (chat_id, user_open_id)

# 升级机制：bot 答不上来时，按 prompt 输出 <<ESCALATE:ou_xxx:component_dir>>
# 标记，handle_question 拦截 → 移除标记 → 在 post 末尾注入 @owner 提醒。
# owner 接受 ou_xxx 或 none；后缀目录可选，由 LLM 基于"问题归属哪个组件"给出，
# 用于归档卡选目录。owner / dir 都做白名单校验防注入和路径穿越。
# 冒号 / who / dir 周围都容许 \s*——LLM 偶尔会写成 `<<ESCALATE: ou_xxx:redis>>`
# 或 `<<ESCALATE:ou_xxx : redis >>`，严格正则漏匹配 → marker 字面糊到答案末尾 +
# @ 不发。和 _FOLLOWUPS_RE 修复一同思路：宽松匹配，下游已有 strip 兜底。
_ESCALATE_RE = re.compile(
    r"<<ESCALATE:\s*(?P<who>ou_[A-Za-z0-9_-]+|none)\s*"
    r"(?::\s*(?P<dir>[A-Za-z0-9._/-]+)\s*)?>>"
)
# 工单类升级：用户让人代为执行变更（加权限/开账号），不是知识 Q&A。bot 只 @
# 负责人、不发归档表单卡（没什么可归档的"答案"）。和 _ESCALATE_RE 互斥，两者同时
# 出现时 ticket 优先。不带 dir：工单不归档，dir 没意义。
_ESCALATE_TICKET_RE = re.compile(
    r"<<ESCALATE_TICKET:\s*(?P<who>ou_[A-Za-z0-9_-]+|none)\s*>>"
)
_OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9_-]+$")
# 同一 (chat, owner) 30 分钟内只 @ 一次，防止用户连环问把负责人刷烦
_escalate_cooldown: TTLCache = TTLCache(maxsize=10000, ttl=1800)

# Escalate drift 兜底：LLM 答出"文档中未找到 / 找不到相关内容"但忘了输出 ESCALATE
# marker。这种 asker 看到答案是"找不到 + 空气"，没人接手——属于 LLM 概率漂移
# （[[project-escalate-trigger-probabilistic]]）。命中时给答案追加一句 asker-facing
# 提示让他有 next step（"在群里 @ 负责人"），并在日志里打 warning + qa 事件加
# escalate_drift_fallback=True 字段，方便事后 grep 漂移频率。
# 不强行猜 owner @：我们没有可靠依据从问题文本推断组件，乱 @ 错人比不 @ 更糟。
_NOT_FOUND_RE = re.compile(
    r"文档(中|里)?(未|没)(找到|有)|"
    r"未找到相关|"
    r"找不到相关"
)

# 正文 @ owner 渲染：LLM 在答案正文里写 `<@ou_xxx>` 字面想表达"候选负责人"（典型
# 场景：drift 兜底时不确定主 owner 是谁，列两三个候选让用户挑）。markdown 解析
# 不认这个语法，会原样渲染成丑陋的尖括号文本。bot 端识别该 pattern → 对照 INDEX.md
# 注册白名单 → 在册的转 feishu <at> tag（用户能点 ping），不在册的（LLM 幻觉编出
# 来的 open_id）静默剥除。容忍空格防 LLM 输出 `< @ou_xxx >` 等变种。
_AT_OWNER_RE = re.compile(r"<\s*@\s*(ou_[A-Za-z0-9_-]+)\s*>")


def _is_escalate_drift(
    raw_answer: str,
    stripped_answer: str,
    escalate_owner: str | None,
    is_clarification: bool,
) -> bool:
    """Escalate marker drift 判定（纯函数，便于单测）。

    True 表示 LLM 说"找不到"但忘了输出 ESCALATE/CLARIFY marker，需要兜底。
    raw_answer 是 LLM 原文（剥 marker 之前），stripped_answer 是剥完 marker
    的答案。escalate_owner 来自 ticket/普通 ESCALATE parser 合并后的结果。

    四个条件全部满足才算 drift：
    1. 非反问轮——反问轮 LLM 本来就不该输出 ESCALATE，不算漂移；
    2. 没识别到任何 owner——已经 @ 上人就不需要兜底；
    3. raw_answer 不含 <<ESCALATE 也不含 <<CLARIFY——LLM 自己 mark 过（哪怕
       是 ESCALATE:none 表示"找不到但也不知道找谁"），按其意图办，不覆盖；
    4. stripped_answer 文本命中"文档中未找到/找不到相关"句式——LLM 确实表达
       了找不到，而不是答了别的东西。
    """
    if is_clarification:
        return False
    if escalate_owner is not None:
        return False
    if "<<ESCALATE" in raw_answer or "<<CLARIFY" in raw_answer:
        return False
    return bool(_NOT_FOUND_RE.search(stripped_answer))

# 快捷追问机制：bot 答完后按 prompt 输出 <<FOLLOWUPS:k1|k2|k3>> 标记，
# handle_question 解析 → 在反馈卡上面挂对应按钮。点击 → 用预设 prompt
# 触发新一轮 handle_question，把用户自然带进下一轮。
# key 必须出自 _FOLLOWUP_LIBRARY；最多 3 个；不在白名单的 key 静默过滤。
# 正则宽松（含空格 / 大小写 / 数字 / 横杠都先收下），合法性靠 _FOLLOWUP_LIBRARY
# 白名单过滤 + _parse_followups 里逐 key strip。**必须容忍空格**：实测 LLM 偶尔
# 写成 `<<FOLLOWUPS: troubleshoot|commands|related>>`（冒号后多个空格）或
# `<<FOLLOWUPS:troubleshoot | commands | related>>`（竖线两侧空格），严格正则
# 会漏匹配 → marker 字面糊到答案末尾 + 追问卡不发。和 _ARCHIVE_Q_RE / _IMG_RE
# 一致用 `[^<>\n\r]+?`（允许除尖括号和换行外的任意字符）。
_FOLLOWUPS_RE = re.compile(r"<<FOLLOWUPS:([^<>\n\r]+?)>>")

# 反问标记：LLM 检测到信息不足以准确答时输出 <<CLARIFY>>，把答案当成反问轮处理。
# 反问轮：不发反馈卡 / 追问按钮 / 升级 @ / 归档卡，让用户专注回答反问；
# 用户在同一 session 里答完，下一轮就按补充信息直接答。
# 内外都容 \s*：LLM 偶尔写成 `<<CLARIFY >>` / `<< CLARIFY>>`，严格正则漏匹配
# → 反问轮被当成普通答完，会挂反馈卡 + 追问卡，用户体验错乱。
_CLARIFY_RE = re.compile(r"<<\s*CLARIFY\s*>>")

# 反问"我也说不清"出口：用户没法回填版本/环境等关键差异时点这个按钮，触发新一轮
# handle_question 喂这段 preset prompt——告诉 LLM 用户无法提供更多信息，按最常见
# 场景假设直接答 + 在答案首行用 ⚠️ 假设 列出关键假设。session 上下文已经带着原始
# 问题 + 反问内容（bot.ask 走 SDK 对话历史），LLM 自然衔接，不需要回灌问题文本。
_CLARIFY_GIVEUP_PROMPT = (
    "我没法提供更细的信息（不知道版本 / 环境 / 具体配置等）。"
    "请按你认为最常见的运维场景假设直接答这次的问题，"
    '并在答案最前面用一行 "⚠️ 假设：xxx；如有不同请告知" 列出你做的关键假设。'
)

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

# 归档问题标题草稿：升级到负责人时，答题那轮 LLM 顺带在答案里输出
# <<ARCHIVE_Q:归一化后的问题标题>>，bot 剥掉标记、把内容当成归档表单里那个
# 可编辑的"问题"输入框的默认值——用户原话往往口语化、带个人上下文，LLM 的
# 归一化标题做归档标题/检索关键词更合适，负责人提交前还能再改。marker 缺失
# 或内容为空时自动回退到用户原话。路径正则放宽到非控制字符，长度/空白净化
# 在 _parse_archive_q 里做；标记只在 <<ESCALATE:ou_xxx>> 那一支要求输出
# （<<ESCALATE:none>> / 无升级都不发归档表单，吐了也没用）。
_ARCHIVE_Q_RE = re.compile(r"<<ARCHIVE_Q:([^<>\n\r]+?)>>")
_ARCHIVE_Q_MAX_LEN = 80  # 字符；超长截断而不是丢弃，负责人可在表单里改
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
# 负责人在群里答完后填写卡片提交，内容写入 docs/<component>/qa-archive.md，
# 同时在原 chat @ 提问者把答案推回去（闭环交付）。
# qid → {chat_id, asker_id, question, question_default, owner_id, component_dir, parent_msg_id}。
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

    def __init__(
        self,
        docs_root: Path,
        idle_ttl: float = 1800.0,
        doc_qa_config: "DocQAConfig | None" = None,
    ):
        self._docs_root = docs_root
        self._idle_ttl = idle_ttl
        self._doc_qa_config = doc_qa_config
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
                bot = OpsQABot(
                    docs_root=self._docs_root,
                    doc_qa_config=self._doc_qa_config,
                )
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


def _rewrite_owner_at_mentions(
    post: dict, registered_owners: set[str]
) -> tuple[int, int]:
    """把答案正文里 LLM 写的 `<@ou_xxx>` 字面文字转成飞书 <at> tag，对照白名单过滤。

    遍历每段每段 `tag=text` 的文字找 _AT_OWNER_RE 命中点。对每个命中：
    - open_id 在 `registered_owners` 里 → 拆 text 段，把 `<@ou_xxx>` 那截换成
      `{tag: at, user_id: ou_xxx}` 段，飞书会渲染成 @ + 推送
    - 不在白名单 → 直接删，**不保留字面**（不在册多半是 LLM 幻觉编的 open_id，
      留着是丑陋字符，又怕 LLM 蒙对了真的 @ 错人）

    返回 `(rendered, dropped)`：分别是渲染成 @ 段的数量、白名单外被丢的数量；
    调用方用来打日志 + 写 qa_record 监控漂移频率。已 tag=at 的段（如答案首段的
    asker @ / 末尾 `_append_escalate_at` 加的 owner @）天然跳过。保留原 text 段
    上的 style（bold/italic 等），拆分后每个文本片段都继承。
    """
    rendered = 0
    dropped = 0
    content = post.get("zh_cn", {}).get("content", [])
    for i, paragraph in enumerate(content):
        new_para: list[dict] = []
        for seg in paragraph:
            if seg.get("tag") != "text":
                new_para.append(seg)
                continue
            text = seg.get("text", "")
            matches = list(_AT_OWNER_RE.finditer(text))
            if not matches:
                new_para.append(seg)
                continue
            style = seg.get("style")
            last = 0
            for m in matches:
                if m.start() > last:
                    chunk: dict = {"tag": "text", "text": text[last:m.start()]}
                    if style:
                        chunk["style"] = style
                    new_para.append(chunk)
                open_id = m.group(1)
                if open_id in registered_owners:
                    new_para.append({"tag": "at", "user_id": open_id})
                    rendered += 1
                else:
                    dropped += 1
                last = m.end()
            if last < len(text):
                chunk = {"tag": "text", "text": text[last:]}
                if style:
                    chunk["style"] = style
                new_para.append(chunk)
        # 段被替换成空（全是被丢的 @ + 没文字）时塞个空 text 段，避免后续渲染崩
        content[i] = new_para if new_para else [{"tag": "text", "text": ""}]
    return rendered, dropped


async def handle_image_question(
    chat_id: str,
    user_id: str,
    image_key: str,
    parent_msg_id: str | None,
    feishu: "FeishuClient",
    session_mgr: "SessionManager",
    *,
    caption: str | None = None,
) -> None:
    """处理 image 类型消息：下载 → 视觉答题。

    任何前置失败（下载报错 / 超大 / 内容为空）都用普通 post 回友好提示，不走
    答题流程，避免把 LLM 报错原文甩给用户。下载成功后用 DEFAULT_IMAGE_PROMPT
    作为引导问题，调用 `handle_question(images=...)` 复用占位 / 反馈卡 / 追问
    等所有现有逻辑。

    `caption` 由 server 层 `_extract_image_caption` 防御性抽出来：标准 image 消息
    没这个字段，但转发 / 富文本 / 第三方客户端偶有；非空时把它当成用户真正问的
    内容，DEFAULT_IMAGE_PROMPT 退化成"先识别再答"的格式说明。
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
        "image question: chat=%s user=%s key=%s size=%dB type=%s caption_len=%d",
        chat_id,
        user_id,
        image_key,
        len(img_bytes),
        media_type,
        len(caption or ""),
    )
    if caption:
        question = (
            f"{caption}\n\n"
            "（同时附了一张截图。先从图里识别关键信息（报错 / 命令 / 指标 / 配置等），"
            "再结合上面的问题去文档里查解决办法。）"
        )
    else:
        question = DEFAULT_IMAGE_PROMPT
    await handle_question(
        chat_id,
        user_id,
        question,
        feishu,
        session_mgr,
        parent_msg_id=parent_msg_id,
        images=[(media_type, img_bytes)],
    )


async def handle_post_question(
    chat_id: str,
    user_id: str,
    text: str,
    image_keys: list[str],
    parent_msg_id: str | None,
    feishu: "FeishuClient",
    session_mgr: "SessionManager",
) -> None:
    """处理 post 类型消息（@bot + 文字 + 截图 的常见组合）。

    串行下载所有 image_key（最多 _POST_MAX_IMAGES 张），过滤超大 / 空 / 失败的，
    剩下的喂给 handle_question 走视觉答题。任何一张图下载失败只丢那一张，不阻塞
    主链路（文字 + 其它图照样能答）。

    text + 图都为空（用户只发了 sticker / 表情 / 链接等暂不抽取的元素）兜底回
    unsupported hint，避免 bot 静默无反应。
    """
    if not parent_msg_id:
        # 资源 API 必须 message_id + image_key，没 parent 拿不到图；纯文字仍可继续
        if image_keys:
            logger.warning(
                "post without parent_msg_id, drop images: chat=%s user=%s n=%d",
                chat_id,
                user_id,
                len(image_keys),
            )
        image_keys = []

    truncated = max(0, len(image_keys) - _POST_MAX_IMAGES)
    image_keys = image_keys[:_POST_MAX_IMAGES]

    images: list[tuple[str, bytes]] = []
    failed = 0
    for key in image_keys:
        try:
            blob, media_type = await feishu.download_message_resource(
                parent_msg_id, key, "image"
            )
        except Exception:
            logger.exception(
                "post image download failed: chat=%s user=%s key=%s",
                chat_id,
                user_id,
                key,
            )
            failed += 1
            continue
        if not blob:
            logger.warning("post image empty: chat=%s key=%s", chat_id, key)
            failed += 1
            continue
        if len(blob) > MAX_IMAGE_BYTES:
            logger.info(
                "post image too large, skip: chat=%s key=%s size=%dB",
                chat_id,
                key,
                len(blob),
            )
            failed += 1
            continue
        images.append((media_type, blob))

    text = text.strip()
    if not text and not images:
        # 啥可用内容都没有：用 unsupported hint 让用户明白看到了但没法处理
        await handle_unsupported_message(
            chat_id, user_id, parent_msg_id, "post", feishu
        )
        return

    logger.info(
        "post question: chat=%s user=%s text_len=%d images=%d truncated=%d failed=%d",
        chat_id,
        user_id,
        len(text),
        len(images),
        truncated,
        failed,
    )

    if images:
        if text:
            question = (
                f"{text}\n\n"
                f"（同时附了 {len(images)} 张截图。先从图里识别关键信息（报错 / "
                "命令 / 指标 / 配置等），再结合上面的问题去文档里查解决办法。）"
            )
        else:
            question = DEFAULT_IMAGE_PROMPT
        await handle_question(
            chat_id,
            user_id,
            question,
            feishu,
            session_mgr,
            parent_msg_id=parent_msg_id,
            images=images,
        )
    else:
        # 只有文字（图全失败 / 用户原本就只发文字带格式）→ 走纯文本路径
        await handle_question(
            chat_id,
            user_id,
            text,
            feishu,
            session_mgr,
            parent_msg_id=parent_msg_id,
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
    每个 button.behaviors.callback.value 自带 chat_id 和 parent_msg_id：parent_msg_id
    是用户原始问题的 message_id，回调时透传给新一轮 `handle_question`，让追问的
    占位/答案/卡片继续引用回到原问题，线程感不断。

    v2 schema：与同模式的 `_feedback_card` / `_clarify_giveup_card` 对齐。原卡 v1 +
    替换卡 v2 的跨版本切换在飞书上不是官方推荐做法，统一到 v2 后未来 SDK / 飞书侧
    schema 校验收紧也不会突然炸。
    """
    columns: list[dict] = []
    for k in followup_keys:
        label, _ = _FOLLOWUP_LIBRARY[k]
        btn = {
            "tag": "button",
            "text": {"tag": "plain_text", "content": label},
            "type": "default",
            "behaviors": [
                {
                    "type": "callback",
                    "value": {
                        "action": "followup",
                        "qid": qid,
                        "key": k,
                        "asker_id": user_id,
                        "chat_id": chat_id,
                        "parent_msg_id": parent_msg_id,
                    },
                }
            ],
        }
        columns.append(
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [btn],
            }
        )
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {"tag": "markdown", "content": "**想再深入？**"},
                {"tag": "column_set", "columns": columns},
            ]
        },
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


def _clarify_giveup_card(
    qid: str,
    user_id: str,
    chat_id: str,
    parent_msg_id: str | None,
) -> dict:
    """反问轮卡片：单按钮 "🤷 说不清楚，按常见情况直接答"。

    用 v2 schema 单按钮（与反馈卡 / 反馈原因表单卡一致），点击触发 clarify_giveup
    回调，后台跑新一轮 handle_question 喂 _CLARIFY_GIVEUP_PROMPT。chat_id /
    parent_msg_id 透过 button value 带回，新一轮按原问题引用回复维持线程。
    asker-only 校验在 handle_clarify_giveup_click 里做。
    """
    btn = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "🤷 说不清楚，按常见情况直接答"},
        "type": "default",
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": "clarify_giveup",
                    "qid": qid,
                    "asker_id": user_id,
                    "chat_id": chat_id,
                    "parent_msg_id": parent_msg_id,
                },
            }
        ],
    }
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        "答不上来？点下面这颗按钮让 bot 按最常见情况假设直接答，"
                        "答案会标 ⚠️ 假设。"
                    ),
                },
                {"tag": "column_set", "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [btn],
                    }
                ]},
            ]
        },
    }


def _clarify_giveup_ack_card() -> dict:
    """点完"说不清"按钮替换原卡：v2 简单 ack。"""
    return {
        "schema": "2.0",
        "body": {
            "elements": [
                {
                    "tag": "markdown",
                    "content": "✅ 收到，按常见情况重新作答中…",
                }
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


async def handle_clarify_giveup_click(
    qid: str | None,
    chat_id: str | None,
    asker_id: str | None,
    clicker_id: str | None,
    feishu: "FeishuClient",
    session_mgr: "SessionManager",
    parent_msg_id: str | None = None,
) -> dict:
    """处理反问轮"🤷 说不清"按钮点击。后台触发新一轮 handle_question 喂
    `_CLARIFY_GIVEUP_PROMPT`，立即返回 ack 卡。

    与 handle_followup_click 同构：校验 chat_id+asker_id 都存在 / 点击者就是
    原提问者；后台 asyncio.create_task 跑新一轮。session 上下文带着原问题 +
    LLM 上一轮反问内容，新一轮 LLM 自然衔接按假设直接答。
    """
    if not chat_id or not asker_id:
        return _followup_error_card("参数缺失，请重试。")
    if clicker_id and clicker_id != asker_id:
        return _followup_error_card(
            f"只有 <at id={asker_id}></at> 能点这个按钮，要追问请重新发一条消息。"
        )
    asyncio.create_task(
        handle_question(
            chat_id,
            asker_id,
            _CLARIFY_GIVEUP_PROMPT,
            feishu,
            session_mgr,
            parent_msg_id=parent_msg_id,
        )
    )
    feedback_logger.info(
        json.dumps(
            {
                "event": "clarify_giveup",
                "qid": qid,
                "asker_id": asker_id,
                "clicker_id": clicker_id,
            },
            ensure_ascii=False,
        )
    )
    logger.info(
        "clarify giveup triggered: qid=%s chat=%s asker=%s",
        qid,
        chat_id,
        asker_id,
    )
    return _clarify_giveup_ack_card()


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

    multi_select 多选原因 + 多行 input 写备注（可选）+ 提交按钮，跳过按钮放 form 外
    （form 内放无 form_action_type 的纯 callback button 行为不明确，官方 demo 没这种
    写法）。submit 不挂 behaviors callback，仅靠 form_action_type:"submit" + button.value
    触发提交回调；事件里 action.value 带 payload，action.form_value 带字段值（多选返回
    数组）。qid / asker_id 透过按钮 value 带回，不依赖服务端状态。
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
                            "tag": "multi_select_static",
                            "name": "reasons",
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "可多选（如过时 + 不完整）",
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


def _parse_escalate_ticket(answer: str) -> tuple[str, str | None]:
    """抽 <<ESCALATE_TICKET:owner>> 标记 → (清理后的答案, owner_id or None)。

    工单类升级：@ 负责人但不发归档表单卡（"加权限"等操作请求不是知识 Q&A）。
    调用方负责把它和 _parse_escalate 的结果合并；两者同时出现时 ticket 优先。
    """
    m = _ESCALATE_TICKET_RE.search(answer)
    if not m:
        return answer, None
    cleaned = _ESCALATE_TICKET_RE.sub("", answer).strip()
    who = m.group("who")
    return cleaned, (who if who != "none" else None)


def _parse_archive_q(answer: str) -> tuple[str, str | None]:
    """抽 <<ARCHIVE_Q:...>> 标记，返回 (清理后的答案文本, 净化后的问题标题草稿 or None)。

    草稿净化：折叠内部空白/换行、去首尾空白；清理后为空则当没给（返回 None）；
    超 _ARCHIVE_Q_MAX_LEN 截断加省略号（不丢弃——它会成为归档表单问题框的
    默认值，负责人可再改）。多次出现取第一处，全部从答案里剥掉。
    """
    m = _ARCHIVE_Q_RE.search(answer)
    if not m:
        return answer, None
    cleaned = _ARCHIVE_Q_RE.sub("", answer).strip()
    draft = " ".join(m.group(1).split()).strip()
    if not draft:
        return cleaned, None
    if len(draft) > _ARCHIVE_Q_MAX_LEN:
        draft = draft[:_ARCHIVE_Q_MAX_LEN].rstrip() + "…"
    return cleaned, draft


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


def _append_escalate_at(
    post: dict,
    owner_id: str,
    archive_path: str,
    *,
    is_ticket: bool = False,
    is_feishu: bool = False,
) -> None:
    """在 post 末尾追加 "📣 已通知负责人 @xxx" 行（+ 普通升级时再加归档去向）。

    is_ticket=False（默认，"文档没答案"升级）：追加两行——@ + 归档去向告知。

    本地组件（is_feishu=False）：archive_path 是相对 docs_root 的路径（如
    "redis/qa-archive.md"），与紧随其后发出的归档表单卡一致。告诉 asker 答案最终
    会落到哪、下次类似问题 bot 能从哪里直接答，避免"通知完就没下文"的预期空白。

    飞书来源组件（is_feishu=True）：这类组件的知识维护在飞书文档里，bot 不会回读
    本地 qa-archive.md（见 [[project_feishu_doc_qa_integration]] 的归档回读缺口），
    所以**不能**承诺"下次我能直接从这里答"——那是假的。改成告诉 asker 答案会同步
    给他，并点明知识维护在飞书文档（第一信号源是负责人维护的飞书文档）。本地仍会
    静默写一份 archive 作留档 + 为将来万一要切"兼读本地"预热数据，但不对外宣称。

    is_ticket=True（工单类升级，"加权限/开账号"操作请求）：只 @ 不提归档；动词
    用"协助处理"而不是"协助回答"——工单 ≠ 知识答疑。archive_path 在 ticket 模式
    下被忽略，调用方可以传空串。
    """
    post["zh_cn"]["content"].append([{"tag": "text", "text": ""}])  # 空行隔开
    verb = "协助处理" if is_ticket else "协助回答"
    post["zh_cn"]["content"].append(
        [
            {"tag": "text", "text": "📣 已通知负责人 "},
            {"tag": "at", "user_id": owner_id},
            {"tag": "text", "text": f" {verb} 🙏"},
        ]
    )
    if not is_ticket:
        if is_feishu:
            line = (
                "📁 答案整理后会同步给你；这个组件的运维知识维护在飞书文档里，"
                "已请负责人补充进去。"
            )
        else:
            line = (
                f"📁 负责人填写后会归档到 {archive_path}，"
                "下次类似问题我能直接从这里答。"
            )
        post["zh_cn"]["content"].append([{"tag": "text", "text": line}])


def _feishu_reference_links(components: list[str], docs_root: Path) -> str | None:
    """本轮查过的飞书组件 → 末尾「📄 参考文档」可点链接 markdown 块（无则 None）。

    provenance 来自 query_feishu_doc 工具调用事件（ground truth），不依赖 LLM 在
    正文里写的"来源"。只链登记成 http(s) URL 的文档——纯 token 拼不出链接，静默跳过；
    一个组件多文档列多行；按组件名去重保序。

    措辞用"参考文档"而非"出处"：/doc_qa 返回的是跨文档综合答案，只能给到**组件级**
    入口，给不到逐行精确出处。块用 markdown 无序列表 + 链接语法，交给
    markdown_to_feishu_post 渲染成飞书可点 a 标签。
    """
    reg = parse_feishu_registry(docs_root)
    seen: set[str] = set()
    lines: list[str] = []
    for c in components:
        entry = reg.get(_feishu_norm_key(c))
        if entry is None or entry.name in seen:
            continue
        seen.add(entry.name)
        urls = [d for d in entry.docs if d.startswith(("http://", "https://"))]
        if not urls:
            continue
        if len(urls) == 1:
            lines.append(f"- [{entry.name} 飞书文档]({urls[0]})")
        else:
            for i, u in enumerate(urls, 1):
                lines.append(f"- [{entry.name} 飞书文档 {i}]({u})")
    if not lines:
        return None
    return "📄 参考文档：\n" + "\n".join(lines)


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
    qid: str,
    question_default: str,
    owner_id: str,
    archive_path_repr: str,
    *,
    is_feishu: bool = False,
) -> dict:
    """归档表单卡（card v2 form）：可编辑问题标题 + 多行答案输入框 + 提交按钮。

    question_default：预填进"问题"输入框的标题——优先是答题那轮 LLM 给的归一化
    标题，否则是用户原话。负责人可在框里改成更通用的说法再提交；最终写盘用框里的值。
    archive_path_repr：展示给 owner 的相对路径（如 "redis/qa-archive.md"），
    让他知道答案会落到哪个文件再决定写多详细。

    is_feishu=True（飞书来源组件）：bot 不回读本地 archive，所以引导文案不提"追加进
    xxx.md / 检索关键词"那套（会误导负责人以为填表单 bot 就学会了），改成"答案会
    同步给提问者 + 请维护飞书文档"。表单照常提交、本地照常静默留档（见
    [[project_feishu_doc_qa_integration]]）。
    """
    if is_feishu:
        intro = (
            f"<at id={owner_id}></at> 下面的「问题」是系统整理的，可改成更通用的"
            "说法；把整理过的答案填进答案框，提交后会同步给提问者。"
            "这个组件的运维知识维护在飞书文档，请把答案一并补充进去。"
        )
    else:
        intro = (
            f"<at id={owner_id}></at> 下面的「问题」是系统自动整理的，"
            "可改成更通用的说法（它会作为归档标题和以后的检索关键词）；"
            "把整理过的答案填进答案框，"
            f"提交后会追加进 `{archive_path_repr}`。"
        )
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
                    "content": intro,
                },
                {
                    "tag": "form",
                    "name": "archive_form",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "question",
                            "default_value": _excerpt(question_default, 100),
                            "max_length": 120,
                            "placeholder": {
                                "tag": "plain_text",
                                "content": "归档用的问题标题（可修订）…",
                            },
                            "required": True,
                        },
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


def _archive_answer_notify_post(
    asker_id: str,
    owner_id: str,
    question: str,
    answer_markdown: str,
    archive_rel: str,
    *,
    is_feishu: bool = False,
) -> dict:
    """构造"负责人答完 → 通知 asker"的 feishu post。

    asker_id 放在第一段以 @ 推送（asker 才会收到飞书侧消息提醒，不然写到归档
    文件里 asker 永远不知道有答案）；owner_id 内嵌作为"谁答的"标记。
    answer_markdown 走 markdown_to_feishu_post，保留答案原本的列表/代码块/换行
    结构。末尾补一行收尾——闭环交付。

    is_feishu=False：本地组件，告知归档路径 + 承诺"下次直接答"（agent 后续轮
    Read 本地 archive 能命中）。

    is_feishu=True：飞书来源组件，bot 不回读本地 archive（见
    [[project_feishu_doc_qa_integration]]），**不能**承诺"下次直接答"——那是
    假的。改成告诉 asker 答案已同步 + 请负责人补充进飞书文档。与
    `_append_escalate_at` / `_archive_form_card` 的 is_feishu 分支保持一致姿态。
    """
    post = markdown_to_feishu_post(answer_markdown, POST_TITLE)
    # 截短 question 防止特别长的标题撑爆头部一行；归档时已经做了 200 字上限但
    # 这里再保险一道（头部行越短越好读，详情看下面的答案 body）。
    q_short = question if len(question) <= 60 else question[:60].rstrip() + "…"
    intro_paragraph = [
        {"tag": "at", "user_id": asker_id},
        {"tag": "text", "text": f" 你之前问的「{q_short}」，"},
        {"tag": "at", "user_id": owner_id},
        {"tag": "text", "text": " 已答复 👇"},
    ]
    post["zh_cn"]["content"].insert(0, intro_paragraph)
    post["zh_cn"]["content"].append([{"tag": "text", "text": ""}])  # 空行隔开
    if is_feishu:
        tail = (
            "📁 答案已同步给你；这个组件的运维知识维护在飞书文档，"
            "已请负责人补充进去。"
        )
    else:
        tail = f"📁 已归档到 {archive_rel}，下次类似问题我能直接从这里答。"
    post["zh_cn"]["content"].append([{"tag": "text", "text": tail}])
    return post


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
        # 问题作为 `## ` 标题写一行：折叠掉换行/多余空白，否则多行会把 markdown
        # 标题撑坏（单行 input 一般进不来换行，这里兜底）
        question_line = " ".join(question.split()) or "（无标题）"
        block = (
            f"\n## Q: {question_line}\n\n"
            f"*{' · '.join(meta_parts)}*\n\n"
            f"{answer.strip()}\n\n"
            f"---\n"
        )
        with file_path.open("a", encoding="utf-8") as f:
            f.write(block)
        return True


def get_archive_expected_owner(qid: str | None) -> str | None:
    """callback 层用来 peek "这个 qid 的归档表单期望的负责人是谁"。

    返回 None 表示 qid 不存在 / 已过期 / 已处理——这种情况让正常 handler 走
    qid-missing / expired ack 路径，不需要 callback 提前介入。
    """
    if not qid:
        return None
    ctx = _pending_archives.get(qid)
    return ctx.get("owner_id") if ctx else None


async def handle_archive_submit(
    qid: str | None,
    question: str,
    answer: str,
    clicker_id: str | None,
    docs_root: Path,
    feishu: "FeishuClient | None" = None,
) -> dict:
    """处理归档表单提交。返回应替换原表单卡的 ack 卡片（card v2）。

    `question` 是表单里那个（预填、可编辑）问题框的值——负责人没改就是预填值，
    改了就是改后的；为空时按 question_default → 用户原话依次回退（旧的 pending
    项可能没 question_default，所以两层都兜）。

    多数失败路径（参数缺失、过期、空答案、写盘异常）都用 ack 卡告诉点击者，
    原卡片被替换避免重复提交困惑。**唯一例外是"非负责人点击"**：返回原表单卡保持
    可见，让真正的负责人还能填——否则其他人误点提交会把负责人的表单顶掉。

    `feishu` 传入时（生产环境总传），写入成功后会在原 chat 发一条"📣 负责人已答复"
    的 post @ 原 asker，把答案推送给提问者；不传或缺 asker_id 则只写文件不推送
    （单元测试场景）。这条通知是闭环的关键——否则 asker 永远不知道负责人答了。
    """
    if not qid:
        return _archive_ack_card("⚠️", "归档参数缺失，请联系管理员。")
    ctx = _pending_archives.get(qid)
    if ctx is None:
        return _archive_ack_card(
            "⏰", "归档会话已过期或已处理，请联系管理员手动补记。"
        )

    expected_owner = ctx["owner_id"]
    question_default = ctx.get("question_default") or ctx["question"]
    if clicker_id and clicker_id != expected_owner:
        # 重建原表单卡返回，保持负责人那张表单可见；同时记一条 archive_rejected
        # 日志便于事后 grep 看"非负责人误点"频率。
        feedback_logger.info(
            json.dumps(
                {
                    "event": "archive_rejected",
                    "qid": qid,
                    "clicker_id": clicker_id,
                    "expected_owner": expected_owner,
                },
                ensure_ascii=False,
            )
        )
        logger.info(
            "archive submit rejected (not owner): qid=%s by=%s expected=%s",
            qid,
            clicker_id,
            expected_owner,
        )
        component_dir = ctx.get("component_dir")
        archive_path_repr = (
            f"{component_dir}/qa-archive.md" if component_dir else "qa-archive.md"
        )
        return _archive_form_card(
            qid,
            question_default,
            expected_owner,
            archive_path_repr,
            is_feishu=bool(ctx.get("is_feishu")),
        )

    answer_text = (answer or "").strip()
    if not answer_text:
        return _archive_ack_card("⚠️", "答案不能为空，请填写后再提交。")
    if len(answer_text) > 10_000:
        return _archive_ack_card("⚠️", "答案过长（>10KB），请精简后再提交。")

    # 表单里那个"问题"框是 required，正常路径拿到的就是负责人确认/改过的值；
    # 为空只可能是 API 重放等异常路径，按 question_default → 原话兜底。折叠成一行
    # 并截断（标题最终写成 `## Q: ...`），不因为 fallback 是长串就报错拒掉。
    question_text = " ".join(((question or "").strip() or question_default).split())
    if not question_text:
        question_text = "（无标题）"
    if len(question_text) > 200:
        question_text = question_text[:200].rstrip() + "…"

    component_dir = ctx.get("component_dir")
    if component_dir:
        file_path = docs_root / component_dir / "qa-archive.md"
    else:
        file_path = docs_root / "qa-archive.md"

    try:
        wrote = await _write_qa_archive(
            file_path=file_path,
            qid=qid,
            question=question_text,
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

    # 通知 asker：写入成功 + 有 asker_id + 有 feishu client 时给原 chat 发一条
    # @ asker 的 post 把答案推过去。**仅在 wrote=True 时通知**——幂等命中说明同
    # qid 的答案已经存档过、asker 多半也通知过了，再发会成"垃圾消息"；写盘失败时
    # 自然不发。通知本身失败（API 出错 / asker 已不在群里）不回阻归档结果，
    # 文件已经写了、表单已经提交了，最坏情况 asker 重新问 bot 也能从归档命中。
    notify_sent = False
    if wrote and feishu is not None and ctx.get("asker_id"):
        try:
            notify_post = _archive_answer_notify_post(
                asker_id=ctx["asker_id"],
                owner_id=expected_owner,
                question=question_text,
                answer_markdown=answer_text,
                archive_rel=str(rel),
                is_feishu=bool(ctx.get("is_feishu")),
            )
            await feishu.send_post(
                ctx["chat_id"],
                notify_post,
                parent_id=ctx.get("parent_msg_id"),
            )
            notify_sent = True
        except Exception:
            logger.exception(
                "notify asker failed (archive still ok): qid=%s asker=%s",
                qid,
                ctx.get("asker_id"),
            )

    # had_draft：答题那轮 LLM 有没有给出（区别于原话的）归一化标题。
    # question_edited：负责人在表单里改没改预填值。两个一起看就知道 LLM 草稿
    # 命中率 + 负责人采纳率，用来判断 <<ARCHIVE_Q:>> 这套值不值 / 要不要再调 prompt。
    had_draft = question_default.strip() != ctx["question"].strip()
    question_edited = (
        " ".join(question_text.split()) != " ".join(question_default.split())
    )
    feedback_logger.info(
        json.dumps(
            {
                "event": "archive",
                "qid": qid,
                "owner_id": expected_owner,
                "asker_id": ctx.get("asker_id"),
                "path": str(rel),
                "answer_excerpt": _excerpt(answer_text, 500),
                "question_final": _excerpt(question_text, 200),
                "had_draft": had_draft,
                "question_edited": question_edited,
                "duplicate": not wrote,
                "notify_sent": notify_sent,
            },
            ensure_ascii=False,
        )
    )
    logger.info(
        "archive written: qid=%s path=%s duplicate=%s notify=%s",
        qid,
        rel,
        not wrote,
        notify_sent,
    )

    if wrote:
        suffix = "，已通知提问者" if notify_sent else ""
        return _archive_ack_card("✅", f"已归档至 `{rel}`{suffix}，谢谢！")
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
    # 本轮调过 query_feishu_doc 的飞书组件（按出现顺序，去重在 _feishu_reference_links
    # 里做）。在 try 外声明，异常路径也能安全引用（虽然那时一般是空）。
    queried_feishu_components: list[str] = []
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
                    if event.get("name") == FULL_TOOL_NAME:
                        comp = str(
                            (event.get("input") or {}).get("component", "")
                        ).strip()
                        if comp:
                            queried_feishu_components.append(comp)
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

    # raw_answer 留作 drift 检测：parsing 会把 marker 剥掉，事后只看 answer 没法
    # 区分"LLM 输出了 <<ESCALATE:none>>（marker 在但 owner=none）"和"LLM 啥 marker
    # 都没输出"。drift 兜底要的是后者，所以这里在剥之前先快照一份原文本。
    raw_answer = answer

    # 解析"找不到 → @ 负责人"标记。owner 为 None 表示不 @
    # escalate_dir_hint 是 LLM 直接给的归档目录（基于答案命中的组件，准确性高于
    # 按 owner 反查；同一负责人挂多组件时只有 LLM 自己知道这次答的是哪个组件）。
    # ticket marker 先剥（独立于普通 ESCALATE），命中后走"@ 但不发归档卡"分支。
    answer, escalate_ticket_owner = _parse_escalate_ticket(answer)
    answer, escalate_owner, escalate_dir_hint = _parse_escalate(answer)
    # 解析归档问题标题草稿（仅在升级时有意义；marker 缺失/为空则为 None，
    # 后面会回退到用户原话）
    answer, archive_q_draft = _parse_archive_q(answer)
    # 解析快捷追问标记，挂在反馈卡上面让用户一键发起新一轮
    answer, followup_keys = _parse_followups(answer)
    # 解析反问标记：是否为"信息不足、需要用户补充"的反问轮
    answer, is_clarification = _parse_clarify(answer)
    # 反问轮防御性清空：prompt 已要求反问时不输出 ESCALATE/FOLLOWUPS/IMG/ARCHIVE_Q，
    # 但 LLM 偶尔会不严格遵守。强制清掉，避免反问轮还 @ 负责人 / 挂追问按钮把用户搞糊涂。
    if is_clarification:
        escalate_owner = None
        escalate_ticket_owner = None
        escalate_dir_hint = None
        archive_q_draft = None
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
    # 工单 marker 优先：和普通 ESCALATE 同时出现（LLM 偶尔会瞎组合）时按工单处理，
    # 归档相关字段强制清空。is_ticket 在这里一次性锁定本次升级类型，后面"要不要发
    # 归档卡 / @ 行措辞用哪个动词"都查这个标志，避免散落多处条件。
    if escalate_ticket_owner is not None:
        escalate_owner = escalate_ticket_owner
        escalate_dir_hint = None
        archive_q_draft = None
    is_ticket = escalate_ticket_owner is not None

    # Escalate marker drift 兜底（[[project-escalate-trigger-probabilistic]]）：
    # LLM 答出"文档中未找到"但忘了输出 ESCALATE/CLARIFY marker。判定条件 +
    # 决策都在 _is_escalate_drift 里，本处只负责"命中后追加 asker-facing 提示
    # + 打 warning + qa_record 标记"。不强行猜 owner @——没可靠依据从问题文本
    # 推断组件，乱 @ 错人比不 @ 更糟。
    escalate_drift = _is_escalate_drift(
        raw_answer, answer, escalate_owner, is_clarification
    )
    if escalate_drift:
        logger.warning(
            "escalate drift: not-found answer with no marker, append fallback hint. "
            "chat=%s user=%s question=%r",
            chat_id,
            user_id,
            _excerpt(question, 200),
        )
        answer = (
            answer.rstrip()
            + "\n\n💡 文档没覆盖到这块；可以在群里 @ 对应组件负责人协助处理。"
        )

    # 飞书组件参考文档链接：本轮调过 query_feishu_doc 的组件，在答案末尾追加可点
    # 链接（provenance 来自工具事件，不依赖 LLM 写的来源）。反问轮跳过——那轮没有
    # 真正基于文档的答案，挂链接是噪音。放在答案文本里，随 _mention_post 一起渲染，
    # 位置在正文之后、post 级追加的"📣 已通知负责人"之前。
    if not is_clarification and queried_feishu_components:
        ref_block = _feishu_reference_links(
            queried_feishu_components, session_mgr.docs_root
        )
        if ref_block:
            answer = answer.rstrip() + "\n\n" + ref_block

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

    # 正文 @ owner 渲染：LLM 在答案正文里写 `<@ou_xxx>` 列候选负责人（drift 兜底
    # 时多见），markdown 渲染会原样输出尖括号文本。对照 INDEX.md 注册白名单转成
    # 飞书 <at> tag，幻觉编出来的 open_id 静默剥除。详见 _rewrite_owner_at_mentions。
    registered_owners = set(
        _index_owner_to_dirs(session_mgr.docs_root).keys()
    )
    at_rendered_in_body, at_dropped_in_body = _rewrite_owner_at_mentions(
        final_post, registered_owners
    )
    if at_dropped_in_body:
        # LLM 编 open_id 的频率高的话该回头看 prompt——可能"列候选负责人"那个
        # hedging 习惯需要更强引导（要么按 INDEX.md 实际名单写，要么别列）。
        logger.warning(
            "stripped %d unregistered <@ou_xxx> mention(s) from answer body: "
            "chat=%s user=%s",
            at_dropped_in_body,
            chat_id,
            user_id,
        )

    escalated_now = False
    # component_dir / archive_path_repr 在 cooldown 不命中时需要喂给 _append_escalate_at
    # （让 asker 知道答案会归档到哪），escalated_now=True 时还要复用给后面的归档表单卡，
    # 保证两边路径完全一致。cooldown 命中走不到，留默认值即可。
    component_dir: str | None = None
    archive_path_repr = "qa-archive.md"
    # 升级到的组件是否飞书来源：决定 @ 行 / 归档卡的措辞（飞书组件不承诺本地回读，
    # 改为引导负责人维护飞书文档）。在 component_dir 定下来后按注册表判定。
    is_feishu_component = False
    if escalate_owner is not None:
        cooldown_key = (chat_id, escalate_owner)
        if cooldown_key in _escalate_cooldown:
            logger.info(
                "escalate cooldown hit: chat=%s owner=%s kind=%s, suppress @",
                chat_id,
                escalate_owner,
                "ticket" if is_ticket else "qa",
            )
        elif is_ticket:
            # 工单类升级：只 @ 不算归档路径、不发归档卡。component_dir / archive_path_repr
            # 留默认值，下面归档卡的分支会因为 is_ticket 整体跳过。
            _escalate_cooldown[cooldown_key] = True
            _append_escalate_at(final_post, escalate_owner, "", is_ticket=True)
            escalated_now = True
            logger.info(
                "escalated to owner (ticket): chat=%s owner=%s",
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
            if component_dir:
                is_feishu_component = (
                    _feishu_norm_key(component_dir)
                    in parse_feishu_registry(session_mgr.docs_root)
                )

            _escalate_cooldown[cooldown_key] = True
            _append_escalate_at(
                final_post,
                escalate_owner,
                archive_path_repr,
                is_feishu=is_feishu_component,
            )
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
        qa_record["escalation_kind"] = "ticket" if is_ticket else "qa"
        if is_feishu_component:
            # 观察期抓手：grep `feishu_component` 统计飞书来源组件多频繁升级。
            # 升级率高 = 飞书文档没覆盖到 / 负责人没在维护，是决定要不要切"兼读
            # 本地 archive"的判据（[[project_feishu_doc_qa_integration]]）。
            qa_record["feishu_component"] = True
    if is_clarification:
        qa_record["clarification"] = True
    if escalate_drift:
        # grep 频率：jq 'select(.escalate_drift_fallback) | {qid, question}' feedback.log
        # 频率高就该回头调 prompt 强化"找不到必须输出 marker"那条
        qa_record["escalate_drift_fallback"] = True
    if at_rendered_in_body or at_dropped_in_body:
        # 正文 @ owner 渲染统计——rendered 是用户实际能 ping 到的人数，
        # dropped 是 LLM 编 open_id 被剥的次数，监控 LLM 输出靠谱程度
        qa_record["at_rendered_in_body"] = at_rendered_in_body
        qa_record["at_dropped_in_body"] = at_dropped_in_body
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
    # 反问轮跳过：让用户专注答反问，下一轮按补充信息再答 + 那时再挂反馈/追问。
    # 反问轮额外发一张"我说不清"出口卡：用户如果答不出反问的关键点（不知道版本/
    # 环境等），点一下让 bot 按假设直接答，免得对话卡死。
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
    else:
        await feishu.send_interactive(
            chat_id,
            _clarify_giveup_card(qid, user_id, chat_id, parent_msg_id),
            parent_id=parent_msg_id,
        )

    # 6. 归档表单卡：仅在本次实际 @ 了负责人**且不是工单类**时发——工单（加权限/
    # 开账号）没什么"答案"可归档，发卡只会让负责人多一步无意义点击。cooldown 命中
    # 或 none 也跳过。component_dir / archive_path_repr 已在 _append_escalate_at 之前
    # 算好，这里直接复用，与答案末尾告知 asker 的归档路径保持一致。
    if escalated_now and not is_ticket:
        # question_default：归档表单"问题"框的预填值——优先用 LLM 给的归一化标题，
        # 没给则回退到用户原话。提交时负责人改了就用改后的；question（原话）单纯
        # 留作最终 fallback + 日志对照。
        question_default = archive_q_draft or question
        _pending_archives[qid] = {
            "chat_id": chat_id,
            "asker_id": user_id,
            "question": question,
            "question_default": question_default,
            "owner_id": escalate_owner,
            "component_dir": component_dir,
            "is_feishu": is_feishu_component,
            # 负责人提交归档时给 asker 发通知 post，引用回原始提问消息保持
            # 话题归属（飞书 UI 会把消息显示在原问题底下，asker 看到不突兀）
            "parent_msg_id": parent_msg_id,
        }
        await feishu.send_interactive(
            chat_id,
            _archive_form_card(
                qid,
                question_default,
                escalate_owner,
                archive_path_repr,
                is_feishu=is_feishu_component,
            ),
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
    reasons: list[str] | None,
    comment: str | None,
    clicker_id: str | None,
    asker_id: str | None,
) -> dict:
    """处理 👎 后原因表单的提交，返回最终 ack 卡。

    reasons 是多选返回的 value 列表，按白名单过滤后落 reasons / reason_labels（list）。
    全部不在白名单或为空（None / 注入 / SDK 字段名变了）会写一行 invalid 标记的日志，
    但仍返回 ack 避免 UI 卡住。grep `event=feedback_reason invalid=true` 可发现这类异常。

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
    # 白名单过滤 + 保序去重，防 SDK 字段抖动 / 注入塞乱码 / 用户重复勾选
    cleaned: list[str] = []
    seen: set[str] = set()
    for r in reasons or []:
        if r in _FEEDBACK_REASONS and r not in seen:
            seen.add(r)
            cleaned.append(r)
    valid = bool(cleaned)
    feedback_logger.info(
        json.dumps(
            {
                "event": "feedback_reason",
                "qid": qid,
                "reasons": cleaned if valid else None,
                "reason_labels": (
                    [_FEEDBACK_REASONS[r] for r in cleaned] if valid else None
                ),
                "comment": _excerpt(comment, 500) if comment else None,
                "clicker_id": clicker_id,
                "asker_id": asker_id,
                "invalid": not valid,
            },
            ensure_ascii=False,
        )
    )
    logger.info(
        "feedback reasons qid=%s reasons=%s by=%s comment_len=%d",
        qid,
        cleaned,
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
