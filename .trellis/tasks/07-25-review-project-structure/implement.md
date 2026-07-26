# 项目结构修复实施计划

## 1. 领域与共享模块迁移

- [x] 新增根级 `CONTEXT.md`，明确 DDL Metadata、Conversation、Long-term Memory 的领域含义。
- [x] 将 `ddl_metadata/models/` 原子移动到 `data_agent/models/`，更新生产代码、测试和当前规范导入。
- [x] 将 `ddl_metadata/memory/` 原子移动到 `data_agent/memory/`，更新生产代码、测试和当前规范导入。
- [x] 将 `ddl_metadata/errors.py`、`ddl_metadata/identifiers.py` 提升到根级，更新符号和导入。
- [x] 将共享 SQLAlchemy `MetaData` 移到 `data_agent/persistence/schema.py`，确保三组 tables 使用同一实例。
- [x] 搜索并消除 `conversation -> ddl_metadata` 与所有活动旧路径。

## 2. Domain 纯度

- [x] 增加不可变 `MemoryVersions`，由 workflow/application 显式传入。
- [x] 删除 memory domain 对 settings 的导入。
- [x] 更新全部生产与测试调用方。
- [x] 增加测试证明版本透传且 UID/content hash 不受版本变化影响。

## 3. 本地运行与治理

- [x] 将 Compose 所有宿主端口绑定到 `127.0.0.1`。
- [x] 为 API 增加 `[project.scripts]` 入口。
- [x] 扩写 README，记录安装、Compose、MySQL bootstrap、API、worker 和验证命令。
- [x] CI 增加 Compose 配置渲染。
- [x] 更新 `.trellis/spec/backend/` 中的目录、导入路径、所有权和质量命令。

## 4. 验证

- [x] `uv lock --check`
- [x] `uv run ruff check src tests`
- [x] `uv run pyright`
- [x] `uv run python -m compileall -q src`
- [x] `uv run python -m data_agent.settings`
- [x] `uv run pytest -m "not integration and not tei" -q`
- [x] `docker compose -f docs/docker/docker-compose.yml config`
- [x] `git diff --check`
- [x] 搜索活动源码、测试与当前规范，确认旧路径和 `conversation -> ddl_metadata` 为零。
- [x] 尝试运行 `uv run pytest -m "integration and not tei" -q`；结果为 `19 failed, 1 passed, 61 deselected`。失败集中在 MySQL/Redis 接通后重置连接，且 Docker Desktop daemon 不可用，因此按外部依赖不可用记录，不声明集成测试通过。

## 5. 检查门禁

- [x] Trellis 实施代理完成主体改动；超过项目十分钟阈值后由主会话中止并完成剩余修复。
- [x] Trellis 检查代理按 PRD、设计、项目规范和 `code_review.md` 独立检查，未发现遗留问题。
- [x] 主会话抽查高风险移动、共享 `MetaData`、错误映射和版本 UID/hash 证据。
- [x] 不提交、不推送；等待用户另行授权。
