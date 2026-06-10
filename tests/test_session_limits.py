"""资源保险丝回归测试：会话数上限（max_sessions）+ 单轮步数上限（max_turns）。

覆盖：
- SessionManager 会话数到顶：驱逐最闲的**空闲**会话腾位（正在答题的不动）；
  全忙时抛 SessionLimitError；已有会话复用不受 cap 影响。
- handle_question 收到 SessionLimitError：回"稍后再试"友好提示，不发泛化错误。
- max_turns 透传链：SessionManager → OpsQABot → ClaudeAgentOptions；<=0 归一成
  不限（None）。
- 答题命中 max_turns（ResultMessage.subtype == "error_max_turns"）：答案末尾追加
  "结论可能不完整"提示。
- load_config：[session].max_sessions / [agent].max_turns 解析 + 环境变量覆盖。

跑法：
    .venv/bin/python -m pytest tests/test_session_limits.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import ops_qa_bot.feishu_core as fc
from ops_qa_bot.bot import OpsQABot
from ops_qa_bot.config import load_config


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


class _FakeBot:
    """顶替 fc.OpsQABot：不起 claude 子进程，记录构造参数和关闭状态。"""

    instances: list["_FakeBot"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        _FakeBot.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True


@pytest.fixture(autouse=True)
def _clear_fake_bots():
    _FakeBot.instances.clear()
    yield


class _RecordingFeishu:
    def __init__(self):
        self.posts: list[tuple] = []
        self.updates: list[tuple] = []
        self.cards: list[tuple] = []

    async def send_post(self, chat_id, post_content, parent_id=None):
        self.posts.append((chat_id, post_content, parent_id))
        return "mid_post"

    async def update_post(self, message_id, post_content):
        self.updates.append((message_id, post_content))
        return True

    async def send_interactive(self, chat_id, card, parent_id=None):
        self.cards.append((chat_id, card, parent_id))
        return "mid_card"


# ---------------------------------------------------------------------------
# SessionManager 会话数上限
# ---------------------------------------------------------------------------


def test_cap_evicts_idlest_unlocked(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fc, "OpsQABot", _FakeBot)

    async def go():
        sm = fc.SessionManager(docs_root=_docs_root(tmp_path), max_sessions=2)
        ea = await sm.get(("c", "alice"))
        eb = await sm.get(("c", "bob"))
        # alice 更闲
        ea.last_used = eb.last_used - 100
        await sm.get(("c", "carol"))
        return sm, ea

    sm, ea = _run(go())
    assert sm.active_count() == 2
    assert ea.bot.closed  # alice 被驱逐并关闭
    snapshot_users = {u for (_, u) in sm._sessions}
    assert snapshot_users == {"bob", "carol"}


def test_cap_all_busy_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fc, "OpsQABot", _FakeBot)

    async def go():
        sm = fc.SessionManager(docs_root=_docs_root(tmp_path), max_sessions=2)
        ea = await sm.get(("c", "alice"))
        eb = await sm.get(("c", "bob"))
        # 两个会话都在答题中（lock 被占）
        await ea.lock.acquire()
        await eb.lock.acquire()
        try:
            with pytest.raises(fc.SessionLimitError):
                await sm.get(("c", "carol"))
        finally:
            ea.lock.release()
            eb.lock.release()
        # 拒了之后没有新建任何会话
        assert sm.active_count() == 2

    _run(go())


def test_cap_existing_session_reuse_not_affected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fc, "OpsQABot", _FakeBot)

    async def go():
        sm = fc.SessionManager(docs_root=_docs_root(tmp_path), max_sessions=1)
        e1 = await sm.get(("c", "alice"))
        await e1.lock.acquire()
        try:
            # 已有会话复用：即使到顶且唯一会话在忙，也不该抛
            e2 = await sm.get(("c", "alice"))
            assert e2 is e1
        finally:
            e1.lock.release()

    _run(go())


def test_handle_question_session_limit_friendly_reply(tmp_path: Path, monkeypatch):
    async def raise_limit(self, key):
        raise fc.SessionLimitError("cap")

    monkeypatch.setattr(fc.SessionManager, "get", raise_limit)
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    _run(
        fc.handle_question(
            "chatA", "userA", "redis 内存爆了", feishu, sm, parent_msg_id="om_q"
        )
    )
    # 占位被刷成"稍后再试"提示，而不是泛化错误
    assert len(feishu.updates) == 1
    flat = repr(feishu.updates[0][1])
    assert "上限" in flat and "再发一次" in flat
    assert "出错" not in flat


# ---------------------------------------------------------------------------
# max_turns 透传 + 命中提示
# ---------------------------------------------------------------------------


def test_max_turns_plumbed_to_bot(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(fc, "OpsQABot", _FakeBot)

    async def go():
        sm = fc.SessionManager(docs_root=_docs_root(tmp_path), max_turns=30)
        await sm.get(("c", "alice"))

    _run(go())
    assert _FakeBot.instances[0].kwargs["max_turns"] == 30


def test_opsqabot_max_turns_in_options(tmp_path: Path):
    docs = _docs_root(tmp_path)
    assert OpsQABot(docs_root=docs, max_turns=30)._options.max_turns == 30
    # <=0 归一成不限
    assert OpsQABot(docs_root=docs, max_turns=0)._options.max_turns is None
    assert OpsQABot(docs_root=docs)._options.max_turns is None


def test_max_turns_hit_appends_incomplete_notice(tmp_path: Path, monkeypatch):
    class _Entry:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.last_used = 0.0

            class _B:
                async def ask(self, question, images=None):
                    yield {"type": "text", "text": "查到一半…"}
                    yield {
                        "type": "done",
                        "num_turns": 30,
                        "subtype": "error_max_turns",
                    }

            self.bot = _B()

    entry = _Entry()

    async def fake_get(self, key):
        return entry

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    _run(
        fc.handle_question(
            "chatA", "userA", "redis 内存爆了", feishu, sm, parent_msg_id="om_q"
        )
    )
    flat = repr(feishu.updates[0][1])
    assert "查到一半" in flat
    assert "可能不完整" in flat


def test_normal_answer_no_incomplete_notice(tmp_path: Path, monkeypatch):
    class _Entry:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.last_used = 0.0

            class _B:
                async def ask(self, question, images=None):
                    yield {"type": "text", "text": "答案如下"}
                    yield {"type": "done", "num_turns": 4, "subtype": "success"}

            self.bot = _B()

    entry = _Entry()

    async def fake_get(self, key):
        return entry

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    _run(
        fc.handle_question(
            "chatA", "userA", "redis 内存爆了", feishu, sm, parent_msg_id="om_q"
        )
    )
    assert "可能不完整" not in repr(feishu.updates[0][1])


# ---------------------------------------------------------------------------
# load_config 解析
# ---------------------------------------------------------------------------


_MIN_TOML = """
docs_root = "./docs"

[feishu]
app_id = "cli_x"
app_secret = "s"
"""


def test_config_defaults(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SESSION_MAX_SESSIONS", raising=False)
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)
    p = tmp_path / "config.toml"
    p.write_text(_MIN_TOML, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.session_max_sessions == 50
    assert cfg.agent_max_turns == 30


def test_config_file_and_env_override(tmp_path: Path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        _MIN_TOML + "\n[session]\nmax_sessions = 8\n\n[agent]\nmax_turns = 12\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SESSION_MAX_SESSIONS", raising=False)
    monkeypatch.delenv("AGENT_MAX_TURNS", raising=False)
    cfg = load_config(p)
    assert cfg.session_max_sessions == 8
    assert cfg.agent_max_turns == 12

    monkeypatch.setenv("SESSION_MAX_SESSIONS", "99")
    monkeypatch.setenv("AGENT_MAX_TURNS", "0")
    cfg = load_config(p)
    assert cfg.session_max_sessions == 99
    assert cfg.agent_max_turns == 0  # 0 = 不限，由 OpsQABot 归一成 None
