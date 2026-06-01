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
    DbChangeRequest,
    build_argv,
    build_change_sql,
    host_allowed,
    make_query_database_handler,
    make_request_db_change_handler,
    resolve_kind,
    sanitize_sql,
    validate_param_value,
    _validate_port,
    _value_literal,
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
# 参数变更：纯逻辑（值/参数校验、SQL 构造、config.admin_enabled）
# ---------------------------------------------------------------------------

def test_value_literal_int_unquoted_else_quoted():
    assert _value_literal("1000") == "1000"
    assert _value_literal("-5") == "-5"
    assert _value_literal("256M") == "'256M'"
    assert _value_literal("ON") == "'ON'"
    assert _value_literal("READ-COMMITTED") == "'READ-COMMITTED'"


def test_build_change_sql_per_kind():
    assert (
        build_change_sql("mysql", "max_connections", "1000")
        == "SET GLOBAL max_connections = 1000"
    )
    assert (
        build_change_sql("ob_mysql", "memory_limit", "8G")
        == "ALTER SYSTEM SET memory_limit = '8G'"
    )
    assert build_change_sql("ob_oracle", "cpu_count", "4") == "ALTER SYSTEM SET cpu_count = 4"


def test_validate_param_value_rejects_injection():
    # 含引号/分号/反引号/括号/美元的值都要被拒（堵死拼进 SQL 的注入面）
    for bad in ("256M'; DROP", 'a"b', "v;x", "`x`", "$(x)", "a,b", "x=y"):
        try:
            validate_param_value("good_param", bad)
        except DatabaseQueryError:
            pass
        else:
            raise AssertionError(f"值 {bad!r} 应被拒")
    # 合法值放行
    validate_param_value("max_connections", "1000")
    validate_param_value("memory_limit", "8G")
    validate_param_value("tx_isolation", "READ-COMMITTED")


def test_validate_param_value_rejects_bad_param_name():
    for bad in ("bad param", "p;x", "drop`", ""):
        try:
            validate_param_value(bad, "1")
        except DatabaseQueryError:
            pass
        else:
            raise AssertionError(f"参数名 {bad!r} 应被拒")


def test_admin_enabled_requires_all_three():
    ok = dict(
        allowed_hosts=("10.0.0.0/8",),
        admin_open_ids=("ou_a",),
        mysql_admin=DbCreds(user="root", password="p"),
    )
    assert DatabaseConfig(**ok).admin_enabled
    assert not DatabaseConfig(**{**ok, "admin_open_ids": ()}).admin_enabled
    assert not DatabaseConfig(**{**ok, "allowed_hosts": ()}).admin_enabled
    # 有白名单 + admin 名单但一套 admin 账号都没配 → 关
    assert not DatabaseConfig(
        allowed_hosts=("10.0.0.0/8",), admin_open_ids=("ou_a",)
    ).admin_enabled


# ---------------------------------------------------------------------------
# DatabaseClient.prepare_change / run_admin
# ---------------------------------------------------------------------------

def test_prepare_change_no_admin_creds_raises():
    client = DatabaseClient(_cfg())  # 只有 ro，没 admin
    try:
        _run(
            client.prepare_change(
                db_type="mysql", mode="", host="10.10.1.2", port=3306,
                tenant="", cluster="", param="max_connections", value="1000",
            )
        )
    except DatabaseQueryError as e:
        assert "admin" in e.agent_hint.lower() or "admin 账号" in e.agent_hint
    else:
        raise AssertionError("缺 admin 账号应抛")


def test_prepare_change_host_not_allowed_raises():
    cfg = _cfg(mysql_admin=DbCreds(user="root", password="apw"))
    client = DatabaseClient(cfg)
    try:
        _run(
            client.prepare_change(
                db_type="mysql", mode="", host="8.8.8.8", port=3306,
                tenant="", cluster="", param="max_connections", value="1000",
            )
        )
    except DatabaseQueryError as e:
        assert "白名单" in e.agent_hint or "允许范围" in e.agent_hint
    else:
        raise AssertionError("越界 host 应抛")


def test_prepare_change_builds_request_and_reads_current():
    # 现值读取走只读账号 + 子进程；monkeypatch 返回 SHOW 输出
    proc = _FakeProc(out=b"Variable_name\tValue\nmax_connections\t151", rc=0)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, {})
    try:
        cfg = _cfg(mysql_admin=DbCreds(user="root", password="apw"))
        client = DatabaseClient(cfg)
        req = _run(
            client.prepare_change(
                db_type="mysql", mode="", host="10.10.1.2", port=3306,
                tenant="", cluster="", param="max_connections", value="1000",
            )
        )
    finally:
        dbq.asyncio.create_subprocess_exec = orig
    assert req.kind == "mysql"
    assert req.sql == "SET GLOBAL max_connections = 1000"
    assert req.new_value == "1000"
    assert req.current_value and "151" in req.current_value


def test_prepare_change_current_value_none_when_read_fails():
    # 读现值失败（非零返回码）不应阻断审批——current_value 退化为 None
    proc = _FakeProc(err=b"ERROR", rc=1)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, {})
    try:
        cfg = _cfg(mysql_admin=DbCreds(user="root", password="apw"))
        client = DatabaseClient(cfg)
        req = _run(
            client.prepare_change(
                db_type="mysql", mode="", host="10.10.1.2", port=3306,
                tenant="", cluster="", param="max_connections", value="1000",
            )
        )
    finally:
        dbq.asyncio.create_subprocess_exec = orig
    assert req.current_value is None
    assert req.sql == "SET GLOBAL max_connections = 1000"


def _mk_req(**over) -> DbChangeRequest:
    base = dict(
        kind="ob_mysql", db_type="oceanbase", mode="mysql",
        host="172.28.65.2", port=2883, tenant="bd_p1", cluster="hx_new",
        param="memory_limit", new_value="8G",
        sql="ALTER SYSTEM SET memory_limit = '8G'",
    )
    base.update(over)
    return DbChangeRequest(**base)


def test_run_admin_executes_with_admin_creds_and_pwd_env():
    captured: dict = {}
    proc = _FakeProc(out=b"", rc=0)
    orig = dbq.asyncio.create_subprocess_exec
    dbq.asyncio.create_subprocess_exec = _patch_spawn(proc, captured)
    try:
        cfg = _cfg(ob_mysql_admin=DbCreds(user="root", password="apw2"))
        client = DatabaseClient(cfg)
        out = _run(client.run_admin(_mk_req()))
    finally:
        dbq.asyncio.create_subprocess_exec = orig
    assert "成功" in out  # 空输出 → "（执行成功，无返回行。）"
    assert captured["env"]["MYSQL_PWD"] == "apw2"
    assert all("apw2" not in a for a in captured["argv"])
    assert "root@bd_p1#hx_new" in captured["argv"]
    # 原样执行卡上那条 SQL
    assert captured["argv"][-1] == "ALTER SYSTEM SET memory_limit = '8G'"


def test_run_admin_no_creds_raises():
    client = DatabaseClient(_cfg())  # 没 admin
    try:
        _run(client.run_admin(_mk_req()))
    except DatabaseQueryError:
        pass
    else:
        raise AssertionError("缺 admin 账号应抛")


# ---------------------------------------------------------------------------
# request_db_change handler
# ---------------------------------------------------------------------------

class _FakeChangeClient:
    def __init__(self, req=None, exc=None):
        self._req = req
        self._exc = exc
        self.calls: list[dict] = []

    async def prepare_change(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._req


def test_change_handler_success_calls_submitter():
    req = _mk_req(kind="mysql", db_type="mysql", host="10.10.1.2", port=3306,
                  tenant="", cluster="", param="max_connections", new_value="1000",
                  sql="SET GLOBAL max_connections = 1000")
    fake = _FakeChangeClient(req=req)
    sent: list = []

    async def submitter(r):
        sent.append(r)
        return "已提交审批卡，等管理员确认"

    handler = make_request_db_change_handler(fake, submitter)
    res = _run(
        handler({"db_type": "mysql", "host": "10.10.1.2",
                 "param": "max_connections", "value": "1000"})
    )
    assert "is_error" not in res
    assert res["content"][0]["text"] == "已提交审批卡，等管理员确认"
    assert sent and sent[0] is req


def test_change_handler_missing_params_is_error():
    fake = _FakeChangeClient()

    async def submitter(r):  # 不该被调到
        raise AssertionError("缺参不应进 submitter")

    handler = make_request_db_change_handler(fake, submitter)
    res = _run(handler({"db_type": "mysql", "host": "10.10.1.2", "param": "x"}))  # 缺 value
    assert res.get("is_error") is True
    assert not fake.calls


def test_change_handler_prepare_failure_returns_hint():
    fake = _FakeChangeClient(exc=DatabaseQueryError("bad", "目标值非法，请核对"))

    async def submitter(r):
        raise AssertionError("校验失败不应进 submitter")

    handler = make_request_db_change_handler(fake, submitter)
    res = _run(
        handler({"db_type": "mysql", "host": "10.10.1.2", "param": "p", "value": "x'"})
    )
    assert res.get("is_error") is True
    assert "核对" in res["content"][0]["text"]


def test_change_handler_submitter_failure_is_error():
    req = _mk_req(kind="mysql", db_type="mysql", host="10.10.1.2", port=3306,
                  tenant="", cluster="", param="p", new_value="1",
                  sql="SET GLOBAL p = 1")
    fake = _FakeChangeClient(req=req)

    async def submitter(r):
        raise RuntimeError("feishu down")

    handler = make_request_db_change_handler(fake, submitter)
    res = _run(handler({"db_type": "mysql", "host": "10.10.1.2", "param": "p", "value": "1"}))
    assert res.get("is_error") is True


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
