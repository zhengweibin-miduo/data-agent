# MySQL 兼容迁移

升级保留数据卷的部署时，请将 `MEMORY_DATABASE` 设置为 `memory.database` 的实际值后执行：

```bash
test -n "$MEMORY_DATABASE" && mysql --database="$MEMORY_DATABASE" \
  < docs/docker/mysql/migrations/20260808_conversation_lease_microseconds.sql
```

迁移只提升 Conversation 租约时间列的精度，不删除或重写既有会话数据。
