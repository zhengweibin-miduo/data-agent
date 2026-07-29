# Generation Lock Boundary Research

## Current race

- `ddl_metadata/persistence/snapshots.py:92-118` commits accepted Meta,
  desired-state and memory work in one managed Session.
- `data_sync/repository.py:75-86,130-187` replaces the desired hash and resets
  task generation state.
- `data_sync/service.py:253-271` opens a DDL Session but delegates authority
  checking to a second managed Session.
- `data_sync/schema_sync.py:101-105` checks authority and then executes
  auto-commit MySQL DDL.
- `data_sync/repository.py:843-851` predicates authority on task ID, desired
  hash, lease token and database-clock lease expiry.

The race is:

```text
old authority check succeeds
  -> new desired generation commits
  -> old MySQL DDL auto-commits
```

The later authority check or settlement cannot undo the DDL.

## Existing lock

`data_sync/schema_sync.py:80-118` uses a per-target DW schema `GET_LOCK`, but the
snapshot publisher does not acquire it and authority checking happens on another
Session. It serializes DDL workers, not generation publication.

## Decision

Add a distinct shared generation advisory lock on the existing Meta MySQL
server. Both publisher and worker honor it. The owner connection remains open
across publisher commit or DDL auto-commit, and lock order is:

```text
generation lock -> schema lock -> task-row operations
```

Lock names are hashed from DW database plus binary target name and acquired in
stable order. Contention is bounded and retryable.

## Required evidence

- Publisher-first ordering rejects stale DDL.
- Worker-first ordering delays publication until DDL and settlement commit.
- Partial acquisition and exception paths release every held lock.
- Different targets do not share a lock.
