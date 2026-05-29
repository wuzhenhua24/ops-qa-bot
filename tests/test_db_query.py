"""数据库只读分析接入的回归测试：纯函数校验 + 子进程执行 + 工具错误映射。

覆盖：
- resolve_kind / host_allowed / _validate_port / sanitize_sql / build_argv 纯逻辑。
- DatabaseClient.run：凭据缺失 / host 越界 / 端口非法在 spawn 前就拒；正常路径
  （monkeypatch create_subprocess_exec）校验 argv 正确、密码走 MYSQL_PWD 不进 argv、
  非零返回码 / 超长截断 / 空结果 / 超时的处理。
- query_database handler：成功透传；缺参 / role!=read / 上游失败都返回 is_error 文字
  结果而不抛。

跑法：
    .venv/bin/python -m tests.test_db_query
"""

from __future__ import annotations

import asyncio

import ops_qa_bot.db_query as dbq
from ops_qa_bot.config import DatabaseConfig, DbCreds
from ops_qa_bot.db_query import (
    DatabaseClient,
    DatabaseQueryError,
    build_argv,
    host_allowed,
    make_query_database_handler,
    resolve_kind,
    sanitize_sql,
    _validate_port,
)


def _run(coro):
    return asyncio.run(coro)


def _cfg(**overrides) -> DatabaseConfig:
    base = dict(
        allowed_hosts=("10.10.0.0/16", "172.28.65.2"),
        query_timeout=5.0,
        max_result_chars=200,
        mysql_ro=DbCreds(user="ro_diag", password="pw1"),
        ob_mysql_ro=DbCreds(user="rw_review", password="pw2"),
        ob_oracle_ro=DbCreds(user="RO_SYS", password="pw3"),
    )
    base.update(overrides)
    return DatabaseConfig(**base)


# ---------------------------------------------------------------------------
# 纯逻辑
# ---------------------------------------------------------------------------

def test_resolve_kind_variants():
    assert resolve_kind("mysql", "") == "mysql"
    assert resolve_kind("oceanbase", "mysql") == "ob_mysql"
    assert resolve_kind("oceanbase", "oracle") == "ob_oracle"
    assert resolve_kind("OB", "oracle") == "ob_oracle"


def test_resolve_kind_bad_db_type_raises():
    try:
        resolve_kind("postgres", "mysql")
    except DatabaseQueryError as e:
        assert e.agent_hint
    else:
        raise AssertionError("非法 db_type 应抛")


def test_resolve_kind_bad_ob_mode_raises():
    try:
        resolve_kind("oceanbase", "weird")
    except DatabaseQueryError as e:
        assert "mode" in e.agent_hint or "模式" in e.agent_hint
    else:
        raise AssertionError("非法 OB mode 应抛")


def test_host_allowed_cidr_and_exact():
    allowed = ("10.10.0.0/16", "172.28.65.2", "db-test-01")
    assert host_allowed("10.10.5.7", allowed)        # CIDR 内
    assert host_allowed("172.28.65.2", allowed)       # 精确 IP
    assert host_allowed("db-test-01", allowed)        # 精确主机名
    assert not host_allowed("10.20.0.1", allowed)     # 越界
    assert not host_allowed("8.8.8.8", allowed)
    assert not host_allowed("", allowed)
    assert not host_allowed("10.10.0.1", ())          # 空白名单全拒


def test_validate_port():
    assert _validate_port(2883) == 2883
    assert _validate_port("3306") == 3306
    for bad in (0, 70000, -1, "abc", None):
        try:
            _validate_port(bad)
        except DatabaseQueryError:
            pass
        else:
            raise AssertionError(f"端口 {bad!r} 应被拒")


def test_sanitize_sql_strips_trailing_semicolon():
    assert sanitize_sql("  SHOW PROCESSLIST ;  ") == "SHOW PROCESSLIST"
    assert sanitize_sql("SELECT 1") == "SELECT 1"


def test_sanitize_sql_rejects_multistatement():
    for bad in ("SELECT 1; DROP TABLE t", "SHOW x; SHOW y;"):
        try:
            sanitize_sql(bad)
        except DatabaseQueryError as e:
            assert "一条" in e.agent_hint
        else:
            raise AssertionError("多语句应被拒")


def test_sanitize_sql_empty_raises():
    try:
        sanitize_sql("   ")
    except DatabaseQueryError:
        pass
    else:
        raise AssertionError("空 SQL 应抛")


def test_build_argv_mysql_vs_obclient():
    mysql_argv = build_argv("mysql", "10.10.1.2", 3306, "ro_diag", "SELECT 1", 5)
    assert mysql_argv[0] == "mysql"
    assert "-h" in mysql_argv and "10.10.1.2" in mysql_argv
    assert mysql_argv[-2] == "-e" and mysql_argv[-1] == "SELECT 1"
    # 密码绝不在 argv 里
    assert all("pw" not in a for a in mysql_argv)

    ob_argv = build_argv(
        "ob_oracle", "172.28.65.2", 2883, "SYS@zj#qd_dev_00001", "SELECT 1 FROM dual", 5
    )
    assert ob_argv[0] == "obclient"
    assert "SYS@zj#qd_dev_00001" in ob_argv


# ---------------------------------------------------------------------------
# DatabaseClient.run —— spawn 前的拒绝路径
# ---------------------------------------------------------------------------

def test_run_no_creds_raises():
    cfg = _cfg(mysql_ro=DbCreds())  # mysql 没只读账号
    client = DatabaseClient(cfg)
    try:
        _run(
            client.run(
                db_type="mysql", mode="", host="10.10.1.2", port=3306,
                tenant="", cluster="", sql="SELECT 1",
            )
        )
    except DatabaseQueryError as e:
        assert "只读账号" in e.agent_hint
    else:
        raise AssertionError("缺只读账号应抛")


def test_run_host_not_allowed_raises():
    client = DatabaseClient(_cfg())
    try:
        _run(
            client.run(
                db_type="mysql", mode="", host="8.8.8.8", port=3306,
                tenant="", cluster="", sql="SELECT 1",
            )
        )
    except DatabaseQueryError as e:
        assert "白名单" in e.agent_hint or "允许范围" in e.agent_hint
    else:
        raise AssertionError("越界 host 应抛")


def test_run_bad_identifier_raises():
    client = DatabaseClient(_cfg())
    try:
        _run(
            client.run(
                db_type="oceanbase", mode="oracle", host="172.28.65.2", port=2883,
                tenant="zj; DROP", cluster="qd", sql="SELECT 1 FROM dual",
            )
        )
    except DatabaseQueryError:
        pass
    else:
        raise AssertionError("非法租户名应抛")


# ---------------------------------------------------------------------------
# DatabaseClient.run —— monkeypatch 子进程的正常/异常路径
# ---------------------------------------------------------------------------

class _FakeProc:
    def __init__(self, out=b"", err=b"", rc=0, hang=False):
        self._out = out
        self._err = err
        self.returncode = rc
        self._hang = hang
        self.killed = False

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(3600)
        return self._out, self._err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _patch_spawn(monkey_proc, captured):
    async def fake_exec(*argv, stdout=None, stderr=None, env=None):
        captured["argv"] = list(argv)
        captured["env"] = env
        return monkey_proc
    return fake_exec


def test_run_success_sets_pwd_env_and_returns_text():
    captured: dict = {}
    proc = _FakeProc(out="col\nval".encode(), rc=0)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, captured)
    try:
        client = DatabaseClient(_cfg())
        out = _run(
            client.run(
                db_type="oceanbase", mode="mysql", host="10.10.1.2", port=2883,
                tenant="bd_p1", cluster="hx_new", sql="SHOW PROCESSLIST",
            )
        )
    finally:
        dbq.asyncio.create_subprocess_exec = orig
    assert "val" in out
    # 密码走 MYSQL_PWD，不在 argv
    assert captured["env"]["MYSQL_PWD"] == "pw2"
    assert all("pw2" not in a for a in captured["argv"])
    assert "rw_review@bd_p1#hx_new" in captured["argv"]


def test_run_nonzero_returncode_raises_with_detail():
    proc = _FakeProc(err=b"ERROR 1142: SELECT command denied", rc=1)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, {})
    try:
        client = DatabaseClient(_cfg())
        try:
            _run(
                client.run(
                    db_type="mysql", mode="", host="10.10.1.2", port=3306,
                    tenant="", cluster="", sql="SELECT 1",
                )
            )
        except DatabaseQueryError as e:
            assert "command denied" in e.agent_hint
        else:
            raise AssertionError("非零返回码应抛")
    finally:
        dbq.asyncio.create_subprocess_exec = orig


def test_run_truncates_oversized_output():
    proc = _FakeProc(out=("x" * 500).encode(), rc=0)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, {})
    try:
        client = DatabaseClient(_cfg(max_result_chars=200))
        out = _run(
            client.run(
                db_type="mysql", mode="", host="10.10.1.2", port=3306,
                tenant="", cluster="", sql="SELECT 1",
            )
        )
    finally:
        dbq.asyncio.create_subprocess_exec = orig
    assert "已截断" in out
    assert len(out) <= 200 + 100


def test_run_empty_output_is_success_note():
    proc = _FakeProc(out=b"   ", rc=0)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, {})
    try:
        client = DatabaseClient(_cfg())
        out = _run(
            client.run(
                db_type="mysql", mode="", host="10.10.1.2", port=3306,
                tenant="", cluster="", sql="SELECT 1 WHERE 1=0",
            )
        )
    finally:
        dbq.asyncio.create_subprocess_exec = orig
    assert "没有返回任何行" in out


def test_run_timeout_kills_and_raises():
    proc = _FakeProc(hang=True)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, {})
    try:
        client = DatabaseClient(_cfg(query_timeout=0.05))
        try:
            _run(
                client.run(
                    db_type="mysql", mode="", host="10.10.1.2", port=3306,
                    tenant="", cluster="", sql="SELECT SLEEP(100)",
                )
            )
        except DatabaseQueryError as e:
            assert "超时" in e.agent_hint
            assert proc.killed
        else:
            raise AssertionError("超时应抛")
    finally:
        dbq.asyncio.create_subprocess_exec = orig


# ---------------------------------------------------------------------------
# query_database handler
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc
        self.calls: list[dict] = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._text


def test_handler_success_passes_text():
    fake = _FakeClient(text="结果行")
    handler = make_query_database_handler(fake)
    res = _run(handler({"db_type": "mysql", "host": "10.10.1.2", "sql": "SELECT 1"}))
    assert res["content"][0]["text"] == "结果行"
    assert "is_error" not in res
    assert fake.calls and fake.calls[0]["sql"] == "SELECT 1"


def test_handler_missing_params_is_error():
    fake = _FakeClient(text="x")
    handler = make_query_database_handler(fake)
    res = _run(handler({"db_type": "mysql", "sql": "SELECT 1"}))  # 缺 host
    assert res.get("is_error") is True
    assert not fake.calls


def test_handler_admin_role_rejected():
    fake = _FakeClient(text="x")
    handler = make_query_database_handler(fake)
    res = _run(
        handler(
            {"db_type": "mysql", "host": "10.10.1.2", "sql": "KILL 1", "role": "admin"}
        )
    )
    assert res.get("is_error") is True
    assert "只读" in res["content"][0]["text"]
    assert not fake.calls  # 没真去连库


def test_handler_upstream_failure_returns_hint_not_raises():
    fake = _FakeClient(exc=DatabaseQueryError("boom", "请修正 SQL 后重试"))
    handler = make_query_database_handler(fake)
    res = _run(handler({"db_type": "mysql", "host": "10.10.1.2", "sql": "SELECT 1"}))
    assert res.get("is_error") is True
    assert "重试" in res["content"][0]["text"]


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
