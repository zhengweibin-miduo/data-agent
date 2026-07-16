# 元数据同步脚本技术设计

## Architecture

本任务保留为一个任务，不拆父子任务：配置解析、DW 读取、Meta MySQL 写入、Qdrant 向量写入和 Elasticsearch 值索引共同组成一次可重放的同步流程，拆开后无法独立满足用户验收。

采用当前仓库的单数包实现 MVC-like CLI 分层：

- Config：`app/conf/meta_config.py` 定义严格的 Pydantic 配置模型与 YAML 加载边界。
- Model：`app/model` 使用 SQLAlchemy 映射 Meta MySQL 的四张表。
- Entity：`app/entity` 使用 dataclass 定义跨 Service、Repository、Qdrant 和 Elasticsearch 的业务实体。
- Controller：`app/script/sync_metadata.py` 解析命令参数、配置日志、初始化/关闭客户端并组装依赖。
- Service：`app/service/metadata_sync_service.py` 校验 DW 结构、把 Config 转为 Entity、生成稳定 ID 并编排各存储写入。
- Repository：`app/repository/metadata_repository.py` 接收 Entity，通过 Model 完成一个 managed MySQL Session 的幂等持久化，并封装 Qdrant、Elasticsearch 的具体读写。
- View：CLI 任务只通过既有 Loguru 输出阶段和计数，不新增 View 包或 HTTP 层。

只建立一个 `MetadataRepository` 和一个 `MetadataSyncService`。当前只有单向写入，不新增 Repository 接口、工厂或一次性 Mapper；Repository 直接使用 `dataclasses.asdict()` 将 Entity 转为 ORM upsert 参数。

## File Boundaries

| File | Responsibility |
| --- | --- |
| `conf/meta_config.yaml` | 5.3 的五表两指标示例配置，AOV 关联字段修正为 `fact_order.order_amount` |
| `app/conf/meta_config.py` | 配置模型、角色枚举约束、重复项和相关字段校验、UTF-8 YAML 加载 |
| `app/model/*.py` | `table_info`、`column_info`、`metric_info`、`column_metric` 四张 Meta 表的 SQLAlchemy 映射 |
| `app/entity/*.py` | `TableInfo`、`ColumnInfo`、`MetricInfo`、`ColumnMetric`、`ValueInfo` 业务 dataclass |
| `app/repository/metadata_repository.py` | DW 结构/取值查询、Entity 到 Model 的 Meta MySQL upsert、Qdrant collection/upsert、Elasticsearch index/bulk |
| `app/service/metadata_sync_service.py` | 同步数据流、Config 到 Entity 转换、embedding 分批、稳定 ID、日志计数 |
| `app/script/sync_metadata.py` | `--config/-c` CLI 与所有外部客户端生命周期 |
| `app_test/service/test_metadata_sync_service.py` | 一个可直接运行的最小测试模块，覆盖配置、转换、过滤、稳定 ID 和错误路径 |

`app/repository/__init__.py` 是用户已有暂存文件，保持原样。测试目录只补必要的无副作用包标记。

## Data Flow

```text
YAML path
  -> MetaConfig.from_yaml() strict validation
  -> MetadataSyncService.sync(config)
  -> MetadataRepository.get_dw_schema()
  -> validate every configured table/column before dynamic queries
  -> read 10 distinct non-null examples per column
  -> build TableInfo/ColumnInfo/MetricInfo/ColumnMetric entities
  -> Repository converts entities into SQLAlchemy Model upserts
  -> TEI embed column/metric name + description + aliases in batches of 10
  -> wrap the same text as Qdrant core BM25 Document input
  -> ensure/upsert Qdrant column and metric hybrid collections
  -> read up to 100,000 distinct non-null values for sync=true columns
  -> build ValueInfo entities and bulk-upsert Elasticsearch value index
```

配置模型是未经信任 YAML 的唯一解析边界；Service 是跨字段业务校验和数据转换的唯一所有者；Repository 不重新解释配置语义。

## Model and Entity Contract

- `app/model/base.py` 定义本地 `Base(DeclarativeBase)`；四个表 Model 使用 `schema="meta"`，字段长度、JSON/Text 类型、可空性和复合主键严格匹配 `docs/docker/mysql/meta.sql`。
- Model 类名沿用 5.3 原文：`TableInfoMySQL`、`ColumnInfoMySQL`、`MetricInfoMySQL`、`ColumnMetricMySQL`；不增加 DDL 中不存在的外键、relationship、时间字段或虚构 ID。
- 业务 Entity 类名沿用原文：`TableInfo`、`ColumnInfo`、`MetricInfo`、`ColumnMetric`、`ValueInfo`，均使用标准库 dataclass；`ValueInfo` 只写 Elasticsearch，不建立 MySQL Model。
- Config 只在 Service 边界出现，Repository 的写入方法只接收业务 Entity；Qdrant payload 和 MySQL 参数从 Entity 生成。
- 当前没有 ORM 查询返回和双向转换需求，因此不建立四组一次性 Mapper；出现第二条双向转换路径时再评估提取。

## Configuration Contract

- `tables`、`metrics` 默认空列表，未知键通过既有 `ConfigModel(extra="forbid")` 拒绝。
- 表角色限定为 `dim | fact`。
- 字段角色限定为 `primary_key | foreign_key | dimension | measure`。
- 表名、同一表字段名、指标名不得重复。
- 每个 `relevant_columns` 必须采用 `<table>.<column>`，并引用当前配置中存在的字段。
- YAML 文件不存在、结构非法或字段引用非法时保留原始文件/YAML/Pydantic 异常，不增加项目异常层。

## MySQL Contract

- 复用 `MysqlClientManager.initialize()` 与 `MysqlClientManager.session()`；不创建第二个 engine。
- 通过 `information_schema.columns` 一次取得配置表的真实 `COLUMN_NAME` 和 `COLUMN_TYPE`。
- 在任何动态标识符查询前确认配置表/字段存在于该结果，并仅允许简单 MySQL 标识符；数据值继续使用绑定参数。
- DW 查询显式使用 `dw.<table>`，Meta 写入显式使用 `meta.<table>`。
- Repository 使用 `sqlalchemy.dialects.mysql.insert(Model).on_duplicate_key_update(...)`；`table_info`、`column_info`、`metric_info` 更新非主键字段，`column_metric` 使用等价的幂等插入。
- ORM `JSON` 列直接接收 Entity 中的 Python list，禁止预先 `json.dumps()` 造成双重 JSON 编码。
- 一个 managed Session 覆盖本次 Meta 写入；正常结束由 manager 提交，任一异常由 manager 回滚并原样抛出。
- 不删除配置之外的旧行。

## Qdrant Contract

- 字段 collection：`data-agent-column`；指标 collection：`data-agent-metric`。
- 当前 TEI 契约固定 `BAAI/bge-small-zh-v1.5` 和 512 维向量；保留匿名 dense 向量，使用 Cosine + 512，并新增命名 `bm25` sparse 关键词向量与 `Modifier.IDF`。
- 每个名称、说明、别名各形成一个点；embedding 文本不重复时才生成点。
- 点 ID 使用标准库 UUID5，由实体类型、实体稳定 ID、文本角色和文本共同派生；相同配置重复运行得到相同点 ID。
- payload 保存规范化后的字段或指标元数据；点的 `vector` 同时包含匿名 dense 键 `""` 与命名 sparse 键 `"bm25"`，后者为 `Document(text=value, model="Qdrant/bm25", options=...)`。
- BM25 配置固定为 `k=1.2`、`b=0.75`、`avg_len=256`、`TokenizerType.MULTILINGUAL`、`language="none"`、`lowercase=True`。Qdrant core 负责词频饱和和文档长度归一化，collection 的 `Modifier.IDF` 负责语料级逆文档频率；不再在应用侧维护 token、哈希或词频公式。
- 新 collection 直接创建匿名 dense + 命名 `bm25`。已有匿名 512 维 Cosine collection 若尚无 `bm25`，使用 `create_vector_name()` 原地新增，不删除、不重建；已有 `bm25` modifier 不为 IDF、dense 命名/维度/距离不兼容时明确失败。已有旧 `sparse` 向量保持原样，避免 raw-TF 与 BM25 在同名向量中混算。
- TEI 每批返回后校验每个 dense 向量长度为 512；Repository 校验 sparse 分支是模型名和参数完全匹配的非空 BM25 `Document`；任一不匹配时在 Qdrant upsert 前失败。
- 不删除已经从配置移除的旧点；Qdrant 请求错误原样传播。
- 后续查询必须用完全相同的 `Qdrant/bm25` `Document` 配置查询 `bm25`，再分别召回匿名 dense 与 `bm25`，最后通过 RRF 等策略融合；检索实现不属于本同步脚本。

## Elasticsearch Contract

- 索引名：`data-agent-value`。
- 映射固定为：`id` keyword、`value` text（`ik_max_word` analyzer/search analyzer）、`column_id` keyword；本地 Docker 镜像已安装 IK 插件。
- 仅处理 `sync: true` 字段，每字段最多读取 100,000 个去重非空值。
- `_id` 使用标准库 SHA-256 从 `column_id` 与规范化值派生，避免超长或特殊字符导致不稳定 ID；payload 保留原值和字段 ID。
- 复用 Elasticsearch 包内置 `helpers.async_bulk` 分块幂等写入并检查 item 失败；不删除旧文档，bulk/映射/连接错误原样传播。

## Lifecycle and Failure Behavior

Controller 在进入业务流程前调用 `setup_logging()`，然后显式初始化 MySQL、Qdrant、Elasticsearch 和 TEI。所有 manager 的 `close()` 放在一个 `finally` 中，关闭未初始化 manager 仍保持既有无害语义。

跨 MySQL、Qdrant、Elasticsearch 不提供伪事务：外部写入中途失败时，MySQL managed Session 回滚，但已完成的外部 upsert 可能保留。由于所有外部 ID 稳定，修复外部依赖后重跑相同配置即可收敛，不需要补偿框架。

日志只记录阶段、对象数量和安全标识，不输出数据库 URL、密钥、完整大字段值或整份配置。

## Compatibility and Trade-offs

- 复用 PyYAML/Pydantic，不引入文档示例中的 OmegaConf。
- 不引入 `fastembed`、分词模型或运行时模型下载；远端 `AsyncQdrantClient` 将 `Qdrant/bm25` `Document` 原样交给 Qdrant 1.18 core 处理。`avg_len=256` 显式固定为 Qdrant 默认值，后续只有在检索评测证明需要时才调参。
- 使用用户指定的单数 `app/model`、`app/entity` 目录，不复制文档的复数兼容目录。
- 一个 Repository 同时封装三种存储是当前单一同步用例的最小边界；当第二个独立消费者或独立生命周期出现时再拆分。
- 单字段 100,000 值上限沿用 5.3 示例；超过该规模时需要分页/流式同步，当前不预建该机制。
- upsert-only 保证安全重放，但配置删除、别名删除和 DW 值消失不会自动清理旧索引；当前配置中的稳定 ID 点会在重跑时增加 `bm25`，配置外旧点可能仍只有 dense 或旧 `sparse`。若要求所有历史点都参与 BM25，需另建显式回填任务，不能在本脚本中暗中删除。

## Rollback

删除本任务新增的配置、Model/Controller/Service/Repository 和测试文件即可回滚应用代码。该操作不自动回滚已经 upsert 到 Meta MySQL、Qdrant 或 Elasticsearch 的数据；如需数据回滚，应由操作者明确删除对应稳定 ID/collection/index，不能由本脚本猜测清理范围。
