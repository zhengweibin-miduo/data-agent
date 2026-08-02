# Frontend architecture reconnaissance

## Observed structure

- `frontend/src` matches the documented feature-first layout: `api/`, `knowledge/`, and `workbench/`, with `App.tsx`/`main.tsx` at the application shell. This is the exact structure specified in `.trellis/spec/frontend/directory-structure.md:5-16`.
- Transport is centralized in `frontend/src/api`: `client.ts` exposes `resolveApiUrl` and `apiRequest` (`frontend/src/api/client.ts:39-113`), `dataAgent.ts` calls only `apiRequest` for health, DDL, chat, and memory endpoints (`frontend/src/api/dataAgent.ts:145-347`), and `jobEvents.ts` resolves SSE URLs through `resolveApiUrl` (`frontend/src/api/jobEvents.ts:1,156-163`). No feature component uses `fetch`, hard-coded origins, or direct `EventSource` construction.
- `App.tsx` is the navigation/application shell: it imports the two feature pages (`frontend/src/App.tsx:3-4`), derives the active view from `window.location.pathname` (`:6-10`), and renders pages at `:101-115`.

## Cross-feature and legacy checks

- No feature-to-feature internal imports were found. The only feature imports are `App.tsx -> knowledge/KnowledgePage` and `App.tsx -> workbench/WorkbenchPage`; feature files import only `../api/*` or local modules.
- No imports from `src/data_agent/frontend/` occur anywhere under `frontend/src`. The only references are backend migration configuration/tests and documentation. This satisfies the migration-only legacy rule.

## State ownership evidence / possible concentration

- URL state is read and written by both the shell and feature pages: `App.tsx` mirrors pathname into React `view`/`routePath` (`:12-24`) while `WorkbenchPage` parses `/workbench/{job_id}` (`frontend/src/workbench/WorkbenchPage.tsx:91-116`) and updates history on submission (`:314-340`); `KnowledgePage` parses and writes `source`, `query`, and `memory` query parameters (`frontend/src/knowledge/KnowledgePage.tsx:11-13,31-48`). This is consistent with URL-as-owner, but the shell/feature split is a maintenance seam.
- Browser persistence follows the state spec: `WorkbenchPage` stores pending submission/conversation coordinates in `sessionStorage` and a local user ID in `localStorage`; no DDL or model credentials are persisted.
- `WorkbenchPage` is a large orchestration module (521 lines) with 18 page-session state/ref holders (`frontend/src/workbench/WorkbenchPage.tsx:116-140`) and restore/submission/SSE/chat/answer handlers in one module (`:154-413`). Rendering concerns are extracted to `LineageCanvas.tsx` and `TraceDock.tsx`, but lifecycle orchestration remains centralized.
- `KnowledgePage` is comparatively compact (114 lines) and delegates API calls to `api/dataAgent.ts`.

## Backend contract projection

- `frontend/src/api/types.ts` is the sole checked client projection. Feature files consume these types rather than importing Python/ORM code.
- `apiRequest<T>` centralizes response/error handling and deadline projection; endpoint adapters pass typed generics, keeping the Pydantic/backend contract authoritative.

## Findings

1. No confirmed structural violation: directory layout, API/UI separation, cross-feature imports, URL construction, and legacy isolation match the checked-in frontend specs.
2. Main maintenance seam: URL ownership is authoritative but mirrored in `App` and feature pages, so route changes must keep shell and feature restoration aligned.
3. Main concentration: `WorkbenchPage.tsx` combines most workbench state and lifecycle/orchestration logic in one 521-line module; no dedicated hooks/state modules exist under `workbench/`.
