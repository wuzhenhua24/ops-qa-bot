"""归档问题标题（<<ARCHIVE_Q:>> marker + 可编辑表单）相关的回归测试。

覆盖 _parse_archive_q 解析、_archive_form_card 字段配置、_write_qa_archive
写入、handle_archive_submit 端到端流程的边界场景。

跑法（不依赖 pytest，裸 assert + 自带 runner）：
    .venv/bin/python -m tests.test_archive_question
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from ops_qa_bot import feishu_core as fc


# ---------------------------------------------------------------------------
# _parse_archive_q
# ---------------------------------------------------------------------------

def test_parse_archive_q_hit():
    answer = "找不到相关内容。\n\n<<ESCALATE:ou_x:redis>>\n<<ARCHIVE_Q:Redis 集群跨机房迁移步骤>>"
    cleaned, draft = fc._parse_archive_q(answer)
    assert draft == "Redis 集群跨机房迁移步骤"
    assert "<<ARCHIVE_Q" not in cleaned
    assert "<<ESCALATE" in cleaned  # 只剥 ARCHIVE_Q，别的标记不动


def test_parse_archive_q_miss():
    answer = "普通答案，没有标记。"
    cleaned, draft = fc._parse_archive_q(answer)
    assert draft is None
    assert cleaned == answer


def test_parse_archive_q_collapses_whitespace():
    cleaned, draft = fc._parse_archive_q("x <<ARCHIVE_Q:  Redis   内存\t告警  >> y")
    assert draft == "Redis 内存 告警"


def test_parse_archive_q_empty_after_strip():
    cleaned, draft = fc._parse_archive_q("x <<ARCHIVE_Q:   >> y")
    assert draft is None
    assert "<<ARCHIVE_Q" not in cleaned


def test_parse_archive_q_overlong_truncated():
    long_title = "标" * 200
    cleaned, draft = fc._parse_archive_q(f"<<ARCHIVE_Q:{long_title}>>")
    assert draft is not None
    assert len(draft) == fc._ARCHIVE_Q_MAX_LEN + 1  # 截断后补一个 "…"
    assert draft.endswith("…")


def test_parse_archive_q_no_angle_brackets_in_payload():
    # 正则 [^<>\n\r]+? 不吃 < >，所以含 < 的内容不会被当成 ARCHIVE_Q 内容
    answer = "<<ARCHIVE_Q:bad<value>>"
    cleaned, draft = fc._parse_archive_q(answer)
    assert draft is None


def test_parse_archive_q_takes_first_strips_all():
    answer = "a<<ARCHIVE_Q:first>>b<<ARCHIVE_Q:second>>c"
    cleaned, draft = fc._parse_archive_q(answer)
    assert draft == "first"
    assert "ARCHIVE_Q" not in cleaned


# ---------------------------------------------------------------------------
# _archive_form_card
# ---------------------------------------------------------------------------

def test_archive_form_card_has_editable_question():
    card = fc._archive_form_card("qid1", "Redis 内存告警怎么处理", "ou_owner", "redis/qa-archive.md")
    form = next(e for e in card["body"]["elements"] if e.get("tag") == "form")
    inputs = {e["name"]: e for e in form["elements"] if e.get("tag") == "input"}
    assert "question" in inputs and "answer" in inputs
    assert inputs["question"]["default_value"] == "Redis 内存告警怎么处理"
    assert inputs["question"].get("required") is True


def test_archive_form_card_default_value_one_line():
    card = fc._archive_form_card("qid1", "多\n行\n标题", "ou_owner", "redis/qa-archive.md")
    form = next(e for e in card["body"]["elements"] if e.get("tag") == "form")
    q_input = next(e for e in form["elements"] if e.get("name") == "question")
    assert "\n" not in q_input["default_value"]


# ---------------------------------------------------------------------------
# _write_qa_archive
# ---------------------------------------------------------------------------

def test_write_qa_archive_heading_is_single_line():
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "qa-archive.md"
        wrote = asyncio.run(fc._write_qa_archive(
            file_path=fp, qid="q1", question="标题\n带换行\n", answer="答案",
            owner_id="ou_owner", asker_id="ou_asker",
        ))
        assert wrote is True
        text = fp.read_text(encoding="utf-8")
        heading = next(ln for ln in text.splitlines() if ln.startswith("## Q:"))
        assert heading == "## Q: 标题 带换行"


def test_write_qa_archive_blank_question_fallback():
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "qa-archive.md"
        asyncio.run(fc._write_qa_archive(
            file_path=fp, qid="q1", question="   \n  ", answer="答案",
            owner_id="ou_owner", asker_id=None,
        ))
        text = fp.read_text(encoding="utf-8")
        assert "## Q: （无标题）" in text


# ---------------------------------------------------------------------------
# handle_archive_submit
# ---------------------------------------------------------------------------

def _seed_pending(qid: str, **overrides):
    ctx = {
        "chat_id": "c1",
        "asker_id": "ou_asker",
        "question": "我们这边 redis7 想迁机房 咋整啊",
        "question_default": "Redis 7.x 集群跨机房迁移步骤",
        "owner_id": "ou_owner",
        "component_dir": None,
        "parent_msg_id": None,
    }
    ctx.update(overrides)
    fc._pending_archives[qid] = ctx
    return ctx


def test_archive_submit_uses_owner_edited_question():
    with tempfile.TemporaryDirectory() as d:
        _seed_pending("qid_edit")
        card = asyncio.run(fc.handle_archive_submit(
            "qid_edit", "Redis 集群跨机房迁移（修订版）", "答案内容", "ou_owner", Path(d),
        ))
        body = card["body"]["elements"][0]["content"]
        assert "✅" in body
        text = (Path(d) / "qa-archive.md").read_text(encoding="utf-8")
        assert "## Q: Redis 集群跨机房迁移（修订版）" in text
        assert "qid_edit" not in fc._pending_archives  # 处理完清掉


def test_archive_submit_blank_question_falls_back_to_default():
    with tempfile.TemporaryDirectory() as d:
        _seed_pending("qid_blank")
        asyncio.run(fc.handle_archive_submit(
            "qid_blank", "   ", "答案内容", "ou_owner", Path(d),
        ))
        text = (Path(d) / "qa-archive.md").read_text(encoding="utf-8")
        assert "## Q: Redis 7.x 集群跨机房迁移步骤" in text


def test_archive_submit_backward_compat_no_question_default():
    # 部署前留下的 pending 项没有 question_default → 回退到用户原话
    with tempfile.TemporaryDirectory() as d:
        ctx = _seed_pending("qid_old")
        del fc._pending_archives["qid_old"]["question_default"]
        asyncio.run(fc.handle_archive_submit(
            "qid_old", "", "答案内容", "ou_owner", Path(d),
        ))
        text = (Path(d) / "qa-archive.md").read_text(encoding="utf-8")
        assert "## Q: 我们这边 redis7 想迁机房 咋整啊" in text


def test_archive_submit_empty_answer_rejected():
    with tempfile.TemporaryDirectory() as d:
        _seed_pending("qid_noans")
        card = asyncio.run(fc.handle_archive_submit(
            "qid_noans", "标题", "   ", "ou_owner", Path(d),
        ))
        assert "答案不能为空" in card["body"]["elements"][0]["content"]
        assert not (Path(d) / "qa-archive.md").exists()
        assert "qid_noans" in fc._pending_archives  # 没处理掉，负责人可重填


def test_archive_submit_non_owner_returns_form_not_write():
    with tempfile.TemporaryDirectory() as d:
        _seed_pending("qid_other")
        card = asyncio.run(fc.handle_archive_submit(
            "qid_other", "标题", "答案", "ou_someone_else", Path(d),
        ))
        # 返回的是表单卡（有 form），不是 ack；预填值是 question_default
        form = next((e for e in card["body"]["elements"] if e.get("tag") == "form"), None)
        assert form is not None
        q_input = next(e for e in form["elements"] if e.get("name") == "question")
        assert q_input["default_value"] == "Redis 7.x 集群跨机房迁移步骤"
        assert not (Path(d) / "qa-archive.md").exists()
        assert "qid_other" in fc._pending_archives


def test_archive_submit_expired_qid():
    card = asyncio.run(fc.handle_archive_submit(
        "nope-not-here", "标题", "答案", "ou_owner", Path("/tmp"),
    ))
    assert "过期" in card["body"]["elements"][0]["content"]


# ---------------------------------------------------------------------------
# Notify asker after successful archive
# ---------------------------------------------------------------------------

class _FakeFeishu:
    """抓住 send_post 调用参数的极简替身，验证 asker 通知路径。"""

    def __init__(self):
        self.posts: list[dict] = []  # 每次 send_post 一个 entry

    async def send_post(self, chat_id, post, *, parent_id=None):
        self.posts.append(
            {"chat_id": chat_id, "post": post, "parent_id": parent_id}
        )
        return "fake_mid"


def _at_user_ids(post: dict) -> list[str]:
    """从 post content 里提取所有 at tag 的 user_id 顺序，便于断言。"""
    return [
        seg["user_id"]
        for line in post["zh_cn"]["content"]
        for seg in line
        if seg.get("tag") == "at"
    ]


def _post_text(post: dict) -> str:
    chunks = []
    for line in post["zh_cn"]["content"]:
        for seg in line:
            if seg.get("tag") == "text":
                chunks.append(seg.get("text", ""))
    return "".join(chunks)


def test_archive_submit_notifies_asker_with_at_and_answer():
    fake = _FakeFeishu()
    with tempfile.TemporaryDirectory() as d:
        _seed_pending("qid_notify", parent_msg_id="om_original_question")
        card = asyncio.run(fc.handle_archive_submit(
            "qid_notify", "Redis 跨机房迁移步骤",
            "1. 起 sentinel\n2. 切流量\n3. ...",
            "ou_owner", Path(d), feishu=fake,
        ))
        # 通知确实发了一次
        assert len(fake.posts) == 1
        sent = fake.posts[0]
        assert sent["chat_id"] == "c1"  # _seed_pending 的默认 chat_id
        assert sent["parent_id"] == "om_original_question"  # 引用回原问题
        # 第一段必须 @ asker（推送）+ @ owner（"谁答的"）
        at_ids = _at_user_ids(sent["post"])
        assert at_ids[0] == "ou_asker"  # asker 必须排在前面，否则不会有推送提醒
        assert "ou_owner" in at_ids
        # 答案 body 落到 post 里（不是只有"已答复"提示）
        text = _post_text(sent["post"])
        assert "起 sentinel" in text
        assert "Redis 跨机房迁移步骤" in text or "Redis 跨机房迁移步骤"[:60] in text
        # ack 卡告诉负责人"已通知提问者"
        ack = card["body"]["elements"][0]["content"]
        assert "已通知提问者" in ack


def test_archive_submit_without_feishu_skips_notify():
    # 单元测试默认走法：不传 feishu → 只写文件不发通知
    with tempfile.TemporaryDirectory() as d:
        _seed_pending("qid_no_notify", parent_msg_id="om_x")
        card = asyncio.run(fc.handle_archive_submit(
            "qid_no_notify", "标题", "答案", "ou_owner", Path(d),
        ))
        # 没有 fake feishu 自然 0 调用；ack 文案也不该带"已通知提问者"
        ack = card["body"]["elements"][0]["content"]
        assert "已通知提问者" not in ack
        # 但归档文件确实落盘了
        assert (Path(d) / "qa-archive.md").exists()


def test_archive_submit_notify_failure_does_not_block_archive():
    # 通知 send_post 抛错（飞书 API 异常 / asker 已退群等）不能回阻已写盘的归档
    class _FlakyFeishu:
        def __init__(self):
            self.called = False

        async def send_post(self, chat_id, post, *, parent_id=None):
            self.called = True
            raise RuntimeError("simulated feishu API error")

    flaky = _FlakyFeishu()
    with tempfile.TemporaryDirectory() as d:
        _seed_pending("qid_flaky")
        card = asyncio.run(fc.handle_archive_submit(
            "qid_flaky", "标题", "答案", "ou_owner", Path(d), feishu=flaky,
        ))
        assert flaky.called  # 通知尝试过了
        ack = card["body"]["elements"][0]["content"]
        assert "已归档" in ack
        # 通知失败 → ack 不该撒谎说已通知
        assert "已通知提问者" not in ack
        assert (Path(d) / "qa-archive.md").exists()


def test_archive_submit_duplicate_does_not_re_notify():
    # 幂等命中（同 qid 已在归档文件里）不重发通知——避免给 asker 刷重复消息
    fake = _FakeFeishu()
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "qa-archive.md"
        # 预先伪造一条已存在的归档（含 qid 哨兵），让 _write_qa_archive 返回 False
        fp.write_text("旧内容\nqid: qid_dup\n", encoding="utf-8")
        _seed_pending("qid_dup")
        card = asyncio.run(fc.handle_archive_submit(
            "qid_dup", "标题", "答案", "ou_owner", Path(d), feishu=fake,
        ))
        # 已存在 → 跳过 → 不通知
        assert len(fake.posts) == 0
        assert "已存在" in card["body"]["elements"][0]["content"]


# pending ctx backward-compat：旧条目没有 parent_msg_id 时通知也能发，只是不引用
def test_archive_submit_notify_without_parent_msg_id():
    fake = _FakeFeishu()
    with tempfile.TemporaryDirectory() as d:
        ctx = _seed_pending("qid_no_parent")
        # 模拟"老的 pending 项"——没有 parent_msg_id 字段
        del fc._pending_archives["qid_no_parent"]["parent_msg_id"]
        asyncio.run(fc.handle_archive_submit(
            "qid_no_parent", "标题", "答案", "ou_owner", Path(d), feishu=fake,
        ))
        assert len(fake.posts) == 1
        assert fake.posts[0]["parent_id"] is None  # 顶层发，不引用


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
