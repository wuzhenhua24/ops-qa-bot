# Redis 常见故障排查

## 连接超时

### 现象
客户端报 `ERR connect timeout` 或连接池耗尽。

### 排查步骤
1. 检查客户端到 Redis 的网络：`telnet redis.internal.example.com 6379`
2. 查看 Redis 慢查询日志：`SLOWLOG GET 10`
3. 检查连接数：`CLIENT LIST | wc -l`，是否接近 `maxclients`（默认 10000）
4. 检查 Redis CPU 是否打满

### 常见原因
- 大 key 导致阻塞（用 `redis-cli --bigkeys` 定位）
- 客户端连接池配置过小
- 网络抖动（联系 SRE 查交换机）

## 内存告警

### 现象
监控报 "Redis memory usage > 85%"。

### 处理流程
1. 执行 `redis-cli INFO memory` 确认实际使用
2. 用 `redis-cli --bigkeys` 找出大 key
3. 与业务方确认是否可清理过期数据
4. 若需扩容：**联系 DBA 团队走工单**，不要自行操作（会触发 resharding，影响线上）

> ⚠️ 危险操作提醒：不要直接 `FLUSHDB` 或 `FLUSHALL`，会清空所有数据。

## 主从同步延迟

### 现象
`INFO replication` 中 `master_repl_offset - slave_repl_offset` 持续增长。

### 处理
- 检查从节点 CPU / 网络
- 若延迟 > 10s，考虑主从切换：`CLUSTER FAILOVER`（需在从节点执行）
- 切换前务必确认从节点数据完整
