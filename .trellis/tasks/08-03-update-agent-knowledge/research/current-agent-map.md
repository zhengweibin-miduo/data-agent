# Current Agent Map

## Confirmed flows

- Workbench submits DDL jobs and consumes their SSE stream; Chat is a separate long HTTP request through Conversation/Chat routes.
- Chat parses the current DDL deterministically, starts or replays an idempotent conversation turn, applies the answer-readiness gate, then calls the shared text model and atomically completes the turn.
- Readiness queries only Data Sync phase and heartbeat freshness. `STREAMING` with fresh heartbeat means ready, but Chat has no DW row-query or SQL tool.
- DDL jobs are atomically accepted into Redis with source lease and dispatch outbox, then best-effort enqueued; cron replay is the crash fallback.
- The DDL graph has 10 nodes and a sync checkpoint. Job statuses are pending, running, waiting_input, succeeded, rejected, failed.
- Accepted snapshot publication runs under generation locks in one MySQL transaction and writes Meta, Memory, Data Sync desired state, and Meta/Memory projection outboxes.
- A separate `data-agent-cdc` process advances Data Sync toward streaming. Meta Projection and Memory Projection remain rebuildable and non-authoritative.

## Confirmed drifts in the old page

- Missing Chat, Answer Readiness, Data Sync, Meta Projection, Frontend transport, DDL preview and CDC process.
- Old flat paths predate application/adapters separation.
- Old page calls LangGraph the whole project's only control flow and lists only three job states.
- Memory search now fuses exact/ES/Qdrant candidates; exact candidates receive priority rather than only filling empty slots.
- MemoryService exposes search/get/history/update/delete, not arbitrary add; accepted snapshots and conversation extraction own controlled candidate creation.
- API and Worker startup have different dependency probes; Meta indexes are worker-fatal while only non-structural Memory index failures may degrade.

## Primary evidence paths

- `backend/src/application.py`
- `backend/src/chat/service.py`
- `backend/src/answer_readiness/{classifier,service,tool}.py`
- `backend/src/ddl_metadata/{api,jobs,workflow,worker}/`
- `backend/src/ddl_metadata/adapters/mysql/accepted_snapshot.py`
- `backend/src/ddl_metadata/meta_projection/`
- `backend/src/data_sync/`
- `backend/src/memory/`
- `backend/src/conversation/`
- `frontend/src/api/` and `frontend/src/workbench/`
