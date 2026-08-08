# MySQL 兼容迁移

从 PR 基线版本升级保留数据卷时，请先备份数据库，将 `MEMORY_DATABASE` 设置为
`memory.database` 的实际值，再停服执行一次：

```bash
test -n "$MEMORY_DATABASE" && mysql --database="$MEMORY_DATABASE" \
  < docs/docker/mysql/migrations/20260808_conversation_lease_microseconds.sql
```

迁移先补齐新版 Conversation 运行时必需的 claim、放弃租约和消息语义指纹列，
再提升租约时间列精度；它不会删除或重写既有会话数据。该脚本以 PR 基线表结构为
输入，不得对已完成本次升级的数据库重复执行。
