# Frontend Directory Structure

## Current Layout

```text
src/data_agent/frontend/
├── __init__.py
├── index.html
├── styles.css
└── app.js
tests/unit/test_frontend.py
```

FastAPI mounts the directory at `/assets` and serves `index.html` for `/`,
`/workbench`, `/workbench/{job_id}`, and `/knowledge`. The files are package
data inside the Python distribution; there is no separate frontend build.

## Rules

- Keep the application in this directory while it remains framework-free.
- Add a file only when it owns a distinct runtime asset; do not create empty
  component, hook, page, or utility trees.
- Route ownership stays in `data_agent.application`; API calls stay relative so
  the local browser uses the same origin.
- Never commit generated caches such as `__pycache__`.
