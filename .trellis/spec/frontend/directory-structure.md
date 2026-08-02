# Frontend Directory Structure

## Current Layout

```text
frontend/
├── src/
│   ├── api/          # typed HTTP and SSE adapters
│   ├── knowledge/    # authoritative-memory workspace
│   ├── workbench/    # DDL canvas, trace, chat, clarification
│   ├── App.tsx
│   ├── main.tsx
│   └── styles.css
├── package.json
├── package-lock.json
└── vite.config.ts

src/data_agent/frontend/  # migration-only legacy assets
tests/unit/test_frontend.py
```

Vite owns development and production builds. FastAPI exposes only API/OpenAPI/
health routes by default. The legacy directory is mounted only when
`ENABLE_LEGACY_FRONTEND=true`.

## Rules

- Put transport code in `frontend/src/api`, page-specific code in its feature
  directory, and application navigation in `App.tsx`.
- Keep `WorkbenchPage` as the Workbench composition/render entry. Pure submission
  recovery rules and hooks for job subscription, restore, and chat session state
  live beside it in `frontend/src/workbench`; each hook owns its effects, refs,
  cleanup, and stale-continuation guards rather than duplicating them in the page.
- Components do not concatenate deployment origins; all URLs go through
  `resolveApiUrl` / `apiRequest` or the SSE adapter.
- Never commit `node_modules`, `dist`, coverage, or `*.tsbuildinfo`.
- Do not import files from `src/data_agent/frontend/` into the Vite application.
