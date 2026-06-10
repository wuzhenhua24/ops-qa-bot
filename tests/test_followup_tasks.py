"""跟进任务管理（/tasks 查看 + 卡片取消）回归测试。

覆盖：
- FollowupScheduler.list_pending：空列表；多任务按剩余时间升序；字段齐全；
  触发完成后自动移出。
- FollowupScheduler.cancel：成功取消（计数清零 + 后台任务真被掐掉，不会再
  触发）；not_found / not_yours / firing 四种状态。
- /tasks 指令：特性未启用 → 文本提示；无挂起 → 文本提示；有挂起 → 发任务
  列表卡（含任务摘要 + record_id + 取消按钮）。
- handle_followup_cancel_click：非 asker 点击返回 None（原卡不动）；成功后
  返回刷新过的列表卡（带 ✅ 通知行）；record 不存在给"已执行或已取消"提示。
- _followup_tasks_card：firing 条目不给取消按钮；空列表收尾成纯文本。

跑法：
    .venv/bin/python -m pytest tests/test_followup_tasks.py -q
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import ops_qa_bot.feishu_core as fc
from ops_qa_bot.cards import _followup_tasks_card
from ops_qa_bot.config import ScheduledFollowupConfig


def _run(coro):
    return asyncio.run(coro)


def _cfg(**over) -> ScheduledFollowupConfig:
    base = dict(
        enabled=True,
        min_delay_minutes=1,
        max_delay_minutes=120,
        max_pending_per_user=5,
    )
    base.update(over)
    return ScheduledFollowupConfig(**base)


_INDEX_MD = """# 索引

| 组件 | 来源 | 目录 | 负责人 | open_id |
|------|------|------|--------|---------|
| Redis | local | `redis/` | 张三 | ou_owner_redis_0001 |
"""


def _docs_root(tmp_path: Path) -> Path:
    (tmp_path / "INDEX.md").write_text(_INDEX_MD, encoding="utf-8")
    return tmp_path


class _RecordingFeishu:
    def __init__(self):
        self.posts: list[tuple] = []
        self.cards: list[tuple] = []

    async def send_post(self, chat_id, post_content, parent_id=None):
        self.posts.append((chat_id, post_content, parent_id))
        return "mid_post"

    async def send_interactive(self, chat_id, card, parent_id=None):
        self.cards.append((chat_id, card, parent_id))
        return "mid_card"


def _flat(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _sched(feishu=None) -> fc.FollowupScheduler:
    return fc.FollowupScheduler(
        feishu=feishu or _RecordingFeishu(), session_mgr=object(), config=_cfg()
    )


# ---------------------------------------------------------------------------
# list_pending
# ---------------------------------------------------------------------------


def test_list_pending_empty_and_sorted():
    async def go():
        sched = _sched()
        assert sched.list_pending(("c", "u")) == []
        sched.schedule("c", "u", 60, "later task")
        sched.schedule("c", "u", 10, "sooner task")
        sched.schedule("c", "other", 5, "someone else")
        items = sched.list_pending(("c", "u"))
        await sched.stop()
        return items

    items = _run(go())
    assert len(items) == 2  # 别人的不掺进来
    assert items[0]["task"] == "sooner task"
    assert items[1]["task"] == "later task"
    assert items[0]["remaining_minutes"] <= 10
    assert all(
        set(i) >= {"record_id", "task", "remaining_minutes", "firing"}
        for i in items
    )
    assert not items[0]["firing"]


def test_list_pending_removed_after_fire(monkeypatch):
    async def fake_hq(*a, **k):
        return None

    monkeypatch.setattr(fc, "handle_question", fake_hq)

    async def go():
        sched = _sched()
        sched.schedule("c", "u", 0, "instant")
        await asyncio.sleep(0.05)
        items = sched.list_pending(("c", "u"))
        await sched.stop()
        return items

    assert _run(go()) == []


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_pending_task_never_fires(monkeypatch):
    fired = []

    async def fake_hq(*a, **k):
        fired.append(1)

    monkeypatch.setattr(fc, "handle_question", fake_hq)

    async def go():
        sched = _sched()
        sched.schedule("c", "u", 0, "to be cancelled")
        rid = sched.list_pending(("c", "u"))[0]["record_id"]
        status = sched.cancel(rid, ("c", "u"))
        # 给被取消的任务一个调度窗口：如果取消失败它会在这里触发
        await asyncio.sleep(0.05)
        cnt = sched.pending_count(("c", "u"))
        await sched.stop()
        return status, cnt

    status, cnt = _run(go())
    assert status == "cancelled"
    assert cnt == 0
    assert fired == []  # 真的没触发


def test_cancel_status_variants():
    async def go():
        sched = _sched()
        sched.schedule("c", "u", 999, "mine")
        rid = sched.list_pending(("c", "u"))[0]["record_id"]
        not_found = sched.cancel("deadbeef", ("c", "u"))
        not_yours = sched.cancel(rid, ("c", "someone_else"))
        # 模拟已进入执行阶段
        sched._records[rid].firing = True
        firing = sched.cancel(rid, ("c", "u"))
        await sched.stop()
        return not_found, not_yours, firing

    assert _run(go()) == ("not_found", "not_yours", "firing")


# ---------------------------------------------------------------------------
# /tasks 指令
# ---------------------------------------------------------------------------


def test_tasks_command_feature_disabled(tmp_path: Path):
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))  # 无 scheduler
    _run(
        fc.handle_question(
            "chatA", "userA", "/tasks", feishu, sm, parent_msg_id="om_q"
        )
    )
    assert len(feishu.posts) == 1 and len(feishu.cards) == 0
    assert "未启用" in _flat(feishu.posts[0][1])


def test_tasks_command_empty(tmp_path: Path):
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(
        docs_root=_docs_root(tmp_path),
        scheduled_followup_config=_cfg(),
        feishu=feishu,
    )
    _run(
        fc.handle_question(
            "chatA", "userA", "跟进任务", feishu, sm, parent_msg_id="om_q"
        )
    )
    assert len(feishu.posts) == 1 and len(feishu.cards) == 0
    assert "没有挂起的定时跟进" in _flat(feishu.posts[0][1])


def test_tasks_command_lists_pending_with_cancel_buttons(tmp_path: Path):
    async def go():
        feishu = _RecordingFeishu()
        sm = fc.SessionManager(
            docs_root=_docs_root(tmp_path),
            scheduled_followup_config=_cfg(),
            feishu=feishu,
        )
        sched = sm._followup_scheduler
        assert sched is not None
        sched.schedule("chatA", "userA", 30, "检查 ALTER 是否完成")
        await fc.handle_question(
            "chatA", "userA", "/tasks", feishu, sm, parent_msg_id="om_q"
        )
        rid = sched.list_pending(("chatA", "userA"))[0]["record_id"]
        await sched.stop()
        return feishu, rid

    feishu, rid = _run(go())
    assert len(feishu.cards) == 1
    card = _flat(feishu.cards[0][1])
    assert "检查 ALTER 是否完成" in card
    assert rid in card
    assert "followup_cancel" in card and "取消这条跟进" in card
    assert feishu.cards[0][2] == "om_q"  # 引用回原消息


# ---------------------------------------------------------------------------
# handle_followup_cancel_click
# ---------------------------------------------------------------------------


def _sm_with_sched(tmp_path: Path, feishu) -> fc.SessionManager:
    return fc.SessionManager(
        docs_root=_docs_root(tmp_path),
        scheduled_followup_config=_cfg(),
        feishu=feishu,
    )


def test_cancel_click_not_asker_keeps_card(tmp_path: Path):
    async def go():
        sm = _sm_with_sched(tmp_path, _RecordingFeishu())
        sm._followup_scheduler.schedule("c", "asker", 999, "task")
        rid = sm._followup_scheduler.list_pending(("c", "asker"))[0]["record_id"]
        res = fc.handle_followup_cancel_click(rid, "c", "asker", "intruder", sm)
        cnt = sm._followup_scheduler.pending_count(("c", "asker"))
        await sm._followup_scheduler.stop()
        return res, cnt

    res, cnt = _run(go())
    assert res is None  # 原卡不动
    assert cnt == 1  # 没被取消


def test_cancel_click_success_refreshes_list(tmp_path: Path):
    async def go():
        sm = _sm_with_sched(tmp_path, _RecordingFeishu())
        sched = sm._followup_scheduler
        sched.schedule("c", "asker", 999, "task one")
        sched.schedule("c", "asker", 999, "task two")
        rid = next(
            i["record_id"]
            for i in sched.list_pending(("c", "asker"))
            if i["task"] == "task one"
        )
        card = fc.handle_followup_cancel_click(rid, "c", "asker", "asker", sm)
        cnt = sched.pending_count(("c", "asker"))
        await sched.stop()
        return card, cnt

    card, cnt = _run(go())
    flat = _flat(card)
    assert "已取消" in flat
    assert cnt == 1
    # 刷新后的列表只剩 task two
    assert "task two" in flat and "task one" not in flat


def test_cancel_click_last_one_collapses_to_text(tmp_path: Path):
    async def go():
        sm = _sm_with_sched(tmp_path, _RecordingFeishu())
        sched = sm._followup_scheduler
        sched.schedule("c", "asker", 999, "only task")
        rid = sched.list_pending(("c", "asker"))[0]["record_id"]
        card = fc.handle_followup_cancel_click(rid, "c", "asker", "asker", sm)
        await sched.stop()
        return card

    flat = _flat(_run(go()))
    assert "已取消" in flat
    assert "当前没有挂起的定时跟进了" in flat
    assert "followup_cancel" not in flat  # 没有残留按钮


def test_cancel_click_not_found(tmp_path: Path):
    async def go():
        sm = _sm_with_sched(tmp_path, _RecordingFeishu())
        card = fc.handle_followup_cancel_click(
            "deadbeef", "c", "asker", "asker", sm
        )
        await sm._followup_scheduler.stop()
        return card

    assert "已执行完成或已被取消" in _flat(_run(go()))


# ---------------------------------------------------------------------------
# 卡片构造细节
# ---------------------------------------------------------------------------


def test_tasks_card_firing_item_has_no_button():
    card = _followup_tasks_card(
        "asker",
        "chat",
        [
            {"record_id": "r1", "task": "running", "remaining_minutes": 0, "firing": True},
            {"record_id": "r2", "task": "waiting", "remaining_minutes": 5, "firing": False},
        ],
    )
    flat = _flat(card)
    assert "正在执行" in flat
    assert flat.count("followup_cancel") == 1  # 只有 waiting 那条有按钮
    assert "r2" in flat and "r1" not in flat