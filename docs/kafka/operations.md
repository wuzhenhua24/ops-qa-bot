# Kafka 运维手册

## 集群信息

- 版本：Kafka 3.6.1（KRaft 模式，无 ZooKeeper）
- broker 数：5
- bootstrap 地址：`kafka.internal.example.com:9092`
- 默认副本数：3，min.insync.replicas：2

## Topic 管理

### 创建 topic
```bash
kafka-topics.sh --bootstrap-server kafka.internal.example.com:9092 \
  --create --topic <name> \
  --partitions 12 --replication-factor 3
```

分区数建议：根据消费者并发数设置，通常 12 或 24。一旦创建，**增加分区容易，减少分区需要重建 topic**。

### 查看消费延迟
```bash
kafka-consumer-groups.sh --bootstrap-server kafka.internal.example.com:9092 \
  --describe --group <group-id>
```

关注 `LAG` 列，持续增长说明消费跟不上。

## 常见故障

### 消费延迟暴涨
1. 检查消费者实例数是否 < 分区数（并发不足）
2. 检查消费者处理耗时（业务日志 + 监控）
3. 看是否卡在某条消息（毒消息）：日志搜索消费者异常堆栈
4. 临时手段：重置 offset 到最新（会丢未消费消息，需业务同意）
   ```bash
   kafka-consumer-groups.sh --bootstrap-server ... \
     --group <group-id> --topic <topic> --reset-offsets --to-latest --execute
   ```

> ⚠️ reset-offsets 会跳过未消费消息，必须先与业务方确认是否可丢弃。

### broker 宕机
- 副本数 3 的 topic 可容忍单 broker 宕机
- 监控会自动告警，SRE on-call 接手
- 业务侧一般无需操作，除非出现 `NOT_ENOUGH_REPLICAS` 错误
