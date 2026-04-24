# MySQL 常见问题

## 慢查询

### 定位
- 慢查询日志：`/var/log/mysql/slow.log`
- 或查 `performance_schema.events_statements_summary_by_digest`
- 监控面板：Grafana > MySQL > Slow Query

### 处理
1. 执行 `EXPLAIN` 看执行计划
2. 缺索引 → 评估后由 DBA 在低峰期添加
3. 大事务 → 拆分批次
4. 不要在主库直接加索引（会锁表），走 pt-osc 或 gh-ost

## 主从延迟

### 现象
`SHOW SLAVE STATUS\G` 中 `Seconds_Behind_Master` > 30

### 排查
- 从库 IO 是否打满（大量 binlog 接收）
- 从库 SQL 线程是否阻塞（大事务重放）
- 网络带宽

### 应急
- 若业务能接受短暂读延迟，等待自然追上
- 若必须立即解决，联系 DBA 考虑临时扩容从库或调整并行复制参数 `slave_parallel_workers`

## 误删数据恢复

> ⚠️ 严重事故，必须立即止损：
> 1. 第一时间联系 DBA on-call（值班表在 `reference/oncall.md`，如果存在）
> 2. 禁止在主库继续写入相关表
> 3. 从 binlog + 全量备份按时间点恢复

备份策略：每日全备 + binlog 增备，保留 14 天。备份路径 `/backup/mysql/prod/`。
