# 技术设计

## 边界与数据流

审查线程输入 → 结构化 publisher invocation → PR 查询参数（必须是 PR #76 的实际 head）→ publisher 处理三条线程。

本任务的修复点位于本地 checkout/task 分支到 PR head 的映射边界：本地分支名可以继续用于开发，但不得直接作为 PR 查询 head，除非二者已被验证相同。

## 方案

1. 检查 PR #76 的远端元数据和现有 publisher/workflow 参数来源，确定实际 head 分支。
2. 选择最小修复点：优先在现有 invocation 或分支解析层显式传递已验证的 PR head；仅在必要时调整 publisher tooling。
3. 保留 publisher 的统一处理和结果校验，不添加直接线程回复或 resolve 分支。
4. 用 dry-run/测试先验证分支查找，再验证三条线程的 publisher 请求均可构造并处理。

## 兼容性与回滚

未改变 PR 内容或线程状态，只改变 PR head 的解析/传递。若验证失败，可回滚该映射层改动，不涉及共享分支历史改写。
