# Research: adapting mem0ai/mem0 to Data Agent

- Repository: [`mem0ai/mem0`](https://github.com/mem0ai/mem0)
- Inspected commit: `ddaa655edf41e3ed375b263fb227da0bcd42ccb9d`
- Date: 2026-07-19
- Scope: open-source memory extraction, scoping, retrieval, history, and vector-store architecture

## Confirmed Mem0 mechanisms

The current open-source `Memory.add()` accepts messages and requires at least
one identity scope from `user_id`, `agent_id`, or `run_id`. With inference
enabled, its current V3 pipeline:

1. loads recent messages for the identity scope;
2. retrieves nearby existing memories from the vector store;
3. makes one LLM call to extract evidence-bound, self-contained facts;
4. emits ADD-only facts with optional links to existing memories;
5. embeds the extracted texts;
6. removes exact hash duplicates;
7. batch-inserts vectors and payloads;
8. records ADD history and links entities.

`Memory.search()` requires an identity filter, embeds the query, applies
metadata filters, retrieves top-k results above a threshold, and optionally
reranks or explains scores. The open-source implementation supports Qdrant
through the vector-store abstraction.

Explicit `update()` and `delete()` operations remain available and append
UPDATE/DELETE history, but automatic V3 extraction is ADD-only. The source also
defines semantic, episodic, and procedural memory concepts, while procedural
memory has an explicit creation path.

## Data Agent adaptation constraints

- The project will not import or execute the `mem0ai` SDK.
- Data Agent already receives typed semantic decisions, questions, answers,
  and metrics from its LangGraph workflow. Accepted structured results, not
  raw unbounded conversation, are the only eligible extraction input.
- DDL AST and deterministic validation remain authoritative. Semantic recall
  can propose candidates but cannot override physical identifiers or current
  validation.
- Existing `source` naturally scopes application memory across jobs. `job_id`
  remains LangGraph checkpoint identity and must not become long-term scope.
- Qdrant and TEI adapters already exist but are not initialized in API or
  worker paths. Using Mem0-style semantic retrieval requires integrating their
  lifecycles and defining consistency/rebuild behavior.
- Meta snapshot and authoritative MySQL memory must retain one-transaction
  consistency. A vector index cannot participate in that MySQL transaction, so
  it must be a rebuildable projection driven through an outbox or equivalent
  retryable synchronization mechanism.

## Initial mapping

| Mem0 concept | Data Agent candidate mapping |
|---|---|
| `user_id` / identity filter | logical DDL `source` |
| `run_id` | `job_id`, checkpoint-only provenance rather than recall scope |
| extracted factual memory | validated semantic decision, user answer, metric definition |
| recent messages | current accepted graph facts and explicit answers |
| vector-store payload | source, kind, object IDs, fingerprints, trust, versions |
| ADD-only extraction | append accepted facts; link or supersede without destructive rewrite |
| history | immutable memory event/history record |
| semantic search | Qdrant + TEI candidate recall |
| metadata filtering | source, kind, object IDs, status, trust, version |
| entity linking | table/column/metric identities and typed memory links |

## Open design boundary

The largest first-release decision is whether Mem0-style semantic recall enters
the main workflow immediately or whether the first change only reshapes the
authoritative memory and history contracts. Without semantic recall, the
result would adopt only part of Mem0's architecture and would leave the
currently provisioned Qdrant/TEI path unused.
