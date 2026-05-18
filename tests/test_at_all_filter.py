"""群里 @所有人 不应触发 bot 答题的回归测试。

背景：群权限本来就是"必须 @ bot 才推事件"，所以未 @ bot 的普通群消息根本
不会到 bot；唯一漏网的是 @所有人——飞书 @_all 把 bot 也算 mention，事件
会被推过来。`is_at_all_broadcast` 同时判 mentions 结构和原始 content 文本
里的 `@_all` 字面（实测 WS SDK 有时不在 mentions 里放 @_all，只在 text 里
留字面字符串）。

跑法：
    .venv/bin/python -m tests.test_at_all_filter
"""

from __future__ import annotations

from ops_qa_bot.feishu_core import is_at_all_broadcast


# ---------------------------------------------------------------------------
# mentions 结构识别
# ---------------------------------------------------------------------------

def test_empty_mentions_no_text():
    assert is_at_all_broadcast(None) is False
    assert is_at_all_broadcast([]) is False
    assert is_at_all_broadcast(None, text="") is False


def test_only_bot_mention_no_at_all():
    # 正常 @bot：key 是 @_user_N，open_id 不是 all
    mentions = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "bot"}]
    assert is_at_all_broadcast(mentions, text='{"text":"@_user_1 你好"}') is False


def test_at_all_by_mention_key():
    mentions = [{"key": "@_all", "id": {"open_id": "all"}, "name": "所有人"}]
    assert is_at_all_broadcast(mentions) is True


def test_at_all_by_mention_open_id():
    # 防御：飞书未来若把 key 重命名，仍可凭 id.open_id == "all" 兜住
    mentions = [{"key": "@_x", "id": {"open_id": "all"}, "name": "所有人"}]
    assert is_at_all_broadcast(mentions) is True


def test_at_all_plus_other_user_still_true():
    mentions = [
        {"key": "@_user_1", "id": {"open_id": "ou_a"}, "name": "A"},
        {"key": "@_all", "id": {"open_id": "all"}, "name": "所有人"},
    ]
    assert is_at_all_broadcast(mentions) is True


def test_object_style_mention_supported():
    # WS SDK 走的是带 .key / .id.open_id 的对象，不是 dict
    class _Id:
        def __init__(self, oid: str):
            self.open_id = oid

    class _M:
        def __init__(self, key: str, oid: str):
            self.key = key
            self.id = _Id(oid)

    assert is_at_all_broadcast([_M("@_all", "all")]) is True
    assert is_at_all_broadcast([_M("@_user_1", "ou_bot")]) is False


def test_malformed_mention_items_skipped():
    assert is_at_all_broadcast([None]) is False
    assert is_at_all_broadcast([{}]) is False
    assert is_at_all_broadcast([{"key": "@_user_1"}]) is False


# ---------------------------------------------------------------------------
# text 兜底：mentions 拿不到时，看原始 content 里有没有 @_all 字面
# ---------------------------------------------------------------------------

def test_text_contains_at_all_drops_even_if_mentions_empty():
    # 实测 WS case：mentions 数组空，但 message.content JSON 文本里有 @_all
    raw = '{"text":"@_all 再测试bot"}'
    assert is_at_all_broadcast(None, text=raw) is True
    assert is_at_all_broadcast([], text=raw) is True


def test_text_at_all_at_string_boundaries():
    # text 开头/结尾/单独出现都应命中
    assert is_at_all_broadcast(None, text="@_all") is True
    assert is_at_all_broadcast(None, text="@_all hello") is True
    assert is_at_all_broadcast(None, text="hello @_all") is True


def test_text_at_allowed_not_matched():
    # 词边界保护：`@_allowed` / `@_all_x` 不应误判
    assert is_at_all_broadcast(None, text="@_allowed list") is False
    assert is_at_all_broadcast(None, text="@_all_x test") is False
    assert is_at_all_broadcast(None, text="x@_all9") is False


def test_text_at_all_inside_json_quoted_string():
    # 实战 raw content 形态：JSON 字符串里被双引号 / 空格围着
    raw = '{"text":"@_all 通知大家"}'
    assert is_at_all_broadcast(None, text=raw) is True


def test_text_without_at_all_is_false():
    assert is_at_all_broadcast(None, text="纯粹的提问") is False
    assert is_at_all_broadcast([], text='{"text":"hello world"}') is False


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
