# Frontend Hook Guidelines

## Current Scope

The frontend uses React function components and built-in hooks. It does not use
a query-cache or global-state dependency.

- Effects that own EventSource, interval, timeout, or browser event listeners
  must return cleanup functions.
- Lifecycle refs reset to their active value in effect setup because development
  `StrictMode` replays setup after cleanup.
- Use refs for imperative resource handles and current job identity; use state
  for renderable projections.
- Memoize callbacks only when they cross effect/component boundaries and stable
  identity is required.
- Do not hide API calls in presentation-only components. Transport remains in
  `frontend/src/api` and page orchestration owns request lifecycles.
