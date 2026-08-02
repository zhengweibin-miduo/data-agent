# Frontend onboarding / flow evidence

## Reconnaissance

- React 19 + Vite 7 TypeScript app. `src/main.tsx:1-10` creates the root and renders `<App />`; global styling is imported there. Build/lint/typecheck/test scripts are declared in `frontend/package.json:9-15`.
- Application shell and routing live in `src/App.tsx:6-17,47-65,77-92,101-115`. It is a lightweight two-view shell (`workbench`/`knowledge`) with History API routing (`pushState`, `popstate`), navigation guards, skip link, header/nav, and feature page composition. No router dependency is present.
- Feature boundaries are explicit: `src/workbench/WorkbenchPage.tsx` (DDL/job/chat workbench), `src/knowledge/KnowledgePage.tsx` (memory search/detail/edit/delete), and shared API adapter modules under `src/api/`.

## Architecture mapping and call chains

### Workbench

`App` renders `WorkbenchPage` and receives unsaved/navigation-block callbacks (`App.tsx:114`). `WorkbenchPage` owns local feature state for source/DDL, preview, `JobRecord`, event stage, answers, chat transcript, busy/error state (`WorkbenchPage.tsx:116-140`).

DDL flow: `handlePreview` validates local source/DDL then calls `previewDDL` (`WorkbenchPage.tsx:279-287`) -> `apiRequest("/api/v1/metadata/ddl-preview")` (`api/dataAgent.ts:204-209`) -> HTTP JSON adapter (`api/client.ts:48-108`). Submit flow persists a client coordinate in `sessionStorage`, updates `/workbench/:submissionId`, calls `submitDDL` (`WorkbenchPage.tsx:290-342`) -> health capability probe + POST `/api/v1/metadata/ddl-jobs` with conditional `Idempotency-Key` and legacy uncertainty handling (`api/dataAgent.ts:143-157,211-269`).

Job flow: accepted job is installed locally, then `watchJob` calls `connectJobEvents` with `getAuthoritativeJob: () => getJob(jobId)` (`WorkbenchPage.tsx:192-202`). SSE adapter opens `EventSource(resolveApiUrl(eventsUrl))`, validates event payloads, and falls back to authoritative GET polling/retry on interruption (`api/jobEvents.ts:72-90,95-141,143-189`). `acceptEvent` deliberately clears waiting-input coordinates until authoritative GET (`WorkbenchPage.tsx:176-190`); `acceptJob` ignores mismatched job IDs and closes on terminal status (`:169-174`).

Chat flow: `sendChatAttempt` owns `localStorage` user ID and `sessionStorage` conversation UID, creates conversation, posts `/api/v1/conversations/:uid/chat-turns`, retries once after 404 conversation loss (`WorkbenchPage.tsx:390-439`; adapters `api/dataAgent.ts:291-312`).

### Knowledge

`KnowledgePage` owns URL-initialized source/query and local result/selection/history/editing/busy/error state (`knowledge/KnowledgePage.tsx:11-23`). Search calls `searchMemories` and writes `source/query` via `history.replaceState` (`:43-50`); selecting memory calls parallel `getMemory` + `getMemoryHistory` and writes `memory` URL param (`:26-41`). Save sends optimistic-versioned `PATCH` via `updateMemory`, reloads authoritative detail, and reports `requires_reprocess` without mutating current snapshot (`:53-68`; adapter `api/dataAgent.ts:329-338`). Delete calls versioned `DELETE` and only updates local list after success (`:70-77`; adapter `:340-347`).

## State ownership / seam assessment

Confirmed ownership matches feature-first guidance: app shell owns URL/view and navigation guards; Workbench owns feature state and ephemeral refs; Knowledge owns memory UI state; `src/api` owns HTTP/SSE transport, response validation, timeout/error mapping, and URL resolution. `apiRequest` is a clear transport seam (`api/client.ts:7-10,48-108`); `dataAgent.ts` is the REST adapter; `jobEvents.ts` is the SSE/authoritative-read adapter.

- URL authority: `App` owns top-level view/path (`App.tsx:12-31,77-92`); feature pages update only their own query/job path (`KnowledgePage.tsx:31,48`; `WorkbenchPage.tsx:221-222,314-342`).
- React state/ref authority: all display and in-flight coordination is local to owning feature (`WorkbenchPage.tsx:116-140`; `KnowledgePage.tsx:13-23`). Refs are used for subscriptions/current job/controller and do not become a second backend source of truth.
- Storage authority: browser storage stores recovery coordinates only (`schema-loom-pending-submission`, conversation UID, user ID; `WorkbenchPage.tsx:397-414` and `:107-110`), while job/memory truth is re-read from REST (`getJob`, `getMemory`, history). This is appropriate persistence scope, but storage values are client hints and can become stale.
- Backend authority: job status is explicitly authoritative through `getAuthoritativeJob` before waiting-input controls and on stream interruption (`jobEvents.ts:60-66,95-134,143-149`); memory edits use `record_version` expected-version concurrency (`KnowledgePage.tsx:60`; `dataAgent.ts:329-338`).

## HTTP/SSE contract and deployment

- API base is `VITE_API_BASE_URL` with same-origin fallback; `/api` is de-duplicated when base already ends in `/api` (`api/client.ts:1,39-46`). All responses are runtime-validated and malformed success payloads become `ApiError(502, invalid_response)` (`:76-97`).
- REST contract coverage includes metadata preview/jobs/answers, conversations/chat turns, and memory search/detail/history/mutation (`api/dataAgent.ts:204-347`). Types and validators encode status/revision/attempt/question/result/error envelopes (`dataAgent.ts:16-201`).
- SSE contract enumerates event types and validates `JobEventData` status/stage/revision/attempt/timestamps (`jobEvents.ts:4-20,49-58`). EventSource reconnect preserves browser Last-Event-ID; GET remains authority (`:143-165`).
- Nginx serves `dist`, proxies `/api/` to `127.0.0.1:8000/api/`, disables buffering/cache, and SPA-falls back to `index.html` (`deploy/nginx.conf:1-14`). Caddy equivalent proxies `/api/*` to FastAPI and serves SPA fallback (`deploy/Caddyfile:1-11`).

## Findings (module/interface/seam/depth/locality)

### Conforms

1. Deep transport seam: UI does not call `fetch` directly; REST calls are centralized behind `apiRequest` + `dataAgent` validators, and SSE is isolated in `jobEvents`.
2. Locality is strong: Workbench and Knowledge do not import each other; App composes features only. Shared API contains no page orchestration.
3. Backend authority is explicit for asynchronous jobs and optimistic memory versions; event payloads are treated as hints until authoritative GET where required.
4. Legacy compatibility is isolated in `submitDDL` (health capability probe, conditional idempotency header, no body field for old backends) rather than spread through components (`dataAgent.ts:224-268`).

### Risks / deviations

- Routing is hand-rolled in `App.tsx` and feature components mutate `window.history` directly. This is acceptable at current two-route scale, but URL ownership is split between shell path state and feature query/path mutation; adding routes could make the seam shallow and error-prone (`App.tsx:47-92`; `KnowledgePage.tsx:31,48`; `WorkbenchPage.tsx:221-222,317,340`).
- `KnowledgePage` captures `const params = new URLSearchParams(window.location.search)` during render and intentionally reads only on entry (`:12,36-41`). Browser back/forward query changes while mounted will not rehydrate local state; this is an ownership/locality gap if history navigation is expected to update the page.
- Browser storage is used for user identity and conversation continuity (`localStorage`/`sessionStorage`) without a shared storage adapter. It is currently feature-local, but key/schema drift would be hard to detect (`WorkbenchPage.tsx:397-414`).
- No explicit generated OpenAPI/JSON Schema source is present under `frontend`; runtime validators in `dataAgent.ts` are the client-side contract authority. Verify backend contract files separately before treating these validators as canonical.

## FastAPI / legacy data-flow boundary

The frontend only targets `/api/v1/...` paths and same-origin reverse proxies. Legacy behavior is detected through `/api/v1/health` 404/capabilities and handled in the client adapter (`dataAgent.ts:143-157,224-268`); no imports from `src/data_agent` or `src/data_agent/frontend` were found in the frontend source. Deployment proxies terminate at FastAPI `127.0.0.1:8000` (`deploy/nginx.conf:5-10`; `deploy/Caddyfile:4-6`).
