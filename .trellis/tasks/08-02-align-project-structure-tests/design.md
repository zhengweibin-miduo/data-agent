# 项目结构与测试重构父任务设计

## Purpose

本父任务把一次大范围架构重构拆成四个可独立规划、实现、检查和回滚的纵向交付物。父任务不直接修改业务源码；它维护统一语言、context map、子任务接口约束、依赖顺序和最终集成验收。

## Target Architecture

### Context ownership

- DDL Metadata owns accepted Meta Snapshot and Meta Projection.
- Long-term Memory owns authoritative memories, lifecycle/history, and Memory Projection.
- Conversation owns conversations, messages, turns, and extraction requests.
- Data Sync owns desired synchronization tasks, DW materialization, CDC progress, and readiness.
- Frontend features communicate with backend only through HTTP/SSE contracts and client projections.

### Dependency rules

- Domain modules depend only on domain values and deterministic policies.
- Application modules define use-case interfaces and driven ports; they do not import concrete infrastructure clients, SQLAlchemy tables, or another context's repository implementation.
- Adapters implement ports and translate HTTP/SSE, persistence, queues, indexes, and external SDK payloads.
- Infrastructure modules own resource construction and lifecycle only.
- Composition roots select adapters and configuration values.
- Cross-context collaboration uses identifiers, application ports, or projection events. No context imports another context's persistence tables or repositories.

### Required seams

| Seam | Stable observable contract | Owner |
|---|---|---|
| DDL Job lifecycle | HTTP submit/read/answer/SSE, idempotency, revision and terminal state | DDL Metadata |
| Accepted snapshot publication | Atomic accepted snapshot or full rollback; generation lock covers commit | DDL Metadata |
| Conversation/Memory | Turn lifecycle, recall, extraction proposal, user-data deletion | Conversation and Long-term Memory application interfaces |
| Data Sync lifecycle | One bounded dispatch step changes durable task/DW state | Data Sync |
| Meta Projection | Desired projection converges or retries; search revalidates authoritative Meta | DDL Metadata |
| Frontend transport/feature | HTTP/SSE adapters project stable outcomes; pages expose user-visible behavior | Frontend API and owning feature |

## Child Tasks

### Child 1: Memory / Conversation boundary

- Define transaction, repository, memory recall/mutation, and configuration ports at the application seam.
- Keep MySQL authoritative and Memory Projection rebuildable.
- Replace tests that mock internal collaborators with tests through Conversation/Memory application interfaces.
- Independent of the other children.

### Child 2: Accepted snapshot / Meta Projection boundary

- Treat `metadata_indexing` as DDL Metadata's Meta Projection implementation.
- Separate pure desired-state policy from persistence and external index adapters.
- Move accepted snapshot cross-context coordination to an explicit publication seam without losing the single MySQL transaction invariant.
- Establish the stable projection input interface consumed by Child 3.

### Child 3: Data Sync ports

- Define application ports for task repository, source reader, DW writer/schema adapter, clock/lease, and Meta Projection notification/input.
- Remove `data_sync.backfill -> metadata_indexing implementation` imports.
- Preserve desired-state, coordinate, lease, backfill/replay/streaming and readiness behavior.
- Depends on Child 2's reviewed projection interface. Implement and integrate only after that interface is available.

### Child 4: Workbench modules

- Keep `WorkbenchPage` as the feature entry while extracting internal restore, submission, job subscription, clarification and chat modules/hooks where they create real internal seams.
- Keep URL/session/backend authority rules and HTTP/SSE contracts unchanged.
- Replace direct callback driving and repeated setup with adapter and feature-interface tests.
- Independent of backend children unless a reviewed backend contract explicitly changes.

## Compatibility and Migration

- This project has no required legacy database/vector-index/history migration. Do not add data migration, rollback schema, or cleanup paths unless the user explicitly authorizes data migration.
- Preserve HTTP route metadata, Pydantic payloads, Redis keys/Lua behavior, MySQL schemas, LangGraph node/state names, configuration keys, log event names, and frontend storage recovery contracts unless a child PRD explicitly changes one.
- Preserve `src/data_agent/frontend/` as read-only, opt-in legacy compatibility assets.
- Package moves are hard internal migrations: update active code, tests and current specs; do not add compatibility modules for retired internal paths.

## Test Strategy

- Each child uses vertical red-green slices at its approved public seam.
- After a deep module interface covers an observable behavior, delete old tests that only exercise private helpers or internal collaborator calls.
- Keep call-count/order assertions only when the count/order is itself an externally required budget, idempotency or transaction contract.
- Run child-focused checks after every slice and the full repository gates during parent integration.

## Rollback Shape

- Each child is independently revertible before integration.
- Child 3 must not merge ahead of the Child 2 projection interface it consumes.
- If a new interface cannot preserve a required invariant, revert that child rather than adding a parallel compatibility layer.
- Parent integration stops on contract drift, unknown history, failed verification, or cross-child file overlap not described here.
