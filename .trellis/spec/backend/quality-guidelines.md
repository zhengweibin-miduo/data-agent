# Backend Quality Guidelines

## Runtime and Dependency Baseline

- Python is constrained to `>=3.13,<3.14` in `pyproject.toml`, and
  `.python-version` pins `3.13`.
- Dependencies and their resolved versions are managed by `uv` and `uv.lock`.
- Runtime packages use explicit type annotations and short Chinese module,
  class, and method docstrings. Representative files are
  `app/conf/app_config.py` and every module under `app/client/`.
- Review findings from Codex, Trellis check agents, and other AI reviewers must
  be written in Simplified Chinese per `AGENTS.md`; identifiers, paths,
  commands, keys, and original errors remain in English.

## Required Local Patterns

- Configuration models inherit `ConfigModel`, whose
  `ConfigDict(extra="forbid")` rejects unknown fields.
- Shared async clients use a typed `ClassVar[ClientType | None]`, idempotent
  `initialize()`, guarded `get_client()`, and async `close()`.
- Package `__init__.py` files remain side-effect free.
- Metadata synchronization keeps configuration parsing in `app/conf/`, CLI
  lifecycle in `app/script/`, business dataclasses in `app/entity/`, Meta table
  mappings in `app/model/`, orchestration in `app/service/`, and concrete
  persistence calls in `app/repository/`.
- Prefer list, set, and dictionary comprehensions when every output item is a
  pure expression. Keep an explicit loop when the body awaits, logs, validates
  and raises, or maintains order-dependent state.
- Async checks use inner coroutines plus synchronous executable wrappers:

```python
async def _test_manager_configuration() -> None:
    ...

def test_mysql_client_manager() -> None:
    asyncio.run(_test_manager_configuration())
```

Current test modules also expose an `if __name__ == "__main__"` path because CI
and focused local validation run them as modules rather than only through
pytest collection.

## Validation Commands

The repository CI in `.github/workflows/ci.yml` defines the baseline:

```powershell
uv sync --locked
uv lock --check
uv run --with ruff ruff check app app_test
uv run --with pyright pyright app app_test
uv run python -m compileall -q app app_test main.py
uv run python -m app.conf.app_config
uv run python -m app_test.client.test_mysql_client_manager
```

The MySQL check is a live integration test and requires the service credentials
shown in CI or `docs/docker/docker-compose.yml`. The TEI live check is not part
of the current CI job; run
`uv run python -m app_test.client.test_tei_embedding_client_manager` when the
TEI integration changes and the local service is available.

Metadata synchronization changes also require these dependency-isolated gates:

```powershell
uv run python -m app_test.service.test_metadata_sync_service
uv run python -m app.script.sync_metadata --help
git diff --check
```

The focused metadata module must not be described as a live integration test.
Run `uv run python -m app.script.sync_metadata --config conf/meta_config.yaml`
only when MySQL, Qdrant, Elasticsearch, and TEI are all available and writable.
Run it twice and inspect Meta row keys, Qdrant point counts, and Elasticsearch
document IDs to validate replay against real services. When any dependency is
unavailable, report the exact missing service and mark the live integration as
not run.

The project does not declare persistent Ruff or Pyright configuration in
`pyproject.toml`; CI currently invokes their defaults through `uv --with`.

## Review Checklist

- Trace configuration changes across `conf/app_config.yaml`, the Pydantic model,
  every consumer, and the module-level configuration assertion.
- Verify that a new client follows the existing lifecycle and closes the exact
  underlying async resource.
- Confirm tests exercise real behavior rather than only checking object shape;
  the MySQL test runs `SELECT 1`, while the TEI test requests vectors and checks
  dimensions and normalization.
- For metadata synchronization, trace YAML through Config, CLI, Service,
  business Entity, Repository, and ORM Model. Check strict configuration
  validation, DW-schema validation before dynamic SQL, bounded reads, stable
  IDs, Model-derived MySQL upsert statements, single-encoded JSON lists,
  Qdrant anonymous dense plus named `bm25`/IDF configuration, shared
  `Document(model="Qdrant/bm25")` and complete `Bm25Config`, additive
  collection migration that preserves but does not write legacy `sparse`,
  Elasticsearch helper actions, and all-manager cleanup.
- Confirm a replay test compares logical ID sets, not an expression with itself;
  alias reordering must leave the Qdrant ID set unchanged. BM25 tests must cover
  English and Chinese `Document` inputs, every fixed config field, rejection of
  extra/legacy vector keys, and the remote client facade preserving the
  `Document`; they must not present in-memory Qdrant as core BM25 verification.
- Run the checks relevant to the changed service and report unavailable live
  dependencies explicitly.
- Keep changes scoped; do not mix unrelated formatting, dependency, or
  infrastructure updates into a focused task.

## Forbidden Patterns

- Unknown or silently ignored configuration fields.
- Unannotated shared client state or a shared client typed as `Any`.
- Sync client calls inside the established async manager layer.
- Resource acquisition without a corresponding async close path.
- Tests that leak a client when an assertion or request fails.
- Random or position-based IDs in a replayable synchronization flow.
- A mocked metadata test reported as proof of real MySQL/Qdrant/Elasticsearch/
  TEI integration.
