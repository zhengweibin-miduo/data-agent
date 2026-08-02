# Context Map

## Contexts

- [DDL Metadata](./backend/src/ddl_metadata/) — validates physical DDL and produces accepted Meta Snapshots. It owns Meta Projection as a rebuildable representation under [`ddl_metadata/meta_projection`](./backend/src/ddl_metadata/meta_projection/).
- [Long-term Memory](./backend/src/memory/) — owns authoritative reusable facts, their lifecycle, history, and rebuildable Memory Projections.
- [Conversation](./backend/src/conversation/) — owns permanent user conversations, messages, turn coordination, and extraction requests.
- [Data Sync](./backend/src/data_sync/) — owns desired synchronization tasks, DW materialization, CDC progress, and data-readiness state.

## Relationships

- **DDL Metadata → Long-term Memory**: an accepted Meta Snapshot proposes validated memory candidates through the Long-term Memory application interface; DDL-specific reference validation remains an injected adapter.
- **DDL Metadata → Data Sync**: an accepted Meta Snapshot publishes desired synchronization state through a Data Sync application port.
- **Conversation → Long-term Memory**: Conversation recalls and proposes user memories through Long-term Memory application interfaces; it does not use Long-term Memory persistence implementations directly.
- **Data Sync → DDL Metadata**: Data Sync materialization participates in value projection through the technology-neutral `ValueProjectionParticipant` application port. Data Sync application and low-level materialization code do not import a Meta Projection adapter; the outer worker composition root selects the DDL Metadata MySQL participant.
- **Meta Projection (inside DDL Metadata)**: consumes accepted Meta Snapshot state and the stable Data Sync value-input contract, while Meta Snapshot remains authoritative and the projection remains rebuildable.
