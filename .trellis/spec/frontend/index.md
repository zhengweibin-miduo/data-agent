# Frontend Development Guidelines

> Current conventions for the independent React + TypeScript Schema Loom UI.

## Overview

The frontend is the independent `frontend/` Vite application. FastAPI is
permanently API-only and the backend contains no frontend assets or compatibility
mount.

## Guidelines Index

| Guide | Description |
|-------|-------------|
| [Directory Structure](./directory-structure.md) | React source ownership and build output |
| [Component Guidelines](./component-guidelines.md) | Semantic DOM and accessibility conventions |
| [Hook Guidelines](./hook-guidelines.md) | React hook and resource-lifecycle rules |
| [State Management](./state-management.md) | URL, browser storage, and in-memory state ownership |
| [Quality Guidelines](./quality-guidelines.md) | npm and Python quality gates |
| [Type Safety](./type-safety.md) | TypeScript HTTP/SSE boundary rules |
| [API and Deployment Contract](./api-deployment-contract.md) | API base, CORS, SSE, static hosting, and proxy behavior |

## Pre-Development Checklist

- Work in `frontend/src/`; never add frontend assets to `backend/`.
- Run `npm ci` from `frontend/` and preserve `package-lock.json`.
- Keep model credentials and LLM calls on the server.
- Read the backend contract before changing a request, response, SSE event, or
  error projection.
- Preserve keyboard access, visible focus, reduced motion, and mobile stacking.

## Quality Check

- Run the commands in `quality-guidelines.md`.
- Confirm `npm run build` produces an independently deployable `frontend/dist/`.
- Confirm FastAPI exposes no frontend routes or static mounts and the backend wheel contains no frontend assets.
- Verify `waiting_input` uses an authoritative job read before answers are
  enabled because SSE events do not contain `question_set_id`.

---

**Language**: All documentation should be written in **English**.
