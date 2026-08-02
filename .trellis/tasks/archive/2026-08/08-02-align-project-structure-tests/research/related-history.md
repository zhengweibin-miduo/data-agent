# 相关任务与历史核验

## 已合并的结构工作

- `07-25-review-project-structure` 已完成并归档；其 PRD 验收项已全部勾选。该任务完成了 backend-only 基线、API/worker 组合根、共享 memory/MetaData、Compose 回环与 CI/README 修复。其历史研究曾指出 Conversation → DDL 依赖和 memory domain 全局配置问题；当前代码需重新核验，不应直接沿用旧结论。
- `08-01-separate-frontend-backend` 已归档并合入 `master`。历史 PRD 中“前端内嵌于 `src/data_agent/frontend`”是重构前事实；当前根 `frontend/` 已存在 React/Vite 应用，`src/data_agent/frontend/` 仅保留显式 legacy 开关后的兼容静态资源。
- `08-01-project-architecture-rules` 已归档并合入。它只建立规则，不承诺迁移所有源码；当前 `AGENTS.md` 中的源码所有权、后端渐进 DDD/Ports-and-Adapters、前端轻量 feature-first 是本任务的目标规则。
- `08-01-evaluate-tdd-guidance` 已归档并合入。它确立公共 seam、可观察行为、避免内部 collaborator mock、垂直切片与完成验证要求，本任务可直接复用，不需再次制定一套测试规范。

## 当前基线

- 任务创建时 `master` 与 `origin/master` 均为 `9bd8643`。
- 相关历史任务均已合并；本任务不是继续未合并的旧分支，而是验证“规则落地后，真实源码和测试是否仍漂移”。
- 独立前端已经落地，因此不能把 legacy 目录的存在本身当作“前后端未分离”；只有默认挂载、业务源码回流、构建耦合或跨端内部依赖才构成违规。

## 对本任务的约束

- 不重复实施已经完成的前后端拆分。
- 将“规范文本漂移”和“代码违反规范”分开：新增 bounded context 未写入目录规范可能需要补规范，也可能需要迁移代码，必须基于依赖方向判断。
- 测试重构以现有公共 seam 规则为依据，先确认回归保护，再删除或合并重复用例。
