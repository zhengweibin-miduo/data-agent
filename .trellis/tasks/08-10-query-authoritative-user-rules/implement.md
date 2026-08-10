---
goal: Implement authoritative Query Binding Rules for deterministic Meta disambiguation
version: 1.0
date_created: 2026-08-10
last_updated: 2026-08-10
owner: zwb
status: 'In Progress'
tags: [query, memory, conversation, correctness, pr85]
---

# Introduction

![Status: In Progress](https://img.shields.io/badge/status-In_Progress-yellow)

Implement the smallest trusted path that lets a user-confirmed business concept
select one current Meta object for exact or semantically equivalent later phrases.
Reuse Long-term Memory authority and preserve QueryIntent's exact-evidence gate.

## 1. Requirements & Constraints

- **REQ-001**: Add `user.query_binding_rule` and typed
  `QueryBindingRuleContent(alias, target, evidence)`; build self-contained lexical
  and vector projection text from alias and target.
- **REQ-002**: Accept a rule proposal only when alias and target both occur
  verbatim in owned user evidence and all existing assistant-confirmation checks
  pass.
- **REQ-003**: Reuse bounded Long-term Memory hybrid recall plus same-user MySQL
  authority readback and adapt candidates/degradation to a Query-owned
  `QueryBindingRulePort`.
- **REQ-004**: Apply a rule only after normal exact Meta binding is non-unique;
  use exact alias selection first, then a zero-temperature allowlisted Rule
  Matcher for semantically equivalent phrases.
- **REQ-005**: Preserve original QueryIntent quotes in `bindings` and record rule
  UID, record version, content hash and target in a Query-owned proof.
- **REQ-006**: Use proof target, not original alias, for later Meta binding
  authority revalidation of rule-assisted bindings.
- **REQ-007**: Require the selected rule target to resolve to exactly one allowed
  current-DDL Meta object; matcher output can select only existing slot quotes and
  recalled memory UIDs and cannot supply targets or object IDs.
- **SEC-001**: Never treat generic business-rule text, assistant prose, model
  confidence, vector score, rule target or historical message text as QueryIntent
  evidence or let retrieval score directly choose an object.
- **SEC-002**: Never log rule alias, target, supporting quote, SQL parameters or
  business rows; tenant filtering must occur in the authoritative MySQL query.
- **CON-001**: Preserve Query HTTP/NDJSON, SELECT-only execution, all-sources
  result scope, no total LIMIT, readiness, generation coordination, EXPLAIN and
  one-repair behavior.
- **CON-002**: Reuse `agent_memory`, history, lifecycle, outbox and user-data
  erasure plus existing ES/Qdrant projections; add no table, column, migration,
  index service, dependency or config.
- **CON-003**: Keep generic `user.business_rule` non-executable and keep natural-
  language Metric definitions non-executable.
- **CON-004**: Stay in Trellis planning until the user explicitly approves
  implementation and `task.py start`.
- **GUD-001**: Follow `.trellis/spec/backend/query-guidelines.md` and
  `.trellis/spec/backend/conversation-memory.md`.
- **PAT-001**: Test observable behavior through Memory application,
  `QueryBindingRulePort`, `QueryRuleMatcherPort`, Query metadata binding and Query
  stream seams; avoid private helper or collaborator call-order tests.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Create authoritative rule candidates through existing extraction
  and Memory lifecycle paths.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Add failing extraction tests in `backend/tests/unit/conversation/test_extraction.py` for exact alias/target acceptance, missing alias, missing target, wrong message UID/role, ambiguous confirmation and explicit assistant-proposal confirmation. Add `QUERY_BINDING_RULE`, `QueryBindingRuleContent` and category-specific prompt guidance in `backend/src/models/memory.py`, `backend/src/conversation/models.py` and `backend/src/conversation/adapters/extraction_model.py`. |  |  |
| TASK-002 | Update `validate_extraction_candidates()` in `backend/src/conversation/application/extraction.py` so QUERY_BINDING_RULE reuses candidate key/value, requires both exact texts in one supporting user quote, derives normalized memory key and constructs typed `QueryBindingRuleContent`. |  |  |
| TASK-003 | Add the `user.query_binding_rule.v1` permanent user-scoped policy in `backend/src/memory/domain/policies.py`; update `build_memory_text()` to include alias and target plus category enumeration/search visibility. Add policy, payload/projection, lifecycle/version and erasure regressions without changing MySQL schema. |  |  |

### Implementation Phase 2

- **GOAL-002**: Provide a bounded authority read and a Query-owned anti-corruption
  adapter.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Add failing Memory search tests proving “销售额 -> 实付金额” projection is recalled for exact and nearby phrases, while all returned rules still pass same-user ACTIVE/current-version/content-hash authority readback; cover ES/Qdrant degradation and other-user exclusion through existing Memory search seams. |  |  |
| TASK-005 | Define `QueryBindingRuleCandidate`, `QueryBindingRuleRecall`, `QueryBindingRulePort`, `QueryRuleDecision` and `QueryRuleMatcherPort` in `backend/src/query/application/contracts.py`. Implement `backend/src/query/adapters/rules.py` as the only Memory-to-Query adapter and project alias, target, UID, version, hash, score, signals and degraded targets. |  |  |
| TASK-006 | Add a zero-temperature structured Rule Matcher to `backend/src/query/adapters/llm.py`; it may return only existing slot quote + recalled memory UID or abstain. Wire recall/matcher through `backend/src/memory/adapters/composition.py` and `backend/src/application.py`; reuse existing clients/configuration and update composition tests. |  |  |

### Implementation Phase 3

- **GOAL-003**: Apply rule snapshots only at the ambiguous Meta binding seam.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-007 | Add failing metadata-binding seam tests in `backend/tests/unit/query/test_context.py` for: normal unique wins; exact alias resolves without matcher; “销售金额/成交额” can select the sales rule; “销售税额” abstains; zero/multiple/out-of-scope/disallowed-kind targets clarify; generic business-rule text and natural-language Metric remain non-executable. |  |  |
| TASK-008 | Update `QueryApplication.stream()` in `backend/src/query/application/service.py` to build one bounded recall text from validated slot quotes, recall rules once, and pass Query-owned candidates/degraded state into `QueryMetadataPort.build_context()`. Map authority/recall failures to retryable stable errors before planning or SQL. |  |  |
| TASK-009 | Refactor `QueryMetadataAdapter.build_context()` in `backend/src/query/adapters/metadata.py` to resolve normal unique candidates, exact alias rules, then batch semantic matcher decisions; validate quote/UID allowlists and retrieve rule targets in the same current-DDL scoped Meta search. Preserve clarification ordering and value search. |  |  |
| TASK-010 | Add `QueryBindingRuleProof` and `QueryContext.rule_proofs` in `backend/src/query/domain.py`; update `bindings_are_authoritative()` to match proof target for assisted bindings while preserving original quote/object ID/kind. Add final-revalidation and SQL reverse-coverage regressions. |  |  |

### Implementation Phase 4

- **GOAL-004**: Prove cross-conversation behavior, preserve contracts and prepare
  PR #85 resolution evidence.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-011 | Add a public-seam Query stream test covering: one conversation confirms “销售额指实付金额”; extraction persists the rule; later conversations ask “销售金额合计” and “成交额合计”; healthy semantic recall + allowlisted matcher reuse one rule, exact target resolves to one allowed column, and planner/validator keeps the current user phrase as evidence. |  |  |
| TASK-012 | Add negative cross-layer tests proving “销售税额” does not falsely select the sales rule; matcher abstention/conflict/degraded recall, rule deletion/supersession/other-user scope and authority failures execute neither planner, EXPLAIN nor SELECT; rule targets cannot create filters/operators/aggregation/Top-N/time/JOIN. |  |  |
| TASK-013 | Update `CONTEXT.md`, `.trellis/spec/backend/query-guidelines.md` and `.trellis/spec/backend/conversation-memory.md` to match executable category, authority, evidence and precedence contracts. Remove any planning wording that implies arbitrary stored business-rule prose is executable. |  |  |
| TASK-014 | Run focused extraction, Memory and Query tests, then full non-integration pytest, relevant MySQL integrations, Ruff, Pyright, compileall, settings/Compose checks and `git diff --check`. Re-read PR #85 review threads and prepare a response mapping code/tests to `PRRT_kwDOTXnY3c6XeklV`; do not write to GitHub without separate authorization. |  |  |

## 3. Alternatives

- **ALT-001**: Pass `ConversationContext.memories` into Query. Rejected because
  free text and ordinary context cannot become deterministic execution authority.
- **ALT-002**: Add a `query_user_rule` table. Rejected because Long-term Memory
  already owns tenant scope, versions, history, lifecycle and deletion.
- **ALT-003**: Store a permanent Meta object ID or schema fingerprint. Rejected
  because the user rule is cross-conversation while current DDL coordinates may
  evolve; exact target text must be re-resolved against current authority.
- **ALT-004**: Let an LLM interpret generic rule prose. Rejected because model
  choice and confidence cannot bypass the deterministic Meta gate.
- **ALT-005**: Support exact aliases only. Rejected after product feedback because
  it stores spelling rather than reusable business semantics and repeatedly asks
  about each nearby phrase.
- **ALT-006**: Let a rule override an already unique direct Meta binding.
  Rejected for MVP because the defect is repeated ambiguity, not rule precedence.

## 4. Dependencies

- **DEP-001**: Existing Long-term Memory authority and lifecycle in
  `agent_memory`, including active-key uniqueness and MySQL tenant filtering.
- **DEP-002**: Existing Conversation extraction claim, exact quote validation and
  atomic Memory/outbox commit.
- **DEP-003**: Existing scoped Meta search completeness and authoritative readback.
- **DEP-004**: Existing Query exact-evidence, clarification, context, binding
  revalidation and SQL validation contracts on PR #85.
- **DEP-005**: Existing zero-temperature structured LLM adapter and independently
  degrading ES/Qdrant Memory retrieval signals.

## 5. Files

- **FILE-001**: Memory/category contracts — `backend/src/models/memory.py`,
  `backend/src/memory/domain/policies.py`, `backend/src/memory/domain/payloads.py`.
- **FILE-002**: Extraction — `backend/src/conversation/models.py`,
  `backend/src/conversation/application/extraction.py`,
  `backend/src/conversation/adapters/extraction_model.py`.
- **FILE-003**: Memory authoritative recall —
  `backend/src/memory/application/search.py`, existing Memory index adapters and
  Query's Memory anti-corruption adapter.
- **FILE-004**: Query contracts and binding —
  `backend/src/query/application/contracts.py`,
  `backend/src/query/application/service.py`, `backend/src/query/domain.py`,
  `backend/src/query/adapters/metadata.py`, `backend/src/query/adapters/rules.py`,
  `backend/src/query/adapters/llm.py`.
- **FILE-005**: Composition — `backend/src/memory/adapters/composition.py` and
  `backend/src/application.py`.
- **FILE-006**: Tests — focused Conversation extraction, Memory application/MySQL,
  Query context/service and application composition suites under `backend/tests`.
- **FILE-007**: Contracts — `CONTEXT.md`, Query guidelines and Conversation/Memory
  guidelines under `.trellis/spec/backend/`.

## 6. Testing

- **TEST-001**: Extraction trust boundary: exact alias+target evidence succeeds;
  missing/mismatched/assistant-only/ambiguous evidence fails.
- **TEST-002**: Memory recall: exact and semantic retrieval, tenant isolation,
  current ACTIVE version/hash, supersession, deletion and degraded projections.
- **TEST-003**: Rule Matcher and Meta binding: exact alias, semantic equivalent,
  abstention, invalid quote/UID, conflict and all fail-closed target combinations.
- **TEST-004**: Query stream: cross-conversation reuse for nearby phrases without
  adding intent slots, evidence text or unsafe SQL inputs.
- **TEST-005**: Regression: generic user memories, Chat recall, user-memory API,
  Query evidence/AST/readiness/generation/execution contracts remain green.
- **TEST-006**: Quality gates: `uv run pytest -m "not integration" -q`, focused
  MySQL tests, `uv run ruff check src tests`, `uv run pyright src tests`,
  `python -m compileall -q src tests`, configuration/Compose checks and
  `git diff --check`.

## 7. Risks & Assumptions

- **RISK-001**: A user-global rule may not fit every future source. MVP follows
  the existing user-scoped cross-conversation contract and fails closed when the
  target is not unique in the current DDL; source-specific rules require a later
  product decision and persisted scope coordinate.
- **RISK-002**: If rule targets are mixed into QueryIntent evidence, AST gates can
  falsely claim the user requested new semantics. Tests must keep target text
  confined to metadata selection and proof.
- **RISK-003**: Current `bindings_are_authoritative()` searches by original quote;
  failing to switch assisted bindings to proof target will reject valid rules or
  accidentally rebind another object.
- **RISK-004**: Semantic equivalence is model-assisted and can be wrong. Limit the
  model to authoritative recalled UIDs, retain abstention, reject degraded recall,
  require exact current Meta target uniqueness and cover confusable phrases such
  as “销售税额”.
- **ASSUMPTION-001**: PR #85 remains unmerged and its initial V1 does not require
  migration/backfill compatibility.
- **ASSUMPTION-002**: Query Binding Rules model reusable business-concept aliases;
  formulas, conditional rules and precedence over unique current bindings remain
  out of scope.

## 8. Related Specifications / Further Reading

- `.trellis/tasks/08-10-query-authoritative-user-rules/prd.md`
- `.trellis/tasks/08-10-query-authoritative-user-rules/design.md`
- `.trellis/tasks/08-10-query-authoritative-user-rules/research/evidence-map.md`
- `.trellis/spec/backend/query-guidelines.md`
- `.trellis/spec/backend/conversation-memory.md`
- `.trellis/tasks/archive/2026-08/08-05-design-query-sql-flow/design.md`
- `CONTEXT.md`
