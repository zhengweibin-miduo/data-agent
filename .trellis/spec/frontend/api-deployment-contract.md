# Frontend API and Deployment Contract

## Scenario: Independent frontend and API-only backend

### 1. Scope / Trigger

Apply this contract whenever frontend code, FastAPI composition, CORS, SSE, API
base resolution, static hosting, or the legacy embedded frontend changes.

### 2. Signatures

- Frontend environment: `VITE_API_BASE_URL=<empty|/api|absolute-origin>`.
- Backend migration environment: `ENABLE_LEGACY_FRONTEND=<boolean>`, default false.
- Liveness: `GET /api/v1/health` returns `status: "ok"` and a capability map.
- Business routes remain under `/api/v1/**`; DDL events remain
  `GET /api/v1/metadata/ddl-jobs/{job_id}/events`.

### 3. Contracts

- `resolveApiUrl` is the only deployment-origin join point. When base is `/api`,
  `/api/v1/...` must remain `/api/v1/...`, not `/api/api/v1/...`.
- CORS allows only configured frontend origins; credentials remain disabled.
  Non-loopback origins require the explicit remote-origin deployment switch,
  while the API process itself remains bound to the loopback interface.
- SSE response remains `text/event-stream`, `Cache-Control: no-cache`, and
  `X-Accel-Buffering: no`.
- Native EventSource network failures keep the source open for browser reconnect
  and `Last-Event-ID`; the client reads authoritative `JobRecord` while waiting.
- An authoritative read requested while another read is in flight is queued, not
  dropped, so a reconnecting `waiting_input` event cannot lose its follow-up GET.
- The shared HTTP client applies a bounded default deadline, composes a caller's
  abort signal, and projects deadline expiry as the stable retryable
  `request_timeout` error. Long-running chat turns use an explicit budget that
  covers the server's sequential readiness, repair, generation, and retry calls.
- DDL submission carries a client-generated UUID that is also the server job ID.
  Send that coordinate in the optional `Idempotency-Key` header while keeping
  the JSON body compatible with backend versions that predate the coordinate.
  First read the simple health endpoint's capability map and omit the custom
  header unless the backend advertises support, because a legacy cross-origin
  deployment does not allow that header in its CORS preflight policy.
  A health endpoint 404 is the definitive legacy-backend signal; timeouts, 5xx,
  and network failures are inconclusive and must block submission rather than
  silently discard the recoverable coordinate.
  A successful health response must contain `status: "ok"` and either omit the
  legacy capability map or provide a boolean idempotency capability; malformed
  successful responses are inconclusive and must block submission.
  A timed-out acceptance request replays that coordinate only when the backend
  advertised idempotency support, and the server returns the original job only
  when its source and DDL match the first submission. A legacy submission must
  remain uncertain after any post-dispatch failure that does not prove rejection,
  including timeout, network failure, malformed success, or proxy/server error,
  rather than issuing a second non-idempotent POST. Only a deterministic,
  non-retryable 4xx may release the coordinate.
  Persist a legacy submission as non-replayable immediately before dispatching
  its POST, not only after its response fails, because document teardown may
  prevent promise rejection handlers from updating the recovery marker.
  Capability discovery shares the submission cancellation signal and must check
  cancellation again before the dispatch callback and POST, so an unmounted
  workbench cannot persist a non-replayable marker for a request never sent.
  Keep that coordinate outside the individual request call until acceptance is
  confirmed, including across repeated timeouts and SPA workbench remounts.
  Do not replace an unconfirmed coordinate when editable input changes; recover
  the original acceptance result before allowing a different submission.
  On workbench remount, reconcile a retained coordinate with the job GET before
  showing sample input. Release it only after an authoritative 404 or another
  deterministic pre-acceptance rejection such as `source_busy`.
  A retained unconfirmed coordinate takes precedence over a task ID left in the
  URL; only its authoritative 404 may fall back to restoring that older task.
  Persist the coordinate and acceptance start time in session storage before
  POST. During the bounded acceptance window, a restore-time 404 is provisional
  and must be retried because it can race the original atomic acceptance.
- A successful HTTP response with malformed JSON is a retryable
  `invalid_response` contract error. Never cast an empty fallback object to a
  success DTO or release an unconfirmed submission coordinate on parse failure.
- Production static hosting is independent from the Python wheel. A same-origin
  proxy should route `/api/` to FastAPI and disable SSE buffering/cache.
- Unauthenticated example proxies bind to `127.0.0.1` by default. Non-loopback
  exposure requires authentication and network access controls.
- Static-host SPA fallback must be isolated from `/api/**`; Caddy configurations
  use mutually exclusive `handle` blocks so `try_files` cannot rewrite API paths.

### 4. Validation & Error Matrix

- Missing legacy env -> API-only, `/`, `/workbench`, `/assets/**` return 404.
- Accepted true value -> mount legacy assets and log a deprecation warning.
- Ambiguous legacy value -> fail application construction with `ValueError`.
- Allowed CORS origin -> preflight 200 with matching allow-origin header.
- Unknown CORS origin -> preflight rejected and no allow-origin grant.
- EventSource unavailable/`stream_error`/malformed event -> bounded GET polling.
- Parse SSE payloads as `unknown` and validate the complete `JobEventData`
  shape before projection; a malformed payload on a named event follows the
  same bounded authoritative GET fallback as malformed generic events.
- Network `error` -> one authoritative GET; native EventSource reconnect remains active.

### 5. Good/Base/Bad Cases

- Good: static host serves `frontend/dist`, `/api/` proxies to FastAPI, SSE
  buffering is off, and `VITE_API_BASE_URL=/api`.
- Base: Vite on `127.0.0.1:5173` calls FastAPI on `127.0.0.1:8000`, with that
  exact origin listed in `api.cors_origins`.
- Bad: component code hardcodes `localhost`, the browser holds model secrets, or
  FastAPI reads Vite source/build files during default startup.

### 6. Tests Required

- Python: default 404 routes, OpenAPI business route, health, valid/invalid legacy
  env, allowed/rejected CORS, and existing SSE headers.
- TypeScript: API-base joining, stable error projection, authoritative
  waiting-input read, native reconnect behavior, polling fallback, chat turn UID
  reuse, DDL limits, and application navigation.
- Build: lint, strict typecheck, tests, and production Vite build.
- Successful JSON is not a trusted DTO merely because it parses. Validate the
  required fields at the endpoint boundary and project malformed shapes as the
  retryable `invalid_response` contract error before releasing recovery state.
  This includes authoritative `JobRecord` GET responses: an invalid shape must
  remain a failed read so `waiting_input` recovery keeps retrying.
  Validate `DDLPreview` and its nested table, column, relationship, and count
  fields before updating canvas state so an incompatible response cannot crash
  the workbench.
  Validate memory search items, nested memory details, ranking scores, signals,
  and degraded targets before rendering results so incompatible responses remain
  recoverable contract errors.
  Validate memory history pagination and every event's identifiers, type,
  contents, actor, and timestamp before rendering record details.
  Validate a memory detail before enabling correction or deletion controls.
  Validate chat messages and readiness decisions before clearing a turn retry
  coordinate or applying a clarification draft.
  Capability discovery must distinguish an explicit legacy response from a
  failed read: never downgrade to a non-idempotent submission after a transient
  health failure. Validate conversation creation and memory mutation responses
  before persisting identifiers or clearing reprocessing guidance.
  Validate clarification-answer `JobRecord` responses before replacing the
  authoritative question state or clearing locally entered answers.
- Normalize every non-success payload from `unknown` into a stable `ApiError`;
  malformed envelopes (including JSON `null`) must fall back to `http_<status>`
  so deterministic client failures still release the correct interaction gate.

### 7. Wrong vs Correct

#### Wrong

```ts
fetch("http://localhost:8000/api/v1/metadata/ddl-jobs");
source.onerror = () => source.close();
```

#### Correct

```ts
apiRequest("/api/v1/metadata/ddl-jobs");
source.onerror = () => readAuthoritativeJobWhileNativeReconnectContinues();
```
