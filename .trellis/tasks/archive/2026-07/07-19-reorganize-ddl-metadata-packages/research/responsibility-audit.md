# Repository Responsibility Audit

## Audit Method

The audit covered every Python module under `src/data_agent/`, all tests,
runtime configuration, Docker bootstrap assets, the current backend specs, and
all active import references. Decisions use four labels:

- **Keep**: one cohesive owner; length alone does not justify a package.
- **Move**: cohesive code currently lives beside a different responsibility.
- **Split**: one module contains independently named responsibilities with
  different dependencies or change reasons.
- **Mirror**: tests/specs should follow the resulting production boundary.

## Root Application Modules

| Current module | Evidence | Decision |
|---|---|---|
| `main.py` | ASGI object and local Uvicorn entry only; 22 lines. | Keep. |
| `application.py` | FastAPI factory, application resource lifespan, and two global exception mappings are all composition-root concerns; 118 lines. | Keep, update imports only. |
| `settings.py` | Typed configuration schemas, cross-setting validation, YAML loader, and shared immutable instance all change with the configuration contract; no runtime resource lifecycle. | Keep as one configuration module. |
| `logging.py` | Loguru formatting, field normalization, and sink construction form one logging boundary. | Keep. |

## Shared Infrastructure

`infrastructure/{mysql,redis,checkpoint_store,llm_client,tei_embeddings,
elasticsearch,qdrant}.py` already follows one external resource per module.
Each adapter owns initialize/get/close lifecycle and reads the shared settings
contract. No module mixes feature orchestration or business persistence.

**Decision:** keep the flat infrastructure package. Adding one-file
subpackages would increase navigation depth without creating a stronger owner.

## DDL Metadata Core

| Current module/package | Evidence | Decision |
|---|---|---|
| `errors.py` | One stable feature error contract. | Keep. |
| `identifiers.py` | Deterministic identifiers for physical, semantic, and memory objects. | Keep. |
| `parsing.py` | Deterministic SQLGlot-to-physical-schema parsing only. | Keep. |
| `validation.py` | Deterministic semantic/metric validation and finalization only. | Keep. |
| `models/` | Typed contracts grouped by `physical`, `semantic`, `jobs`, and `memory`; the large memory file is still contract-only. | Keep. |
| `api.py` | Job routes and memory browser routes depend on different services and change independently. | Split into an `api/` package with job routes, memory routes, and an aggregate router. |

## Jobs

The current `jobs/` package places an application-facing `DDLJobStore` facade
beside Redis keys, canonical payload encoding, Lua scripts, atomic state,
dispatch/checkpoint outboxes, and source leases. Seven of nine implementation
files are Redis-specific.

| Current module | Responsibility | Decision |
|---|---|---|
| `ddl_job_store.py` | Application-facing state/dispatch/lease facade plus `question_set_id`. | Move facade to `jobs/store.py`; move the pure question-set identifier to `jobs/identifiers.py`. |
| `redis_base_store.py` | Narrow redis-py awaitable typing support. | Move to `jobs/redis/base.py`. |
| `job_keys_store.py` | Redis keyspace and member names. | Move to `jobs/redis/keys.py`. |
| `job_codec_store.py` | Redis hash/payload encoding and public projection. | Move to `jobs/redis/codec.py`. |
| `job_scripts_store.py` | Atomic Lua protocols. | Move to `jobs/redis/scripts.py`. |
| `redis_job_state_store.py` | Atomic Redis job-state persistence. | Move to `jobs/redis/state_store.py`. |
| `job_outbox_store.py` | Redis dispatch/checkpoint cleanup outboxes. | Move to `jobs/redis/outbox_store.py`. |
| `source_lease_store.py` | Redis source lease lifecycle. | Move to `jobs/redis/lease_store.py`. |

Redis keys, serialized field names, Lua bodies, arq job name, retention,
revision transitions, and public `DDLJobStore` behavior are compatibility
contracts and must remain byte/semantics compatible.

## Memory

The current `memory/` package mixes pure transformations, application
orchestration, MySQL persistence, and two search-engine adapters.

| Current module | Evidence | Decision |
|---|---|---|
| `payloads.py` | Pure canonical JSON, hashes, object references, and projection text. | Move to `memory/domain/payloads.py`. |
| `snapshots.py` | Pure candidate construction plus transaction-owning `MetadataSnapshotService`. | Split into `memory/domain/candidates.py` and `memory/application/snapshots.py`. |
| `context.py` | Context contract/selection plus MySQL/search application orchestration. | Move to `memory/application/context.py`; keep selection beside its only owner unless later reuse appears. |
| `service.py` | Browser-facing memory use cases and validation. | Move to `memory/application/service.py`. |
| `search.py` | Pure RRF plus MySQL/ES/Qdrant/TEI search orchestration. | Split into `memory/domain/ranking.py` and `memory/application/search.py`. |
| `indexes.py` | Independent Elasticsearch and Qdrant adapters in one file. | Split into `memory/indexing/elasticsearch.py` and `memory/indexing/qdrant.py`. |
| `outbox.py` | Runtime dispatch and operator-triggered rebuild are independent index synchronization use cases. | Split into `memory/indexing/dispatcher.py` and `memory/indexing/rebuilder.py`. |
| `persistence/memory_repository.py` | One 794-line class owns authoritative records, history, links, browser mutations, exact queries, index outbox claiming/retry, projection reads, and rebuild scans. | Move under `memory/mysql/` and split record persistence from index-outbox persistence while retaining one transaction-scoped composition facade where atomic calls require both. |
| memory definitions in `persistence/tables.py` | Four schema-qualified memory tables have a different owner and schema from the four default-schema Meta tables. | Move to `memory/mysql/tables.py`, sharing a central SQLAlchemy `MetaData` object so integration `create_all()` and cross-database transactions remain unchanged. |

The MySQL engine and Session remain shared through `infrastructure/mysql.py`;
moving tables does not introduce a second engine or transaction.

## Workflow

`workflow/graph.py` is 657 lines and contains state schema, dependency
protocols, ten node implementations, seven routing functions, and graph
assembly. These are separate reasons to change and have different test seams.

**Decision:**

- `workflow/state.py`: `DDLGraphState`.
- `workflow/contracts.py`: dependency protocols and `DDLGraphDependencies`.
- `workflow/nodes.py`: dependency-bound node implementations.
- `workflow/routing.py`: pure conditional-edge routing.
- `workflow/graph.py`: graph registration and compilation only.
- `workflow/metadata_generator.py`: keep the `MetadataGenerator` protocol in
  `contracts.py`; move the LLM implementation to
  `workflow/llm_metadata_generator.py`.

Node names, edge topology, interrupt payloads, state keys, graph version,
checkpoint thread IDs, durability, and replay behavior remain unchanged.

## Worker

`worker.py` is 611 lines and combines job recovery/execution, checkpoint
projection, retry policy, scheduled maintenance, resource lifecycle, graph
composition, and arq discovery settings.

**Decision:**

- `worker/job_runner.py`: job execution, snapshot projection, retries, and
  execution outcome logging.
- `worker/maintenance.py`: dispatch outbox, waiting expiry, checkpoint cleanup,
  and memory-index outbox dispatch.
- `worker/lifecycle.py`: dependency initialization, graph composition, and
  reverse-order shutdown.
- `worker/settings.py`: arq `WorkerSettings` registration only.

The executable arq import path will be migrated to
`data_agent.ddl_metadata.worker.settings.WorkerSettings` wherever referenced.
Function names, schedules, timeouts, retry limits, `keep_result=0`, and log
event names remain unchanged.

## Persistence Outside Memory

`MetadataRepository` is cohesive around the four Meta snapshot tables. The
Meta tables are cohesive and use the default database. They remain under
`ddl_metadata/persistence/`; only memory-owned tables and repository code move
out. A small shared `persistence/schema.py` may own the single SQLAlchemy
`MetaData` object imported by both table modules.

## Tests and Non-Python Assets

- Unit tests should mirror new packages when a current file tests multiple new
  owners: job Redis primitives, memory domain behavior, workflow routing/nodes,
  and worker helpers.
- Existing integration scenarios may remain scenario-oriented even when they
  span packages; their imports must use final paths.
- `tests/helpers/factories.py` and `tests/helpers/fakes.py` are test-support
  collections shared across scenarios; they remain cohesive enough to keep.
- `conf/app_config.yaml` and `docs/docker/` already have clear ownership.
  No directory change is justified, but runtime command/import references and
  schema contracts must be audited after the migration.
- Current Trellis backend specs must be updated. Archived tasks and journals
  remain historical and are excluded from stale-path cleanup.

## Why This Remains One Task

Although the audit identifies several packages, the changes share one hard
internal import migration and one set of cross-layer compatibility contracts.
Splitting them into child tasks would temporarily require compatibility shims
or leave mutually inconsistent import paths and specs. The implementation will
therefore use ordered phases and rollback points inside one task.
