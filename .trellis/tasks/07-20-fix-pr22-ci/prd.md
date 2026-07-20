# 修复 PR 22 CI 检查

## Goal

修复 PR #22 中 `quality` 作业的 MySQL 集成测试事件循环冲突，使新增会话仓储测试遵守共享异步引擎的生命周期约束，并通过与 CI 一致的 pytest 检查。

## Background

- GitHub Actions run `29736411633` 的 Ruff、Pyright、编译和配置检查均通过，唯一失败步骤是 `Run pytest suite`。
- 失败测试为 `tests/integration/persistence/test_conversation_repository.py::test_extraction_claims_one_ordered_turn_per_conversation`。
- pytest 为每个异步测试使用 function-scoped event loop；同文件第一个测试初始化全局 `MySQLDatabase` 后没有关闭，第二个测试复用旧连接池时触发 `got Future ... attached to a different loop`。
- `.trellis/spec/backend/database-guidelines.md` 要求集成测试通过 `finally` 始终执行 `await MySQLDatabase.close()`，现有仓储集成测试也采用该模式。

## Requirements

- 在新增会话仓储集成测试中补齐确定性的 MySQL 全局引擎清理。
- 清理必须位于 `finally` 路径，确保测试成功或失败时都释放引擎并清空 Session 工厂。
- 不修改生产数据库生命周期实现，不扩大到与失败日志无关的重构。
- 保留现有按会话数据清理和测试断言语义。

## Acceptance Criteria

- [x] 连续运行两个会话仓储集成测试时不再跨事件循环复用 MySQL 连接池。
- [x] `uv run pytest tests/integration/persistence/test_conversation_repository.py` 通过。
- [x] `uv run ruff check src tests`、`uv run pyright src tests` 和 `git diff --check` 通过。
- [x] 修复 diff 仅包含必要的测试生命周期清理与本任务 Trellis 元数据。

## Out of Scope

- 修改 `MySQLDatabase` 的生产单例模型。
- 调整 pytest 的事件循环作用域。
- 修复或重构 PR #22 中与本次 CI 失败无关的功能。
