# Frontend Development Guidelines

> Current conventions for the framework-free local Schema Loom UI.

## Overview

The frontend is a small HTML/CSS/JavaScript application served by FastAPI from
`src/data_agent/frontend/`. It has no package manifest, framework, bundler, or
browser dependency. Keep that boundary until a concrete requirement outgrows
the static implementation.

## Guidelines Index

| Guide | Description |
|-------|-------------|
| [Directory Structure](./directory-structure.md) | Static asset ownership and FastAPI routes |
| [Component Guidelines](./component-guidelines.md) | Semantic DOM and accessibility conventions |
| [Hook Guidelines](./hook-guidelines.md) | Explicit non-applicability of framework hooks |
| [State Management](./state-management.md) | URL, browser storage, and in-memory state ownership |
| [Quality Guidelines](./quality-guidelines.md) | Runnable JavaScript and Python checks |
| [Type Safety](./type-safety.md) | Plain-JavaScript API boundary checks |

## Pre-Development Checklist

- Reuse `index.html`, `styles.css`, and `app.js`; do not add a frontend framework
  or dependency for a change these files can express directly.
- Keep model credentials and LLM calls on the server.
- Read the backend contract before changing a request, response, SSE event, or
  error projection.
- Preserve keyboard access, visible focus, reduced motion, and mobile stacking.

## Quality Check

- Run the commands in `quality-guidelines.md`.
- Confirm static assets are included in the built wheel.
- Verify `waiting_input` uses an authoritative job read before answers are
  enabled because SSE events do not contain `question_set_id`.

---

**Language**: All documentation should be written in **English**.
