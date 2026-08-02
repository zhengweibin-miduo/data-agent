# Backend and Frontend Root Separation Design

## Purpose

Replace the mixed root `src/data_agent` Python layout with independently owned `backend/` and `frontend/` roots while preserving runtime behavior and the previously established bounded-context seams.

## Target Layout

```text
backend/
├── src/
│   ├── main.py
│   ├── application.py
│   ├── settings.py
│   ├── app_logging.py
│   ├── errors.py
│   ├── identifiers.py
│   ├── answer_readiness/
│   ├── chat/
│   ├── conversation/
│   ├── data_sync/
│   ├── ddl_metadata/
│   ├── infrastructure/
│   ├── memory/
│   ├── models/
│   └── persistence/
├── tests/
├── conf/
├── pyproject.toml
├── uv.lock
└── README.md

frontend/
├── src/
├── deploy/
├── package.json
└── README.md

.github/
docs/docker/
README.md
AGENTS.md
CONTEXT.md
CONTEXT-MAP.md
```

There is no `data_agent` Python package and no replacement umbrella namespace.

## Module and Interface Rules

- Bounded contexts remain deep modules at the same application seams: `memory.application`, `conversation.application`, `data_sync.application` and `ddl_metadata.meta_projection.application`.
- Moving directories must not enlarge their interfaces or expose internal seams. Existing production and in-memory adapters remain behind the same ports.
- Root composition modules (`main`, `application`, `settings`, `app_logging`) may import bounded-context adapters and infrastructure. Domain/application packages retain inward dependency direction.
- `logging.py` becomes `app_logging.py` because an installed top-level `logging` module would shadow the standard library.
- Imports become top-level package/module imports such as `from memory.application...`, `from settings...` and `from app_logging...`. No compatibility re-exports are created.

## Packaging and Entry Points

- Move project metadata and lockfile to `backend/`.
- Replace implicit `uv_build` discovery with an explicit setuptools configuration:
  - `package-dir = {"" = "src"}`;
  - package discovery limited to the named backend package families;
  - `py-modules` explicitly lists `main`, `application`, `settings`, `app_logging`, `errors` and `identifiers`.
- Preserve distribution and CLI branding. Console targets become `main:main` and `data_sync.worker:main`; the Uvicorn target becomes `main:app`.
- Verify wheel contents and import the installed wheel from outside the repository so editable/source-path leakage cannot hide missing modules.

## Configuration and Workspace Ownership

- `backend/conf/app_config.yaml` is the backend-owned default configuration.
- `DATA_AGENT_CONFIG` remains the explicit override. CWD lookup is evaluated from `backend/`; the source-tree fallback resolves `Path(__file__).parents[1] / "conf/app_config.yaml"`.
- Root `.python-version`, `.github/`, `docs/docker/`, `README.md`, Trellis and agent configuration remain repository/workspace owners.
- Backend CI steps run with `backend/` as working directory. Root Compose/SQL checks explicitly reference `docs/docker/` without copying those assets into the backend.

## Legacy Frontend Removal

- Delete the packaged legacy frontend directory.
- Delete `ENABLE_LEGACY_FRONTEND` parsing, `StaticFiles`/`FileResponse` imports, static mounts and legacy SPA routes.
- Keep the default API-only tests and CORS tests; remove compatibility-switch tests.
- The independent frontend remains served by its Nginx/Caddy deployment and communicates only over HTTP/SSE.

## Compatibility

- Preserve HTTP/SSE payloads and routes under `/api/`, MySQL/Redis schemas and keys, LangGraph/arq names, configuration fields, structured logs and frontend observable behavior.
- Root legacy SPA URLs permanently remain absent from FastAPI after this migration.
- Preserve external identifiers containing `data_agent`; only Python code namespace/path references are removed.
- No database, index or history migration is introduced.

## Test Seams

- Backend package/import boundary: installed-wheel smoke test and active-tree forbidden-path search.
- API composition: FastAPI is API-only with no static mount or frontend package data.
- Existing six behavior seams from the parent refactor remain authoritative; path changes do not add duplicate tests.
- Frontend transport/feature tests remain in `frontend/` and are not mirrored under `backend/tests/`.

## Rollback Shape

The move is one hard internal migration. Roll back the task commits as a unit if package installation, entry points or import boundaries cannot be preserved. Do not add dual import paths, a `data_agent` shim or duplicate source trees to make a partial move pass.
