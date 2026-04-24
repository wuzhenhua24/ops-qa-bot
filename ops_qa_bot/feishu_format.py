"""把 markdown 文本转成飞书 post 消息结构。

飞书 post 是结构化富文本，不支持完整 markdown。本模块覆盖常见语法：

- 标题 `#` / `##` / `###`   → 加粗整行
- 粗体 `**text**`            → bold
- 斜体 `*text*`              → italic
- 行内代码 `` `code` ``      → 保留反引号原样（post 无代码样式）
- 围栏代码块 ``` ```...``` ``` → 以 `---` 分隔，内部逐行保留
- 链接 `[text](url)`         → a 标签
- 无序列表 `- item` / `* item` → 前缀 `• `
- 有序列表 `1. item`         → 保留原格式
- 空行                       → 空段落

不支持：嵌套粗斜体、表格、图片。
"""

import re
from typing import Any

_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_INLINE_RE = re.compile(
    r"\*\*(?P<bold>[^*]+)\*\*"
    r"|\*(?P<italic>[^*]+)\*"
    r"|`(?P<code>[^`]+)`"
)


def _parse_bold_italic_code(text: str) -> list[dict[str, Any]]:
    """解析 bold/italic/inline-code，返回 post span 列表。"""
    spans: list[dict[str, Any]] = []
    last = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > last:
            spans.append({"tag": "text", "text": text[last:m.start()]})
        if m.group("bold") is not None:
            spans.append({"tag": "text", "text": m.group("bold"), "style": ["bold"]})
        elif m.group("italic") is not None:
            spans.append({"tag": "text", "text": m.group("italic"), "style": ["italic"]})
        elif m.group("code") is not None:
            spans.append({"tag": "text", "text": f"`{m.group('code')}`"})
        last = m.end()
    if last < len(text):
        spans.append({"tag": "text", "text": text[last:]})
    return spans


def _inline_spans(text: str) -> list[dict[str, Any]]:
    """先抽出链接，再对 plain text 部分解析 bold/italic/code。"""
    spans: list[dict[str, Any]] = []
    pos = 0
    for m in _LINK_RE.finditer(text):
        if m.start() > pos:
            spans.extend(_parse_bold_italic_code(text[pos:m.start()]))
        spans.append({"tag": "a", "text": m.group(1), "href": m.group(2)})
        pos = m.end()
    if pos < len(text):
        spans.extend(_parse_bold_italic_code(text[pos:]))
    if not spans:
        spans.append({"tag": "text", "text": ""})
    return spans


def markdown_to_feishu_post(markdown: str, title: str = "") -> dict[str, Any]:
    """转为飞书 post 消息的 content 字典。

    返回值可直接序列化后放入飞书 API 的 `content` 字段：
        msg_type = "post"
        content  = json.dumps(markdown_to_feishu_post(text, "标题"))
    """
    paragraphs: list[list[dict[str, Any]]] = []
    in_code = False

    for raw in markdown.splitlines():
        stripped = raw.lstrip()

        # 围栏代码块：用 --- 分隔，内部逐行原样
        if stripped.startswith("```"):
            in_code = not in_code
            paragraphs.append([{"tag": "text", "text": "---"}])
            continue
        if in_code:
            paragraphs.append([{"tag": "text", "text": raw or " "}])
            continue

        # 空行
        if stripped == "":
            paragraphs.append([{"tag": "text", "text": ""}])
            continue

        # 标题
        for prefix in ("### ", "## ", "# "):
            if stripped.startswith(prefix):
                paragraphs.append(
                    [{"tag": "text", "text": stripped[len(prefix):], "style": ["bold"]}]
                )
                break
        else:
            # 无序列表
            if stripped.startswith("- ") or stripped.startswith("* "):
                spans: list[dict[str, Any]] = [{"tag": "text", "text": "• "}]
                spans.extend(_inline_spans(stripped[2:]))
                paragraphs.append(spans)
            else:
                paragraphs.append(_inline_spans(raw))

    return {"zh_cn": {"title": title, "content": paragraphs}}
