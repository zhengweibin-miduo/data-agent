# Data Sync ports verification

Date: 2026-08-02

## Acceptance evidence

| Acceptance criterion | Evidence | Result |
|---|---|---|
| Application does not create MySQL Sessions or import concrete source/schema/projection implementations | `tests/unit/data_sync/application/test_package_boundary.py`; AST scan covers every application module | Pass |
| `data_sync -> metadata_indexing` implementation imports are zero | Package-boundary test and repository search; retired root `data_sync/service.py` is absent | Pass |
| `dispatch_once` works with in-memory adapters and proves phases, coordinates, DW intent, lease, and error behavior | `tests/unit/data_sync/application/test_service.py`; 12 public-seam cases | Pass |
| Private lifecycle/call-order tests are removed without losing required behavior | Retired `tests/unit/data_sync/test_service.py` deleted; search found no test calls to `_process`, `_capture`, `_retry`, `_reschedule`, `_renew_lease`, `_with_lease_heartbeat`, or `_synchronize_schema` | Pass |
| Production adapter contracts preserve transaction, lock, projection, and cancellation behavior | `tests/unit/data_sync/adapters/test_mysql.py`; 3 adapter-contract cases | Pass |
| CDC and DW convergence remain live | `tests/integration/data_sync` plus `tests/integration/answer_readiness`: 4 passed | Pass |
| Meta Projection transaction participant remains live | `tests/integration/ddl_metadata/test_meta_projection_resumable_refresh.py`: 1 passed | Pass |
| No schema or historical-data migration | No bootstrap, migration, schema, or cleanup-path files changed | Pass |

## TDD evidence

The application slices were observed red before green:

- missing `data_sync.application` failed test collection before the tracer module existed;
- streaming backlog, capture persistence, saturated backfill, schema contention,
  generation baseline, replay completion, and five failure classifications each
  failed on their required durable outcome before the corresponding implementation
  was added;
- the final public-seam module passes 12 cases without invoking private lifecycle
  methods.

## Fresh successful commands

```text
uv lock --check
  Resolved 109 packages

uv run ruff check src tests
  All checks passed

uv run pyright src tests
  0 errors, 0 warnings, 0 informations

uv run python -m compileall -q src tests
  exit 0

uv run python -m data_agent.settings
  exit 0

uv build
  Successfully built sdist and wheel

git diff --check
  exit 0

uv run pytest -m "not integration" -q
  392 passed, 27 deselected

uv run pytest tests/integration/data_sync tests/integration/answer_readiness -q
  4 passed

uv run pytest tests/integration/ddl_metadata/test_meta_projection_resumable_refresh.py -q
  1 passed
```

## Full-suite environment result

`uv run pytest -m "not tei" -q` completed with `416 passed`, `1 deselected`,
and `2 failed`. Both failures are outside the Data Sync change and have the same
external-state cause: the shared local MySQL `metric_info` table lacks the current
`fact_table_id` column, producing MySQL error 1054 in accepted-snapshot metric
writes. The failing scenarios are:

- `tests/integration/persistence/test_metadata_repository.py::test_snapshot_expires_removed_column_and_metric_memories`
- `tests/integration/test_ddl_metadata_flow.py::test_ddl_metadata_flow`

This task does not alter, reset, migrate, or reprovision the shared database.
The user did not authorize a database/history migration, and the task explicitly
forbids one. All Data Sync and Meta Projection scenarios in this task pass against
the same live environment.

## Independent review

A read-only independent agent reviewed the complete tracked/untracked change set
against `code_review.md` and reported: `未发现需要阻止合并的 P0/P1 问题。` The
main agent independently verified every cited invariant and did not rely on that
agent's unavailable pytest environment.
