---
status: accepted
---

# Use one MySQL integration adapter for accepted snapshot publication

DDL Metadata exposes `AcceptedSnapshotPublisher` as its application boundary, while the production `MySQLAcceptedSnapshotPublisher` deliberately coordinates Meta, Long-term Memory, Data Sync desired state, and Meta Projection outbox writes in one generation-lock-protected MySQL transaction. This trades independent bounded-context persistence for the required all-or-nothing visibility of an accepted snapshot; inner application and domain modules remain isolated from the participating repositories, and a future split into asynchronous events must first replace this atomicity contract explicitly.
