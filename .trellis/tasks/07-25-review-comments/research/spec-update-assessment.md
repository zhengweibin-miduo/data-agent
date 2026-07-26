# Code-Spec 更新判断

## 结论

更新 `.trellis/spec/backend/conversation-memory.md` 与
`.trellis/spec/backend/database-guidelines.md`，明确用户级和 DDL 级记忆修正的
活动版本、`requires_reprocess`、Meta 应用及来源租约边界。

## 判断依据

- `.trellis/spec/backend/quality-guidelines.md:63-74` 已覆盖一般 Docstring 与注释质量，无需重复。
- 原 Code-Spec 分别描述了用户记忆与 DDL 记忆，但没有把共享 service 的两个分支并列说明。
- 这次错误文案的根因正是混淆了“活动记忆版本立即生成”和“DDL 修正需完整工作流后才应用到 Meta”。
- 新规范明确用户级更新返回 `requires_reprocess=false`、不使用 DDL 来源租约；DDL 更新返回 `true`、使用来源租约，但其权威记忆版本同样立即活动。
- 删除仍是软删除加双目标 DELETE outbox，只有 DDL 作用域使用来源租约。
- 本次只记录现有可执行行为，没有改变 API、数据库或运行契约。
