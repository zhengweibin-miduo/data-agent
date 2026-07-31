# Design: V1 peer-scoped value indexing and byte-bounded reads

## Context and boundary

This change keeps MySQL as the authority and Elasticsearch as a rebuildable projection. It does not introduce a migration framework or mutate an initialized environment. The implementation changes only the fresh V1 contract and the resumable value-refresh behavior.

## 1. Peer ownership contract

Each logical Meta column keeps its own `(table_id, column_id)` projection even when multiple physical sources share the same DW target and physical column name.

Eligibility is target-wide: a shared physical name is usable only when every peer column with that name is eligible. Unlike the current contract, more than one eligible peer ID is allowed.

The value source remains unambiguous:

1. A projection plan identifies one `DesiredSyncTable.source`.
2. SCAN first reads a bounded ordered page of DW primary keys and value byte lengths.
3. It calculates each primary-key hash with `primary_key_identity()`.
4. It batch-loads active `data_sync_key_owner` rows for the target and retains only hashes owned by the plan source.
5. It fetches complete values only for the retained primary keys.

The owner lookup and cursor update run in the same short transaction as frequency changes. The cursor advances across rows owned by another source and values that deterministically exceed the supported value budget, so retries converge.

CDC and backfill updates already know their `DesiredSyncTable.source`. `prepare_frequency_mutation()` returns only peer states whose plan source matches that source; a row event cannot update another peer's logical column frequencies.

No new DW provenance column or relational schema migration is needed.

## 2. Byte-bounded SCAN

`scan_rows()` becomes a two-stage operation:

- Preflight query: primary keys plus `OCTET_LENGTH(value)` only, ordered by the existing keyset cursor and row-count limited.
- Ownership query: small primary-key hashes only.
- Prefix selection: advance through the preflight rows in order, skip foreign-owner and individually oversized values, and stop before the first accepted value that would exceed the claim's 4 MiB read budget.
- Payload query: fetch primary keys plus the complete value for only that selected prefix.

The returned result separates `payload_rows` from `last_scanned_key`. `_scan()` applies frequencies for payload rows and persists the cursor using `last_scanned_key`. It never truncates a value.

## 3. Byte-bounded SELECT_TOP_N

Top-N ranking uses keyset order `(frequency DESC, value_hash ASC)`. A V1 cursor is stored in the existing `metadata_index_outbox.bulk_cursor` JSON:

```json
{
  "v": 1,
  "phase": "select_top_n",
  "desired_version": "<hash>",
  "frequency_version": "<hash>",
  "index_generation": "<hash>",
  "column_id": "<id>",
  "last_frequency": 42,
  "last_value_hash": "<hash>",
  "ranked_count": 100
}
```

Each claim preflights a small ranked page using hash, frequency, and `OCTET_LENGTH(value_text)`. It chooses an ordered prefix under the same 4 MiB budget, fetches complete text only for accepted hashes, and upserts membership. Individually oversized values count toward Top-N rank but are not fetched or published, matching the existing deterministic unindexable-value behavior.

When `ranked_count == value_top_n` or no later row exists, the field is complete and the cursor is cleared. Otherwise the same column and cursor are persisted for the next claim. Desired/frequency/generation mismatches invalidate the cursor and restart that field safely.

## 4. V1-only version policy

The following configured project protocol/content versions become `v1`:

- `llm.prompt_version` (already V1)
- `llm.graph_version`
- `memory.content_version`
- `memory.ddl_semantic_content_version`
- `memory.projection_version`
- `metadata_index.projection_version` (already V1)

The cursor envelope remains numeric `v: 1`. Content hashes and refresh generations remain 64-character hashes because they identify changing authority, not historical schema versions. The old-environment SQL upgrade file is removed; fresh bootstrap SQL and SQLAlchemy Core definitions remain canonical.

## 5. Failure and recovery

- A crash before commit rolls back frequency, publication membership, and cursor together.
- A crash after commit resumes strictly after the persisted keyset cursor.
- Missing/mismatched owner rows cause the row to be excluded from that peer, never copied to every peer.
- A single value larger than the supported payload budget is skipped without fetching or truncation; cursor progress prevents retry loops.
- A cursor with a mismatched V1 identity is ignored/restarted or rejected according to the existing phase authority rules.

## 6. Compatibility and migration

There is intentionally no compatibility migration. Existing databases, indices, volumes, and remote PR state are not mutated by this task. If an environment later needs preservation, migration requires a separate explicit user request and design.

