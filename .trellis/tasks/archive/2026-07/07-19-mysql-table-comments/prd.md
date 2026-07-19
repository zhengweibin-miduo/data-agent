# 补齐 MySQL 建表备注

## Goal

为本地 MySQL 初始化脚本中的业务表补齐清晰、可直接由 MySQL 保存和展示的中文备注，便于开发者通过数据库管理工具理解表和字段用途。

## Background

- 真实初始化脚本位于 `docs/docker/mysql/meta.sql`、`docs/docker/mysql/data_agent.sql` 和 `docs/docker/mysql/dw.sql`。
- 三份脚本共创建 13 张表；`meta.sql` 已有字段备注，但没有表备注，另外两份脚本的表和字段均未设置备注。
- 仓库没有迁移框架；SQLAlchemy Core 表定义和本地初始化 SQL 共同维护关系型结构。

## Requirements

- R1：为三份初始化脚本中的 13 张表添加准确的中文表级 `COMMENT`。
- R2：为尚无备注的业务字段添加准确的中文列级 `COMMENT`，并保留 `meta.sql` 中已有且正确的字段备注。
- R3：备注应说明业务语义；状态、类型、版本、时间和关联标识等字段应能看出其具体用途，避免仅把英文名直译成中文。
- R4：不得改变字段类型、约束、索引、外键、初始化数据及建表顺序。
- R5：仅处理真实 MySQL 初始化脚本；测试夹具和测试内联 DDL 不属于本次范围。
- R6：SQLAlchemy Core 表定义继续与初始化结构兼容；本次不新增运行时建表逻辑，也不要求把数据库备注重复维护到 Python 定义中。

## Acceptance Criteria

- [x] AC1：`meta.sql`、`data_agent.sql`、`dw.sql` 中每个 `CREATE TABLE` 都包含中文表备注。
- [x] AC2：上述脚本中每个业务字段都包含中文列备注，且原有字段备注未丢失。
- [x] AC3：修改前后的表、列、约束、索引、外键和初始化数据保持一致。
- [x] AC4：通过静态检查确认 13 张表无遗漏，并验证三份 SQL 的 MySQL 语法；本地 MySQL 可用时执行实际初始化验证，否则明确报告环境限制。

## Out of Scope

- 修改测试代码中的示例 DDL。
- 改变数据库结构、约束、索引或样例数据。
- 为 SQLAlchemy Core 定义引入新的运行时建表流程。
