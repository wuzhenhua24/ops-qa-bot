"""取消在途答题（/cancel 指令）回归测试。

覆盖：
- SessionManager 在途登记表：register / unregister / cancel_inflight（计数 +
  收集需 interrupt 的 bot，排队中无 bot 只翻标记）。
- /cancel 无在途：友好提示"无需取消"。
- 取消运行中的答题：bot.interrupt() 被调用、流收尾后占位刷成"已取消"、结果
  丢弃、不发反馈卡；取消指令本身得到回执；feedback 日志落 event=cancelled。
- 取消排队中的答题：bot.ask 根本不被调用（拿到锁后直接放弃），占位同样收尾。
- 跨用户隔离：B 发 /cancel 取消不掉 A 的在途答题（按 (chat, user) 隔离，
  B 只得到"无需取消"），A 自己取消照常生效。
- 答题正常结束后 /cancel：在途已注销，回"无需取消"。

跑法：
    .venv/bin/python -m pytest tests/test_cancel_question.py -q
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import ops_qa_bot.feishu_core as fc


def _run(coro):
    return asyncio.run(coro)


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
        self.updates: list[tuple] = []
        self.cards: list[tuple] = []

    async def send_post(self, chat_id, post_content, parent_id=None):
        self.posts.append((chat_id, post_content, parent_id))
        return f"mid_post_{len(self.posts)}"

    async def update_post(self, message_id, post_content):
        self.updates.append((message_id, post_content))
        return True

    async def send_interactive(self, chat_id, card, parent_id=None):
        self.cards.append((chat_id, card, parent_id))
        return "mid_card"


def _flat(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


class _HangingBot:
    """模拟跑着的 agent：吐一段文本后挂起，直到 interrupt 才收尾（不带 done）。"""

    def __init__(self):
        self._interrupted = asyncio.Event()
        self.interrupt_calls = 0

    async def interrupt(self):
        self.interrupt_calls += 1
        self._interrupted.set()

    async def ask(self, question, images=None):
        yield {"type": "text", "text": "查到一半的内容"}
        await self._interrupted.wait()


class _Entry:
    def __init__(self, bot):
        self.lock = asyncio.Lock()
        self.last_used = 0.0
        self.bot = bot


# ---------------------------------------------------------------------------
# 在途登记表
# ---------------------------------------------------------------------------


def test_inflight_registry_roundtrip(tmp_path: Path):
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    key = ("c", "u")
    assert sm.cancel_inflight(key) == (0, [])

    s1 = fc._InflightScope()
    s2 = fc._InflightScope()
    bot = object()
    s2.bot = bot
    id1 = sm.register_inflight(key, s1)
    sm.register_inflight(key, s2)

    n, bots = sm.cancel_inflight(key)
    assert n == 2
    assert bots == [bot]  # 排队中的 s1 没绑 bot，只翻标记
    assert s1.cancelled and s2.cancelled

    sm.unregister_inflight(key, id1)
    n, _ = sm.cancel_inflight(key)
    assert n == 1  # 只剩 s2


# ---------------------------------------------------------------------------
# /cancel 指令
# ---------------------------------------------------------------------------


def test_cancel_with_nothing_inflight(tmp_path: Path):
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    _run(
        fc.handle_question(
            "chatA", "userA", "/cancel", feishu, sm, parent_msg_id="om_c"
        )
    )
    assert len(feishu.posts) == 1
    assert "无需取消" in _flat(feishu.posts[0][1])


def test_cancel_running_answer(tmp_path: Path, monkeypatch, caplog):
    bot = _HangingBot()
    entry = _Entry(bot)

    async def fake_get(self, key):
        return entry

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))

    async def go():
        task = asyncio.create_task(
            fc.handle_question(
                "chatA", "userA", "redis 怎么扩容", feishu, sm,
                parent_msg_id="om_q",
            )
        )
        await asyncio.sleep(0.05)  # 让答题进入流式挂起阶段
        with caplog.at_level(logging.INFO, logger="ops_qa_bot.feedback"):
            await fc.handle_question(
                "chatA", "userA", "取消", feishu, sm, parent_msg_id="om_c"
            )
            await task

    _run(go())
    assert bot.interrupt_calls == 1
    # posts: 占位 + 取消回执
    assert len(feishu.posts) == 2
    assert "已请求取消" in _flat(feishu.posts[1][1])
    # 占位最终被刷成"已取消"，半截答案被丢弃
    final = _flat(feishu.updates[-1][1])
    assert "已取消" in final
    assert "查到一半" not in final
    # 不发反馈卡/追问卡
    assert feishu.cards == []
    # feedback 日志落 cancelled 事件
    cancelled = [
        r for r in caplog.records
        if r.name == "ops_qa_bot.feedback" and '"event": "cancelled"' in r.message
    ]
    assert len(cancelled) == 1


def test_cancel_queued_answer_never_asks(tmp_path: Path, monkeypatch):
    class _MustNotAskBot:
        async def interrupt(self):
            raise AssertionError("排队中的不该 interrupt")

        async def ask(self, question, images=None):
            raise AssertionError("被取消的排队问题不该开始答题")
            yield  # pragma: no cover

    entry = _Entry(_MustNotAskBot())

    async def fake_get(self, key):
        return entry

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))

    async def go():
        await entry.lock.acquire()  # 模拟前一条问题占着锁
        task = asyncio.create_task(
            fc.handle_question(
                "chatA", "userA", "第二条问题", feishu, sm, parent_msg_id="om_q2"
            )
        )
        await asyncio.sleep(0.05)
        # 排队期间取消（scope 未绑 bot → 不 interrupt，只翻标记）
        await fc.handle_question(
            "chatA", "userA", "/cancel", feishu, sm, parent_msg_id="om_c"
        )
        entry.lock.release()
        await task

    _run(go())
    assert "已请求取消" in _flat(feishu.posts[-1][1])
    assert "已取消" in _flat(feishu.updates[-1][1])
    assert feishu.cards == []


def test_cancel_by_other_user_does_not_touch_askers_question(
    tmp_path: Path, monkeypatch
):
    """B 发 /cancel 取消不掉 A 的在途答题——取消按 (chat, user) 隔离。"""
    bot = _HangingBot()
    entry = _Entry(bot)

    async def fake_get(self, key):
        return entry

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))

    async def go():
        task = asyncio.create_task(
            fc.handle_question(
                "chatA", "asker", "redis 怎么扩容", feishu, sm,
                parent_msg_id="om_q",
            )
        )
        await asyncio.sleep(0.05)  # A 的答题进入流式挂起阶段
        # B 在同一个群里发 /cancel：查的是 (chatA, intruder) 名下的在途
        await fc.handle_question(
            "chatA", "intruder", "/cancel", feishu, sm, parent_msg_id="om_c"
        )
        assert bot.interrupt_calls == 0  # A 的 agent 没有被打断
        # A 自己取消，正常生效
        await fc.handle_question(
            "chatA", "asker", "取消", feishu, sm, parent_msg_id="om_c2"
        )
        await task

    _run(go())
    assert bot.interrupt_calls == 1  # 只有 asker 自己那次生效
    # B 得到的是"无需取消"，且回执 @ 的是 B 自己
    intruder_reply = _flat(feishu.posts[1][1])
    assert "无需取消" in intruder_reply
    assert "intruder" in intruder_reply
    # A 的占位最终因 A 自己的取消而收尾
    assert "已取消" in _flat(feishu.updates[-1][1])


def test_cancel_after_done_is_noop(tmp_path: Path, monkeypatch):
    class _QuickBot:
        async def interrupt(self):
            raise AssertionError("答完之后不该 interrupt")

        async def ask(self, question, images=None):
            yield {"type": "text", "text": "秒答"}
            yield {"type": "done", "subtype": "success"}

    entry = _Entry(_QuickBot())

    async def fake_get(self, key):
        return entry

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))

    async def go():
        await fc.handle_question(
            "chatA", "userA", "快问题", feishu, sm, parent_msg_id="om_q"
        )
        await fc.handle_question(
            "chatA", "userA", "/cancel", feishu, sm, parent_msg_id="om_c"
        )

    _run(go())
    # 第一条正常答完（含反馈卡），/cancel 落空
    assert "秒答" in _flat(feishu.updates[0][1])
    assert len(feishu.cards) == 1
    assert "无需取消" in _flat(feishu.posts[-1][1])