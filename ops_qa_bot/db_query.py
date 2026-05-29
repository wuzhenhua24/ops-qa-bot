"""数据库只读分析接入：本机 mysql / obclient + SDK MCP 工具。

部署机本身装了 `mysql`（连 MySQL）和 `obclient`（连 OceanBase，分 mysql / oracle
两种模式）客户端。asker 在问题里给出连接信息（IP、端口、租户、集群），本模块把
"用固定的**只读**账号连上目标库、跑一条诊断 SQL、把结果原样返回"包成主 `OpsQABot`
的一个进程内工具 `query_database`，让 agent 在排查"数据库 CPU 高 / 连接数高 / 慢查询"
这类问题时能自由迭代地查（`SHOW PROCESSLIST` → 看可疑 query → 翻 `gv$ob_sql_audit`/
`sys`/`performance_schema` …），而不必预先写死 SQL。

设计要点（与 gateway_trace.py / doc_qa.py 同姿态）：
- **只依赖 stdlib + claude_agent_sdk**，不引入 bot / feishu_core（避免循环）。直连不
  走 jumphost——本机就有 client。
- **只读由数据库引擎强制，不靠解析 SQL**：连接用的是 DBA 预先在每个实例建好的
  **只读账号**（只有 SELECT/SHOW/PROCESS 等权限）。写操作会被引擎直接拒、`INTO
  OUTFILE` 因无 FILE 权限也失败。所以这里**不做 SQL 白/黑名单**，agent 想怎么读就
  怎么读——既不误杀诊断语句，也没有黑名单 fail-open 的风险。唯一拦的是多语句拼接
  （`;`），保住"一次调用一条语句"的契约。
- **凭据工具内注入，LLM 全程看不到**：账号/密码来自 config（`DatabaseConfig`），按
  (引擎, 模式) 选对应的只读账号；密码经 `MYSQL_PWD` 环境变量传给 client，**不进命令行
  argv**（避免被 `ps` 看到），更不进 agent 上下文 / 日志 / 飞书进度展示。
- **目标受 IP 白名单约束**：host 必须落在 config 的 `allowed_hosts`（IP / CIDR）内，
  asker 给的越界 IP 直接拒——配合"生产天生隔离"，把 bot 能连到的实例限在测试环境。
- **用 create_subprocess_exec 而非 shell**：argv 列表传参，SQL 作为单个 `-e` 实参，
  没有 shell 注入面；租户/集群/账号名再做字符集校验，防被拼进 `user@tenant#cluster`。
- 失败**返回文字结果而不是抛异常**：抛会打断 agent 这一轮；返回引导文字（带数据库
  报错原文）让 agent 自己改 SQL 重试，或按升级规则通知 DBA。
- **role 入参为将来留口**：当前只支持 `role="read"`（只读账号）。管理员账号（改参数、
  杀 session 等写/变更操作）需要配合人工确认机制，本期不接入，传 `admin` 直接拒。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from .config import DatabaseConfig

logger = logging.getLogger("ops_qa_bot.db_query")

# 工具名（allowed_tools 引用）。agent 侧全名是 mcp__<server_key>__<tool_name>。
TOOL_NAME = "query_database"
SERVER_KEY = "db"
FULL_TOOL_NAME = f"mcp__{SERVER_KEY}__{TOOL_NAME}"

# 连接类型（引擎 + OceanBase 模式）。决定用哪个 client、哪套只读账号、哪种方言。
_KIND_MYSQL = "mysql"  # 原生 MySQL，用 mysql client
_KIND_OB_MYSQL = "ob_mysql"  # OceanBase mysql 模式，用 obclient
_KIND_OB_ORACLE = "ob_oracle"  # OceanBase oracle 模式，用 obclient

# 账号 / 租户 / 集群名允许的字符：拼进 `user@tenant#cluster` 前做校验，防注入。
_IDENT_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
_MAX_IDENT_LEN = 128

# SQL 长度上限：诊断语句远不到这个量级，超长当异常拒。
_MAX_SQL_LEN = 8000

# connect 阶段超时上限：不让它吃满整条 query_timeout（query 本身可能慢）。
_MAX_CONNECT_TIMEOUT = 10


class DatabaseQueryError(Exception):
    """数据库查询失败。`agent_hint` 是给主 agent 看的引导文字。"""

    def __init__(self, log_detail: str, agent_hint: str):
        super().__init__(log_detail)
        self.agent_hint = agent_hint


_HINT_HOST = (
    "目标地址不在允许范围内（本工具只允许连测试环境白名单内的数据库实例）。"
    "请确认 asker 给的 IP 是测试环境实例；若确属测试环境但被拒，是部署侧 "
    "allowed_hosts 没覆盖该网段，按升级规则通知运维补白名单，不要换地址重试。"
)
_HINT_TIMEOUT = (
    "连接或查询超时。可能是实例不可达、负载过高，或这条 SQL 太重。"
    "可以让用户确认连接信息（IP/端口/租户/集群）是否正确，或把查询收窄"
    "（加 WHERE/LIMIT）后重试；多次超时按升级规则通知 DBA。"
)
_ADMIN_NOT_SUPPORTED = (
    "本工具当前只支持只读分析（role=read）。改参数、杀 session、kill query 等"
    "写/变更操作尚未接入（需配合人工确认流程），请按「写操作建议输出格式」把"
    "对应 SQL 以文字建议返回给用户、标注风险，由 DBA 人工执行。"
)


def _hint_no_creds(kind: str) -> str:
    return (
        f"部署侧没有为该数据库类型（{kind}）配置只读账号，无法查询。"
        "请按升级规则通知运维在 config 的 [database] 下补 ro_user/ro_password，"
        "不要换种方式重试。"
    )


def _hint_query_failed(detail: str) -> str:
    short = detail.strip().replace("\n", " ")
    if len(short) > 500:
        short = short[:500] + "…"
    return (
        f"数据库返回错误：{short or '（无错误详情）'}。"
        "这只是**这一条 SQL** 的问题，不代表连接或整体权限有问题——请换种写法**继续**"
        "排查，不要因为一两次报错就放弃或直接下「无权限」结论：\n"
        "- 表/视图不存在（如 ORA-00942、MySQL 1146）：多半是**对象名不对**。不同库/模式"
        "视图名不一样，OceanBase 的动态视图常带 `OB` 前缀（如 GV$OB_PROCESSLIST、"
        "GV$OB_LOCKS），别照搬标准 Oracle 的 V$SESSION。**先从数据字典查实际存在的对象名"
        "再查**：oracle 模式 `SELECT view_name FROM dba_views WHERE view_name LIKE "
        "'%SESSION%'`（或 %LOCK%/%PROCESS%），mysql 模式用 `SHOW TABLES` / 查 "
        "information_schema；或换 OB 专用视图名重试，**多试几种再下结论**。"
        "（注意：oracle 模式 ORA-00942 既可能是名字错、也可能是无权访问，光看报错分不清，"
        "所以不能一见 ORA-00942 就当没权限。）\n"
        "- 语法/列名错（如 ORA-00904、MySQL 1064/1054）：按报错修正后重试。\n"
        "- 只有在「确属连接失败」或「在确认存在的对象上、换多种写法仍明确被拒访问」时，"
        "才按升级规则通知 DBA。"
    )


def resolve_kind(db_type: str, mode: str) -> str:
    """(引擎, 模式) → 连接类型。非法组合抛 DatabaseQueryError。"""
    db_type = (db_type or "").strip().lower()
    mode = (mode or "").strip().lower() or "mysql"
    if db_type == "mysql":
        return _KIND_MYSQL
    if db_type in ("oceanbase", "ob"):
        if mode == "mysql":
            return _KIND_OB_MYSQL
        if mode == "oracle":
            return _KIND_OB_ORACLE
        raise DatabaseQueryError(
            f"bad oceanbase mode: {mode!r}",
            "OceanBase 必须指定 mode 为 mysql 或 oracle。请确认目标租户是哪种模式。",
        )
    raise DatabaseQueryError(
        f"bad db_type: {db_type!r}",
        "db_type 只支持 mysql 或 oceanbase。请确认目标数据库类型。",
    )


def host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    """host 是否落在白名单内：先精确字符串匹配（覆盖主机名），再按 IP/CIDR 匹配。"""
    host = (host or "").strip()
    if not host or not allowed:
        return False
    if host in allowed:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 非 IP 且不在精确名单里 → 拒
    for entry in allowed:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if ip in net:
            return True
    return False


def _validate_identifier(value: str, field: str) -> None:
    if not value or not _IDENT_RE.match(value) or len(value) > _MAX_IDENT_LEN:
        raise DatabaseQueryError(
            f"bad {field}: {value!r}",
            f"{field} 含非法字符或为空（只允许字母/数字/._-）。请核对连接信息。",
        )


def _validate_port(port: object) -> int:
    try:
        p = int(port)
    except (TypeError, ValueError):
        p = -1
    if not (1 <= p <= 65535):
        raise DatabaseQueryError(
            f"bad port: {port!r}",
            "端口非法（应为 1-65535 的整数）。请确认 asker 给的端口，OceanBase 通常是 2883。",
        )
    return p


def sanitize_sql(sql: str) -> str:
    """去首尾空白 + 去末尾分号；含内嵌分号（多语句）则拒。

    只读由引擎保证，这里只守"一次一条语句"的契约——多语句拼接既破坏结果可读性，
    也是常见的注入/堆叠手法，直接拒掉最干净。
    """
    s = (sql or "").strip()
    if not s:
        raise DatabaseQueryError("empty sql", "调用缺少 SQL 语句。")
    if len(s) > _MAX_SQL_LEN:
        raise DatabaseQueryError(
            "sql too long",
            "SQL 过长，疑似异常。请精简查询后重试。",
        )
    stripped = s.rstrip("; \t\r\n")
    if ";" in stripped:
        raise DatabaseQueryError(
            "multi-statement sql rejected",
            "一次只能查一条语句（检测到多条用 ; 拼接）。请拆成多次调用，每次一条。",
        )
    return stripped


def build_argv(
    kind: str, host: str, port: int, conn_user: str, sql: str, query_timeout: float
) -> list[str]:
    """拼 client 命令 argv（密码不在其中，走 MYSQL_PWD 环境变量）。"""
    connect_timeout = max(2, min(_MAX_CONNECT_TIMEOUT, int(query_timeout)))
    client = "mysql" if kind == _KIND_MYSQL else "obclient"
    return [
        client,
        "-h",
        host,
        "-P",
        str(port),
        "-u",
        conn_user,
        f"--connect-timeout={connect_timeout}",
        "-e",
        sql,
    ]


class DatabaseClient:
    """本机 mysql / obclient 的薄封装：选只读账号 → 校验 → 跑子进程 → 返回文本。"""

    def __init__(self, config: DatabaseConfig):
        self._allowed = config.allowed_hosts
        self._timeout = config.query_timeout
        self._max_chars = config.max_result_chars
        # kind → 只读凭据
        self._creds = {
            _KIND_MYSQL: config.mysql_ro,
            _KIND_OB_MYSQL: config.ob_mysql_ro,
            _KIND_OB_ORACLE: config.ob_oracle_ro,
        }

    async def run(
        self,
        *,
        db_type: str,
        mode: str,
        host: str,
        port: object,
        tenant: str,
        cluster: str,
        sql: str,
    ) -> str:
        """连库跑一条只读 SQL，返回结果文本。失败抛 DatabaseQueryError（带 agent_hint）。"""
        kind = resolve_kind(db_type, mode)
        creds = self._creds[kind]
        if not creds.configured:
            raise DatabaseQueryError(f"no ro creds for {kind}", _hint_no_creds(kind))
        if not host_allowed(host, self._allowed):
            raise DatabaseQueryError(f"host not allowed: {host}", _HINT_HOST)
        p = _validate_port(port)
        _validate_identifier(creds.user or "", "user")
        if kind in (_KIND_OB_MYSQL, _KIND_OB_ORACLE):
            _validate_identifier(tenant, "tenant")
            _validate_identifier(cluster, "cluster")
            conn_user = f"{creds.user}@{tenant}#{cluster}"
        else:
            conn_user = creds.user or ""
        clean_sql = sanitize_sql(sql)

        argv = build_argv(kind, host.strip(), p, conn_user, clean_sql, self._timeout)
        env = dict(os.environ)
        env["MYSQL_PWD"] = creds.password or ""

        logger.info(
            "db query: kind=%s host=%s port=%d user=%s sql_len=%d",
            kind,
            host,
            p,
            conn_user,
            len(clean_sql),
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as e:
            raise DatabaseQueryError(
                f"client binary missing: {e!r}",
                "数据库客户端（mysql/obclient）未安装在部署机上。"
                "按升级规则通知运维，不要重试。",
            ) from e

        try:
            out, err = await asyncio.wait_for(
                proc.communicate(), timeout=self._timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as e:
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            raise DatabaseQueryError(f"db query timeout: {e!r}", _HINT_TIMEOUT) from e

        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", errors="replace").strip()
            logger.warning("db query rc=%s: %s", proc.returncode, detail)
            raise DatabaseQueryError(
                f"db query failed rc={proc.returncode}: {detail}",
                _hint_query_failed(detail),
            )

        text = (out or b"").decode("utf-8", errors="replace").strip()
        if not text:
            return "（查询执行成功，但没有返回任何行。）"
        if len(text) > self._max_chars:
            text = (
                text[: self._max_chars]
                + "\n…（结果过长，已截断；请用更具体的条件或 LIMIT 收窄查询）"
            )
        return text


_TOOL_DESC = (
    "连接到测试环境的数据库，执行一条**只读** SQL 做分析/排查，返回结果文本。"
    "适用：用户报告某个数据库实例的实时问题（CPU 高、连接数高、慢查询、锁等待、"
    "空间增长等），并给出了连接信息。这类排查通常要多次调用、迭代收敛——先看面上"
    "（如 SHOW PROCESSLIST / gv$ob_processlist），再挑可疑点深入（EXPLAIN、慢查询"
    "视图、gv$ob_sql_audit、sys/performance_schema 等），每次调用跑**一条**语句。\n"
    "参数：db_type=mysql 或 oceanbase（必填）；oceanbase 必须给 mode=mysql 或 oracle、"
    "并给 tenant（租户）和 cluster（集群）；host/port 用 asker 给的连接信息（必填 host，"
    "OceanBase 端口通常 2883）；sql 是你要跑的那条只读语句（必填）。账号密码由系统按"
    "类型注入，你**不用也拿不到**。\n"
    "只读由数据库账号权限强制：写/变更语句（INSERT/UPDATE/DELETE/DDL 等）会被引擎"
    "直接拒。用户要求改参数、杀 session、kill query 等变更时**不要**调本工具，按"
    "「写操作建议输出格式」给文字建议让 DBA 人工执行。\n"
    "方言注意：oracle 模式没有 SHOW，用数据字典/动态性能视图（如 gv$ 视图、"
    "v$session、dba_* 等），且查 dual 而非空 FROM。\n"
    "出错时本工具返回引导提示（含数据库报错原文），据此修正 SQL 或按升级规则处理。"
)

# 工具入参 JSON Schema：只有 db_type/host/sql 必填；mode/tenant/cluster/port/role
# 可选（原生 MySQL 不需要 tenant/cluster；role 留给将来的管理员路径）。
_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "db_type": {
            "type": "string",
            "enum": ["mysql", "oceanbase"],
            "description": "数据库类型",
        },
        "mode": {
            "type": "string",
            "enum": ["mysql", "oracle"],
            "description": "OceanBase 租户模式；db_type=oceanbase 时必给，mysql 时忽略",
        },
        "host": {"type": "string", "description": "目标实例 IP（asker 提供）"},
        "port": {
            "type": "integer",
            "description": "端口（asker 提供，OceanBase 通常 2883）",
        },
        "tenant": {
            "type": "string",
            "description": "OceanBase 租户名；oceanbase 时必给",
        },
        "cluster": {
            "type": "string",
            "description": "OceanBase 集群名；oceanbase 时必给",
        },
        "sql": {"type": "string", "description": "要执行的一条只读 SQL"},
        "role": {
            "type": "string",
            "enum": ["read"],
            "description": "权限角色；当前只支持 read（只读）",
        },
    },
    "required": ["db_type", "host", "sql"],
    "additionalProperties": False,
}


def _text_result(text: str, *, is_error: bool = False) -> dict:
    res: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        res["is_error"] = True
    return res


def make_query_database_handler(client: "DatabaseClient"):
    """造 query_database 的 async handler（与 SDK 解耦，便于单测直接 await）。

    失败一律返回 is_error 文字结果而不抛——抛会打断 agent 这一轮，返回提示则让
    agent 自己按引导处理（改 SQL 重试 / 走升级规则）。
    """

    async def handler(args: dict) -> dict:
        role = str(args.get("role") or "read").strip().lower()
        if role != "read":
            return _text_result(_ADMIN_NOT_SUPPORTED, is_error=True)

        db_type = str(args.get("db_type") or "").strip()
        mode = str(args.get("mode") or "mysql").strip()
        host = str(args.get("host") or "").strip()
        port = args.get("port")
        tenant = str(args.get("tenant") or "").strip()
        cluster = str(args.get("cluster") or "").strip()
        sql = str(args.get("sql") or "")

        if not db_type or not host or not sql.strip():
            return _text_result(
                "调用缺少必填参数：db_type / host / sql。请补齐后重试。",
                is_error=True,
            )

        try:
            text = await client.run(
                db_type=db_type,
                mode=mode,
                host=host,
                port=port,
                tenant=tenant,
                cluster=cluster,
                sql=sql,
            )
        except DatabaseQueryError as e:
            logger.warning("db query failed: %s", e)
            return _text_result(e.agent_hint, is_error=True)
        return _text_result(text)

    return handler


def build_database_server(config: DatabaseConfig) -> McpSdkServerConfig:
    """构建挂数据库只读分析工具的进程内 MCP server。

    工具 `query_database(db_type, mode, host, port, tenant, cluster, sql, role)`：
    用 config 里对应类型的只读账号连库跑一条只读 SQL，结果原样返给主 agent。
    """
    handler = make_query_database_handler(DatabaseClient(config))
    decorated = tool(TOOL_NAME, _TOOL_DESC, _TOOL_SCHEMA)(handler)
    return create_sdk_mcp_server(
        name="ops-qa-db", version="1.0.0", tools=[decorated]
    )
