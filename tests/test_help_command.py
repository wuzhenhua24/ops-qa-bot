"""/help 指令回归测试：触发词 → 直接回能力清单，不进答题流程。

覆盖：
- 触发词匹配：/help、help、帮助、大小写混排都认；普通问题不误触发。
- handle_question 短路：命中后只发一条 post（@ 提问者 + 帮助文案），不创建
  session、不消费"上下文已过期"的一次性提示。
- _help_text 动态拼装：可选工具（网关链路 / 数据库分析 / 参数变更 / 定时跟进）
  没启用就不出现在清单里；组件清单来自 INDEX.md，解析不到就降级不列。
- _index_component_names：组件表「组件」列保序去重；无 INDEX.md 返回空。

跑法：
    .venv/bin/python -m pytest tests/test_help_command.py -q
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import ops_qa_bot.feishu_core as fc
from ops_qa_bot.config import (
    DatabaseConfig,
    DbCreds,
    GatewayTraceConfig,
    ScheduledFollowupConfig,
)


def _run(coro):
    return asyncio.run(coro)


_INDEX_MD = """# 运维文档索引

## 组件目录

| 组件 | 来源 | 目录 | 负责人 | open_id |
|------|------|------|--------|---------|
| Redis | local | `redis/` | 张三 | ou_owner_redis_0001 |
| MySQL | local | `mysql/` | 李四 | ou_owner_mysql_0002 |
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
        return "mid_help"

    async def update_post(self, message_id, post_content):
        self.updates.append((message_id, post_content))
        return True

    async def send_interactive(self, chat_id, card, parent_id=None):
        self.cards.append((chat_id, card, parent_id))
        return "mid_card"


def _db_cfg_full() -> DatabaseConfig:
    return DatabaseConfig(
        allowed_hosts=("10.10.0.0/16",),
        mysql_ro=DbCreds(user="ro", password="pw"),
        mysql_admin=DbCreds(user="admin", password="pw"),
        admin_open_ids=("ou_dba_0001",),
    )


# ---------------------------------------------------------------------------
# _index_component_names
# ---------------------------------------------------------------------------


def test_component_names_parsed_in_order_dedup(tmp_path: Path):
    names = fc._index_component_names(_docs_root(tmp_path))
    assert names == ["Redis", "MySQL"]


def test_component_names_missing_index_returns_empty(tmp_path: Path):
    assert fc._index_component_names(tmp_path) == []


# ---------------------------------------------------------------------------
# _help_text 动态拼装
# ---------------------------------------------------------------------------


def test_help_text_baseline_lists_core_only(tmp_path: Path):
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    text = fc._help_text(sm)
    # 核心能力 + 指令永远在
    assert "文档问答" in text
    assert "Redis、MySQL" in text
    assert "实时诊断" in text
    assert "/reset" in text and "/help" in text
    # 可选工具一个没启用：清单里不该出现
    assert "Hi-Trace-Id" not in text
    assert "数据库实时分析" not in text
    assert "参数变更申请" not in text
    assert "定时跟进" not in text
    # idle_ttl 默认 1800s → 30 分钟
    assert "30 分钟" in text


def test_help_text_includes_enabled_optional_tools(tmp_path: Path):
    sm = fc.SessionManager(
        docs_root=_docs_root(tmp_path),
        gateway_trace_config=GatewayTraceConfig(base_url="http://gw.local"),
        database_config=_db_cfg_full(),
        scheduled_followup_config=ScheduledFollowupConfig(enabled=True),
        feishu=_RecordingFeishu(),
    )
    text = fc._help_text(sm)
    assert "Hi-Trace-Id" in text
    assert "数据库实时分析" in text
    assert "参数变更申请" in text
    assert "定时跟进" in text


def test_help_text_db_readonly_without_admin(tmp_path: Path):
    # 只读账号在、admin 链路不齐：只列分析，不列变更申请
    cfg = DatabaseConfig(
        allowed_hosts=("10.10.0.0/16",),
        mysql_ro=DbCreds(user="ro", password="pw"),
    )
    sm = fc.SessionManager(
        docs_root=_docs_root(tmp_path),
        database_config=cfg,
        feishu=_RecordingFeishu(),
    )
    text = fc._help_text(sm)
    assert "数据库实时分析" in text
    assert "参数变更申请" not in text


# ---------------------------------------------------------------------------
# handle_question 短路
# ---------------------------------------------------------------------------


def _ask_help(tmp_path: Path, question: str):
    feishu = _RecordingFeishu()
    sm = fc.SessionManager(docs_root=_docs_root(tmp_path))
    _run(
        fc.handle_question(
            "chatA", "userA", question, feishu, sm, parent_msg_id="om_q1"
        )
    )
    return feishu, sm


def test_help_trigger_replies_without_session(tmp_path: Path):
    feishu, sm = _ask_help(tmp_path, "/help")
    assert len(feishu.posts) == 1
    chat_id, post, parent_id = feishu.posts[0]
    assert chat_id == "chatA"
    # 引用回原消息 + @ 提问者 + 帮助正文
    assert parent_id == "om_q1"
    flat = repr(post)
    assert "userA" in flat
    assert "我能做什么" in flat
    # 不进答题流程：没有创建任何 session
    assert sm.active_count() == 0


def test_help_trigger_case_insensitive(tmp_path: Path):
    for q in ("HELP", "Help", "帮助"):
        feishu, _ = _ask_help(tmp_path, q)
        assert len(feishu.posts) == 1, q
        assert "我能做什么" in repr(feishu.posts[0][1]), q


def test_help_not_triggered_by_normal_question(tmp_path: Path, monkeypatch):
    # 含 help 字样但不是独立指令的正常问题：不该走帮助短路。
    # 为避免真起 agent，断言它走到了占位消息那一步（session 路径）即可。
    sentinel = {}

    async def fake_get(self, key):
        sentinel["reached"] = True
        raise RuntimeError("stop before real bot")

    monkeypatch.setattr(fc.SessionManager, "get", fake_get)
    feishu, _ = _ask_help(tmp_path, "helpdesk 平台登录报错怎么办")
    assert sentinel.get("reached") is True
    # 第一条是占位消息而不是帮助
    assert "我能做什么" not in repr(feishu.posts[0][1])
