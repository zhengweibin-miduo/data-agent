# 重构 Python 项目结构与命名注释规范

## Goal

将当前 Python 后端从通用横向目录迁移到可安装的 `src/data_agent`
业务功能优先结构，统一公开标识符与中文 Google Style Docstring，并把
Ruff 的公开对象 Docstring 检查纳入本地和 CI 质量门禁。

本次重构应提升代码定位、导入隔离、职责表达和自动文档质量，同时保持
现有 DDL 元数据 API、LangGraph 工作流、arq worker、MySQL/Redis 持久化
及错误处理行为不变。

## Background

- 当前运行时代码位于 `app/`，测试位于 `app_test/`，入口为根目录
  `main.py`。
- DDL 元数据功能分散在 `api/`、`model/`、`service/`、`repository/`
  和 `worker/` 横向目录中。
- 当前存在 `MysqlClientManager`、`LlmClientManager`、
  `DdlJobRequest` 等不符合目标职责或缩写规则的公开类名。
- 当前源码已有中文短 Docstring，但 `pyproject.toml` 未持久化 Ruff
  Docstring 规则，CI 仅使用 Ruff 默认配置。
- `.github/workflows/ci.yml` 以及 `.trellis/spec/backend/` 的多份规范
  明确引用旧路径、旧测试模块和 `*ClientManager` 约定，必须随迁移同步
  更新。

## Requirements

### R1. Packaged source layout

- 运行时代码迁移到 `src/data_agent/`。
- `pyproject.toml` 声明构建系统，使项目能由 `uv sync` 安装，并保证
  `data_agent` 从安装后的包导入。
- 根入口迁移到包内，ASGI/命令入口使用 `data_agent` 导入路径。
- 一次性硬切换所有内部和外部导入路径，不提供旧 `app.*` 兼容包。
- 删除迁移完成后不再使用的空目录和旧包，不保留两套实现。

### R2. Feature-first organization

- DDL 元数据代码聚合到 `data_agent/ddl_metadata/`。
- MySQL、Redis、Checkpoint、LLM、TEI、Elasticsearch 与 Qdrant 等共享
  技术设施聚合到 `data_agent/infrastructure/`。
- 配置加载、日志和应用组合入口保留在 `data_agent` 根包附近。
- 不引入没有当前职责依据的 `utils`、`common`、完整 DDD 或 CQRS 层级。

### R3. Tests

- `app_test/` 迁移为标准 `tests/`。
- 测试按 `unit/`、`integration/` 及业务功能组织；共享 fake、factory 和
  fixture 不再从其他 `test_*.py` 导入。
- 使用 pytest 统一收集和执行测试，不再保留逐模块
  `python -m tests...`、同步 `asyncio.run()` 包装或测试文件
  `if __name__ == "__main__"` 入口。
- 异步测试使用 pytest 的 async 支持；集成测试使用明确 marker，CI 在
  MySQL/Redis 服务就绪后执行完整测试。
- pytest 及其异步支持作为开发依赖由 `pyproject.toml` 和 `uv.lock`
  持久化。
- CI 和本地命令应测试安装后的 `data_agent` 包，避免依赖仓库根目录的
  偶然导入。
- 保持现有无外部付费 LLM 的 CI 行为和 MySQL/Redis 集成覆盖。

### R4. Identifier naming

- 包、模块、函数和变量使用 `snake_case`，类使用 `PascalCase`，常量使用
  `UPPER_SNAKE_CASE`。
- 固定技术缩写在类名中使用规范形式：`DDL`、`LLM`、`API`、`TEI` 和
  `MySQL`。
- 类名优先表达对外能力和职责，使用 `Client`、`Repository`、`Store`、
  `Service`、`Factory`、`Loader` 等精确后缀，移除不准确的 `Manager`、
  `Helper`、`Utils`。
- `Config`/`ConfigModel` 运行时配置类统一为 `Settings` 语义。
- 所有引用、错误消息、测试名和类型注解同步更新，不留下新旧公开名称
  混用。
- 允许 Pydantic 类名规范化同步改变 OpenAPI 自动生成的 schema component
  名称；API 路径、字段、状态码和 JSON 数据契约仍须保持不变。

### R5. Docstring and comments

- 采用 PEP 257 基础上的中文 Google Style Docstring。
- Google Style 章节标题保持 `Args:`、`Returns:`、`Yields:`、
  `Raises:`、`Examples:`、`Note:` 和 `Warning:`。
- 所有公开模块、类、函数和方法必须具有有效 Docstring。
- 公开 package、测试模块、fixture 和测试函数同样纳入 Docstring 检查。
- 简单公开对象使用单行 Docstring；存在参数特殊语义、返回约束、异常、
  副作用、事务、并发或生命周期要求时使用多行 Docstring。
- 类型已由注解表达时，Docstring 不重复类型。
- Pydantic 字段的字段级业务含义优先使用 `Field(description=...)`，不在
  类 Docstring 中机械重复。
- 行内注释解释原因、约束和删除条件，不复述代码；禁止保留注释掉的旧
  实现。

### R6. Enforced quality gate

- 在 `pyproject.toml` 中持久化 Ruff 配置并启用 `D` 规则及 Google
  convention。
- 中文 Docstring 可豁免仅适用于英文祈使语气或英文结束标点的规则，
  但不得豁免公开模块、类、函数和方法缺失 Docstring 的规则。
- CI 对 `src/` 与 `tests/` 运行 Ruff、Pyright、compileall、配置校验和
  现有测试。
- 更新 `.trellis/spec/backend/` 中所有仍代表当前约定的旧路径、旧名称
  和旧校验命令；归档任务和历史 journal 不做追溯改写。

### R7. Behavioral compatibility

- API 路径、请求响应字段、HTTP 状态映射和序列化结果保持不变。
- Redis key、checkpoint、job 状态机与恢复行为保持不变。
- MySQL 表名、列名、事务边界和现有数据兼容性保持不变。
- DDL 解析、验证、稳定 ID、LLM 调用约束和记忆逻辑保持不变。

## Acceptance Criteria

- [ ] `uv sync --locked` 能安装项目，`uv run python -c "import data_agent"`
      成功，活动代码不再导入 `app` 或 `app_test`。
- [ ] 运行时代码仅存在于 `src/data_agent/`，测试仅存在于 `tests/`，
      旧 `app/`、`app_test/` 和根 `main.py` 不再作为活动实现保留。
- [ ] 仓库中不存在为 `app.*` 提供导入转发的兼容包。
- [ ] DDL 元数据功能与共享 infrastructure 边界符合 R2，且不存在新的
      无职责 `utils`/`common` 包。
- [ ] 公开类名及其引用符合 R4；代码、测试、CI 和当前 Trellis 规范中
      不再出现被替换的公开名称。
- [ ] OpenAPI component 名称反映规范化后的 Pydantic 类名，API 路径、
      字段、状态码和 JSON 数据契约与重构前一致。
- [ ] 所有公开模块、类、函数和方法具有符合项目约定的 Docstring。
- [ ] Ruff 配置启用 Google Docstring convention；删除任一公开对象
      Docstring 会导致 Ruff `D` 规则失败。
- [ ] Ruff、Pyright、compileall、配置校验及所有现有非付费端点测试通过。
- [ ] pytest 能收集新的 `tests/` 套件，测试源码中不存在旧的
      `asyncio.run()` 包装入口或 `if __name__ == "__main__"` 测试入口。
- [ ] MySQL/Redis 可用时，repository、worker、API 和完整 DDL metadata
      integration flow 通过。
- [ ] `docker compose -f docs/docker/docker-compose.yml config` 和
      `git diff --check` 通过。
- [ ] `.github/workflows/ci.yml` 与 `.trellis/spec/backend/` 只引用新的
      活动路径、公开名称和校验命令。
- [ ] 除已允许的 OpenAPI component 名称规范化外，重构没有改变 API
      schema、Redis/MySQL 持久化契约或业务行为。

## Out of Scope

- 新增业务功能、API endpoint 或数据库表。
- 改写 LangGraph 工作流业务逻辑。
- 引入完整 DDD、CQRS、依赖注入框架或 ORM entity/migration 层。
- 追溯修改 `.trellis/tasks/archive/` 和 developer journal 中的历史路径。
- 联系真实付费 LLM 端点。
