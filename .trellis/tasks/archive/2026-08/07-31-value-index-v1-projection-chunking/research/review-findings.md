# PR #71 unresolved review findings

## Shared-target peer projection

- `src/data_agent/metadata_indexing/projections.py:103-112` only accepts a physical name when exactly one peer column ID owns it. `MetadataProjectionRepository._shared_target_eligible_columns()` therefore excludes every same-name column shared by two eligible sources.
- `ValueProjectionPlan.desired` already identifies `source`, `target_table`, primary-key columns, and schema fingerprint. Frequency and Elasticsearch identities already contain the logical `table_id` and `column_id`.
- `src/data_agent/data_sync/models.py:140-163` exposes `primary_key_identity()`, the same canonical primary-key document/hash used by `data_sync_key_owner`.
- `data_sync_key_owner` records `(target_table, primary_key_hash) -> source`. A bounded DW keyset batch can recompute the hash from its primary-key values and filter payload reads to the plan's source without adding a DW migration column.
- `prepare_frequency_mutation()` currently discovers every peer VALUES state, and `apply_frequency_row_changes()` applies one source event to every returned state. The selected states must be restricted to the DML source.

## Long-value memory boundary

- `MetadataValueFrequencyRepository.scan_rows()` selects primary keys plus the complete value, limits only row count, and materializes the whole result.
- `materialize_top_n()` selects up to 10,000 complete `LONGTEXT` values and materializes them before applying the Elasticsearch document-size gate.
- Publication already uses a safe two-stage pattern: select IDs plus `OCTET_LENGTH`, choose a prefix under `_ACTION_PAYLOAD_BYTE_LIMIT`, then fetch full payloads.
- SCAN already has a durable primary-key cursor. SELECT_TOP_N needs a V1 field-internal cursor stored in the existing JSON `bulk_cursor` so each claim handles one byte-bounded keyset page.

## Version classification

- `metadata_index.projection_version` and the value-scan cursor schema are already V1.
- `desired_version`, `frequency_version`, and `index_generation` are SHA-256 identities for desired content, frequency baselines, and refresh generations. They are not release/schema versions and must remain hashes.
- `llm.graph_version`, `memory.content_version`, `memory.ddl_semantic_content_version`, and `memory.projection_version` are project protocol/content versions and can be reset to `v1` because the user confirmed there is no data to preserve.
- `docs/docker/mysql/upgrades/20260730_metadata_semantic_value_index.sql` is only for upgrading an existing environment. The canonical bootstrap SQL already owns a fresh schema, so the upgrade script is out of scope for a V1-only fresh start.

