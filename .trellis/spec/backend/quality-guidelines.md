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
- Async live checks use an inner coroutine plus a synchronous executable
  wrapper:

```python
async def _test_mysql_client() -> None:
    ...

def test_mysql_client() -> None:
    asyncio.run(_test_mysql_client())
```

Both current checks also expose an `if __name__ == "__main__"` path because CI
and local validation run them as modules rather than through pytest.

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
