# Current Layout Migration Impact

## Confirmed Inventory

- The current baseline has no `backend/`; backend runtime is under `src/data_agent/` and backend tests are under root `tests/`.
- About 192 active source/test files contain `data_agent` imports or module-path strings. The hard migration must update imports, monkeypatch targets, package-boundary searches, entry points, CI and current specs together.
- Root `pyproject.toml` owns console scripts, Ruff, Pyright, pytest and `uv_build`; root `uv.lock` records the editable source as `.`. Both must move together to `backend/`.
- `settings.py` resolves `conf/app_config.yaml` relative to the current source tree. After moving directly to `backend/src/settings.py`, the source-tree fallback must resolve `backend/conf/app_config.yaml` explicitly.
- The backend still owns legacy frontend assets and an `ENABLE_LEGACY_FRONTEND` static-mount path. The directory, switch, routes and compatibility tests must be removed together.
- `frontend/` already has independent npm, CI, Vite and Nginx/Caddy ownership; it does not import legacy backend assets.
- Root `.github/`, `docs/docker/` and `README.md` are repository/workspace assets. Compose paths are relative to `docs/docker/` and should remain together.

## Packaging Decision

The official uv build-backend documentation supports multiple root modules with an explicit `module-name` list but recommends a single module/package and describes discovered modules as directories containing `__init__.py`: <https://docs.astral.sh/uv/configuration/build-backend/>.

The required target also contains top-level entry/shared `.py` modules. The design therefore uses an explicit setuptools package list plus `py-modules` instead of relying on implicit discovery. This keeps `backend/src/` direct, makes wheel contents auditable and avoids recreating a replacement application namespace.

`logging.py` cannot remain a top-level installed module because it would shadow Python's standard-library `logging`; it becomes `app_logging.py`. External stable identifiers containing `data_agent` remain unchanged by user decision.

## Evidence Anchors

- `pyproject.toml:31-37,47-70` — scripts, build backend and quality paths.
- `src/data_agent/main.py:5-14` — application import and Uvicorn module string.
- `src/data_agent/settings.py:589-637` — config environment key and path precedence.
- `src/data_agent/application.py:166-208,240-242` — legacy frontend switch and mounts.
- `tests/unit/test_frontend.py:42-85,124-143` — API-only and legacy compatibility tests.
- `.github/workflows/ci.yml:20-55,104-165` — separate frontend job and root-oriented backend job.
- `frontend/deploy/nginx.conf:3-10` — independent frontend static root and API proxy.

## Historical and External-Identity Exclusions

- `.trellis/tasks/archive/` and workspace journals remain historical and are not rewritten.
- Database/schema/user names, `DATA_AGENT_CONFIG`, `data_agent_conversation`, log event names and CLI distribution branding are external stable identifiers, not Python package references.
