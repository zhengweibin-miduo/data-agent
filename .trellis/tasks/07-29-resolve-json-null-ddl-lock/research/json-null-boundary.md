# JSON Null Boundary Research

## Locked dependency

- `uv.lock:913-924` resolves `mysql-replication==1.0.16`.
- Installed dependency
  `pymysqlreplication/row_event.py:172-240` reads the ROW-event null bitmap.
- `row_event.py:259-261` returns Python `None` when the null bitmap marks SQL
  `NULL`.
- `row_event.py:362-366` calls `packet.read_binary_json()` for a non-null JSON
  column; JSON literal `null` also returns Python `None`.
- `row_event.py:613-621` builds `none_sources`, but both paths retain the same
  default `"null"` source and cannot be distinguished by the public row payload.

## Application boundary

- `src/data_agent/data_sync/binlog.py:265-324` first accesses lazy event rows,
  then encodes every `None` from a JSON column as JSON literal `null`.
- `src/data_agent/data_sync/models.py:166-175` intentionally keeps ordinary
  `None` as SQL `NULL` and uses `{"$json":"null"}` only when
  `json_value=True`.
- `src/data_agent/data_sync/backfill.py:201-209` already preserves SQL `NULL`
  on the non-Binlog path.

## Decision

The distinction must be captured before the dependency discards the null bitmap.
Use an event-instance compatibility adapter around the locked private decoder,
not post-hoc inference, dependency vendoring or a durable schema change.

## Required evidence

- Unit regression for dependency signature and both null values.
- Live MySQL capture and DW replay proving `IS NULL` semantics.
