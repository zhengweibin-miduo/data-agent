# 元数据同步客户端 API 契约

## 版本与仓库证据

- `uv.lock:280-281`：`elasticsearch==8.19.3`。
- `uv.lock:882-883`：`qdrant-client==1.18.0`。
- `uv.lock:945-946`：`sqlalchemy==2.0.51`。
- `docs/docker/docker-compose.yml:3,21,32`：本地服务分别为 MySQL 8.4、Qdrant 1.18.0、Elasticsearch 8.19.17；客户端与服务端均处于各自兼容的 8.x / 1.18 版本线。
- `docs/docker/docker-compose.yml:51` 与 `app_test/client/test_tei_embedding_client_manager.py:10,23`：TEI 使用 `BAAI/bge-small-zh-v1.5`，仓库已将输出维度锁定为 512。

以下签名均由当前 `.venv` 中的锁定版本通过 `inspect.signature` 核实。

## Qdrant 1.18.0

关键签名：

```python
AsyncQdrantClient.collection_exists(
    collection_name: str,
    **kwargs: Any,
) -> bool

AsyncQdrantClient.get_collection(
    collection_name: str,
    **kwargs: Any,
) -> CollectionInfo

AsyncQdrantClient.create_collection(
    collection_name: str,
    vectors_config: VectorParams | Mapping[str, VectorParams] | None = None,
    ...,
) -> bool

AsyncQdrantClient.upsert(
    collection_name: str,
    points: Batch | Sequence[PointStruct | grpc.PointStruct],
    wait: bool = True,
    ...,
) -> UpdateResult
```

`PointStruct.id` 的实际类型为 `int | str | uuid.UUID`，但 Qdrant 的字符串 ID 必须能解析为 UUID。内存客户端实测普通字符串 `plain-id` 抛出 `ValueError: Point id plain-id is not a valid UUID`，规范 UUID 字符串、`UUID` 对象和整数均可写入。对应源码证据为：

- `.venv/Lib/site-packages/qdrant_client/async_qdrant_client.py:832,1520,1532,1616`
- `.venv/Lib/site-packages/qdrant_client/http/models/models.py:2219,3800,4020,4215`
- `.venv/Lib/site-packages/qdrant_client/local/local_collection.py:2497-2500`

锁定版本还支持 Qdrant core BM25：`PointStruct.vector` 的命名向量值可为
`Document`，`Document.options` 可为 `Bm25Config`。远端 client 模式下
`ModelEmbedder` 将 `Qdrant/bm25` 文档原样交给 Qdrant >= 1.15.3，不导入
FastEmbed；本地/in-memory 模式没有服务端能力，才会要求 `fastembed`。当前
Qdrant 1.18 应使用：

```python
Document(
    text=text,
    model="Qdrant/bm25",
    options=Bm25Config(
        k=1.2,
        b=0.75,
        avg_len=256,
        tokenizer=TokenizerType.MULTILINGUAL,
        language="none",
        lowercase=True,
    ),
)
```

服务端文档权重包含 BM25 TF 饱和与长度归一化，query token 权重为 1；命名
`bm25` sparse storage 的 `Modifier.IDF` 再应用 collection 统计的 BM25 IDF。
`MULTILINGUAL` 支持中文分词，`language="none"` 关闭默认英文 stopwords 和
stemmer。旧手工 raw-TF 向量名 `sparse` 与 BM25 语义不兼容，因此新实现使用
`bm25` 名并保留已有 `sparse`，避免未重写旧点混入 BM25 排名。

最小实现建议：

```python
from uuid import NAMESPACE_URL, uuid5

point_id = uuid5(
    NAMESPACE_URL,
    f"data-agent://{collection_name}/{logical_id}/{text_kind}/{text}",
)
```

- 使用标准库 UUID v5；相同逻辑实体和文本类型始终得到相同 ID，不新增依赖。
- `text_kind` 区分 `name`、`description`、`alias`；别名 ID 使用别名文本而不是数组下标，配置重排不会改变 ID。
- `upsert` 源码明确规定同 ID 覆盖，满足幂等 upsert。
- 建 collection 时使用 `VectorParams(size=512, distance=Distance.COSINE)`。
- collection 已存在时调用 `get_collection()`，要求 `info.config.params.vectors` 是匿名 `VectorParams` 且 `size == 512`；不匹配时明确失败，不自动删除或重建 collection。
- 每次 upsert 前仍校验 embedding 长度为 512，让 TEI 模型漂移在写入边界立即暴露。

## Elasticsearch async 8.19.3

关键签名：

```python
client.indices.exists(
    *,
    index: str | Sequence[str],
    ...,
) -> HeadApiResponse

client.indices.create(
    *,
    index: str,
    mappings: Mapping[str, Any] | None = None,
    settings: Mapping[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    ...,
) -> ObjectApiResponse[Any]

client.bulk(
    *,
    operations: Sequence[Mapping[str, Any]] | None = None,
    body: Sequence[Mapping[str, Any]] | None = None,
    index: str | None = None,
    refresh: bool | Literal["false", "true", "wait_for"] | None = None,
    ...,
) -> ObjectApiResponse[Any]
```

`HeadApiResponse.__bool__` 根据 HTTP 状态是否为 2xx 返回真假，因此可直接写：

```python
if not await client.indices.exists(index=index_name):
    await client.indices.create(
        index=index_name,
        mappings={"properties": {"column_id": {"type": "keyword"}}},
    )
```

`mappings=` 直接接 mapping 内容，不要再次包成 `{"mappings": ...}`，也不要与 `body=` 同时传。

直接调用低层 `client.bulk()` 时，`operations` 必须是扁平的 NDJSON 行序列，`index` action 与 source 文档交替出现：

```python
operations = [
    {"index": {"_index": index_name, "_id": document_id}},
    {"column_id": column_id, "value": value},
]
response = await client.bulk(operations=operations, refresh="wait_for")
if response["errors"]:
    raise RuntimeError(f"Elasticsearch bulk 部分写入失败: {response['items']}")
```

- 使用 `index` 而不是 `create`：同 `_id` 时替换文档，才能幂等。
- HTTP 200 仍可能包含 item 级失败，必须检查 `response["errors"]`。
- `operations` 与兼容参数 `body` 只能传一个；锁定客户端会显式拒绝同时传入。
- 不要把 helper 风格的 `{"_index", "_id", "_source"}` action 直接传给低层 `client.bulk()`。

字段去重值没有数量上限时，更短且更稳的是复用同包自带的 `elasticsearch.helpers.async_bulk`：它接受每个文档一个 helper action，默认分块并在 item 失败时抛错，无需新增依赖。

```python
await async_bulk(
    client,
    (
        {
            "_op_type": "index",
            "_index": index_name,
            "_id": document_id,
            "_source": document,
        }
        for document_id, document in documents
    ),
    refresh="wait_for",
)
```

建议实现选 `async_bulk`；低层形状保留为审查依据。对应源码证据为：

- `.venv/Lib/site-packages/elasticsearch/_async/client/indices.py:612,1512`
- `.venv/Lib/site-packages/elasticsearch/_async/client/__init__.py:630,674-680,834`
- `.venv/Lib/site-packages/elasticsearch/_async/helpers.py:351-405`

## SQLAlchemy AsyncSession 与 MySQL upsert

锁定的 SQLAlchemy 2.0.51 签名：

```python
AsyncSession.execute(
    statement: Executable,
    params: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    ...,
) -> Result[Any]

text(text: str) -> TextClause
```

SQLAlchemy 源码把多参数定义为 `Sequence[Mapping[str, Any]]`；传入字典列表会走 DBAPI `cursor.executemany()`。最终实现已建立与现有 DDL 对齐的 ORM Model，因此固定 meta 表使用 MySQL dialect upsert：

```python
statement = mysql_insert(TableInfoMySQL)
upsert = statement.on_duplicate_key_update(
    name=statement.inserted.name,
    role=statement.inserted.role,
    description=statement.inserted.description,
)
await session.execute(upsert, [asdict(entity) for entity in entities])
```

- `sqlalchemy.dialects.mysql.insert(Model).on_duplicate_key_update()` 由 Model
  提供表、列和 JSON 类型契约，不重复维护 raw SQL 字符串。
- `examples`、`alias`、`relevant_columns` 是 ORM `JSON` 列；参数必须保持 Python
  `list`，由 SQLAlchemy 序列化一次。预先 `json.dumps()` 会把数组存成 JSON 字符串。
- 空列表先返回，不调用 executemany。
- `column_metric` 没有可更新列，重复键时对 `metric_id` 做等价更新。
- 一个 `MysqlClientManager.session()` 包住全部 relational upsert，复用现有自动 commit / rollback 契约。
- Meta 目标表由四个 Model 固定。DW 表名和字段名不能参数绑定，应先用实际 schema 查询结果做精确成员校验，通过后才按受控标识符拼接。
- mapping 结果应从已执行的 `Result` 获取：
  `(await session.execute(statement, params)).mappings()`；`AsyncSession` 本身没有
  `mappings()` 方法，不能反转调用顺序。

源码证据：

- `.venv/Lib/site-packages/sqlalchemy/ext/asyncio/session.py:403-432`
- `.venv/Lib/site-packages/sqlalchemy/engine/interfaces.py:231-236`
- `.venv/Lib/site-packages/sqlalchemy/ext/asyncio/engine.py:641-648`
- `app/client/mysql_client_manager.py`：现有 Session 自动提交、异常回滚、关闭。

## 512 维 collection 处理结论

不要用“存在就跳过”的单一判断，也不要自动重建：

1. collection 不存在：创建匿名 dense vector `size=512`、`Distance.COSINE`，
   同时创建命名 `bm25` sparse storage 与 `Modifier.IDF`。
2. collection 已存在：读取 `get_collection()` 并验证其为匿名
   `VectorParams(size=512, distance=Distance.COSINE)`；缺少 `bm25` 时使用
   `create_vector_name()` 原地补充，不删除、不重建，也不移除旧 `sparse`。
3. 配置为 named dense vectors、dense 维度/距离不兼容，或已有 `bm25` 不是
   IDF：抛出包含 collection 名和实际配置的明确异常，由运维显式处理。
4. TEI 每批返回后校验每个向量长度为 512；每个新点只包含匿名 dense 与命名
   `bm25` `Document`，不得继续写入旧 `sparse`。

内存 Qdrant 实测：512 维 collection 可正常读取 `VectorParams.size == 512`，写入 511 维向量立即失败。显式前置校验能把底层 shape 错误转换成可定位的同步错误。

## 最小测试策略

不新增 pytest 依赖，沿用仓库的 `asyncio.run()` + 直接 `assert` 模式，并使用标准库 `unittest.mock.AsyncMock` / `Mock`。一个聚焦测试模块即可覆盖：

1. 纯转换：同一输入重复转换得到相同 UUID；别名重排不改变各别名 ID；非法 `relevant_columns` 明确失败；仅 `sync: true` 字段触发字段值读取。
2. MySQL：`AsyncMock(spec=AsyncSession)`，断言四个 Model 生成的语句均包含
   `ON DUPLICATE KEY UPDATE`，`execute()` 第二个参数是字典列表且 JSON 值仍是
   Python list；空列表不调用。
3. Qdrant：mock `collection_exists/get_collection/create_collection/upsert`，断言新建时 `size=512`，已存在错误维度时失败，`PointStruct.id` 为稳定 UUID，向量错误维度不调用 upsert。
4. Elasticsearch：`indices.exists/create` 与 bulk/helper 使用 `AsyncMock`；断言 mapping 未重复包装、action 使用 `index`、稳定 `_id`，并覆盖 item error 的抛错路径。

执行入口保持现有风格：

```python
def test_metadata_sync() -> None:
    asyncio.run(_test_metadata_sync())

if __name__ == "__main__":
    test_metadata_sync()
```

这能被现有 `uv run --with ruff ...`、`uv run --with pyright ...` 和直接模块执行覆盖；无需测试框架、fixture 或真实外部服务。
