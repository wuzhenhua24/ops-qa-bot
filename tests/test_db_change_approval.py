"""数据库参数变更审批流程的回归测试（feishu_core 侧）。

覆盖：
- make_db_change_submitter：发确认卡 + 登记 pending + 返回确认文字；卡发失败则
  不登记 pending、返回失败文字。
- handle_db_change_confirm：非管理员点击保持卡片可见、pending 不动；管理员点击
  执行（fake DatabaseClient）+ 替换 ack 卡 + @asker 通知 + pending 清掉；执行失败
  也清 pending（已先 pop）+ 通知 asker 失败；过期/缺参走 ack。
- handle_db_change_reject：管理员驳回清 pending + 通知 asker；非管理员保持卡片。
- _db_admins：直接读 admin_open_ids（与文档负责人解耦）。

跑法：
    .venv/bin/python -m pytest tests/test_db_change_approval.py -q
"""

from __future__ import annotations

import asyncio
import json

import ops_qa_bot.feishu_core as fc
from ops_qa_bot.config import DatabaseConfig, DbCreds
from ops_qa_bot.db_query import DatabaseQueryError, DbChangeRequest


def _run(coro):
    return asyncio.run(coro)


def _mk_req(**over) -> DbChangeRequest:
    base = dict(
        kind="ob_mysql", db_type="oceanbase", mode="mysql",
        host="172.28.65.2", port=2883, tenant="bd_p1", cluster="hx_new",
        param="memory_limit", new_value="8G",
        sql="ALTER SYSTEM SET memory_limit = '8G'", current_value="4G",
    )
    base.update(over)
    return DbChangeRequest(**base)


def _admin_cfg(**over) -> DatabaseConfig:
    base = dict(
        allowed_hosts=("172.28.0.0/16", "10.0.0.0/8"),
        admin_open_ids=("ou_admin",),
        mysql_admin=DbCreds(user="root", password="p"),
        ob_mysql_admin=DbCreds(user="root", password="p"),
    )
    base.update(over)
    return DatabaseConfig(**base)


class _FakeFeishu:
    def __init__(self, card_msg_id: str | None = "msg1"):
        self._card_msg_id = card_msg_id
        self.cards: list = []
        self.posts: list = []

    async def send_interactive(self, chat_id, card, *, parent_id=None):
        self.cards.append((chat_id, card))
        return self._card_msg_id

    async def send_post(self, chat_id, post, *, parent_id=None):
        self.posts.append((chat_id, post))
        return "p1"


class _FakeAdminClient:
    """替换 fc.DatabaseClient：run_admin 返回固定文本或抛。"""

    instances: list = []

    def __init__(self, config):
        self.config = config
        self.ran: list = []
        _FakeAdminClient.instances.append(self)

    async def run_admin(self, req):
        self.ran.append(req)
        if isinstance(getattr(self, "_exc", None), Exception):
            raise self._exc
        return "（执行成功，无返回行。）"


def _card_text(card: dict) -> str:
    """把卡片里所有 markdown 段的文字拼起来，便于断言。"""
    out = []
    for el in card.get("body", {}).get("elements", []):
        if el.get("tag") == "markdown":
            out.append(el.get("content", ""))
    return "\n".join(out)


def _has_action(card: dict, action: str) -> bool:
    return action in json.dumps(card, ensure_ascii=False)


def _register(req, *, chat="oc_chat", asker="ou_asker", cid="cid-test") -> str:
    fc._pending_db_changes[cid] = {
        "req": req, "chat_id": chat, "asker_id": asker,
        "card_msg_id": "m1", "created_at": 0.0,
    }
    return cid


# ---------------------------------------------------------------------------
# _db_admins
# ---------------------------------------------------------------------------

def test_db_admins_reads_open_ids():
    assert fc._db_admins(_admin_cfg()) == {"ou_admin"}
    assert fc._db_admins(None) == set()
    assert fc._db_admins(_admin_cfg(admin_open_ids=())) == set()


# ---------------------------------------------------------------------------
# submitter
# ---------------------------------------------------------------------------

def test_submitter_sends_card_and_registers_pending():
    fc._pending_db_changes.clear()
    feishu = _FakeFeishu()
    submit = fc.make_db_change_submitter(feishu, "oc_chat", "ou_asker")
    text = _run(submit(_mk_req()))
    assert "审批" in text or "确认" in text
    assert len(feishu.cards) == 1
    assert len(fc._pending_db_changes) == 1
    cid = next(iter(fc._pending_db_changes))
    # 卡片按钮 value 带 change_id，且确认/驳回两个 action 都在
    assert cid in json.dumps(feishu.cards[0][1], ensure_ascii=False)
    assert _has_action(feishu.cards[0][1], "db_change_confirm")
    assert _has_action(feishu.cards[0][1], "db_change_reject")
    # 登记内容
    ctx = fc._pending_db_changes[cid]
    assert ctx["asker_id"] == "ou_asker" and ctx["chat_id"] == "oc_chat"


def test_submitter_card_send_fail_no_pending():
    fc._pending_db_changes.clear()
    submit = fc.make_db_change_submitter(_FakeFeishu(card_msg_id=None), "c", "a")
    text = _run(submit(_mk_req()))
    assert "失败" in text
    assert len(fc._pending_db_changes) == 0


# ---------------------------------------------------------------------------
# confirm
# ---------------------------------------------------------------------------

def test_confirm_non_admin_keeps_card_and_pending():
    fc._pending_db_changes.clear()
    cid = _register(_mk_req())
    feishu = _FakeFeishu()
    card = _run(fc.handle_db_change_confirm(cid, "ou_other", _admin_cfg(), feishu=feishu))
    # 原确认卡（带按钮）返回，pending 不动，没执行没通知
    assert _has_action(card, "db_change_confirm")
    assert cid in fc._pending_db_changes
    assert feishu.posts == []


def test_confirm_admin_executes_notifies_and_pops():
    fc._pending_db_changes.clear()
    _FakeAdminClient.instances.clear()
    req = _mk_req()
    cid = _register(req)
    feishu = _FakeFeishu()
    orig = fc.DatabaseClient
    fc.DatabaseClient = _FakeAdminClient
    try:
        card = _run(
            fc.handle_db_change_confirm(cid, "ou_admin", _admin_cfg(), feishu=feishu)
        )
    finally:
        fc.DatabaseClient = orig
    assert "已执行" in _card_text(card)
    assert cid not in fc._pending_db_changes  # 已 pop
    assert _FakeAdminClient.instances[0].ran[0] is req  # 真去执行了
    assert len(feishu.posts) == 1  # 通知 asker


def test_confirm_exec_failure_pops_and_notifies_failure():
    fc._pending_db_changes.clear()
    _FakeAdminClient.instances.clear()
    cid = _register(_mk_req())
    feishu = _FakeFeishu()

    class _FailClient(_FakeAdminClient):
        def __init__(self, config):
            super().__init__(config)
            self._exc = DatabaseQueryError("boom", "数据库拒绝：参数只读")

    orig = fc.DatabaseClient
    fc.DatabaseClient = _FailClient
    try:
        card = _run(
            fc.handle_db_change_confirm(cid, "ou_admin", _admin_cfg(), feishu=feishu)
        )
    finally:
        fc.DatabaseClient = orig
    assert "执行失败" in _card_text(card)
    assert cid not in fc._pending_db_changes  # 失败也已 pop（执行前 pop）
    assert len(feishu.posts) == 1  # 通知 asker 失败


def test_confirm_expired_returns_ack():
    fc._pending_db_changes.clear()
    card = _run(fc.handle_db_change_confirm("nope", "ou_admin", _admin_cfg()))
    assert "过期" in _card_text(card)


def test_confirm_missing_change_id_returns_ack():
    card = _run(fc.handle_db_change_confirm(None, "ou_admin", _admin_cfg()))
    assert "缺失" in _card_text(card)


# ---------------------------------------------------------------------------
# reject
# ---------------------------------------------------------------------------

def test_reject_admin_drops_and_notifies():
    fc._pending_db_changes.clear()
    cid = _register(_mk_req())
    feishu = _FakeFeishu()
    card = _run(fc.handle_db_change_reject(cid, "ou_admin", _admin_cfg(), feishu=feishu))
    assert "驳回" in _card_text(card)
    assert cid not in fc._pending_db_changes
    assert len(feishu.posts) == 1


def test_reject_non_admin_keeps_card():
    fc._pending_db_changes.clear()
    cid = _register(_mk_req())
    feishu = _FakeFeishu()
    card = _run(fc.handle_db_change_reject(cid, "ou_other", _admin_cfg(), feishu=feishu))
    assert _has_action(card, "db_change_confirm")  # 原卡返回
    assert cid in fc._pending_db_changes
    assert feishu.posts == []
