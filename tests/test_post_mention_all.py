"""post（富文本）形态 @所有人 过滤的回归测试。

主因：重构后 @所有人 过滤全权交给 channel PolicyGate（``respond_to_mention_all
=False``）。但 PolicyGate 的 ``mentioned_all`` 只认纯文本里的 ``@_all`` 占位符——
post 里 @所有人 是 ``{"tag":"at","user_id":"all"}`` 元素，被 SDK 的 post 转换器
渲染成字面 ``@所有人``，"all" 信号丢失，于是 post 形态的 @所有人 漏过 PolicyGate，
bot 把全员广播当问题答了（线上实测：某同事 @所有人 发广播通知 → bot 答题）。

修复：PostContent 分支用 ``_post_mention_all_ids`` 走 raw AST 兜底——纯 @所有人
广播不答题；"@所有人 + 同时单独 @bot" 仍放行（对齐 text 路径语义）。

跑法：
    .venv/bin/python -m tests.test_post_mention_all
"""

from __future__ import annotations

import asyncio
import json

from ops_qa_bot import feishu_core as fc
from lark_oapi.channel.normalize.pipeline import (
    InboundPipeline,
    PipelineConfig,
    PipelineDeps,
)

BOT = "ou_bot_self"


# ---------------------------------------------------------------------------
# 纯函数：_post_mention_all_ids 直接吃 AST
# ---------------------------------------------------------------------------

def test_at_all_only():
    post = {"title": "", "content": [[
        {"tag": "at", "user_id": "all", "user_name": "所有人"},
        {"tag": "text", "text": " 各同事注意"},
    ]]}
    has_all, ids = fc._post_mention_all_ids(post)
    assert has_all is True
    assert ids == set()


def test_at_all_plus_bot():
    post = {"title": "", "content": [[
        {"tag": "at", "user_id": "all", "user_name": "所有人"},
        {"tag": "at", "user_id": BOT, "user_name": "ops-qa-bot"},
        {"tag": "text", "text": " 帮我看下"},
    ]]}
    has_all, ids = fc._post_mention_all_ids(post)
    assert has_all is True
    assert BOT in ids


def test_at_someone_not_all():
    post = {"title": "", "content": [[
        {"tag": "at", "user_id": "ou_zhang", "user_name": "张三"},
        {"tag": "text", "text": " 你看下"},
    ]]}
    has_all, ids = fc._post_mention_all_ids(post)
    assert has_all is False
    assert ids == {"ou_zhang"}


def test_no_at():
    post = {"title": "", "content": [[{"tag": "text", "text": "纯文字"}]]}
    has_all, ids = fc._post_mention_all_ids(post)
    assert has_all is False
    assert ids == set()


def test_locale_wrapped_ast():
    # 飞书最新 wire / SDK fixture 形态：locale key 包一层
    post = {"zh_cn": {"title": "", "content": [[
        {"tag": "at", "user_id": "all"},
        {"tag": "text", "text": " hi"},
    ]]}}
    has_all, ids = fc._post_mention_all_ids(post)
    assert has_all is True


def test_open_id_fallback_field():
    # 历史形态：at 元素用 open_id 字段而非 user_id
    post = {"content": [[{"tag": "at", "open_id": "all"}]]}
    has_all, _ = fc._post_mention_all_ids(post)
    assert has_all is True


def test_at_all_real_wire_underscore():
    # 线上真实 wire（2026-06-24 [DEBUG raw-post-ast] 抓的）：@所有人 的 user_id
    # 是 "@_all" 而非文档约定的 "all"——关键回归，最初漏拦就是因为只认了 "all"
    post = {"content": [[
        {"tag": "at", "user_id": "@_all", "user_name": "所有人", "style": []},
        {"tag": "text", "text": " ", "style": []},
    ]]}
    has_all, ids = fc._post_mention_all_ids(post)
    assert has_all is True
    assert ids == set()


# ---------------------------------------------------------------------------
# 端到端：dispatch_inbound 的 PostContent 分支 drop / 放行
# ---------------------------------------------------------------------------

class _FakeFeishu:
    def __init__(self, bot_ids):
        self._bot_ids = set(bot_ids)

    @property
    def bot_self_ids(self):
        return self._bot_ids


def _dispatch_post(content_dict, *, bot_ids=(BOT,), mentions=None):
    """构造真实 InboundMessage（走 SDK normalize）→ dispatch_inbound，
    返回 handle_post_question 是否被调用（True=放行答题，False=被 drop）。"""
    pipe = InboundPipeline(PipelineConfig(), PipelineDeps())
    event = {
        "message_id": "om_x", "chat_id": "oc", "chat_type": "group",
        "message_type": "post", "content": json.dumps(content_dict),
        "mentions": mentions or [],
    }
    sender = {"sender_id": {"open_id": "ou_asker"}, "sender_type": "user"}

    called: list[bool] = []

    async def _stub(*_a, **_k):
        called.append(True)

    orig = fc.handle_post_question
    fc.handle_post_question = _stub
    try:
        async def _go():
            inbound = await pipe.process("e", event, sender)
            await fc.dispatch_inbound(inbound, _FakeFeishu(bot_ids), None)
        asyncio.run(_go())
    finally:
        fc.handle_post_question = orig
    return bool(called)


def test_dispatch_real_wire_at_all_image_dropped():
    # 线上那条 @所有人 图文消息的真实结构（从 [DEBUG raw-post-ast] 日志原样还原）：
    # @所有人(user_id="@_all") + 图 + 文字，未单独 @bot → 不答题。
    # 这是最初 user_id=="all" 漏拦的线上 case，锁死。
    handled = _dispatch_post({"title": "", "content": [
        [{"tag": "at", "user_id": "@_all", "user_name": "所有人", "style": []},
         {"tag": "text", "text": " ", "style": []}],
        [{"tag": "img", "image_key": "img_v3_x", "width": 1263, "height": 347}],
        [{"tag": "text", "text": " 文字", "style": []}],
    ]})
    assert handled is False


def test_dispatch_pure_at_all_dropped():
    # 线上那条纯文本广播的形态：纯 @所有人 + 文字（+图），未单独 @bot → 不答题
    handled = _dispatch_post({"content": [[
        {"tag": "at", "user_id": "all", "user_name": "所有人"},
        {"tag": "text", "text": " 各同事，禁止压测，分支环境请关闭回收。"},
        {"tag": "img", "image_key": "img_v3_x"},
    ]]})
    assert handled is False


def test_dispatch_at_all_plus_bot_answered():
    # @所有人 + 同时单独 @bot → 当定向求助，仍答题（对齐 text 路径）
    handled = _dispatch_post(
        {"content": [[
            {"tag": "at", "user_id": "all", "user_name": "所有人"},
            {"tag": "at", "user_id": BOT, "user_name": "ops-qa-bot"},
            {"tag": "text", "text": " 帮我看下这个报错"},
        ]]},
        mentions=[{"key": "@_user_1", "id": {"open_id": BOT}, "name": "bot"}],
    )
    assert handled is True


def test_dispatch_at_all_plus_bot_answered_even_if_mentions_empty():
    # 兜底：飞书未填 mentions 数组时，仍靠 AST 里的 bot at 元素识别 @bot
    handled = _dispatch_post(
        {"content": [[
            {"tag": "at", "user_id": "all"},
            {"tag": "at", "user_id": BOT},
            {"tag": "text", "text": " 看下"},
        ]]},
        mentions=[],
    )
    assert handled is True


def test_dispatch_no_at_all_answered():
    # 普通 post（无 @所有人）：正常答题，不受影响
    handled = _dispatch_post({"content": [[
        {"tag": "text", "text": "这个接口怎么调用"},
    ]]})
    assert handled is True


def test_dispatch_at_all_unresolved_bot_identity_dropped():
    # bot identity 未解析（bot_self_ids 为空）时 fail-safe：@所有人 一律 drop，
    # 宁可漏答也不误答广播
    handled = _dispatch_post(
        {"content": [[
            {"tag": "at", "user_id": "all"},
            {"tag": "at", "user_id": BOT},
            {"tag": "text", "text": " 看下"},
        ]]},
        bot_ids=(),
    )
    assert handled is False


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
