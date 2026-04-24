# MySQL 架构概览

## 部署

- 版本：MySQL 8.0.35
- 架构：一主两从 + MHA 自动故障切换
- 主库：mysql-prod-master.internal.example.com:3306
- 从库：mysql-prod-slave-{01,02}.internal.example.com:3306
- 读写分离由应用层 ProxySQL 接管

## 账号体系

- 应用账号：`app_rw` / `app_ro`，权限按库粒度隔离
- DBA 账号：需走堡垒机，禁止直连
- 账号申请：运维平台 > 数据库 > 申请账号
