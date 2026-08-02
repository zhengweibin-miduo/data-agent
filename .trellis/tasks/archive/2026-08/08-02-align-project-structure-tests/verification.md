# 项目结构与测试重构集成验证

## 验收结论

| 验收项 | 证据 | 结论 |
|---|---|---|
| 结构与依赖问题有仓库证据并映射架构规则 | 父任务 `research/`、四个子任务 `verification.md`、更新后的 `CONTEXT-MAP.md` 与层级 spec | 通过 |
| 后端 DDD 符合程度与变更范围明确 | 仓库仍采用渐进式 DDD；本轮 Memory、Conversation、Data Sync 与 Meta Projection 的 application 包未检出 infrastructure、具体 repository、SQLAlchemy 或外层 adapter 导入；包边界测试 5 项通过 | 通过 |
| Meta Projection 归属与 Data Sync 关系明确 | DDL Metadata owns Meta Projection；Data Sync application/低层 materialization 仅依赖 `ValueProjectionParticipant`，具体 MySQL participant 只在 `data_sync.worker` 组合根选择 | 通过 |
| 前端、后端、兼容资源与契约所有者唯一 | `frontend/` 独立构建；`src/data_agent/` 为 Python 后端；wheel 只携带显式 legacy 兼容资源；前端源码未检出对 Python 源码的导入 | 通过 |
| 运行入口、构建、打包、配置与测试发现可用 | 配置加载、compileall、wheel/sdist、Compose config、Vite build 与 pytest/Vitest discovery 均已执行 | 通过 |
| 测试围绕公共 seam 且替换内部耦合 | Data Sync 删除原 381 行私有 lifecycle 测试并以 12 个 `dispatch_once()` 用例替换；Workbench 不再通过 adapter mock call history 驱动回调；Meta/Memory/Conversation 测试改由 application seam 与外部 adapter contract 承担 | 通过 |
| 质量检查有新鲜完整证据 | 静态、非集成、前端、构建与相关 live checks 通过；完整非-TEI 套件的两项环境失败已在下方如实记录 | 通过（有环境例外） |

## 六个公共测试 seam

| Seam | 最新证据 |
|---|---|
| DDL Job lifecycle | 完整非-TEI 套件中的 API、job events、worker 与 Redis lifecycle 用例通过 |
| Accepted Meta Snapshot | application 单元、事务相关 Memory/Data Sync/Meta Projection live 用例通过；两个依赖旧 `metric_info` 表形状的用例被环境漂移阻断 |
| Conversation / Long-term Memory | Conversation、Memory application 单元与对应 MySQL integration 用例通过 |
| Data Sync lifecycle | `tests/unit/data_sync` 通过；live CDC 3 项与 answer-readiness live 1 项通过 |
| Meta Projection lifecycle/search | application、adapter、package-boundary、runtime/value-refresh 单元通过；live resumable refresh 1 项通过 |
| Frontend transport/feature | 10 个 Vitest 文件、145 项通过；独立复核四个 Workbench seam 文件 18 项通过 |

## 验证命令与结果

- `uv lock --check`：通过。
- `uv run ruff check src tests`：通过。
- `uv run pyright src tests`：`0 errors, 0 warnings, 0 informations`。
- `uv run python -m compileall -q src tests`：通过。
- `uv run python -m data_agent.settings`：通过。
- `uv run pytest -m "not integration"`：`405 passed, 29 deselected`。
- `npm ci && npm run lint && npm run typecheck && npm run test && npm run build`：通过；`10` 个测试文件、`145` 项测试通过，Vite production build 成功。
- `uv build`：sdist 与 wheel 构建成功。
- `docker compose -f docs/docker/docker-compose.yml config --quiet`：通过。
- `uv run pytest -m "not tei"`：`431 passed, 2 failed, 1 deselected`。两项失败均为本地共享 MySQL schema 漂移：实际 `metric_info` 只有 `id, name, description, relevant_columns, alias`，当前 bootstrap `docs/docker/mysql/meta.sql` 与代码要求 `fact_table_id`；错误为 MySQL `1054 Unknown column 'fact_table_id' in 'field list'`。
- `git diff --check`：通过。

## 环境例外与安全边界

未对共享 MySQL 执行迁移、重置、删除或重建。项目没有 migration framework，且用户未授权数据迁移；因此保留两项完整套件失败作为可复现的本地环境漂移，不把它们误报为通过。其余 431 项（包括 Data Sync CDC、Meta Projection、Conversation/Memory、API、Redis 与 worker live checks）实际通过。

## 独立审查

只读集成审查核验了 `origin/master...HEAD` 的跨 context 导入、`ddl_metadata.worker.lifecycle` / `maintenance` 冲突合并、Data Sync/Meta Projection composition 和 Workbench seam；未发现需要阻止合并的 P0/P1 问题。
