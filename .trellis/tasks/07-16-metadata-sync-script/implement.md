# 元数据同步脚本实施计划

## Ordered Checklist

1. 新增 `conf/meta_config.yaml`，完整录入 5.3 的五表两指标配置，并把 AOV 相关字段修正为 `fact_order.order_amount`。
2. 新增 `app/conf/meta_config.py`：复用 `ConfigModel`，定义严格角色类型、列表默认值、重复项/相关字段验证和 `from_yaml()`。
3. 新增 `app/model` 和 `app/entity`：
   - 用 SQLAlchemy 映射 `meta` 四张表，严格匹配现有 DDL；
   - 用 dataclass 定义五种同步业务实体，`ValueInfo` 仅供 Elasticsearch；
   - 不增加 Mapper、外键关系、自动建表或配置模型副本。
4. 新增 `app/repository/metadata_repository.py`：
   - 从 `information_schema.columns` 读取 DW 结构并验证标识符；
   - 读取字段去重非空值；
   - 接收业务 Entity，通过 MySQL ORM Model 批量幂等 upsert 四张 Meta 表；
   - 创建或校验两个匿名 512 维 dense + 命名 `bm25`/IDF 的 Qdrant collection，已有 collection 原地新增 `bm25`；
   - 创建并通过内置 `async_bulk` 分块写入 `data-agent-value` Elasticsearch index。
5. 新增 `app/service/metadata_sync_service.py`：
   - 在写入前完成所有配置字段与 DW 结构校验；
   - 将 Config 构造为表、字段、指标、字段指标关系和字段值业务 Entity；
   - 为名称/说明/别名分批生成 dense embedding，并构造配置固定的 Qdrant core BM25 `Document`；
   - 为 Qdrant 点和 Elasticsearch 文档生成稳定 ID；
   - 只为 `sync: true` 字段同步最多 100,000 个去重非空值；
   - 输出安全的阶段与数量日志。
6. 新增 `app/script/sync_metadata.py`：提供 `-c/--config` 必填参数，调用 `setup_logging()`，组装现有 manager/client，并在 `finally` 中关闭所有外部客户端。
7. 新增一个 `app_test/service/test_metadata_sync_service.py` 可执行测试模块及必要包标记，覆盖：
   - 示例配置五表两指标和 AOV 修正；
   - 配置未知键、重复项、非法相关字段失败；
   - DW 缺表/缺字段在写入前失败；
   - Config 到 Entity、Entity 到 ORM Model upsert、`sync` 过滤、embedding 输入和 payload 转换；
   - 四张 ORM 表的 schema、表名、JSON 类型和关系表复合主键；
   - 相同输入重复处理产生相同 Qdrant/Elasticsearch ID；
   - BM25 `Document` 的模型名、参数和中英文 multilingual tokenizer 契约，以及新建/升级/拒绝不兼容 Qdrant collection。
8. 运行完整静态和最小单元验证；仅在本地四个外部服务均可用且确认是示例环境时运行真实同步命令。
9. 质量检查通过后，由主会话按 Phase 3 更新 `.trellis/spec/backend/` 中新增的目录、同步数据访问和验证契约；未经额外 Git 授权不提交或推送。

## Validation

依次执行：

```powershell
uv sync --locked
uv lock --check
uv run --with ruff ruff check app app_test
uv run --with pyright pyright app app_test
uv run python -m compileall -q app app_test main.py
uv run python -m app.conf.app_config
uv run python -m app_test.service.test_metadata_sync_service
git diff --check
```

如果 MySQL、Qdrant、Elasticsearch 和 TEI 均为可写的本地示例服务，再运行：

```powershell
uv run python -m app.script.sync_metadata --config conf/meta_config.yaml
```

真实同步后至少核对：

- Meta MySQL 中五张表、配置字段、两个指标和字段指标关系存在；
- 第二次运行无主键冲突；
- Qdrant 使用两个独立的匿名 512 维 dense + 命名 sparse/IDF collection，重复运行逻辑点数量不增长；
- Elasticsearch 只包含 `sync: true` 字段的值，重复运行同 `_id` 被覆盖。

外部服务不可用时记录具体探测/异常，只报告未执行 live integration，不能把单元 mock 结果描述为真实集成通过。

## Review Gates

- 只使用现有 PyYAML、Pydantic、SQLAlchemy、Qdrant、Elasticsearch、TEI 和 Loguru 依赖；`pyproject.toml`、`uv.lock` 不应变化。
- CLI 位于单数 `app/script`，Service/Repository 位于单数 `app/service`、`app/repository`；不新增复数兼容目录。
- ORM 表实体位于单数 `app/model`，业务实体位于单数 `app/entity`；配置 Pydantic 模型仍位于 `app/conf`。
- `app/repository/__init__.py` 保持用户原有暂存内容，不覆盖、不取消暂存。
- `AOV.relevant_columns` 必须为 `fact_order.order_amount`。
- 任何动态表名/字段名查询前必须完成实际 DW schema 校验；数据值必须使用绑定参数。
- MySQL、Qdrant、Elasticsearch 使用稳定业务 ID/upsert，禁止随机 UUID 和先清空再重建。
- Qdrant 字段/指标 collection 必须分开；每个点同时包含匿名 512 维 dense 和命名 `bm25` 关键词向量。
- 已有 Qdrant collection 和每批 TEI embedding 都必须显式校验 dense 维度、距离与 `bm25`/IDF 配置；已有兼容 collection 只允许原地新增 `bm25`，禁止自动删除或重建不兼容 collection，并保留任何旧 `sparse` 向量。
- BM25 分支必须使用 `Document(model="Qdrant/bm25")` 与统一 `Bm25Config`，可供未来查询端复用；不新增 `fastembed`、`jieba`、tokenizer 模型或运行时下载。
- Elasticsearch index 映射使用仓库 Docker 已安装的 IK analyzer，不静默回退其他 analyzer。
- 异常不得被宽泛吞掉；所有已初始化 manager 必须在失败路径关闭。
- 不新增自动删除、分页框架、重试框架、ORM 自动建表、Mapper、Repository 接口或通用工厂。

## Risk and Rollback Points

- DW/Meta schema 未初始化：停止 live integration，报告缺失数据库或表；不修改 bootstrap SQL 猜测修复。
- 已有 Qdrant collection 的 dense 维度/距离或 `bm25` modifier 不兼容：明确失败并停止，不自动删除 collection；只有缺少 `bm25` 时执行原生 additive migration。
- Elasticsearch 未安装 IK 插件：index 创建失败并停止，不改映射规避环境问题。
- TEI 不可用或输出维度漂移：保留 Hugging Face/Qdrant 原始异常，不创建其他 embedding 路径。
- 跨存储中途失败：允许已完成的外部 upsert 保留；修复依赖后通过稳定 ID 重跑收敛，不实现补偿事务。
- 代码回滚只删除本任务新增文件并恢复本任务修改的规范；不自动清除已经写入的外部数据。

## Validation Record — 2026-07-16

以下命令最终复跑通过：

```powershell
uv sync --locked
uv lock --check
uv run --with ruff ruff check app app_test
uv run --with pyright pyright app app_test
uv run python -m compileall -q app app_test main.py
uv run python -m app.conf.app_config
uv run python -m app_test.service.test_metadata_sync_service
uv run python -m app_test.client.test_mysql_client_manager
uv run python -m app.script.sync_metadata --help
git diff --check
```

- Pyright 结果为 `0 errors, 0 warnings, 0 informations`；Ruff 为
  `All checks passed!`。
- 聚焦测试额外断言四张 ORM Model 完整匹配 `meta.sql`、五种业务 Entity
  字段正确、四条 Model 生成的 Meta SQL 均为幂等 upsert、JSON 参数保持
  Python list、DW 校验失败前无任何外部写入、TEI 数量/维度异常与 Qdrant
  named-vector 配置被拒绝，以及只有关闭异常时四个 manager 全部完成关闭并
  抛出 `BaseExceptionGroup`。
- 端口复核：MySQL `3306` 可用；Qdrant `6333`、Elasticsearch `9200`、
  TEI `8080` 不可用。
- 因四个服务未同时可用，未运行真实同步命令，也未声称 MySQL/Qdrant/
  Elasticsearch/TEI 四服务联调或真实双次幂等数量核对通过。
- 用户已暂存的空文件 `app/repository/__init__.py` 保持原样，未执行
  `git add`、`git commit`、`git push`、`git reset` 或 `git stash`。

## Hybrid Index Validation Record — 2026-07-16

混合索引增量完成后再次运行并通过：

```powershell
uv sync --locked
uv lock --check
uv run --with ruff ruff check app app_test
uv run --with pyright pyright app app_test
uv run python -m compileall -q app app_test main.py
uv run python -m app.conf.app_config
uv run python -m app_test.service.test_metadata_sync_service
uv run python -m app.script.sync_metadata --help
git diff --check
```

- Ruff 为 `All checks passed!`；Pyright 为
  `0 errors, 0 warnings, 0 informations`；其余命令 exit 0。
- 聚焦测试独立枚举 `order_amount` 与中文单字/双字 token 的词频和哈希期望，
  覆盖 uint32 索引、哈希碰撞合并、唯一排序、dense+sparse 点结构、新建
  collection、dense-only additive migration、已有 IDF 复用及不兼容配置拒绝。
- 两次独立 in-memory Qdrant 1.18 冒烟均证明：匿名 dense collection 可通过
  `create_vector_name()` 原地新增 `sparse`/IDF，混合点可 upsert、retrieve，
  dense 与 sparse 两路查询均命中。
- `pyproject.toml`、`uv.lock` 和用户已暂存的空
  `app/repository/__init__.py` 均未修改；未执行任何 Git 写操作。
- Qdrant、Elasticsearch 与 TEI 本地端口仍未满足真实四服务同步前提，因此
  未运行真实 CLI 双次同步，不能把 mock/in-memory 结果描述为四服务联调。

## BM25 Correction — 2026-07-16

- 先前的标准库 `sparse` encoder 只是 lexical TF-IDF 风格实现，不满足用户要求的
  BM25；本轮删除该 encoder，改用 Qdrant 1.18 core `Qdrant/bm25`，并使用新向量
  名 `bm25` 防止与可能已存在的 raw-TF `sparse` 数据混算。
- 单元测试必须断言 `Document` 与完整 `Bm25Config`，不能再用手工 token/hash
  断言冒充 BM25 验证。
- in-memory Qdrant 不支持 core BM25 服务端处理；只有远端 HTTP Qdrant 1.18
  验证可作为真实 BM25 冒烟，服务不可用时明确记录未执行。

## BM25 Validation Record — 2026-07-16

- 删除应用侧 token/hash/raw-TF 编码，Qdrant 点现在只写匿名 512 维 dense 与
  命名 `bm25` 的 `Document(model="Qdrant/bm25", options=BM25_CONFIG)`。
- 新建 collection 使用 `bm25`/IDF；已有兼容 collection additive 增加 `bm25`，
  旧 `sparse` 配置保持原样但 Repository 拒绝新点继续写入它。
- 聚焦测试覆盖固定 BM25 参数、中英文 Document、稳定 ID、collection 创建/升级、
  旧 `sparse` 保留、错误模型/参数/额外向量拒绝，以及远端 client facade 将
  `Document` 原样转发到 HTTP client。
- `uv sync --locked`、`uv lock --check`、Ruff、Pyright、`compileall`、配置加载、
  聚焦测试、CLI help 与 `git diff --check` 全部通过。
- 端口检查仅 MySQL `3306` 可用；Qdrant `6333`、Elasticsearch `9200`、TEI
  `8080` 不可用，因此未运行真实四服务 CLI 双次同步或 Qdrant 服务端 BM25
  upsert/query 冒烟。
- 未修改依赖，未执行 Git 写操作；用户已暂存的空
  `app/repository/__init__.py` 保持原样。

## Live Integration Record — 2026-07-16

用户启动 MySQL、Qdrant 1.18、Elasticsearch 和 TEI 后，真实执行两次：

```powershell
uv run python -m app.script.sync_metadata --config conf/meta_config.yaml
```

两次命令均以 exit 0 结束。首次同步后记录的存储数量与第二次同步后的实际查询
结果一致：

- Meta MySQL：`table_info=5`、`column_info=24`、`metric_info=2`、
  `column_metric=2`。
- Qdrant：`data-agent-column=98`、`data-agent-metric=8`，两个 collection 均为
  green，匿名 dense 为 `512/Cosine`，命名 sparse 为 `bm25/IDF`。
- Elasticsearch：`data-agent-value=75`；`value` 字段使用 `ik_max_word`
  analyzer。由于 search analyzer 与 analyzer 相同，Elasticsearch 返回 mapping
  时省略等价的 `search_analyzer`，实际搜索分析器仍为 `ik_max_word`。
- Elasticsearch cluster 为 `yellow`：当前只有 1 个节点，主分片正常，但索引
  配置的 1 个 replica 无法分配；bulk、count 和查询均成功，因此这是本地单节点
  的副本冗余状态，不是同步失败。
- 第二次同步分别再次 upsert `98`、`8`、`75` 条稳定 ID 数据，实际总数没有
  增长；结合稳定 UUID5/SHA-256 回放测试，幂等 upsert 契约通过。

从两个 Qdrant collection 各 scroll 一个真实点，均只包含 `""` 与 `"bm25"`
两个向量键；dense 长度为 512，BM25 的 indices/values 长度一致。使用查询文本
`平均订单金额` 做两路真实检索：

- dense 第一名为 AOV 的 `平均订单金额` 别名，score `0.85926294`；
- `using="bm25"` 第一名为同一点，score `4.3344917`，第二名为 AOV 描述，
  score `4.232311`。

BM25 查询请求使用与写入端相同的 `Document(model="Qdrant/bm25")` 和完整
`Bm25Config`。本轮没有实现检索 API、RRF 融合或 rerank；这些仍属于 PRD 已声明
的 Out of Scope。未执行任何 Git 写操作，用户已暂存的空
`app/repository/__init__.py` 保持原样。

真实联调完成后，最终全量门禁再次通过：`uv sync --locked`、
`uv lock --check`、Ruff、Pyright、`compileall`、配置加载、MySQL live test、
TEI live test、metadata 聚焦测试、CLI help 和 `git diff --check`。Ruff 输出
`All checks passed!`，Pyright 输出 `0 errors, 0 warnings, 0 informations`，
其余命令均为 exit 0。
