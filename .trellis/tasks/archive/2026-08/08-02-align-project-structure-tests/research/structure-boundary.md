# Structure and boundary reconnaissance

## Facts observed

- Repository has a dedicated `frontend/` application containing Vite/React source (`frontend/src`), package/build files (`frontend/package.json`, `frontend/vite.config.ts`), and deployment examples (`frontend/deploy/nginx.conf`, `frontend/deploy/Caddyfile`). This matches the documented layout in `.trellis/spec/frontend/directory-structure.md:5-20`.
- Python backend lives under `src/data_agent/`; `src/data_agent/frontend/` contains only three legacy static files (`index.html`, `app.js`, `styles.css`) plus `__init__.py` (glob evidence). The compatibility directory is mounted only by `_mount_legacy_frontend` in `src/data_agent/application.py:159-188`, gated by `_legacy_frontend_enabled()` (`:146-156`) and called from `create_app()` (`:220-222`). Default env is `false` (`:149`), and tests assert `/`, `/workbench`, `/assets` return 404 by default (`tests/unit/test_frontend.py:42-67`).
- FastAPI mounts legacy `/assets` and legacy SPA routes (`/`, `/workbench`, `/workbench/{job_id}`, `/knowledge`) only when `ENABLE_LEGACY_FRONTEND` is an accepted true value (`src/data_agent/application.py:175-188`). Invalid values raise `ValueError` (`:154-156`); startup logs a deprecation warning (`:171-174`). This agrees with the API/deployment contract (`.trellis/spec/frontend/api-deployment-contract.md:12-14,84-87`).
- API composition registers versioned routers and health at `/api/v1/health` (`src/data_agent/application.py:204-218`); no code path reads Vite source or `frontend/dist` during default startup. Production static hosting is documented as independent (`README.md:95-115`) and examples proxy `/api/` to loopback FastAPI.
- No root `contracts/` directory exists in this worktree (root glob lists no `contracts`); this is allowed because AGENTS.md describes it as optional (`AGENTS.md:68-75`). No frontend file imports `src/data_agent/frontend/` (search found no matches), and no hardcoded API origin appears in frontend TS/TSX; the only loopback literals are deployment/Vite host settings (`frontend/vite.config.ts:7-13`, `frontend/deploy/nginx.conf:1-8`).

## Potential inconsistencies / risks

1. **Wheel inclusion of legacy assets is not explicit (needs confirmation).** AGENTS.md says `src/data_agent/frontend/` may be distributed with the Python package (`AGENTS.md:70-72`), while `pyproject.toml` only declares `uv_build` and project scripts (`pyproject.toml:31-37`) and has no explicit package-data/include rule. Runtime source-tree mounting is correct, but packaging a wheel should be verified separately; no definitive violation can be proven from static config alone.
2. **Legacy directory still contains a full UI bundle.** `src/data_agent/frontend/app.js` and `index.html` are present, but this is consistent with the explicitly permitted migration-only compatibility assets (`AGENTS.md:70-72`; spec layout `directory-structure.md:18-24`) so it is not a confirmed ownership violation unless the intent is to remove legacy assets entirely.
3. **`contracts/` absence is not a violation.** The directory is optional and no contract source is required by current code; generated/request types remain in `frontend/src/api/types.ts` (under the frontend owner), which is consistent with the current documented layout.

## Verification anchors

- Boundary rules: `AGENTS.md:68-75`.
- Frontend layout and legacy mount policy: `.trellis/spec/frontend/directory-structure.md:5-34`.
- Deployment/API contract: `.trellis/spec/frontend/api-deployment-contract.md:75-87,95-114`.
- Runtime mount and env parsing: `src/data_agent/application.py:146-188,191-224`.
- Structure tests: `tests/unit/test_frontend.py:18-40,42-86`.
- Packaging metadata: `pyproject.toml:31-37`.
