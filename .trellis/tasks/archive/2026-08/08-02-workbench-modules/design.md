# Workbench Internal Modules Design

## Purpose

Refactor the Workbench feature into explicit internal state owners without changing its visual direction, HTTP/SSE contracts, URL/storage authority, or user-visible workflow. The page remains the feature entry and composition surface; hooks own asynchronous lifecycles and pure modules own recovery decisions.

## Frontend Design Direction

This is a structural refactor, not a visual redesign. Preserve the existing Schema Loom “instrument console” direction:

- **Palette**: Ink `#08111f`, Slate `#13233a`, Cyan `#38bdf8`, Violet `#8b7cff`, Amber `#f3b64c`, Ice `#e8f0f7`.
- **Type**: Segoe UI Variable / Microsoft YaHei UI for interface text; Cascadia Code / IBM Plex Mono for DDL, trace, IDs, and machine state.
- **Layout**: keep the current editor + lineage + trace workspace and mobile stacking.
- **Signature**: the live trace dock remains the single memorable console element; extracted components and hooks must not add decorative UI.
- **Accessibility floor**: preserve semantic controls, visible focus, live status/error regions, keyboard reachability, and reduced motion.

The deliberate choice is restraint: module boundaries should make the interface more reliable without creating a visually new product.

## State Ownership

- URL owns the active Workbench route and `job_id`.
- `WorkbenchPage` owns DDL/source inputs, page-level interaction gate, rendered job projection, answers, and composition of internal hooks.
- `useJobSubscription` owns current job identity ref, SSE/poll subscription handle, stage projection, authoritative job updates, and cleanup.
- `useJobRestore` owns deep-link and persisted-attempt reconciliation, restore lock, retry/not-found handling, and stale continuation rejection.
- `submissionRecovery` owns pure storage/URL decision rules and fingerprint matching; it performs no API calls.
- `useChatSession` owns user/conversation IDs, retry coordinates, same-`turn_uid` retry, deterministic-vs-uncertain failure gate, and late draft snapshot checks.
- Backend GET/SSE remains authoritative; session/local storage stores recovery coordinates only.

## Internal Interfaces

### `submissionRecovery.ts`

Pure types and functions project persisted submission attempts into one of: resume current coordinate, reconcile foreign coordinate with deep-link fallback, mark legacy attempt non-replayable, clear confirmed coordinate, or ignore stale continuation. Browser storage access stays behind the existing storage adapter functions.

### `useJobSubscription.ts`

Receives stable transport functions and callbacks for accepted authoritative jobs. It exposes `watch(jobId)`, `stop()`, current connection state, and renderable stage/error state. Native EventSource reconnect/poll behavior remains exclusively tested in `api/jobEvents.test.ts`; feature tests drive the hook through a narrow in-memory job lifecycle adapter rather than reading mock call history.

### `useJobRestore.ts`

Coordinates URL job ID, persisted attempt, authoritative GET retry, not-found release, source/DDL reset, and ownership cancellation. It never enables clarification from SSE alone; `waiting_input` first triggers authoritative GET.

### `useChatSession.ts`

Coordinates conversation creation/retry and AI clarification draft snapshots. It reuses failed `turn_uid`, preserves navigation guards for uncertain/lease-bearing failures, releases deterministic validation failures, and rejects late drafts after the answer changed.

## Test Responsibility

- `api/jobEvents.test.ts` owns EventSource reconnect, polling fallback, malformed events, close races, and authoritative GET mechanism.
- Hook/module tests own recovery decisions, subscription cleanup, stale continuation guards, retry coordinates, and draft snapshot rules.
- `WorkbenchPage.test.tsx` owns only rendered/user-observable restore, submission, clarification, chat, navigation guard, and terminal result behavior.
- Remove all tests that fetch `connectJobEvents.mock.calls[n][1]` and invoke internal callbacks. Use narrow harnesses to publish lifecycle outcomes through the new seam.
- Consolidate repeated preview/submit setup in local scenario helpers; keep payload/call-count assertions only for idempotency, bounded retry, and stable public contracts.

## Compatibility

- No backend, HTTP, SSE, `ApiError`, URL, storage-key, payload, copy, CSS token, or layout change.
- No new global state/query-cache dependency.
- No changes under `src/data_agent/frontend/`.
- Effects must clean up EventSource, polling, abort controllers, timers, and browser listeners; StrictMode setup restores active refs.

## Verification

Run frontend lint, typecheck, focused and full Vitest, build, Python frontend compatibility tests, static searches for callback-history driving, and browser/design-guideline review. Fix findings and repeat the review before task completion.
