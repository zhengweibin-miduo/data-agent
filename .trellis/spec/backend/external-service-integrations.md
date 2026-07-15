# External Service Integrations

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

- Required configuration: `conf/app.yaml` key `mysql.url`, using the `mysql+asyncmy` driver. The URL is the only project-defined MySQL setting.
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
uv run python -m app_test.clients.test_mysql_client_manager
uv run --with ruff ruff check app app_test
uv run --with pyright pyright app app_test
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
