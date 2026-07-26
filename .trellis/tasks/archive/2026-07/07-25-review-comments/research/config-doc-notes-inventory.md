# 配置与文档备注盘点（非 `src/`/`tests/`）

## 范围与文件类型

- 运行配置：`conf/app_config.yaml`（YAML，87 行），当前没有注释/TODO。
- 本地基础设施：`docs/docker/docker-compose.yml`、`docs/docker/elasticsearch/Dockerfile`、`docs/docker/mysql/*.sql`。Compose/Dockerfile 无人工备注；SQL 中大量 `COMMENT` 属于数据库 schema 业务元数据，不是开发 TODO。
- 项目元数据：`pyproject.toml`（TOML）、`.github/workflows/ci.yml`（YAML）、`.codex/*.toml|json`、根 `README.md`、`AGENTS.md`、`code_review.md`。
- Trellis 文档/状态：`.trellis/workflow.md`、`.trellis/spec/**`、`.trellis/workspace/**`、任务 PRD/日志等 Markdown/JSONL。

## 应纳入维护性审查的备注

1. **Python 版本兼容性注释**：`pyproject.toml:6` 原文 `# ponytail: asyncmy 0.2.11 lacks a Windows Python 3.14 wheel; lift when available.`。这是唯一命中的 TODO 风格人工维护备注；应核验 asyncmy wheel 状态与 `<3.14` 上限是否仍需保留，且 `ponytail` 术语对贡献者含义不明，存在过期/不可执行风险。
2. **CI action 版本注释**：`.github/workflows/ci.yml:61` 原文 `uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b # v8.1.0`。这是可审查的 pin 版本人工备注，需确认 SHA 与 v8.1.0 是否对应；若升级 action，注释必须同步。
3. **Compose/Docker 运行约束（隐含备注）**：虽无注释，`docs/docker/docker-compose.yml:32-37` 固定 Elasticsearch `8.19.17`、禁用安全、512MB JVM；`49-61` TEI CPU 镜像及 2 分钟启动窗口；`63-74` Redis AOF 与健康检查。这些是维护性配置约束，若审查任务覆盖“人工备注/说明”，应连同 README 文档是否解释端口、凭据和启动等待策略一起核验。
4. **SQL COMMENT 语义**：`docs/docker/mysql/data_agent.sql:12-48` 等字段/表 COMMENT（如 `access_count` “真正进入召回结果的累计次数”）是运行时数据契约，应审查与代码字段语义一致性；不要把它们误报为注释风格问题。

## 代表性 SQL 证据

- `docs/docker/mysql/data_agent.sql:30-33`：`access_count`、`last_accessed_at`、`content_version`、`projection_version` 的中文 COMMENT，定义记忆热度与投影版本契约。
- `docs/docker/mysql/meta.sql:10-14`：表/字段 COMMENT 定义 DDL 元数据语义。
- `docs/docker/mysql/dw.sql:90-95`：日期维度 COMMENT 明确 `date_id` 格式 `YYYYMMDD`、月份取值 1 至 12。

## 应排除项

- `.trellis/workspace/**`（`index.md`、`journal-*.md`）是自动/人工会话日志；内容包含历史 PR、旧状态和过程备注，不应作为产品注释缺陷审查对象。
- `.trellis/tasks/archive/**`、历史 PRD/design/implement/research 文件是归档产物；除非审查目标明确要求历史文档，否则排除，避免把过期计划当当前规范。
- `.agents/skills/**` 与 `.codex/**` 下技能说明、模板、生成配置属于工具/第三方或平台元数据；只在审查 Codex/Trellis 平台本身时纳入。
- `.trellis/.template-hashes.json`、`uv.lock` 等机器生成锁定/哈希文件不做人工备注审查。
- SQL `COMMENT` 子句属于数据库 schema 元数据，不按普通源代码注释规则评判；仅在字段/表契约一致性审查时核对。

## 建议边界

优先审查根级 `pyproject.toml` 注释、CI action 版本注释、当前 `README.md`/`AGENTS.md`/`code_review.md` 的现行说明，以及 `conf/` 与 `docs/docker/` 中影响运行的配置语义。默认排除 Trellis 工作区/归档、平台技能和生成锁文件；对 SQL COMMENT 采取“契约一致性”而非“注释风格”标准。
