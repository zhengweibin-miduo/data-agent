# Frontend State Management

## Ownership

- The URL owns the active view, known `job_id`, memory query, and selected memory.
- The module-level `state` object owns only the current page session: DDL text,
  job projection, SSE/poll handles, chat retry coordinates, and selected memory.
- `sessionStorage` keeps the current conversation UID. `localStorage` keeps the
  local user ID. Neither stores DDL, model credentials, or server authority.
- Redis/MySQL and GET responses remain authoritative for jobs, conversations,
  and memories; browser state is never treated as proof of acceptance.

## Rules

- A chat retry reuses the failed attempt's `turn_uid`; generating a new ID leaves
  the persisted active turn busy.
- A `waiting_input` SSE event clears submit coordinates and triggers GET of the
  current `JobRecord` before rendering answer controls.
- Close old EventSource, interval, and timeout handles before starting fallback
  updates. Terminal jobs stop all update handles.
- Keep unsaved-DDL navigation protection while the input view is active.
