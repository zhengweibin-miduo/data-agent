# Context Map

## Contexts

- [DDL Metadata](./src/data_agent/ddl_metadata/) — validates physical DDL and produces accepted Meta Snapshots. It owns Meta Projection as a rebuildable representation, currently implemented with supporting modules under [`metadata_indexing`](./src/data_agent/metadata_indexing/).
- [Long-term Memory](./src/data_agent/memory/) — owns authoritative reusable facts, their lifecycle, history, and rebuildable Memory Projections.
- [Conversation](./src/data_agent/conversation/) — owns permanent user conversations, messages, turn coordination, and extraction requests.
- [Data Sync](./src/data_agent/data_sync/) — owns desired synchronization tasks, DW materialization, CDC progress, and data-readiness state.

## Relationships

- **DDL Metadata → Long-term Memory**: an accepted Meta Snapshot proposes validated memory candidates through the Long-term Memory application interface; DDL-specific reference validation remains an injected adapter.
- **DDL Metadata → Data Sync**: an accepted Meta Snapshot publishes desired synchronization state through a Data Sync application port.
- **Conversation → Long-term Memory**: Conversation recalls and proposes user memories through Long-term Memory application interfaces; it does not use Long-term Memory persistence implementations directly.
- **Data Sync → DDL Metadata**: Data Sync exposes stable value-read and readiness information through a port or projection event. It does not invoke Meta Projection implementations.
- **Meta Projection (inside DDL Metadata)**: consumes accepted Meta Snapshot state and stable Data Sync value inputs, while Meta Snapshot remains authoritative and the projection remains rebuildable.
