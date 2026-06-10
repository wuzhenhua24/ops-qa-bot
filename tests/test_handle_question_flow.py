"""handle_question 主编排链路的集成测试。

此前只有解析器/卡片级单测，450 行的主流程（占位 → 流式答题 → marker 解析 →
升级/归档 → 反馈卡）没有测试保护。本文件用脚本化 bot（按预设吐流式事件）+
记录型 FeishuClient 把真实编排逻辑完整跑一遍，锁住每种场景的出站消息序列：

- 正常答题：占位 post → 编辑成最终答案 → 仅反馈卡；qa 事件落 feedback 日志。
- 追问标记：FOLLOWUPS 解析 → 追问卡 + 反馈卡两张，marker 不漏进答案。
- 升级流：ESCALATE + ARCHIVE_Q → @ 负责人 + 归档表单卡（预填标题）+ pending
  登记；同 (chat, owner) 30 分钟 cooldown 内第二次升级不再 @ / 不再发卡。
- 工单流：ESCALATE_TICKET → 只 @ 不发归档卡。
- 反问轮：CLARIFY → 只发"说不清楚"出口卡，不发反馈/追问/归档，升级标记被强制清空。
- 会话过期提示 / drift 兜底 hint / 答题异常的友好错误（反馈卡照发）。
- 排队占位：session lock 被占时先 🕒 排队中，拿到锁刷成 🔍 翻文档中。
- 答案嵌图：<<IMG:path>> 校验 → 上传 → 渲染成 img 段。

跑法：
    .venv/bin/python -m pytest tests/test_handle_question_flow.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import ops_qa_bot.feishu_core as fc


def _run(coro):
    return asyncio.run(coro)


_INDEX_MD = """# 索引

| 组件 | 来源 | 目录 | 负责人 | open_id |
|------|------|------|--------|---------|
| Redis | local | `redis/` | 张三 | ou_owner_redis_0001 |
| MySQL | local | `mysql/` | 李四 | ou_owner_mysql_0002 |
"""

OWNER = "ou_owner_redis_0001"


def _docs_root(tmp_path: Path) -> Path:
    (tmp_path / "INDEX.md").write_text(_INDEX_MD, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _clear_module_state():
    """升级 cooldown / 归档 pending 都是模块级 TTLCache，测试间必须互不污染。"""
    fc._escalate_cooldown.clear()
    fc._pending_archives.clear()
    yield
    fc._escalate_cooldown.clear()
    fc._pending_archives.clear()


class _RecordingFeishu:
    def __init__(self):
        self.posts: list[tuple] = []
        self.updates: list[tuple] = []
        self.cards: list[tuple] = []
        self.uploaded: list[bytes] = []

    async def send_post(self, chat_id, post_content, parent_id=None):
        self.posts.append((chat_id, post_content, parent_id))
        return f"mid_post_{len(self.posts)}"

    async def update_post(self, message_id, post_content):
        self.updates.append((message_id, post_content))
        return True

    async def send_interactive(self, chat_id, card, parent_id=None):
        self.cards.append((chat_id, card, parent_id))
        return f"mid_card_{len(self.cards)}"

    async def upload_image(self, image_bytes):
        self.uploaded.append(image_bytes)
        return "img_key_123"


def _scripted_entry(answer_text: str, *, subtype: str = "success", error=None):
    """造一个假 session entry：bot.ask 按脚本吐 text + done（或直接抛错）。"""

    class _Bot:
        async def ask(self, question, images=None):
            if error is not None:
                raise error
            yield {"type": "text", "text": answer_text}
            yield {
                "type": "done",
                "cost_usd": 0.01,
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "num_turns": 4,
                "duration_ms": 1500,
                "duration_api_ms": 1200,
                "subtype": subtype,
            }

    class _Entry:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.last_used = 0.0
            self.bot = _Bot()

    return _Entry()


def _ask(
    tmp_path: Path,
    monkeypatch,
    answer: str,
    *,
    question: str = "redis 内存满了怎么办",
    feishu: "_RecordingFeishu | None" = None,
    **entry_kw,
) -> _RecordingFeishu:
    entry = _scripted_entry(answer, **entry_kw)

    async def fake_get(self, key):
        return entry

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu = feishu or _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    _run(
        fc.handle_question(
            "chatA", "userA", question, feishu, sm, parent_msg_id="om_q1"
        )
    )
    return feishu


def _flat(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 正常答题流
# ---------------------------------------------------------------------------


def test_normal_flow_message_sequence(tmp_path: Path, monkeypatch):
    feishu = _ask(tmp_path, monkeypatch, "调大 maxmemory 即可。（来源：redis/overview.md）")
    # 1 条占位（引用回原消息 + @ 提问者 + 问题摘要）
    assert len(feishu.posts) == 1
    chat_id, placeholder, parent_id = feishu.posts[0]
    assert chat_id == "chatA" and parent_id == "om_q1"
    assert "翻文档中" in _flat(placeholder)
    assert "userA" in _flat(placeholder)
    # 占位被编辑成最终答案
    assert len(feishu.updates) == 1
    mid, final = feishu.updates[0]
    assert mid == "mid_post_1"
    assert "maxmemory" in _flat(final)
    # 只有一张反馈卡，引用回原消息
    assert len(feishu.cards) == 1
    _, card, card_parent = feishu.cards[0]
    assert card_parent == "om_q1"
    assert "👍 有帮助" in _flat(card)
    # 无升级：没有归档 pending
    assert len(fc._pending_archives) == 0


def test_qa_event_logged_with_usage(tmp_path: Path, monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="ops_qa_bot.feedback"):
        _ask(tmp_path, monkeypatch, "答案正文。")
    qa = [
        json.loads(r.message)
        for r in caplog.records
        if r.name == "ops_qa_bot.feedback" and '"event": "qa"' in r.message
    ]
    assert len(qa) == 1
    rec = qa[0]
    assert rec["chat_id"] == "chatA" and rec["user_id"] == "userA"
    assert rec["question"] == "redis 内存满了怎么办"
    assert rec["usage"]["input_tokens"] == 100
    assert rec["num_turns"] == 4
    assert "escalated_to" not in rec and "clarification" not in rec


# ---------------------------------------------------------------------------
# 追问卡
# ---------------------------------------------------------------------------


def test_followups_card_and_marker_stripped(tmp_path: Path, monkeypatch):
    feishu = _ask(
        tmp_path, monkeypatch, "答案正文。\n\n<<FOLLOWUPS:risks|rollback>>"
    )
    assert "FOLLOWUPS" not in _flat(feishu.updates[0][1])
    # 追问卡在前、反馈卡在后，两张都引用回原消息
    assert len(feishu.cards) == 2
    followup_card = _flat(feishu.cards[0][1])
    assert "风险点" in followup_card and "回滚方案" in followup_card
    assert "👍 有帮助" in _flat(feishu.cards[1][1])
    assert all(parent == "om_q1" for _, _, parent in feishu.cards)


# ---------------------------------------------------------------------------
# 升级 / 归档 / cooldown
# ---------------------------------------------------------------------------

_ESCALATE_ANSWER = (
    "文档中未找到相关内容。\n"
    f"<<ESCALATE:{OWNER}:redis>>\n"
    "<<ARCHIVE_Q:Redis 内存满后的扩容流程>>"
)


def test_escalate_mentions_owner_and_sends_archive_form(
    tmp_path: Path, monkeypatch
):
    feishu = _ask(tmp_path, monkeypatch, _ESCALATE_ANSWER)
    final = _flat(feishu.updates[0][1])
    # marker 全部剥掉，@ 负责人渲染进答案 post
    assert "ESCALATE" not in final and "ARCHIVE_Q" not in final
    assert OWNER in final
    assert "redis/qa-archive.md" in final
    # 反馈卡 + 归档表单卡
    assert len(feishu.cards) == 2
    archive_card = _flat(feishu.cards[1][1])
    assert "Redis 内存满后的扩容流程" in archive_card  # LLM 草稿预填
    # pending 登记完整（负责人提交归档时要用）
    assert len(fc._pending_archives) == 1
    ctx = next(iter(fc._pending_archives.values()))
    assert ctx["owner_id"] == OWNER
    assert ctx["component_dir"] == "redis"
    assert ctx["asker_id"] == "userA"


def test_escalate_cooldown_suppresses_second_at(tmp_path: Path, monkeypatch):
    _ask(tmp_path, monkeypatch, _ESCALATE_ANSWER)
    feishu2 = _ask(tmp_path, monkeypatch, _ESCALATE_ANSWER)
    final2 = _flat(feishu2.updates[0][1])
    # cooldown 命中：不再 @ 负责人、不再发归档卡（只有反馈卡）
    assert OWNER not in final2
    assert len(feishu2.cards) == 1
    assert len(fc._pending_archives) == 1  # 没有新增


def test_escalate_ticket_no_archive_card(tmp_path: Path, monkeypatch):
    feishu = _ask(
        tmp_path,
        monkeypatch,
        f"权限申请请联系负责人开通。\n<<ESCALATE_TICKET:{OWNER}>>",
    )
    final = _flat(feishu.updates[0][1])
    assert OWNER in final
    assert "ESCALATE_TICKET" not in final
    # 工单类：只有反馈卡，不发归档表单
    assert len(feishu.cards) == 1
    assert len(fc._pending_archives) == 0


# ---------------------------------------------------------------------------
# 反问轮
# ---------------------------------------------------------------------------


def test_clarify_round_only_giveup_card(tmp_path: Path, monkeypatch):
    # LLM 不守规矩在反问轮塞了升级/追问标记：必须被强制清空
    feishu = _ask(
        tmp_path,
        monkeypatch,
        "请问你的 Redis 是哪个版本？单机还是集群？\n<<CLARIFY>>\n"
        f"<<ESCALATE:{OWNER}:redis>><<FOLLOWUPS:risks>>",
    )
    final = _flat(feishu.updates[0][1])
    assert "哪个版本" in final
    assert OWNER not in final  # 升级被压掉
    # 只有"说不清楚"出口卡，没有反馈卡/追问卡/归档卡
    assert len(feishu.cards) == 1
    assert "说不清楚" in _flat(feishu.cards[0][1])
    assert len(fc._pending_archives) == 0


# ---------------------------------------------------------------------------
# 过期提示 / drift 兜底 / 错误兜底
# ---------------------------------------------------------------------------


def test_session_expired_notice_prefixed(tmp_path: Path, monkeypatch):
    async def fake_expired(self, key):
        return True

    monkeypatch.setattr(fc.SessionManager, "take_expired_notice", fake_expired)
    feishu = _ask(tmp_path, monkeypatch, "答案正文。")
    assert "上下文已过期" in _flat(feishu.updates[0][1])


def test_drift_fallback_appends_hint(tmp_path: Path, monkeypatch):
    # 说了"未找到"但没输出任何 marker：追加引导提示
    feishu = _ask(tmp_path, monkeypatch, "文档中未找到相关内容，抱歉。")
    assert "文档没覆盖到这块" in _flat(feishu.updates[0][1])


def test_answer_error_friendly_message_feedback_still_sent(
    tmp_path: Path, monkeypatch
):
    feishu = _ask(
        tmp_path, monkeypatch, "", error=RuntimeError("proxy connection refused")
    )
    final = _flat(feishu.updates[0][1])
    # 友好错误而不是堆栈；反馈卡照发（用户仍可反馈这次体验）
    assert "proxy connection refused" not in final
    assert len(feishu.cards) == 1


# ---------------------------------------------------------------------------
# 排队占位
# ---------------------------------------------------------------------------


def test_queued_placeholder_then_refresh(tmp_path: Path, monkeypatch):
    class _FakeBot:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def ask(self, question, images=None):
            yield {"type": "text", "text": "排到我了，答案在此。"}
            yield {"type": "done", "subtype": "success"}

    monkeypatch.setattr(fc, "OpsQABot", _FakeBot)

    async def go():
        sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
        key = ("chatA", "userA")
        entry = await sm.get(key)
        await entry.lock.acquire()  # 模拟前一条问题还在答
        feishu = _RecordingFeishu()
        task = asyncio.create_task(
            fc.handle_question(
                "chatA", "userA", "第二条问题", feishu, sm, parent_msg_id="om_q2"
            )
        )
        await asyncio.sleep(0.05)
        # 锁被占：占位显示排队中
        assert len(feishu.posts) == 1
        assert "排队中" in _flat(feishu.posts[0][1])
        entry.lock.release()
        await task
        return feishu

    feishu = _run(go())
    # 拿到锁后第一次编辑刷成"翻文档中"，最后一次编辑是最终答案
    assert len(feishu.updates) == 2
    assert "翻文档中" in _flat(feishu.updates[0][1])
    assert "排到我了" in _flat(feishu.updates[1][1])


# ---------------------------------------------------------------------------
# 答案嵌图
# ---------------------------------------------------------------------------


def test_img_marker_uploaded_and_rendered(tmp_path: Path, monkeypatch):
    docs = _docs_root(tmp_path)
    img = docs / "redis" / "images" / "step1.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    feishu = _ask(
        tmp_path,
        monkeypatch,
        "按下图操作：\n<<IMG:redis/images/step1.png>>\n完成后验证。",
    )
    final = _flat(feishu.updates[0][1])
    assert len(feishu.uploaded) == 1
    assert "img_key_123" in final  # 渲染成飞书 img 段
    assert "<<IMG" not in final


def test_invalid_img_marker_stripped_silently(tmp_path: Path, monkeypatch):
    feishu = _ask(
        tmp_path,
        monkeypatch,
        "按下图操作：\n<<IMG:../etc/passwd>>\n<<IMG:redis/nonexist.png>>\n完成。",
    )
    final = _flat(feishu.updates[0][1])
    assert len(feishu.uploaded) == 0
    assert "<<IMG" not in final and "passwd" not in final
