# Phase 1 Reconnaissance

## Manifest, languages, frameworks

- Python package `data-agent` version `0.1.0`; Python constraint `>=3.13,<3.14` in `pyproject.toml:1-8`. Build backend is `uv_build` (`pyproject.toml:35-37`) and lockfile is `uv.lock`.
- Backend dependencies fingerprint FastAPI/Uvicorn, SQLAlchemy + asyncmy, Redis/arq, Elasticsearch async client, Qdrant, LangGraph/LangChain, MySQL replication, Pydantic and sqlglot (`pyproject.toml:9-28`).
- Python console scripts: `data-agent-api = data_agent.main:main` and `data-agent-cdc = data_agent.data_sync.worker:main` (`pyproject.toml:31-33`).
- Independent frontend under `frontend/`: Node `>=20.19.0`, React 19, Vite 7, TypeScript 5.8, Vitest (`frontend/package.json:2-14`, `frontend/package.json:16-35`).
- `.python-version` is consumed by CI (`.github/workflows/ci.yml:104-108`); exact value is not inferred here.

## Top-level / second-level structure

- `src/data_agent/` is the Python backend package. Major bounded areas visible at second level: `answer_readiness`, `chat`, `conversation`, `data_sync`, `ddl_metadata`, `infrastructure`, `memory`, `metadata_indexing`, `models`, `persistence`, plus `main.py`, `application.py`, `settings.py`, `logging.py`, `errors.py`.
- `src/data_agent/frontend/` contains migration-era static assets (`app.js`, `index.html`, `styles.css`); the independent frontend source is rooted at `frontend/` (see `application.py:159-188` for compatibility mount).
- `tests/` is split into `unit/`, `integration/`, and `helpers/`; test modules mirror backend areas (directory snapshot via `tests/**/*`).
- `docs/docker/` contains `docker-compose.yml` and MySQL initialization scripts; `.github/workflows/` contains CI plus Codex/Claude automation workflows.

## Runtime entry points and request composition

- `src/data_agent/main.py:5-18` imports `create_app`, exposes module-level ASGI `app = create_app()`, and `main()` runs `uvicorn` using configured host/port.
- `src/data_agent/application.py:191-224` creates FastAPI titled `Data Agent DDL Metadata API`, adds CORS and request logging middleware, registers `DataAgentError`/Redis exception handlers, includes DDL metadata, conversation and chat routers, and exposes `/api/v1/health`.
- Application lifespan initializes Redis, MySQL, Elasticsearch, Qdrant, TEI and LLM clients, builds an arq queue, then mounts `DDLJobStore`, `MemoryService`, `ConversationService`, and `ChatService` on `app.state` (`application.py:43-81`); resources close in reverse order (`application.py:82-98`).
- Legacy embedded frontend routes `/`, `/workbench`, `/knowledge` and assets are only mounted when `ENABLE_LEGACY_FRONTEND` parses true (`application.py:146-188`, `application.py:220-224`); default is API-only (`application.py:191-194`).

## CDC / worker entry

- `data-agent-cdc` resolves to `data_agent.data_sync.worker:main` (`pyproject.toml:31-33`).
- `src/data_agent/data_sync/worker.py:20-72` defines async `run_worker()`: initializes MySQL, creates one `MySQLSourceClient` per configured source, acquires worker locks, checks binlog capabilities and SELECT access, then loops `DataSyncService.dispatch_once()` with configured backoff. `main()` wraps it with logging boundary and `asyncio.run` (`worker.py:87-98`).
- Binlog source implementation is `src/data_agent/data_sync/binlog.py` (imported by worker at `worker.py:12`); CDC tests include `tests/integration/data_sync/test_cdc_pipeline.py` and unit tests under `tests/unit/data_sync/`.

## Build, packaging, Docker and CI

- Python packaging uses `uv_build`; development tools are Ruff, Pyright, pytest and pytest-asyncio (`pyproject.toml:35-45`). Ruff targets `py313`, scans `src` and `tests` (`pyproject.toml:47-56`).
- Pytest discovers `tests`, uses importlib mode, auto asyncio, and markers `integration` and `tei` (`pyproject.toml:58-66`).
- Frontend scripts are Vite dev/build, ESLint, TypeScript typecheck and Vitest (`frontend/package.json:9-14`). CI runs frontend quality (npm ci/lint/typecheck/test/build) in `frontend/` (`.github/workflows/ci.yml:19-55`).
- No root `Dockerfile` matched. `docs/docker/docker-compose.yml` provisions MySQL 8.4 with ROW/FULL binlog, Qdrant 1.18, custom Elasticsearch IK image 8.19.17, TEI CPU image, and Redis 8.8; host mappings are loopback-only (`docs/docker/docker-compose.yml:1-80`).
- GitHub Actions `quality` job starts MySQL, Redis and Elasticsearch services, installs with `uv sync --locked`, initializes SQL fixtures, renders Compose config, checks lock, runs Ruff, Pyright, compileall, settings validation, then `pytest -m "not tei"` (`.github/workflows/ci.yml:56-166`).

## Test structure

- Unit tests live under `tests/unit/` and mirror package areas: answer readiness, chat, conversation, data_sync, DDL metadata (including jobs/redis/worker/workflow), infrastructure, memory, metadata indexing, settings and persistence.
- Integration tests live under `tests/integration/` and cover API, DDL metadata flow, job events, worker, memory services/index refresh, persistence repositories, data sync CDC pipeline, infrastructure MySQL/Redis/TEI, and answer readiness (`tests/integration/**/*`).
- Shared test support is in `tests/helpers/{checks.py,factories.py,fakes.py}`.

## Phase 1 unknowns

- No root Dockerfile was present in the reconnaissance glob; deployment may rely on external packaging/hosting not represented by this worktree.
- No framework config beyond FastAPI application code and frontend Vite config was required to fingerprint the stack; detailed request data flow is Phase 2 scope.
