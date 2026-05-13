# Redis 飞书文档索引

> 维护人：张三（ou_REPLACE_WITH_REAL_ID_1）。本文件登记 Redis 相关的飞书云文档，
> 作为本地 md 之外的补充知识源。bot 通过 `ask_feishu_doc` 工具按需读取。
>
> 新增条目按下面格式补一段即可，注意"覆盖"那一行要写清楚关键词/场景。

## Redis 集群扩容 SOP（示例）

- URL: https://feishu.cn/docx/REPLACE_WITH_REAL_DOC_TOKEN_1
- 覆盖：集群分片重平衡、新节点上线、灰度扩容、reshard 操作、扩容期间的流量切换、回滚步骤
- 更新于：2026-04

## Redis 慢查询调优手册（示例）

- URL: https://feishu.cn/wiki/REPLACE_WITH_REAL_WIKI_TOKEN_1
- 覆盖：slowlog 分析、常见慢命令模式（KEYS / 大集合扫描 / Lua 脚本超时）、参数调优（slowlog-log-slower-than、slowlog-max-len）、pipeline 优化
- 更新于：2026-03

## Redis 主从切换演练记录（示例）

- URL: https://feishu.cn/docx/REPLACE_WITH_REAL_DOC_TOKEN_2
- 覆盖：sentinel failover 演练步骤、手动 slaveof、切换后的数据校验、回切流程、演练 checklist
- 更新于：2026-02
