# 5.3 配置与仓库适配研究

## 来源

- 文档：`D:/dev/智能问数/1.笔记/尚硅谷大模型项目之掌柜问数.docx`
- 章节：第 5 章“元数据知识库”中的 5.3“具体实现”
- 相关段落：配置文件、入口脚本、核心业务逻辑、数据读写操作
- 本机缺少 LibreOffice，`render_docx.py` 无法完成页面渲染；内容通过工作区绑定 Python 和 `python-docx` 结构化提取核对。

## 配置契约

根节点包含：

- `tables`: 可选表配置列表。
  - `name`: DW 真实表名。
  - `role`: `dim` 或 `fact`。
  - `description`: 表业务说明。
  - `columns`: 字段列表。
    - `name`: DW 真实字段名。
    - `role`: `primary_key`、`foreign_key`、`dimension` 或 `measure`。
    - `description`: 字段业务说明。
    - `alias`: 字段别名列表。
    - `sync`: 是否将该字段的去重取值同步到 Elasticsearch。
- `metrics`: 可选指标配置列表。
  - `name`: 指标名。
  - `description`: 指标业务含义与口径。
  - `relevant_columns`: `<table>.<column>` 格式的相关字段列表。
  - `alias`: 指标别名列表。

示例配置包含五张表：`dim_region`、`dim_customer`、`dim_product`、`dim_date`、`fact_order`；包含两个指标：`GMV`、`AOV`。

## 同步目标

1. 从 `dw` 读取配置表的真实字段类型和字段示例值。
2. 将表、字段、指标及字段指标关系写入 `meta.table_info`、`meta.column_info`、`meta.metric_info`、`meta.column_metric`。
3. 对字段和指标的名称、说明、别名生成 embedding，并分别写入 Qdrant。
4. 对 `sync: true` 字段的去重值建立 Elasticsearch 全文索引。

## 5.3 的 Model 与 Entity 分层

- Meta MySQL 表实体：`TableInfoMySQL`、`ColumnInfoMySQL`、`MetricInfoMySQL`、`ColumnMetricMySQL`，分别映射 `table_info`、`column_info`、`metric_info`、`column_metric`。
- 业务实体：`TableInfo`、`ColumnInfo`、`MetricInfo`、`ColumnMetric`、`ValueInfo`，原文使用 dataclass；其中 `ValueInfo` 只用于 Elasticsearch。
- 原文另建四组 Mapper 做双向转换；当前同步流程只有 Entity 到持久化参数的单向写入，先由 Repository 使用 `asdict()` 完成，不增加一次性 Mapper。
- 原文个别 Mapper 导入的 Model 文件名与定义标题不一致；实现统一使用用户指定的单数 `app/model`、`app/entity` 包和清晰的实体文件名。

## 已确认的适配决定

- 用户确认所有写入采用幂等 upsert，不自动删除配置之外的旧数据。
- 5.3 原文把 AOV 描述为平均订单金额，却关联 `fact_order.order_quantity`；用户确认修正为 `fact_order.order_amount`。
- 文档示例使用 `app/scripts`、`app/services`、`app/repositories` 和 OmegaConf；当前仓库使用单数包，并按用户修正新增单数 `app/model`、`app/entity`，不引入 OmegaConf 或复数兼容目录。
- 文档早期配置以 1024 维 embedding 为例；当前仓库固定 TEI 模型 `BAAI/bge-small-zh-v1.5`，规范和集成检查均要求 512 维，因此 Qdrant collection 使用 512 维。
- 文档示例使用随机 UUID 写 Qdrant 且 MySQL 仅 insert，重复执行会产生重复向量或主键冲突；本任务改为稳定 ID 与 upsert，以满足用户确认的同步语义。
- 当前 `mysql.url` 默认连接 `meta`，同一账号拥有 `dw.*` 与 `meta.*` 权限；使用一个 managed Session 和限定库名访问两库，不新增第二套 MySQL 客户端配置。

## 文档示例中不直接照搬的缺陷

- `metric_qdrant_repository` 代码段误复制了列 collection/class 名称。
- 动态 SQL 直接拼接调用方提供的表名和字段名，缺少结构校验。
- 外部客户端关闭不在 `finally` 中，异常时可能泄漏资源。
- Qdrant 使用随机 UUID，重复执行会无界追加逻辑重复点。
- MySQL 写入仅调用 `add_all`，重复执行会触发主键冲突。
