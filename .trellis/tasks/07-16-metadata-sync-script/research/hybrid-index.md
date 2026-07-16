# Qdrant 混合索引研究

## 需求解释

用户提供的 RAG 面试材料以“纯向量检索的问题与为什么需要混合检索”为参照。本任务中的混合索引明确指同一个 Qdrant 元数据点同时保存 dense 语义向量和 sparse 关键词向量，不是把 MySQL、Qdrant、Elasticsearch 三种存储统称为混合。

原 DOCX 5.3 只定义匿名 Cosine dense vector，没有 sparse/BM25/SPLADE、分词算法或向量命名约定。当前实现需在不扩大同步脚本职责的前提下补齐关键词索引；真正检索时再执行 dense/sparse 两路召回与 RRF 融合。

## 锁定 API 证据

当前 `qdrant-client==1.18.0` 支持：

- `create_collection(vectors_config=..., sparse_vectors_config=...)`；
- `PointStruct.vector` 使用向量字典，同时容纳 dense list 与服务端推理 `Document`；
- `SparseVector(indices: list[int], values: list[float])`；
- `SparseVectorParams(modifier=Modifier.IDF)`；
- `create_vector_name()` 在已有 collection 上新增 dense 或 sparse 向量名。

Qdrant 客户端内部的匿名 dense 名为 `""`。因此新点使用：

```python
{
    "": dense_vector,
    "bm25": Document(text=text, model="Qdrant/bm25", options=BM25_CONFIG),
}
```

## Qdrant core BM25

手工词频加 `Modifier.IDF` 只属于 TF-IDF 风格 sparse，缺少 BM25 的词频饱和和文档长度归一化，不能满足本任务最终要求。锁定的 Qdrant client/server 1.18 支持由服务端直接处理：

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

远端 client 模式会保留该 `Document` 并交给 Qdrant core，无需安装 `fastembed` 或下载模型。`k` 控制词频饱和，`b` 与 `avg_len` 完成长度归一化，collection 的 `Modifier.IDF` 应用 collection 级 IDF。查询端必须复用同一模型与配置，并查询命名向量 `bm25`。

## Collection 兼容策略

- 新 collection：匿名 `VectorParams(size=512, distance=Cosine)` + 命名 `bm25`/IDF。
- 已有匿名 512 维 Cosine collection：调用 `create_vector_name()` 原地增加 `bm25`，不删除、不重建、不复制 dense。
- 已有兼容 `bm25`：直接复用；已有旧 `sparse` 保留但不再写入。
- dense 是 named vector、维度或距离不一致，或已有 `bm25` modifier 不是 IDF：写入前明确失败。
- 当前配置中的稳定 ID 点重跑后补齐 `bm25`；配置外旧点保留 dense-only 或旧 `sparse`，符合既有 no-delete 契约。

## 验证重点

- BM25 `Document` 对相同文本具有完全一致的模型名与参数，并使用 multilingual tokenizer。
- 新 collection 创建参数同时包含匿名 dense 和命名 `bm25`/IDF。
- 已有 dense-only collection 只新增一次 `bm25`；重复运行不再迁移，旧
  `sparse` 配置保持原样。
- PointStruct 只携带 `""` 和 `"bm25"`，UUID5 与 payload 保持稳定，不再向
  旧 `sparse` 写入新数据。
- 不兼容 collection、错误 dense 维度和非 BM25 `Document` 均在 upsert 前失败。

## 运行时验证

先前使用锁定的 `qdrant-client==1.18.0` 和
`AsyncQdrantClient(location=":memory:")` 实测了 collection 的混合向量结构：

1. 创建匿名 512/Cosine dense-only collection；
2. 通过 `SparseVectorNameConfig(sparse=SparseVectorConfig(modifier=IDF))`
   调用 `create_vector_name()` 原地新增 `sparse`；
3. upsert `{"": dense, "sparse": SparseVector(...)}`；
4. `retrieve(with_vectors=True)` 返回 `""` 和 `"sparse"` 两个键；
5. 匿名 dense 与 `using="sparse"` 两路查询均成功命中。

新建 collection 直接传匿名 `vectors_config` 与命名
`sparse_vectors_config` 也完成同样读写和两路查询。该验证只证明手工
`SparseVector` 的本地读写形状；in-memory 模式会要求 FastEmbed，不能验证
Qdrant core BM25。真实 BM25 必须由远端 Qdrant 1.18 HTTP 冒烟证明。

聚焦测试额外实例化真实远端模式 `AsyncQdrantClient(url=...)`，只 mock 最底层
HTTP upsert，并确认客户端转换后仍把完整 `Document/Bm25Config` 交给服务端。
这证明无需 FastEmbed 的 client passthrough 契约，但不等同于服务端公式冒烟。
