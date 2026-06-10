"""定时跟进任务的回归测试：工具入参校验 + FollowupScheduler 触发/上限/收尾。

覆盖：
- schedule_followup handler：delay 越界 / 非整数 / task 空 / task 超长都返回
  is_error 文字而不抛；合法入参透传给 submitter 并返回其确认文字。
- FollowupScheduler：到点用前缀拼好的问题调 handle_question（delay=0 即时触发）；
  per-(chat,asker) 上限到顶后拒登记；stop() 取消未触发任务并清计数；
  到点执行失败时往群里 @ asker 补失败提示（通知本身失败也不炸、计数照常回收）。
- make_followup_submitter：未超限返回 ✅ 确认文字 + 真的登记；超限返回 ⚠️ 引导文字
  且不登记。
- 特性开关：SessionManager 在 enabled=False / 无 feishu 时不建 scheduler。

跑法：
    .venv/bin/python -m pytest tests/test_scheduled_followup.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import ops_qa_bot.feishu_core as fc
from ops_qa_bot.config import ScheduledFollowupConfig
from ops_qa_bot.scheduled_followup import (
    ScheduledFollowupRequest,
    make_schedule_followup_handler,
)


def _run(coro):
    return asyncio.run(coro)


def _cfg(**over) -> ScheduledFollowupConfig:
    base = dict(
        enabled=True,
        min_delay_minutes=1,
        max_delay_minutes=120,
        max_pending_per_user=2,
    )
    base.update(over)
    return ScheduledFollowupConfig(**base)


def _result_text(res: dict) -> str:
    return res["content"][0]["text"]


# ---------------------------------------------------------------------------
# handler 入参校验
# ---------------------------------------------------------------------------


def test_handler_rejects_out_of_range_delay():
    async def submit(req):  # pragma: no cover - 不应被调用
        raise AssertionError("submitter should not be called on invalid delay")

    h = make_schedule_followup_handler(_cfg(), submit)
    res = _run(h({"delay_minutes": 999, "task": "查点东西"}))
    assert res.get("is_error")
    assert "120" in _result_text(res)


def test_handler_rejects_non_integer_delay():
    async def submit(req):  # pragma: no cover
        raise AssertionError("should not be called")

    h = make_schedule_followup_handler(_cfg(), submit)
    res = _run(h({"delay_minutes": "soon", "task": "查点东西"}))
    assert res.get("is_error")


def test_handler_rejects_empty_task():
    async def submit(req):  # pragma: no cover
        raise AssertionError("should not be called")

    h = make_schedule_followup_handler(_cfg(), submit)
    res = _run(h({"delay_minutes": 10, "task": "   "}))
    assert res.get("is_error")


def test_handler_rejects_overlong_task():
    async def submit(req):  # pragma: no cover
        raise AssertionError("should not be called")

    h = make_schedule_followup_handler(_cfg(), submit)
    res = _run(h({"delay_minutes": 10, "task": "x" * 1001}))
    assert res.get("is_error")


def test_handler_passes_valid_to_submitter():
    seen: list[ScheduledFollowupRequest] = []

    async def submit(req):
        seen.append(req)
        return "已登记 OK"

    h = make_schedule_followup_handler(_cfg(), submit)
    res = _run(h({"delay_minutes": 20, "task": "检查 10.1.2.3 的 ALTER 完成没"}))
    assert not res.get("is_error")
    assert _result_text(res) == "已登记 OK"
    assert len(seen) == 1
    assert seen[0].delay_minutes == 20
    assert "ALTER" in seen[0].task


def test_handler_submitter_exception_returns_error():
    async def submit(req):
        raise RuntimeError("boom")

    h = make_schedule_followup_handler(_cfg(), submit)
    res = _run(h({"delay_minutes": 5, "task": "查点东西"}))
    assert res.get("is_error")


# ---------------------------------------------------------------------------
# FollowupScheduler 触发 / 上限 / 收尾
# ---------------------------------------------------------------------------


def test_scheduler_fires_handle_question_with_prefixed_task(monkeypatch):
    fired: list[tuple] = []

    async def fake_hq(chat_id, user_id, question, feishu, session_mgr, **kw):
        fired.append((chat_id, user_id, question, kw))

    monkeypatch.setattr(fc, "handle_question", fake_hq)

    async def go():
        sched = fc.FollowupScheduler(
            feishu=object(), session_mgr=object(), config=_cfg()
        )
        # delay=0 → 立即触发（handler 才管下限，scheduler 直接信任入参）
        ok = sched.schedule("chatA", "userA", 0, "检查表 foo 的 ALTER 完成没")
        assert ok
        assert sched.pending_count(("chatA", "userA")) == 1
        # 让事件循环跑一圈，触发 sleep(0) 后的 handle_question
        await asyncio.sleep(0.05)
        await sched.stop()
        return fired

    res = _run(go())
    assert len(res) == 1
    chat_id, user_id, question, kw = res[0]
    assert chat_id == "chatA" and user_id == "userA"
    assert question.startswith(fc._FOLLOWUP_QUESTION_PREFIX)
    assert "检查表 foo 的 ALTER 完成没" in question
    # top-level 推送：scheduler 不带 parent_msg_id（原消息可能已隔很久）
    assert "parent_msg_id" not in kw


def test_scheduler_decrements_pending_after_fire(monkeypatch):
    async def fake_hq(*a, **k):
        return None

    monkeypatch.setattr(fc, "handle_question", fake_hq)

    async def go():
        sched = fc.FollowupScheduler(
            feishu=object(), session_mgr=object(), config=_cfg()
        )
        sched.schedule("c", "u", 0, "task")
        await asyncio.sleep(0.05)
        cnt = sched.pending_count(("c", "u"))
        await sched.stop()
        return cnt

    assert _run(go()) == 0


def test_scheduler_enforces_per_user_cap():
    async def go():
        sched = fc.FollowupScheduler(
            feishu=object(), session_mgr=object(), config=_cfg(max_pending_per_user=1)
        )
        # 长延时让任务一直挂起，不会自动触发清空计数
        assert sched.schedule("c", "u", 999, "first")
        # 同一 (chat, user) 第二个超上限被拒
        assert not sched.schedule("c", "u", 999, "second")
        # 另一个用户不受影响
        assert sched.schedule("c", "other", 999, "ok")
        await sched.stop()

    _run(go())


class _RecordingFeishu:
    """记录 send_post 调用的假 outbound client；fail=True 时模拟飞书接口也挂。"""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.posts: list[tuple] = []

    async def send_post(self, chat_id, post_content, parent_id=None):
        if self.fail:
            raise RuntimeError("feishu down")
        self.posts.append((chat_id, post_content))
        return "mid_notice"


def test_scheduler_failure_notifies_asker(monkeypatch):
    async def boom_hq(*a, **k):
        raise RuntimeError("answer pipeline exploded")

    monkeypatch.setattr(fc, "handle_question", boom_hq)

    async def go():
        feishu = _RecordingFeishu()
        sched = fc.FollowupScheduler(
            feishu=feishu, session_mgr=object(), config=_cfg()
        )
        sched.schedule("chatA", "userA", 0, "检查表 foo 的 ALTER 完成没")
        await asyncio.sleep(0.05)
        cnt = sched.pending_count(("chatA", "userA"))
        await sched.stop()
        return feishu.posts, cnt

    posts, cnt = _run(go())
    assert len(posts) == 1
    chat_id, post = posts[0]
    assert chat_id == "chatA"
    flat = repr(post)
    # @ 回发起人 + 失败提示 + 带任务摘要让他知道是哪笔跟进黄了
    assert "userA" in flat
    assert "执行失败" in flat
    assert "检查表 foo 的 ALTER 完成没" in flat
    # 失败路径同样要清挂起计数，不能占着每人上限
    assert cnt == 0


def test_scheduler_failure_notice_failure_is_swallowed(monkeypatch):
    async def boom_hq(*a, **k):
        raise RuntimeError("answer pipeline exploded")

    monkeypatch.setattr(fc, "handle_question", boom_hq)

    async def go():
        sched = fc.FollowupScheduler(
            feishu=_RecordingFeishu(fail=True),
            session_mgr=object(),
            config=_cfg(),
        )
        sched.schedule("c", "u", 0, "task")
        # 通知也失败：不能炸 task、计数照样回收
        await asyncio.sleep(0.05)
        cnt = sched.pending_count(("c", "u"))
        await sched.stop()
        return cnt

    assert _run(go()) == 0


def test_scheduler_stop_cancels_pending():
    async def go():
        sched = fc.FollowupScheduler(
            feishu=object(), session_mgr=object(), config=_cfg()
        )
        sched.schedule("c", "u", 999, "long pending")
        assert sched.pending_count(("c", "u")) == 1
        await sched.stop()
        # closing 后不再接受新登记
        assert not sched.schedule("c", "u", 0, "after stop")
        return sched.pending_count(("c", "u"))

    assert _run(go()) == 0


# ---------------------------------------------------------------------------
# make_followup_submitter
# ---------------------------------------------------------------------------


def test_submitter_registers_and_confirms():
    async def go():
        sched = fc.FollowupScheduler(
            feishu=object(), session_mgr=object(), config=_cfg()
        )
        sub = fc.make_followup_submitter(sched, "chatA", "userA")
        text = await sub(
            ScheduledFollowupRequest(delay_minutes=999, task="long pending task")
        )
        cnt = sched.pending_count(("chatA", "userA"))
        await sched.stop()
        return text, cnt

    text, cnt = _run(go())
    assert text.startswith("✅")
    assert "999" in text
    assert cnt == 1


def test_submitter_refuses_over_cap():
    async def go():
        sched = fc.FollowupScheduler(
            feishu=object(), session_mgr=object(), config=_cfg(max_pending_per_user=1)
        )
        sub = fc.make_followup_submitter(sched, "chatA", "userA")
        # 占满上限
        await sub(ScheduledFollowupRequest(delay_minutes=999, task="first"))
        # 第二个超限
        text = await sub(ScheduledFollowupRequest(delay_minutes=999, task="second"))
        cnt = sched.pending_count(("chatA", "userA"))
        await sched.stop()
        return text, cnt

    text, cnt = _run(go())
    assert text.startswith("⚠️")
    assert "上限" in text
    assert cnt == 1  # 第二个没登记上


# ---------------------------------------------------------------------------
# 特性开关：SessionManager 是否建 scheduler
# ---------------------------------------------------------------------------


class _DummyFeishu:
    pass


def test_session_manager_no_scheduler_when_disabled(tmp_path: Path):
    sm = fc.SessionManager(
        docs_root=tmp_path,
        scheduled_followup_config=_cfg(enabled=False),
        feishu=_DummyFeishu(),
    )
    assert sm._followup_scheduler is None


def test_session_manager_no_scheduler_without_feishu(tmp_path: Path):
    sm = fc.SessionManager(
        docs_root=tmp_path,
        scheduled_followup_config=_cfg(enabled=True),
        feishu=None,
    )
    assert sm._followup_scheduler is None


def test_session_manager_builds_scheduler_when_enabled(tmp_path: Path):
    sm = fc.SessionManager(
        docs_root=tmp_path,
        scheduled_followup_config=_cfg(enabled=True),
        feishu=_DummyFeishu(),
    )
    assert isinstance(sm._followup_scheduler, fc.FollowupScheduler)
    assert sm._followup_scheduler.max_pending_per_user == 2
