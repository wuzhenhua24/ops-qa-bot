# Redis 集群概览

## 部署拓扑

生产环境 Redis 采用 Redis Cluster 模式，共 6 个节点，3 主 3 从。

- 主节点：redis-prod-01 ~ redis-prod-03
- 从节点：redis-prod-04 ~ redis-prod-06
- 端口：6379
- 版本：7.2.4

## 访问方式

- 内网地址：`redis.internal.example.com:6379`
- 连接需走 VPN 或堡垒机
- 密码存放在 Vault：`secret/redis/prod/password`

## 容量规划

- 单节点内存上限：32GB
- 当前使用率告警阈值：75%（warning）/ 85%（critical）
- 扩容流程见 `redis/troubleshooting.md` 的 "内存告警" 章节
