"""把 markdown 文本转成飞书 post 消息结构。

markdown 解析（粗体/斜体/链接/列表/标题/代码块/strikethrough/水平线/at 标签/...）
委托给 lark-oapi 的 ``markdown_to_post_ast``——比我们之前手写的解析器覆盖更全，
后续 markdown 解析改进/修复跟着 SDK 走。本模块只额外处理业务专属的
``<<IMG_KEY:xxx>>`` 独立行占位符：bot 在答题流程里把 LLM 的 ``<<IMG:path>>``
校验/上传后替换成 ``<<IMG_KEY:img_xxx>>``，渲染层把这种独立行转成飞书 post
的 img 段。行内嵌入（行中混文字）不识别。
"""

from __future__ import annotations

import re
from typing import Any

from lark_oapi.channel.outbound.markdown import markdown_to_post_ast

_IMG_KEY_LINE_RE = re.compile(r"^<<IMG_KEY:([A-Za-z0-9_-]+)>>$")


def markdown_to_feishu_post(markdown: str, title: str = "") -> dict[str, Any]:
    """转为飞书 post 消息的 content 字典。

    返回值可直接序列化后放入飞书 API 的 ``content`` 字段::

        msg_type = "post"
        content  = json.dumps(markdown_to_feishu_post(text, "标题"))

    实现：按 ``<<IMG_KEY:xxx>>`` 独立行 split，文本段交给 SDK 的
    ``markdown_to_post_ast``，img 段拼成飞书 post 的 ``tag:img`` 元素。
    """
    paragraphs: list[list[dict[str, Any]]] = []
    text_buf: list[str] = []

    def _flush_text() -> None:
        if not text_buf:
            return
        md = "\n".join(text_buf)
        # SDK 的 markdown_to_post_ast 内部对纯空白段做了去重——这是有意的，
        # 飞书 post 段落本身有间距，不需要显式空段落
        ast = markdown_to_post_ast(md)
        paragraphs.extend(ast["zh_cn"]["content"])
        text_buf.clear()

    for raw in markdown.splitlines():
        m = _IMG_KEY_LINE_RE.match(raw.strip())
        if m:
            _flush_text()
            paragraphs.append([{"tag": "img", "image_key": m.group(1)}])
        else:
            text_buf.append(raw)
    _flush_text()

    if not paragraphs:
        paragraphs = [[{"tag": "text", "text": ""}]]

    return {"zh_cn": {"title": title, "content": paragraphs}}
