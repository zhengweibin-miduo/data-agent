# Query Binding Rule evidence map

## Confirmed authority already present

- `backend/src/models/memory.py:21-39` defines user memory categories; lines
  `277-317` show `MemoryDetail` already carries user scope, stable UID, category,
  key, typed content, hash, status and record version.
- `backend/src/memory/application/search.py:120-188` proves derived indexes only
  contribute candidate UIDs and MySQL performs the tenant/status/version/hash
  authority readback.
- `backend/src/memory/mysql/repository.py:76-86` derives the unique active slot
  from source, user, category and memory key; lines `256-289` lock that logical
  scope before lifecycle decisions.
- `.trellis/spec/backend/conversation-memory.md:82-112` defines user-memory recall
  as same-user MySQL authority; lines `123-133` require exact owned user evidence
  for extraction.

## Missing executable rule shape

- `backend/src/models/memory.py:155-170` stores generic user facts as free text.
- `backend/src/conversation/application/extraction.py:45-149` validates value and
  evidence, but the model-provided key has no Query alias semantics.
- `backend/src/conversation/adapters/extraction_model.py:13-17` asks for general
  identities, preferences, constraints and business rules only.
- Therefore `user.business_rule` cannot safely mean “this business concept maps
  to that exact Meta name”. A typed category supplies the missing semantics and
  self-contained projection text without a second storage stack.

## Query gap and preserved evidence boundary

- `backend/src/query/application/service.py:87-108` starts Query with
  `include_context=False`; lines `160-218` consume only the dedicated pending
  clarification chain before Meta binding.
- `backend/src/query/adapters/metadata.py:198-237` binds each slot only when exact
  `name/aliases` matching yields one allowed candidate; lines `289-297` contain
  that exact matcher.
- `backend/src/query/domain.py:1001-1025` stores original-quote bindings but no
  rule provenance.
- `.trellis/spec/backend/query-guidelines.md:121-137` correctly prohibits
  arbitrary completed turns as QueryIntent evidence. The new rule must select an
  object without adding evidence or intent.
- `.trellis/tasks/archive/2026-08/08-05-design-query-sql-flow/design.md:156-168`
  already states that a unique authoritative candidate or explicit stored user
  rule may bypass clarification.

## Selected minimal design

1. Reuse `agent_memory`, existing lifecycle/history/outbox, ES/Qdrant projections,
   MySQL authority readback and exact user evidence.
2. Add `user.query_binding_rule` with typed alias/target/evidence content so one
   confirmation can be recalled for nearby phrases.
3. Run one bounded hybrid recall over validated QueryIntent slot text; exact
   aliases short-circuit, while a structured allowlisted matcher may select one
   authoritative rule for a semantically equivalent phrase.
4. Apply a rule only after ordinary Meta binding is non-unique, and only when
   its target uniquely resolves inside the current DDL and slot kind.
5. Preserve memory UID/version/hash as a Query rule proof and use its target for
   later Meta authority revalidation; never treat recalled text as intent evidence.

## Rejected directions

- Directly pass `started.context.memories` into Query: Query deliberately does
  not load ordinary context, and free text would become an unsafe execution hint.
- Add a `query_user_rule` table: duplicates existing Memory authority, version,
  deletion, tenant and projection machinery.
- Exact-alias-only reuse: product feedback established that it would repeatedly
  ask about every nearby phrase and therefore does not preserve business semantics.
- Store permanent Meta object IDs in user rules: object IDs and schema versions
  can change; exact target names/aliases are re-resolved against current authority.
- Let an LLM choose among candidates using rule prose: this merely moves the
  original untrusted choice into another prompt. The selected matcher design is
  narrower: it can only choose a recalled memory UID for an existing slot quote,
  then deterministic code resolves the rule target against current Meta.
