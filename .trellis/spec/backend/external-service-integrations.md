# External Service Integrations

## Scenario: Replayable Metadata Synchronization

### 1. Scope / Trigger

Use this contract when changing `conf/meta_config.yaml`, its Pydantic model,
the metadata synchronization CLI, or its TEI, Qdrant, and Elasticsearch flow.
It is a cross-layer and multi-storage boundary: validation, stable identity,
write semantics, and cleanup must change together.

### 2. Signatures

```powershell
uv run python -m app.script.sync_metadata --config <yaml-path>
```

```python
class MetaConfig(ConfigModel):
    @classmethod
    def from_yaml(cls, path: str | Path) -> "MetaConfig": ...

class MetadataSyncService:
    async def sync(self, config: MetaConfig) -> None: ...

def stable_point_id(
    collection_name: str,
    entity_id: str,
    text_kind: str,
    value: str,
) -> UUID: ...

def stable_value_id(column_id: str, value: str) -> str: ...

BM25_CONFIG = Bm25Config(
    k=1.2,
    b=0.75,
    avg_len=256,
    tokenizer=TokenizerType.MULTILINGUAL,
    language="none",
    lowercase=True,
)

def bm25_document(text: str) -> Document: ...

class MetadataRepository:
    async def upsert_metadata(
        self,
        tables: Sequence[TableInfo],
        columns: Sequence[ColumnInfo],
        metrics: Sequence[MetricInfo],
        relations: Sequence[ColumnMetric],
    ) -> None: ...

    async def upsert_vectors(
        self,
        collection_name: str,
        points: Sequence[PointStruct],
    ) -> None: ...

    async def upsert_values(
        self,
        documents: Sequence[ValueInfo],
    ) -> None: ...
```

### 3. Contracts

- `--config/-c` is required. The YAML root has `tables` and `metrics`; unknown
  keys are forbidden. Tables, columns, and metrics are unique in their scopes,
  and every metric reference is an existing `<table>.<column>`.
- Reuse `mysql.url`, `qdrant.url`, `elasticsearch.url`, and `tei.url` from
  `conf/app_config.yaml`, including the existing optional service API keys. The
  synchronization flow adds no connection setting or environment variable.
- Table roles are `dim | fact`; column roles are
  `primary_key | foreign_key | dimension | measure`. YAML table and column
  identifiers use the simple SQL identifier pattern. Configuration is loaded
  as UTF-8 through `yaml.safe_load()` and Pydantic.
- Validate the entire configured DW shape before any distinct-value query or
  storage write. The MySQL details are defined in
  [Database Guidelines](./database-guidelines.md#metadata-synchronization-sql-boundary).
- Service converts Config into the business dataclasses under `app/entity/`.
  Repository accepts those Entities, derives Qdrant/Elasticsearch payloads with
  `asdict()`, and uses the four `app/model/` mappings for Meta MySQL upserts.
- Build dense embeddings in batches of 10 from each field or metric name,
  description, and alias. Within one entity, encode each distinct text value
  once; name, description, then aliases define the first retained `text_kind`.
- Wrap the same text in `Document(model="Qdrant/bm25", options=BM25_CONFIG)`.
  The locked remote `AsyncQdrantClient` forwards this document to Qdrant 1.18
  core without FastEmbed. `k=1.2` applies term-frequency saturation;
  `b=0.75` and `avg_len=256` apply document-length normalization;
  `TokenizerType.MULTILINGUAL` handles Chinese and English; `language="none"`
  disables English stopwords and stemming. Query code must call the same
  `bm25_document()` helper.
- Use Qdrant collections `data-agent-column` and `data-agent-metric`. A newly
  created collection has an anonymous dense vector of size 512 with Cosine
  distance plus named `bm25` sparse storage with `Modifier.IDF`.
- An existing collection must retain the compatible anonymous dense vector. If
  it has no `bm25` storage, add that storage in place with
  `create_vector_name(..., SparseVectorNameConfig(...))`; never delete or
  rebuild the collection. Reject incompatible dense or existing `bm25`
  configurations before point upsert. Preserve any legacy raw-TF `sparse`
  storage, but never write it on new points; a separate name prevents mixed
  ranking semantics for old points outside the current configuration.
- Qdrant point IDs are standard-library UUID5 values derived from an
  unambiguous structured encoding of collection, entity ID, text role, and
  text value. Delimiter characters inside any component must not create an ID
  collision. Alias reordering therefore does not change IDs. The payload
  contains the normalized entity row plus `text_kind` and `text`.
- Every point stores both vectors as
  `{"": dense_512, "bm25": bm25_document(text)}` and no additional vector
  keys. TEI must return one dense vector per requested text and every dense
  vector must be 512 dimensions. The BM25 document text must be nonblank and
  its model plus every `Bm25Config` field must equal the shared constants before
  Qdrant is called. `Modifier.IDF` supplies Qdrant's collection-level BM25 IDF.
- Use Elasticsearch index `data-agent-value`. When absent, create fields `id`
  and `column_id` as `keyword`, and `value` as `text` with
  `ik_max_word` for both analyzer and search analyzer. Do not rebuild or mutate
  an existing index automatically.
- Only `sync: true` fields produce value documents. Read at most 100,000
  distinct non-null values per field. A document ID is SHA-256 over
  `<column_id> + NUL + <value>`; write with the helper's `index` operation via
  `elasticsearch.helpers.async_bulk(..., refresh="wait_for")`.
- All stores are upsert-only. A replay updates the same IDs and inserts new
  IDs, but never deletes stale Meta rows, changed/removed Qdrant text points,
  or values that disappeared from DW. Points outside the current configuration
  may remain dense-only or retain legacy raw-TF `sparse` after the additive
  `bm25` migration. Historical BM25 backfill is a separate explicit task.
- The order is DW validation and examples, Meta SQL execution, column Qdrant,
  metric Qdrant, then per-field Elasticsearch. MySQL commits only when the
  managed Session exits. There is no cross-storage transaction or automatic
  compensation; repair the dependency and replay the same configuration.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| Config path is missing, YAML is malformed, or a key/value is invalid | Propagate the file, YAML, or Pydantic error; CLI exits nonzero before client initialization |
| Metric references an unconfigured field | Pydantic validation fails before external access |
| Configured DW table or column is absent | Raise `ValueError` before distinct-value reads or writes |
| TEI vector count differs from input count | Raise `RuntimeError`; do not construct a partial batch of points |
| TEI or supplied anonymous dense vector is not 512 dimensions | Raise `RuntimeError` before Qdrant collection access or upsert |
| A point lacks `bm25`, includes any extra vector such as legacy `sparse`, has blank BM25 text, or uses a different model/config | Raise `RuntimeError` before Qdrant collection access or upsert |
| Existing Qdrant collection uses named dense vectors, a non-512 size, or a non-Cosine distance | Raise `RuntimeError`; do not delete or recreate the collection |
| Compatible existing collection has no `bm25` storage | Add named `bm25`/IDF storage with `create_vector_name()`, preserve legacy `sparse`, then upsert current stable-ID points |
| Existing `bm25` storage does not use `Modifier.IDF` | Raise `RuntimeError`; do not mutate, delete, or recreate it |
| IK analyzer is unavailable during index creation | Preserve the Elasticsearch error; do not fall back to another analyzer |
| Elasticsearch bulk has an item or transport failure | Let `async_bulk` propagate the failure |
| MySQL, Qdrant, Elasticsearch, or TEI request fails | Preserve the primary exception, roll back MySQL, and attempt all four manager closes |
| One or more closes fail after a primary failure | Keep the primary exception and attach each close failure as an exception note |
| Only close operations fail | Raise one `BaseExceptionGroup` containing all close failures |

### 5. Good / Base / Bad Cases

- Good: run the five-table, 24-field, two-metric sample twice; Meta primary keys,
  Qdrant logical point IDs and both vector values, and Elasticsearch document
  IDs remain stable, while AOV references `fact_order.order_amount`.
- Base: a valid config with empty `tables` or `metrics` remains loadable. Empty
  row/point writes are harmless; collection readiness may still be checked.
  A compatible legacy collection gains `bm25` without losing its existing
  dense points or old `sparse` storage.
- Bad: use random Qdrant UUIDs, hand-roll raw-TF vectors and call them BM25,
  reuse the old `sparse` name, use a different BM25 config at query time, add a
  second dense copy solely for naming, use the Elasticsearch `create` action,
  issue blind MySQL inserts, or clear a collection/index before every run.

### 6. Tests Required

- Parse `conf/meta_config.yaml` and assert 5 tables, 24 fields, 2 metrics, and
  `AOV.relevant_columns == ["fact_order.order_amount"]`.
- Reject unknown keys, duplicate tables/columns/metrics, duplicate or missing
  metric references, invalid identifiers, missing files, and malformed YAML.
- Assert DW schema failures occur before any value read or storage write; assert
  the 10-example limit and `sync: true` 100,000-value filter.
- Assert all four Meta ORM statements target the expected mapped tables and use
  `ON DUPLICATE KEY UPDATE`. Assert JSON parameters remain Python lists before
  binding and the four Models match the bootstrap DDL contract.
- Replay equivalent input and assert stable Qdrant UUID sets even when aliases
  are reordered. Also assert distinct component tuples cannot collide when
  their values contain delimiters, plus identical BM25 documents and
  Elasticsearch document IDs.
- Assert English and Chinese input produce `Document` values with
  `model="Qdrant/bm25"` and the complete shared `Bm25Config`.
- Assert the real remote `AsyncQdrantClient` conversion facade forwards the
  `Document` unchanged to its HTTP client without importing FastEmbed. This is
  a client-boundary test, not a server integration test.
- Assert Qdrant creation uses anonymous size 512/Cosine plus named `bm25`/IDF;
  a compatible collection calls `create_vector_name()` once while preserving
  legacy `sparse`; an already hybrid collection does not; wrong dense/BM25
  configurations, extra vector keys, and malformed documents fail before upsert.
- Assert Elasticsearch mapping is not double-wrapped and `async_bulk` failures
  propagate unchanged.
- Inject initialization and close failures; assert all four closes are awaited,
  the primary exception survives with close notes, and close-only failures are
  collected into one `BaseExceptionGroup`.
- Always run the lock, Ruff, Pyright, `compileall`, configuration, focused
  metadata test, CLI `--help`, and `git diff --check` gates from
  [Quality Guidelines](./quality-guidelines.md).
- Run the real CLI twice and inspect record counts only when MySQL, Qdrant,
  Elasticsearch, and TEI are all available and writable. Otherwise record the
  missing services and describe the live integration as not run.
- Do not use `AsyncQdrantClient(location=":memory:")` as evidence for core BM25:
  local mode requires FastEmbed and does not exercise Qdrant server-side BM25.
  A true BM25 smoke test requires the remote Qdrant 1.18 HTTP service and a
  `bm25_document()` upsert plus `query_points(..., using="bm25")`.

### 7. Wrong vs Correct

```python
# Wrong: replay adds another point and the point still has dense only.
PointStruct(id=uuid4(), vector=dense_vector, payload=payload)
{"_op_type": "create", "_id": f"{column_id}:{value}"}

# Correct: content-derived IDs and both vector branches converge on replay.
PointStruct(
    id=uuid5(
        NAMESPACE_URL,
        f"data-agent://{collection}/{entity_id}/{text_kind}/{value}",
    ),
    vector={
        "": dense_vector,
        "bm25": bm25_document(text),
    },
    payload=payload,
)
{
    "_op_type": "index",
    "_id": sha256(f"{column_id}\0{value}".encode()).hexdigest(),
}
```

## Scenario: Local Text Embeddings Inference

### 1. Scope / Trigger

Use this contract when changing the local TEI Compose service, its application configuration, or its LangChain Hugging Face client. It prevents the server model, vector shape, and query encoding behavior from drifting independently.

### 2. Signatures

```python
class TeiEmbeddings(HuggingFaceEndpointEmbeddings):
    async def aembed_query(self, text: str) -> list[float]: ...

class TeiEmbeddingClientManager:
    @classmethod
    def initialize(cls) -> TeiEmbeddings: ...
    @classmethod
    def get_client(cls) -> TeiEmbeddings: ...
    @classmethod
    async def close(cls) -> None: ...
```

### 3. Contracts

- Compose service: `text-embeddings-inference`.
- Client modules live in the singular package `app/client/`; matching integration tests live in `app_test/client/`.
- Image: `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`; no GPU device requests.
- Model: `BAAI/bge-small-zh-v1.5`; output dimension is 512.
- Endpoint: `conf/app_config.yaml` key `tei.url`, with Hugging Face requests sent to `{url}/embed`.
- Cache: named volume mounted at `/data`.
- Documents receive no query instruction; the LangChain client replaces newlines with spaces. Queries prepend `为这个句子生成表示以用于检索相关文章：`.
- All requests use `normalize=True` and `truncate=True`.
- The managed embedding instance comes from `langchain_huggingface`; only the BGE query instruction is overridden locally.
- Construct it with Pydantic `model_construct`, inject `AsyncInferenceClient(model="{url}/embed")`, and set `client=None` because the standard constructor rejects self-hosted URLs and creates a sync client for accepted repo IDs.
- Pass `normalize=True` and `truncate=True` through `model_kwargs`.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| `tei.url` missing or unknown config key present | Pydantic configuration validation fails at startup |
| `get_client()` called before `initialize()` | Raise `RuntimeError` with the initialization instruction |
| Standard `HuggingFaceEndpointEmbeddings(model="http://...")` construction | Rejects the URL; do not use this path |
| Managed client initialization | `client is None` and `async_client` targets `{url}/embed` |
| Input exceeds model token limit | TEI truncates it because `truncate=True` |
| TEI unavailable or returns an HTTP error | Preserve the original `huggingface_hub` exception |
| Model output is not 512 dimensions | The current integration assertion fails |

### 5. Good / Base / Bad Cases

- Good: initialize once, reuse the manager client, call `aembed_documents` / `aembed_query`, then close it in the executable check's `finally` block.
- Base: a one-item documents batch returns one normalized 512-dimensional vector.
- Bad: use the standard endpoint constructor for a self-hosted URL, create a sync inference client, omit normalization, or change the Compose model without updating the query instruction and vector dimension.

### 6. Tests Required

```powershell
docker compose -f docs/docker/docker-compose.yml config
docker compose -f docs/docker/docker-compose.yml up -d text-embeddings-inference
uv run python -m app.conf.app_config
uv run python -m app_test.client.test_tei_embedding_client_manager
```

The integration test must assert the LangChain client type, `client is None`, async query/document calls, normalized vectors, and 512 dimensions. Compose inspection must show a healthy container, the `/data` volume, and no GPU device request.

### 7. Wrong vs Correct

```python
# Wrong: the standard constructor rejects a self-hosted URL.
HuggingFaceEndpointEmbeddings(model="http://localhost:8080/embed")

# Correct: manager injects only the async client.
HuggingFaceEndpointEmbeddings.model_construct(
    client=None,
    async_client=AsyncInferenceClient(model="http://localhost:8080/embed"),
)
```

## Scenario: MySQL Async Engine and Transactional Sessions

### 1. Scope / Trigger

Use this contract when changing the MySQL application configuration, the managed SQLAlchemy async engine, or business-code Session access. It keeps connection health, transaction ownership, concurrency, and shutdown behavior aligned.

### 2. Signatures

```python
class MysqlClientManager:
    @classmethod
    def initialize(cls) -> AsyncEngine: ...
    @classmethod
    def get_client(cls) -> AsyncEngine: ...
    @classmethod
    def session(cls) -> AbstractAsyncContextManager[AsyncSession]: ...
    @classmethod
    async def close(cls) -> None: ...
```

Business code uses one managed context and does not repeat transaction boilerplate:

```python
async with MysqlClientManager.session() as session:
    await session.execute(statement)
```

### 3. Contracts

- Required configuration: `conf/app_config.yaml` key `mysql.url`, using the `mysql+asyncmy` driver. The URL is the only project-defined MySQL setting.
- Construct the engine with `pool_pre_ping=True` and `pool_recycle=3600`.
- Initialize one reusable `async_sessionmaker` bound to the managed engine with `expire_on_commit=False`.
- Create a fresh `AsyncSession` for every `session()` context; never share one Session across concurrent tasks.
- A normal context exit commits. An exceptional exit rolls back and re-raises the original exception. The Session closes on either path.
- Repeated `initialize()` calls reuse the active engine and Session factory.
- `close()` must capture the old engine and clear both shared references before awaiting `old_engine.dispose()`. A concurrent reinitialization during disposal must survive the old close operation.

### 4. Validation & Error Matrix

| Condition | Required behavior |
|---|---|
| `mysql.url` missing, invalid, or an unknown MySQL config key is present | Pydantic configuration validation fails at startup |
| `get_client()` called before `initialize()` | Raise `RuntimeError` with the initialization instruction |
| `session()` entered before `initialize()` | Raise `RuntimeError` with the initialization instruction |
| `session()` body and commit complete | Commit once, then close the Session |
| `session()` body or commit raises | Roll back, close the Session, and propagate the exception |
| Two tasks use `session()` concurrently | Give each task a distinct `AsyncSession` bound to the same engine |
| `initialize()` runs while an older engine is disposing | Preserve the replacement engine and Session factory |
| MySQL is unavailable | Preserve the SQLAlchemy/asyncmy connection exception |

### 5. Good / Base / Bad Cases

- Good: initialize during application startup, use one `session()` context per business transaction, and close the manager during application shutdown.
- Base: a managed Session executes `SELECT 1`, commits on exit, and closes.
- Bad: store a global `AsyncSession`, require callers to repeat commit/rollback logic, disable stale-connection protection, or clear shared references after awaiting engine disposal.

### 6. Tests Required

```powershell
uv run python -m app.conf.app_config
uv run python -m app_test.client.test_mysql_client_manager
uv run ruff check app app_test
uv run pyright app app_test
```

The focused test must assert engine health settings, factory reuse, `expire_on_commit=False`, distinct concurrent Sessions, automatic commit and rollback, Session closure, live Engine/Session `SELECT 1`, close/reinitialize behavior, and the initialize-during-dispose race.

### 7. Wrong vs Correct

```python
# Wrong: a replacement created while dispose() awaits can be erased here.
await cls._client.dispose()
cls._client = None
cls._session_factory = None

# Correct: detach shared state first and dispose only the captured engine.
client = cls._client
cls._client = None
cls._session_factory = None
if client is not None:
    await client.dispose()
```
