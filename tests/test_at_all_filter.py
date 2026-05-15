"""群里 @所有人 不应触发 bot 答题的回归测试。

背景：群权限本来就是"必须 @ bot 才推事件"，所以未 @ bot 的普通群消息根本
不会到 bot；唯一漏网的是 @所有人——飞书 @_all 把 bot 也算 mention，事件
会被推过来。`has_at_all_mention` 用来识别这种全员通知并 drop。

跑法：
    .venv/bin/python -m tests.test_at_all_filter
"""

from __future__ import annotations

from ops_qa_bot.feishu_core import has_at_all_mention


def test_empty_mentions_false():
    assert has_at_all_mention(None) is False
    assert has_at_all_mention([]) is False


def test_only_bot_mention_false():
    # 正常 @bot：key 是 @_user_N，open_id 不是 all
    mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "bot"}]
    assert has_at_all_mention(mentions) is False


def test_at_all_by_key():
    mentions = [{"key": "@_all", "id": {"open_id": "all"}, "name": "所有人"}]
    assert has_at_all_mention(mentions) is True


def test_at_all_by_open_id():
    # 防御：飞书未来若把 key 重命名，仍可凭 id.open_id == "all" 兜住
    mentions = [{"key": "@_x", "id": {"open_id": "all"}, "name": "所有人"}]
    assert has_at_all_mention(mentions) is True


def test_at_all_plus_other_user_still_true():
    mentions = [
        {"key": "@_user_1", "id": {"open_id": "ou_a"}, "name": "A"},
        {"key": "@_all", "id": {"open_id": "all"}, "name": "所有人"},
    ]
    assert has_at_all_mention(mentions) is True


def test_object_style_mention_supported():
    # WS SDK 走的是带 .key / .id.open_id 的对象，不是 dict
    class _Id:
        def __init__(self, oid: str):
            self.open_id = oid

    class _M:
        def __init__(self, key: str, oid: str):
            self.key = key
            self.id = _Id(oid)

    assert has_at_all_mention([_M("@_all", "all")]) is True
    assert has_at_all_mention([_M("@_user_1", "ou_bot")]) is False


def test_malformed_items_skipped():
    # 防御：None 项 / 缺 id 的项不应炸
    assert has_at_all_mention([None]) is False
    assert has_at_all_mention([{}]) is False
    assert has_at_all_mention([{"key": "@_user_1"}]) is False


# ---------------------------------------------------------------------------

def _run_all():
    fns = [
        v
        for k, v in sorted(globals().items())
        if k.startswith("test_") and callable(v)
    ]
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
