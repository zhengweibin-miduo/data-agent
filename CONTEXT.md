# Data Agent Domain

Data Agent turns physical database definitions and explicit user knowledge into
validated semantic metadata that can be recovered, corrected, and reused.

## Language

**DDL Metadata**:
Validated semantic descriptions and metrics derived from a physical DDL schema.
_Avoid_: Schema memory, generated documentation

**Conversation**:
A permanent, user-owned sequence of text turns used to supply bounded context
and explicit evidence.
_Avoid_: Chat session, checkpoint

**Long-term Memory**:
An authoritative reusable fact derived from accepted DDL results or explicit
user evidence, with history and lifecycle state.
_Avoid_: Cache, embedding, checkpoint

**Meta Snapshot**:
The accepted semantic tables, columns, and metrics for one physical schema
version.
_Avoid_: Memory snapshot, index snapshot

**Memory Projection**:
A rebuildable search representation of an authoritative Long-term Memory.
_Avoid_: Memory record, source of truth

**Meta Projection**:
A rebuildable search representation of an accepted Meta Snapshot, including semantic objects and bounded value candidates.
_Avoid_: Metadata index, source of truth

**Query Intent**:
The structured meaning of a business-data question, supported only by exact user evidence and not yet bound to database objects.
_Avoid_: Keyword list, SQL draft

**Validated Query**:
A read-only DW query whose referenced objects, parameters, joins, and execution policy have passed deterministic gates.
_Avoid_: Generated SQL, query draft
