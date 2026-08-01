# Frontend API and Deployment Contract

## Scenario: Independent frontend and API-only backend

### 1. Scope / Trigger

Apply this contract whenever frontend code, FastAPI composition, CORS, SSE, API
base resolution, static hosting, or the legacy embedded frontend changes.

### 2. Signatures

- Frontend environment: `VITE_API_BASE_URL=<empty|/api|absolute-origin>`.
- Backend migration environment: `ENABLE_LEGACY_FRONTEND=<boolean>`, default false.
- Liveness: `GET /api/v1/health -> {"status":"ok"}`.
- Business routes remain under `/api/v1/**`; DDL events remain
  `GET /api/v1/metadata/ddl-jobs/{job_id}/events`.

### 3. Contracts

- `resolveApiUrl` is the only deployment-origin join point. When base is `/api`,
  `/api/v1/...` must remain `/api/v1/...`, not `/api/api/v1/...`.
- CORS allows only configured frontend origins; credentials remain disabled.
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
