# Frontend Quality Guidelines

## Required Checks

Run from `frontend/`:

```powershell
npm ci
npm run lint
npm run typecheck
npm run test
npm run build
```

From `backend/` also run `uv run pytest tests/unit/test_frontend.py`, Ruff,
Pyright, and non-integration pytest. CI must run the npm gates in an independent
job using `frontend/package-lock.json`.

## Review Checklist

- Test DDL byte counting, authoritative waiting-input refresh, native SSE
  reconnect semantics, polling fallback, stable chat retry IDs, safe server-only
  LLM access, API-only startup, and CORS.
- API adapter tests own EventSource reconnect, polling fallback, malformed-event,
  and close-race mechanisms. Workbench hook tests own restore/subscription/chat
  lifecycle decisions; page tests assert rendered user-observable outcomes and
  must not invoke callbacks through mocked adapter call history.
- Check 360px, 768px, and desktop layouts; keyboard-only operation; visible
  focus; 200% zoom; and `prefers-reduced-motion`.
- Review API errors by stable `code`, `stage`, and `retryable` fields. Do not
  expose stack traces, model credentials, control-plane state, or raw model data.
- Review findings are written in Simplified Chinese per `AGENTS.md`.
