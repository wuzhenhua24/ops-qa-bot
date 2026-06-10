"""答案 marker 解析层：正则 + 纯函数，从 feishu_core 拆出（行为零变化）。

LLM 在答案文本里通过 <<MARKER:...>> 约定结构化意图（升级 / 反问 / 追问 /
归档标题 / 嵌图），本模块负责"识别 + 剥离 + 取值"这一层——全部是无状态纯
函数，不碰飞书 API、不碰会话/缓存/编排。拿解析结果发卡 / @ / 上传的编排
仍在 feishu_core。
"""

from __future__ import annotations

import re

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


# 答案内嵌图机制：步骤截图 / 标注图 / 强相关故障截图，文字转述不如直接展示。
# LLM 在答案里独立一行写 <<IMG:redis/images/step1.png>>（路径相对 docs_root），
# bot 校验路径 → 上传飞书拿 image_key → 把标记换成 <<IMG_KEY:img_xxx>>，渲染层
# 把这种行渲染为飞书 post 的 img 段。每条最多 5 张，超限剥除 + 末尾告知用户。
# 路径正则放宽到非控制字符以兼容中文/带空格的文件名；安全校验在 _validate 里做。
_IMG_RE = re.compile(r"<<IMG:([^<>\n\r]+?)>>")
_IMG_ALLOWED_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_IMG_MAX_BYTES = 5 * 1024 * 1024  # 飞书消息图官方上限 10MB，留点余量
_IMG_MAX_PER_ANSWER = 5


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


