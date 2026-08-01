# Frontend State Management

## Ownership

- The URL owns the active view, known `job_id`, memory query, and selected memory.
- React state owns only the current page session: DDL text, job projection,
  chat retry coordinates, answers, and selected memory. Refs own SSE handles and
  the active job identity.
- `sessionStorage` keeps the current conversation UID. `localStorage` keeps the
  local user ID. Neither stores DDL, model credentials, or server authority.
- Redis/MySQL and GET responses remain authoritative for jobs, conversations,
  and memories; browser state is never treated as proof of acceptance.

## Rules

- A chat retry reuses the failed attempt's `turn_uid`; generating a new ID leaves
  the persisted active turn busy.
- A `waiting_input` SSE event clears submit coordinates and triggers GET of the
  current `JobRecord` before rendering answer controls.
- Keep native EventSource alive on network `error` so the browser can reconnect
  with `Last-Event-ID`; perform one authoritative GET while waiting. Switch to
  polling only when EventSource is unavailable, event payload parsing fails, or
  the server emits `stream_error`. Terminal jobs stop every handle.
- Keep unsaved-DDL navigation protection while the input view is active.
