"""Bash 写命令兜底拦截（_WRITE_PATTERNS / _block_write_bash_hook）回归测试。

两个方向都要锁：
- **必须拦**：文件系统写、Redis 数据写（SET/DEL 一族，含嵌套 ssh 写法）、
  SQL 写（含 SET GLOBAL / ALTER SYSTEM 这类参数变更）、文件搬运/定时任务装载。
- **不许误杀**：日常诊断命令（free/top/ss、redis-cli INFO/CONFIG GET/--bigkeys、
  SHOW PROCESSLIST / SHOW CREATE TABLE）、管道后的 grep set、名字带 set 的
  key 字面量、awk 比较表达式、crontab -l、sed 只读用法。

跑法：
    .venv/bin/python -m pytest tests/test_write_block.py -q
"""

from __future__ import annotations

import asyncio

import pytest

from ops_qa_bot.bot import _block_write_bash_hook, _match_write_pattern

# ---------------------------------------------------------------------------
# 必须拦截
# ---------------------------------------------------------------------------

BLOCKED = [
    # 文件系统 / 系统管理（原有覆盖）
    "rm -rf /tmp/x",
    "sudo systemctl restart redis",
    "systemctl stop mysqld",
    "kill -9 12345",
    "chmod 777 /etc/redis.conf",
    # 文件写新增
    "cp /etc/redis.conf /etc/redis.conf.bak",
    "truncate -s 0 /var/log/app.log",
    "shred -u /var/log/secret.log",
    "unlink /tmp/lockfile",
    "echo 'x' | tee /etc/sysctl.conf",
    "sed -i 's/old/new/' /etc/redis.conf",
    "sed -ri 's/old/new/g' /etc/my.cnf",
    "sed --in-place 's/a/b/' f.conf",
    "scp /tmp/dump.rdb 10.1.2.3:/data/",
    "rsync -av /data/ backup:/data/",
    "crontab /tmp/cron.txt",
    "echo '* * * * * /x.sh' | crontab -",
    # Redis 写（原有覆盖）
    "redis-cli -h 10.1.2.3 FLUSHALL",
    "redis-cli CONFIG SET maxmemory 8gb",
    'ssh jumphost "ssh 10.1.2.3 \'redis-cli config set maxmemory 8gb\'"',
    # Redis 数据写新增（含嵌套 ssh 写法）
    "redis-cli -h 10.1.2.3 SET cache:k v",
    "redis-cli -h 10.1.2.3 -p 6379 DEL cache:user:1",
    'ssh jumphost "ssh 10.1.2.3 \'redis-cli -h 127.0.0.1 DEL cache:user:1\'"',
    "redis-cli EXPIRE session:1 60",
    "redis-cli -h x RENAME a b",
    "redis-cli SETEX k 60 v",
    "redis-cli -h x HSET h f v",
    "redis-cli LPUSH queue job1",
    "redis-cli -h x SADD s m",
    "redis-cli ZADD rank 1 a",
    "redis-cli -h x XADD stream '*' f v",
    "redis-cli SLAVEOF 10.0.0.1 6379",
    "redis-cli REPLICAOF NO ONE",
    "redis-cli BGSAVE",
    "redis-cli -h x BGREWRITEAOF",
    "redis-cli -h x MIGRATE dest 6379 key 0 5000",
    "redis-cli RESTORE k 0 payload",
    'redis-cli EVAL "return redis.call(\'set\', KEYS[1], 1)" 1 k',
    "redis-cli -h x SCRIPT FLUSH",
    "redis-cli ACL SETUSER bad on '>pwd'",
    # SQL 写（原有覆盖）
    "mysql -h x -e 'DROP TABLE orders'",
    "mysql -e 'DELETE FROM t WHERE 1=1'",
    # SQL 写新增
    'mysql -h x -e "SET GLOBAL max_connections = 500"',
    "mysql -e 'SET PERSIST slow_query_log = ON'",
    "obclient -e 'ALTER SYSTEM SET cpu_quota_concurrency=4'",
    "obclient -e 'ALTER TENANT t1 SET ...'",
    "mysql -e 'CREATE TABLE t (id int)'",
    "mysql -e 'CREATE INDEX idx ON t(c)'",
    "mysql -e 'REPLACE INTO t VALUES (1)'",
    "mysql -e \"LOAD DATA INFILE '/tmp/x' INTO TABLE t\"",
    "mysql -e 'RENAME TABLE a TO b'",
]

# ---------------------------------------------------------------------------
# 不许误杀（日常诊断只读命令）
# ---------------------------------------------------------------------------

ALLOWED = [
    "free -m",
    "top -bn1 | head -20",
    "ss -s",
    "df -h",
    "cat /proc/meminfo 2>/dev/null",
    "ps aux | grep redis",
    # awk 比较表达式里的 > 不是重定向
    "awk '$3 > 100 {print $1}' /tmp/stats",
    # redis 只读诊断（含嵌套 ssh）
    "redis-cli -h 10.1.2.3 INFO memory",
    'ssh jumphost "ssh 10.1.2.3 \'redis-cli -h 127.0.0.1 INFO replication\'"',
    "redis-cli -h x CLIENT LIST",
    "redis-cli -h x SLOWLOG GET 10",
    "redis-cli -h x CONFIG GET maxmemory",
    "redis-cli -h x CONFIG GET maxmemory-policy",
    "redis-cli --bigkeys",
    "redis-cli -h x MEMORY USAGE big:key",
    "redis-cli -h x OBJECT ENCODING k",
    # 管道后的 set/del 字样不在 redis-cli 段内，不该被牵连
    "redis-cli -h x INFO keyspace | grep set",
    "redis-cli INFO | grep -i del",
    # key 字面量带 set/写命令子串：不带参数语法不算写
    "redis-cli -h x TTL set:members",
    "redis-cli -h x GET settings",
    "redis-cli -h x HGETALL myset",
    # 不带 redis-cli 锚的通用词
    "grep -r 'set' /etc/redis/",
    "grep 'expire' /var/log/redis.log",
    # SQL 只读诊断
    "mysql -h x -e 'SHOW PROCESSLIST'",
    "mysql -e 'SHOW CREATE TABLE orders'",
    "mysql -e 'SHOW GLOBAL VARIABLES LIKE \"max_connections\"'",
    "mysql -e 'SELECT * FROM information_schema.tables LIMIT 5'",
    "mysql -e 'SHOW SLAVE STATUS'",
    # crontab 只读列出
    "crontab -l",
    # sed 只读过滤（无 -i）
    "sed -n '1,5p' /etc/redis.conf",
    "sed -e 's/#.*//' /etc/my.cnf | head",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocked(cmd):
    assert _match_write_pattern(cmd) is not None, f"应当被拦截: {cmd}"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(cmd):
    label = _match_write_pattern(cmd)
    assert label is None, f"误杀（命中 {label}）: {cmd}"


# ---------------------------------------------------------------------------
# hook 行为
# ---------------------------------------------------------------------------


def test_hook_denies_write_command():
    res = asyncio.run(
        _block_write_bash_hook(
            {"tool_name": "Bash", "tool_input": {"command": "redis-cli SET k v"}},
            None,
            None,
        )
    )
    out = res["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "写命令被系统拦截" in out["permissionDecisionReason"]


def test_hook_passes_read_command():
    res = asyncio.run(
        _block_write_bash_hook(
            {"tool_name": "Bash", "tool_input": {"command": "redis-cli INFO"}},
            None,
            None,
        )
    )
    assert res == {}


def test_hook_ignores_non_bash():
    res = asyncio.run(
        _block_write_bash_hook(
            {"tool_name": "Read", "tool_input": {"file_path": "/x"}}, None, None
        )
    )
    assert res == {}
