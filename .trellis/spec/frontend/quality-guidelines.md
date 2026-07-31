# Frontend Quality Guidelines

## Required Checks

The frontend deliberately has no package manager or build command. Run:

```powershell
node --check src/data_agent/frontend/app.js
node src/data_agent/frontend/app.js --self-check
uv run pytest tests/unit/test_frontend.py
uv build --wheel
```

Inspect the wheel and confirm it contains `data_agent/frontend/index.html`,
`styles.css`, and `app.js`. The repository-wide Ruff, Pyright, compileall, and
non-integration pytest gates remain required because FastAPI owns static serving
and chat orchestration.

## Review Checklist

- Test DDL byte counting, authoritative waiting-input refresh, stable chat retry
  IDs, safe server-only LLM access, and static route serving.
- Check 360px, 768px, and desktop layouts; keyboard-only operation; visible
  focus; 200% zoom; and `prefers-reduced-motion`.
- Review API errors by stable `code`, `stage`, and `retryable` fields. Do not
  expose stack traces, model credentials, control-plane state, or raw model data.
- Review findings are written in Simplified Chinese per `AGENTS.md`.
