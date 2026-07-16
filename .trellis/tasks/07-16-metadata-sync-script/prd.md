# 实现元数据同步脚本

## Goal

提供一个可从命令行运行的元数据同步脚本，读取《尚硅谷大模型项目之掌柜问数》5.3 节定义的 YAML 配置，将表、字段和指标元数据同步到 Meta MySQL，并同步构建 Qdrant dense 语义 + sparse 关键词混合索引与 Elasticsearch 字段值索引。

## Background

- 5.3 配置包含 `tables` 和 `metrics`：表配置定义真实表名、角色、说明、字段角色、别名及字段值是否同步；指标配置定义名称、说明、相关字段和别名。
- 示例配置覆盖 `dim_region`、`dim_customer`、`dim_product`、`dim_date`、`fact_order` 五张 DW 表及 `GMV`、`AOV` 两个指标。
- 仓库已有异步 MySQL、Qdrant、Elasticsearch、TEI Embedding 客户端管理器，以及 `app/script`、`app/service`、`app/repository` 包。
- 当前 MySQL 账号同时拥有 `meta.*` 与 `dw.*` 权限，可通过一个现有连接使用限定库名访问两库。
- `app/repository/__init__.py` 是用户已有的暂存改动，本任务必须保留且不得覆盖。

## Requirements

- R1. 在 `conf/meta_config.yaml` 提供 5.3 节的完整示例配置，并使用现有 PyYAML/Pydantic 能力严格校验配置结构，不新增配置解析依赖。
- R2. 命令入口位于 `app/script`，接受必填配置文件路径，并可通过 `uv run python -m app.script.sync_metadata --config <path>` 运行。
- R3. 按当前仓库的 MVC-like 分层组织：`script` 负责命令参数和客户端生命周期，`service` 负责同步编排并构造 `app/entity` 业务实体，`repository` 接收业务实体并通过 `app/model` 表实体执行 MySQL 持久化，同时封装 Qdrant 和 Elasticsearch 读写；不新增 HTTP Controller 或 View。
- R4. 表同步从 `dw` 读取真实字段类型和去重示例值，将 `table_info`、`column_info` 写入 `meta`；字段 ID 使用稳定格式 `<table>.<column>`。
- R5. 指标同步将 `metric_info` 和 `column_metric` 写入 `meta`，并校验 `relevant_columns` 引用配置中存在的字段。
- R6. 字段和指标的名称、说明、别名分别生成 512 维 dense 语义向量和 Qdrant 原生 BM25 sparse 关键词向量，并写入各自的 Qdrant collection；同一点必须同时携带两种向量，点 ID 必须稳定，重复运行不得无界追加重复点。
- R7. 仅对 `sync: true` 的字段读取去重非空值，并按 5.3 示例的单字段 100,000 条上限写入 Elasticsearch 字段值索引；文档 ID 必须稳定。
- R8. 复用现有客户端管理器和日志设施；正常或异常结束都必须关闭已初始化的外部客户端，错误向命令调用方传播并产生非零退出码。
- R9. 配置中的表名、字段名在用于动态 SQL 前必须通过 DW 实际结构校验，避免无效标识符和任意 SQL 片段进入查询。
- R10. 同步采用幂等 upsert：更新同 ID 数据、插入新数据，不自动删除本次配置之外的旧元数据或索引记录。
- R11. 示例配置修正 5.3 原文的 AOV 字段矛盾：`AOV.relevant_columns` 使用 `fact_order.order_amount`，与“平均订单金额”的定义一致。
- R12. 新增单数目录 `app/model` 与 `app/entity`：前者映射 Meta MySQL 的四张表，后者定义表、字段、指标、字段指标关系及 Elasticsearch 字段值五种业务实体；配置 Pydantic 模型继续留在 `app/conf`。
- R13. sparse 关键词分支使用 Qdrant 1.18 原生 `Document(model="Qdrant/bm25")` 与 `Bm25Config(k=1.2, b=0.75, avg_len=256, tokenizer=MULTILINGUAL, language="none", lowercase=true)`；collection 保持 `Modifier.IDF`，由 Qdrant 完成 BM25 词频饱和、文档长度归一化和 collection 级 IDF，不新增 `fastembed` 或运行时模型下载。查询端必须复用完全相同的 BM25 配置。
- R14. 新 collection 保留现有匿名 dense 向量并新增命名 `bm25` sparse 向量；已有兼容的匿名 dense collection 通过 Qdrant 原生 `create_vector_name()` 无损补充 `bm25` 配置，不删除、不重建，也不清理旧点。旧 `sparse` 名可能含 raw-TF 数据，必须保留但不得与 BM25 混写。

## Acceptance Criteria

- [x] AC1. 使用 `conf/meta_config.yaml` 运行时，脚本能解析并校验 5 张表、全部字段和 2 个指标，且 AOV 关联 `fact_order.order_amount`。
- [x] AC2. 单元测试证明配置到业务 Entity 的转换、Entity 到 ORM Model 的幂等持久化、稳定 ID、`sync: true` 过滤、dense+BM25 点结构及非法字段引用行为符合要求。
- [x] AC3. 重复执行相同配置不会因 MySQL 主键冲突失败，也不会在 Qdrant/Elasticsearch 中产生重复逻辑记录。
- [x] AC4. 配置路径不存在、YAML 结构非法、DW 表或字段不存在时，脚本明确失败且返回非零状态。
- [x] AC5. 实现仅落在配置、`model`、`entity`、`script`、`service`、`repository` 和对应测试范围，不引入新的第三方依赖或无关框架。
- [x] AC6. 通过仓库规定的 lock、Ruff、Pyright、`compileall`、配置加载和相关最小测试检查；外部服务不可用时如实报告，不能伪称集成验证通过。
- [x] AC7. 新建 Qdrant collection 同时具有匿名 512 维 Cosine dense 与命名 `bm25`/IDF 配置；已有匿名 dense collection 可无损补充 `bm25` 配置，不兼容配置在写入前明确失败。

## Acceptance Notes

- AC3 除四条 MySQL `ON DUPLICATE KEY UPDATE` 断言、稳定 Qdrant UUID5、
  稳定 Elasticsearch SHA-256 `_id` 及重复 Service 执行测试外，已在本地四服务
  上真实执行两次 CLI：Meta 行数稳定为 `5/24/2/2`，Qdrant 点数稳定为
  `98/8`，Elasticsearch 文档数稳定为 `75`，没有逻辑记录增长。
- AC4 已验证缺失配置和非法 YAML 的 CLI 非零退出，以及 DW 缺表/缺字段在
  任何取值或写入前失败。CLI 不捕获该业务异常，因此进程保持非零退出。
- AC2 额外核对四张 ORM 表的完整 DDL 契约、五种 dataclass 字段顺序、
  Service 到 Repository 的 Entity 边界，以及 ORM JSON 参数仍为 Python list。
- 所有静态与聚焦检查已通过；MySQL、Qdrant、Elasticsearch、TEI 启动后已完成
  真实双次同步、Qdrant dense 查询和 Qdrant core BM25 查询，详情记录在
  `implement.md` 的 Live Integration Record。
- 先前手工 token/词频/哈希实现只属于 lexical TF-IDF 风格 sparse，缺少 BM25
  的词频饱和与长度归一化，不能作为本次 BM25 验收证据；AC2、AC6 曾重新打开，
  并已在 Qdrant core BM25 修正和真实联调后重新验证通过。
- `AsyncQdrantClient(location=":memory:")` 会走本地 FastEmbed 路径，不能验证
  Qdrant core BM25；本次单元测试只核对 `Document`/`Bm25Config` 请求契约，真实
  BM25 服务端处理仅在可用的 Qdrant 1.18 HTTP 服务上验证，否则明确标记未执行。
- 聚焦测试通过真实远端模式客户端 facade 证明 `Document/Bm25Config` 会原样转发
  给 Qdrant core，并覆盖旧 `sparse` 不得继续写入、旧 collection 配置保留、
  `bm25` additive migration 和非法模型/参数拒绝；该测试不发网络请求。
- 最终 lock、Ruff、Pyright、`compileall`、配置、聚焦测试、CLI help 和
  `git diff --check` 全部通过；随后完成的真实联调证明四个外部客户端可共同执行
  同步，并且查询端使用相同 `Qdrant/bm25` 配置可以命中已写入的 BM25 索引。

## Out of Scope

- HTTP API、前端页面、定时调度和后台任务系统。
- 数据库迁移框架、ORM 自动建表、通用 Repository 基类、一次性 Mapper 层。
- 自动安装或启动 MySQL、Qdrant、Elasticsearch、TEI 服务。
- 自动清理已从 YAML 配置移除的旧表、字段、指标、别名或字段值记录。
- 检索 API、dense/sparse 两路召回、RRF 融合和 rerank；本任务只建立查询所需的两种索引，并固定查询端复用相同的 `Qdrant/bm25` 配置。
