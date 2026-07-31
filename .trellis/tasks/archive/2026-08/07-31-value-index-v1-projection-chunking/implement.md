# Implementation plan

1. Update peer eligibility and source scoping.
   - Allow same-name peer columns when every peer is eligible.
   - Restrict CDC/backfill frequency mutation states to the current source.
   - Add owner-hash batch lookup for SCAN.
2. Refactor SCAN into length preflight plus byte-bounded payload fetch.
   - Return a durable last-scanned primary key independently from fetched values.
   - Skip foreign-owner and individually oversized values without truncation.
3. Refactor SELECT_TOP_N into V1 keyset pages.
   - Add cursor validation against desired/frequency/generation/column identity.
   - Preflight lengths, fetch only a 4 MiB prefix, persist cursor, and finish one field across claims.
4. Reset configured project protocol/content versions to `v1`.
   - Update configuration assertions and current specs.
   - Remove the old-environment metadata-index upgrade SQL.
   - Preserve desired/frequency/generation hashes and normal stale-generation cleanup.
5. Add regression coverage.
   - Multiple eligible peers with the same physical column name remain visible independently.
   - SCAN filters rows by key ownership and CDC updates only the matching source.
   - SCAN preflight excludes full text, enforces byte-prefix behavior, advances across skipped values, and resumes by primary key.
   - SELECT_TOP_N resumes by stable rank cursor, never preloads an oversized value, and converges after restart.
   - Configuration versions are all V1.
6. Validate.
   - `uv run pytest tests/unit/metadata_indexing/test_runtime.py tests/unit/metadata_indexing/test_value_refresh.py tests/unit/metadata_indexing/test_outbox.py`
   - `uv run pytest tests/integration/test_metadata_index_resumable_refresh.py`
   - `uv lock --check`
   - `uv run ruff check .`
   - `uv run pyright`
   - `uv run python -m compileall -q src tests`
   - `uv run python -c "from data_agent.settings import app_config; print(app_config.metadata_index.projection_version)"`

## Risk and rollback points

- Primary-key normalization must use `primary_key_identity()` exactly; a duplicate implementation risks owner mismatches.
- Top-N cursor comparisons must preserve `frequency DESC, value_hash ASC` ordering.
- Do not persist a cursor past an accepted row whose frequency/publication update did not commit.
- Revert the local task commits to roll back; no environment data migration is performed.
