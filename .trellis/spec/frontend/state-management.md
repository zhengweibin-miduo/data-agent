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
- Keep navigation blocked while an uncertain or lease-bearing failed chat is
  awaiting retry; otherwise unmounting loses the only reusable `turn_uid`.
- A `waiting_input` SSE event clears submit coordinates and triggers GET of the
  current `JobRecord` before rendering answer controls.
- Keep native EventSource alive on network `error` so the browser can reconnect
  with `Last-Event-ID`; perform one authoritative GET while waiting. Switch to
  polling only when EventSource is unavailable, event payload parsing fails, or
  the server emits `stream_error`. Terminal jobs stop every handle.
- Keep unsaved-DDL navigation protection while the input view is active.
- Treat a change between two `/workbench` pathnames as leaving the current
  workbench session; history navigation must apply the same unsaved-input and
  chat-coordinate guards before remounting for another task.
- Preserve the latest `/workbench/{job_id}` URL when navigating to another SPA
  view so returning to the workbench restores the active task and subscription.
- Abort an in-flight task submission when its workbench unmounts, and reject its
  late continuation before changing browser history or opening an event stream.
- Do not replace a non-terminal task with another submission unless the UI first
  preserves a discoverable recovery coordinate for the active task.
- When a legacy backend times out during acceptance, keep the attempt explicitly
  non-replayable in session storage so neither automatic nor manual actions
  duplicate the POST, including after a full document reload.
- A deep-link restore clears sample input and locks DDL actions before starting
  its GET. Retry transient restore failures while keeping that lock; only an
  authoritative not-found response may release the coordinate. Ignore the whole
  continuation when it no longer owns the active job.
- Keep the failed chat retry gate only for uncertain or lease-bearing failures.
  Deterministic non-retryable client validation failures must return the inputs
  to an editable state so the user can correct the frozen DDL context.
- Initialize the remembered workbench route only from `/workbench` paths; a
  directly loaded non-workbench view must navigate to `/workbench`, not reuse
  its own pathname as a workbench coordinate.
- Do not let a late AI clarification draft overwrite an answer changed after
  the request began. Apply the draft only while the target answer still matches
  the request-time snapshot.
- A restored waiting-input task freezes the reloaded source and DDL when the
  user explicitly requests an AI draft. Missing context must leave draft mode
  so ordinary chat remains recoverable.
- Workbench operations share one interaction gate. Every handler and control
  must reject a new operation while another request owns that gate so one
  request cannot release or replace another request's busy state.
- Write a client-generated submission ID to the workbench URL before sending
  the acceptance request because that ID is also the server job ID and must
  survive a full document reload before the response arrives.
- Keep an unconfirmed submission marker in session storage for a bounded
  acceptance window. A GET 404 inside that window may have raced the original
  POST and must not unlock the sample workbench or discard the job coordinate.
- While a memory detail switch is loading, keep mutation controls disabled and
  identify the selected record in destructive confirmations.
- When a persisted submission coordinate differs from the current task deep
  link, reconcile it first but retain the deep link as the fallback after an
  authoritative 404.
- A deterministic validation failure for a restored task's AI clarification
  draft releases both draft mode and its frozen reloaded DDL context so the
  corrected editor state can be frozen by the next request.
