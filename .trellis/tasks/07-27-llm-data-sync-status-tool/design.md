# Design: LLM Data Readiness Gate

## Context and Dependency

The repository currently has typed structured-output LLM calls but no general
question-answer agent, tool registry, or user-facing answer orchestration.
This task therefore delivers a reusable internal module only.

Implementation depends on the final committed form of
`07-27-sync-conversation-metadata-dw`. That task owns `SyncPhase`,
`data_sync_task`, and `DataSyncRepository`; this task must consume those
contracts instead of copying them. The implementation branch must be rebased or
stacked on the dependency commit before coding starts.

## Module Boundary

Place the feature under a focused backend domain such as
`src/data_agent/answer_readiness/`:

- `models.py`: typed intent, dependency, and gate-result contracts.
- `classifier.py`: structured LLM intent recognition with one repair attempt.
- `repository.py` or an extension to `DataSyncRepository`: read-only readiness
  lookup.
- `tool.py`: LangChain-compatible async readiness tool.
- `service.py`: deterministic routing and the fixed not-ready response.

Do not add an HTTP route, conversation endpoint, agent loop, scheduler, or
second data-sync state model.

## Data Flow

1. A future answer caller supplies the current user question and a bounded
   catalog of accepted DW targets.
2. `AnswerReadinessClassifier` uses the existing managed LLM client and typed
   structured output to produce:
   - `requires_sync_completion: bool`
   - `dependencies: list[AnswerDataDependency]`
   - an internal bounded reason used only for diagnostics.
3. Deterministic validation requires:
   - `false` => no dependencies;
   - `true` => at least one dependency;
   - every target/source is a member of the supplied catalog;
   - no duplicates and bounded list sizes.
4. An invalid result receives one structured repair invocation. A second
   invalid result returns `INTENT_UNRESOLVED`; it never falls through to a
   business answer.
5. `requires_sync_completion=false` returns `PROCEED` without querying
   `data_sync`.
6. `true` invokes the readiness tool:
   - source specified => check that source/target task;
   - source omitted => check every effective task for that target;
   - multiple dependencies => all must be ready.
7. Only `SyncPhase.STREAMING` is ready. Missing, ambiguous, paused, conflict,
   dead, schema, buffering, backfill, or replay state is not ready.
8. Ready returns `PROCEED`. Not ready returns `DATA_PREPARING` plus the exact
   user-safe text `数据准备中，请稍后重试`.

## Contracts

```python
class AnswerDataDependency(ContractModel):
    target_table: str
    source: str | None = None

class AnswerReadinessIntent(ContractModel):
    requires_sync_completion: bool
    dependencies: list[AnswerDataDependency]
    reason: str

class AnswerGateDecision(StrEnum):
    PROCEED = "proceed"
    DATA_PREPARING = "data_preparing"
    INTENT_UNRESOLVED = "intent_unresolved"

class DataReadinessToolResult(ContractModel):
    ready: bool
```

The future caller owns the actual business answer. The gate never receives or
returns business rows.

## Read-Only Query Contract

The readiness query uses a fresh managed `AsyncSession` and plain `SELECT`.
It must not call task claiming, renew leases, settle phases, advance offsets,
or increment retries.

The LangChain tool result contains only `ready`. Internal structured logs may
record bounded task IDs, target names, and phase codes for operations, but those
details are not returned to the LLM or user.

## Multiple Sources

- Source-scoped question: the dependency includes `source`; only that task is
  required.
- Source-unscoped or aggregate question: `source=None`; every effective task
  targeting that DW table must be `streaming`.
- No matching task is fail-closed (`ready=false`).

## Safety and Compatibility

- Credentials, connection URLs, leases, desired JSON, row payloads, Binlog
  coordinates, retry counts, errors, and progress never enter tool output.
- Existing DDL, conversation, Meta, and data-sync APIs are unchanged.
- No precise progress percentage is calculated or exposed.
- Tool and intent contracts are reusable by a future answer entrypoint without
  requiring that entrypoint in this task.

## Rollout and Rollback

The module is inert until a future caller invokes it. Rollback removes the
module and its tests; it does not alter `meta`, `dw`, or `data_sync` schemas or
state.
