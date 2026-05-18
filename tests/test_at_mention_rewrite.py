"""正文 @ owner 渲染（_rewrite_owner_at_mentions）的回归测试。

场景：LLM 在答案正文里写 `<@ou_xxx>` 字面（drift 兜底列候选负责人时多见），
markdown 渲染会原样输出尖括号文本。bot 端识别 pattern → 对照 INDEX.md 注册
白名单 → 在册的转 feishu <at> tag，不在册的（LLM 幻觉）静默剥除。

覆盖：
- pattern 识别（含空格变种）
- 白名单过滤（在册 → at tag，不在册 → 删除）
- text 段拆分正确性（@ 在首 / 中 / 尾 / 多个 / 跨段）
- style 保留（bold/italic）
- 已 tag=at 的段跳过（不重复处理 _mention_post / _append_escalate_at 加的 @）
- 边界：空段降级 / 完全没匹配 / docs_root 无 INDEX.md

跑法：
    .venv/bin/python -m tests.test_at_mention_rewrite
"""

from __future__ import annotations

from ops_qa_bot import feishu_core as fc


# ---------------------------------------------------------------------------
# Pattern 识别：_AT_OWNER_RE
# ---------------------------------------------------------------------------

def test_pattern_canonical():
    m = fc._AT_OWNER_RE.search("联系 <@ou_abc123> 协助")
    assert m and m.group(1) == "ou_abc123"


def test_pattern_tolerates_whitespace_after_angle():
    m = fc._AT_OWNER_RE.search("< @ou_abc123>")
    assert m and m.group(1) == "ou_abc123"


def test_pattern_tolerates_whitespace_around_open_id():
    m = fc._AT_OWNER_RE.search("<@ ou_abc123 >")
    assert m and m.group(1) == "ou_abc123"


def test_pattern_rejects_non_ou_prefix():
    # 必须 ou_ 开头才算 owner id
    assert fc._AT_OWNER_RE.search("<@user123>") is None
    assert fc._AT_OWNER_RE.search("<@ou>") is None


# ---------------------------------------------------------------------------
# 白名单过滤
# ---------------------------------------------------------------------------

def _wrap_post(*paragraphs):
    """构造一个最小 post 结构便于断言。"""
    return {"zh_cn": {"title": "T", "content": list(paragraphs)}}


def _at_segs(post):
    """提取所有 at tag 的 user_id 顺序。"""
    return [
        seg["user_id"]
        for para in post["zh_cn"]["content"]
        for seg in para
        if seg.get("tag") == "at"
    ]


def _texts(post):
    """提取所有 text 段的文本拼接，便于断言剩余文字内容。"""
    return "".join(
        seg.get("text", "")
        for para in post["zh_cn"]["content"]
        for seg in para
        if seg.get("tag") == "text"
    )


def test_registered_owner_rendered_as_at_tag():
    post = _wrap_post(
        [{"tag": "text", "text": "联系 <@ou_known> 协助"}]
    )
    rendered, dropped = fc._rewrite_owner_at_mentions(post, {"ou_known"})
    assert rendered == 1 and dropped == 0
    assert _at_segs(post) == ["ou_known"]
    assert _texts(post) == "联系  协助"  # 占位空格保留


def test_unregistered_owner_silently_dropped():
    # LLM 幻觉编出来的 open_id（不在 INDEX.md）→ 不渲染、不保留字面
    post = _wrap_post(
        [{"tag": "text", "text": "联系 <@ou_hallucinated> 协助"}]
    )
    rendered, dropped = fc._rewrite_owner_at_mentions(post, {"ou_real"})
    assert rendered == 0 and dropped == 1
    assert _at_segs(post) == []
    assert "<@" not in _texts(post)
    assert "ou_hallucinated" not in _texts(post)


def test_mixed_registered_and_unregistered():
    post = _wrap_post(
        [{"tag": "text", "text": "A: <@ou_real> 或 B: <@ou_fake>"}]
    )
    rendered, dropped = fc._rewrite_owner_at_mentions(post, {"ou_real"})
    assert rendered == 1 and dropped == 1
    assert _at_segs(post) == ["ou_real"]
    # 假 @ 被剥但前后文字保留
    text = _texts(post)
    assert "A:" in text
    assert "或 B:" in text
    assert "ou_fake" not in text


# ---------------------------------------------------------------------------
# 拆分位置：首 / 中 / 尾 / 多个
# ---------------------------------------------------------------------------

def test_at_at_start_of_segment():
    post = _wrap_post(
        [{"tag": "text", "text": "<@ou_a> 是负责人"}]
    )
    fc._rewrite_owner_at_mentions(post, {"ou_a"})
    para = post["zh_cn"]["content"][0]
    # 第一段应该是 at tag，第二段是" 是负责人"
    assert para[0] == {"tag": "at", "user_id": "ou_a"}
    assert para[1] == {"tag": "text", "text": " 是负责人"}


def test_at_at_end_of_segment():
    post = _wrap_post(
        [{"tag": "text", "text": "负责人是 <@ou_a>"}]
    )
    fc._rewrite_owner_at_mentions(post, {"ou_a"})
    para = post["zh_cn"]["content"][0]
    assert para[0] == {"tag": "text", "text": "负责人是 "}
    assert para[1] == {"tag": "at", "user_id": "ou_a"}


def test_multiple_at_in_one_segment():
    post = _wrap_post(
        [{"tag": "text", "text": "可能是 <@ou_a> 或 <@ou_b> 或 <@ou_c>"}]
    )
    rendered, dropped = fc._rewrite_owner_at_mentions(
        post, {"ou_a", "ou_b", "ou_c"}
    )
    assert rendered == 3 and dropped == 0
    assert _at_segs(post) == ["ou_a", "ou_b", "ou_c"]


# ---------------------------------------------------------------------------
# style 保留
# ---------------------------------------------------------------------------

def test_style_preserved_on_split_text_chunks():
    post = _wrap_post(
        [{"tag": "text", "text": "联系 <@ou_a> 协助", "style": ["bold"]}]
    )
    fc._rewrite_owner_at_mentions(post, {"ou_a"})
    para = post["zh_cn"]["content"][0]
    # 拆出来的两个 text 段都带 bold；at 段没 style 字段
    text_segs = [s for s in para if s.get("tag") == "text"]
    for s in text_segs:
        assert s.get("style") == ["bold"]


# ---------------------------------------------------------------------------
# 不串扰其他段
# ---------------------------------------------------------------------------

def test_existing_at_segments_untouched():
    # _mention_post / _append_escalate_at 已经加了 at 段——rewriter 不重复处理
    post = _wrap_post(
        [
            {"tag": "at", "user_id": "ou_asker"},  # 已有的 @ asker
            {"tag": "text", "text": " 答案 <@ou_owner> 看这里"},
        ]
    )
    rendered, dropped = fc._rewrite_owner_at_mentions(post, {"ou_owner"})
    # 一个 at 段是 rewriter 新加的；原有 at 段不计入
    assert rendered == 1 and dropped == 0
    assert _at_segs(post) == ["ou_asker", "ou_owner"]


def test_non_text_segments_untouched():
    # img / code_block 等非 text 段不处理
    post = _wrap_post(
        [{"tag": "img", "image_key": "img_xxx"}],
        [{"tag": "code_block", "text": "ssh ... <@ou_in_code>"}],
        [{"tag": "text", "text": "正文 <@ou_a>"}],
    )
    fc._rewrite_owner_at_mentions(post, {"ou_a", "ou_in_code"})
    assert _at_segs(post) == ["ou_a"]  # code_block 里的不被识别
    # img / code_block 原样保留
    assert post["zh_cn"]["content"][0][0] == {"tag": "img", "image_key": "img_xxx"}
    assert post["zh_cn"]["content"][1][0]["tag"] == "code_block"


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

def test_no_at_in_post_returns_zero():
    post = _wrap_post([{"tag": "text", "text": "纯文字答案"}])
    rendered, dropped = fc._rewrite_owner_at_mentions(post, {"ou_a"})
    assert rendered == 0 and dropped == 0
    # post 内容没变
    assert post["zh_cn"]["content"][0][0]["text"] == "纯文字答案"


def test_empty_registered_drops_everything():
    # INDEX.md 没解析出任何 owner（坏文件 / 缺失）→ 所有 <@> 都被剥
    post = _wrap_post(
        [{"tag": "text", "text": "<@ou_a> 和 <@ou_b>"}]
    )
    rendered, dropped = fc._rewrite_owner_at_mentions(post, set())
    assert rendered == 0 and dropped == 2
    assert "ou_a" not in _texts(post)
    assert "ou_b" not in _texts(post)


def test_segment_becomes_empty_after_strip_keeps_paragraph_valid():
    # 段里只有一个被剥的 @，剥完应该有占位空 text 段（避免后续渲染崩）
    post = _wrap_post([{"tag": "text", "text": "<@ou_fake>"}])
    fc._rewrite_owner_at_mentions(post, set())
    para = post["zh_cn"]["content"][0]
    # 至少有一个段（即便是空 text）让段结构合法
    assert len(para) >= 1


# ---------------------------------------------------------------------------

def _run_all():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _run_all() else 0)
