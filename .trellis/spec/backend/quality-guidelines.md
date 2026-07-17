# Backend Quality Guidelines

## Runtime and Dependency Baseline

- Python is constrained to `>=3.13,<3.14` in `pyproject.toml`, and
  `.python-version` pins `3.13`.
- Dependencies and their resolved versions are managed by `uv` and `uv.lock`.
- Runtime packages use explicit type annotations and short Chinese module,
  class, and method docstrings. Representative files are
  `app/conf/app_config.py`, `app/model/ddl_metadata.py`, and modules under
  `app/client/`, `app/service/`, `app/repository/`, and `app/worker/`.
- Review findings from Codex, Trellis check agents, and other AI reviewers must
  be written in Simplified Chinese per `AGENTS.md`; identifiers, paths,
  commands, keys, and original errors remain in English.

## Required Local Patterns

- Configuration models inherit `ConfigModel`, whose
  `ConfigDict(extra="forbid")` rejects unknown fields.
- Shared async clients use a typed `ClassVar[ClientType | None]`, idempotent
  `initialize()`, guarded `get_client()`, and async `close()`.
- Package `__init__.py` files remain side-effect free.
- HTTP, model, graph, Redis, and persistence boundaries reuse the Pydantic
  contracts in `app/model/ddl_metadata.py`; consumers do not cast shared JSON
  payloads independently.
- Unit-style graph/model checks use deterministic fakes and must not require a
  live LLM. Live integration modules clearly identify their MySQL/Redis/TEI
  dependencies.
- Repository integration data uses UUID-derived sources/stable IDs and scoped
  cleanup. Tests never reset or delete the shared developer Docker volume.
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
uv run python -m app_test.core.test_logging
uv run python -m app_test.client.test_mysql_client_manager
uv run python -m app_test.client.test_redis_client_manager
uv run python -m app_test.client.test_llm_client_manager
uv run python -m app_test.service.ddl_metadata.test_parser
uv run python -m app_test.service.ddl_metadata.test_validator
uv run python -m app_test.repository.ddl_metadata.test_meta
uv run python -m app_test.repository.ddl_metadata.test_memory
uv run python -m app_test.service.ddl_metadata.test_memory
uv run python -m app_test.service.ddl_metadata.test_graph
uv run python -m app_test.worker.test_ddl_metadata
uv run python -m app_test.api.test_ddl_metadata_api
uv run python -m app_test.integration.test_ddl_metadata_flow
docker compose -f docs/docker/docker-compose.yml config
git diff --check
```

The MySQL repository and integration checks require the service credentials
shown in CI or `docs/docker/docker-compose.yml`. Redis manager, worker, and
combined flow checks require Redis 8. CI provides both services. The TEI live
check is not part of the current CI job; run
`uv run python -m app_test.client.test_tei_embedding_client_manager` when the
TEI integration changes and the local service is available.

Before MySQL repository checks, CI applies
`docs/docker/mysql/data_agent.sql` through the root account. Developers reusing
an initialized Compose volume must do the same once because MySQL entrypoint
bootstrap scripts run only for an empty volume. This command creates/grants the
application database idempotently and must not be replaced with destructive
cleanup of legacy Meta memory tables.

No CI check contacts a live LLM. `test_llm_client_manager` mocks the capability
probe; the real worker startup probe must be run separately against the
configured endpoint before deployment.

The project does not declare persistent Ruff or Pyright configuration in
`pyproject.toml`; CI invokes their defaults through `uv --with`.

## Review Checklist

- Trace configuration changes across `conf/app_config.yaml`, the Pydantic model,
  every consumer, and the module-level configuration assertion.
- Verify that a new client follows the existing lifecycle and closes the exact
  underlying async resource.
- Confirm tests exercise real behavior rather than only checking object shape;
  MySQL runs live transactions, Redis checks atomic state, the integration flow
  uses real Redis checkpoints plus MySQL persistence, and TEI requests vectors
  and checks dimensions/normalization.
- For parser/LLM work, prove unsupported SQL rejects before a model call and
  physical objects cannot be added, removed, renamed, or retyped by model
  output.
- For graph/worker work, prove interrupt/resume revision safety and that a
  persistence retry does not repeat completed model calls.
- For repository/memory work, prove scoped cleanup, rollback, exact compatible
  reuse, archive exclusion, and correction supersession.
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
- Tests that call a real paid/model endpoint in CI.
- Destructive integration cleanup against the shared MySQL or Redis volume.
- Claims that a skipped or unavailable live-service check passed.
