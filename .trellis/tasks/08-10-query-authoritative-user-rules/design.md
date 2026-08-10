# Design: semantic Query Binding Rule recall and safe application

## 1. Decision

Implement Query Binding Rule as a typed category inside the existing Long-term
Memory module. One user confirmation establishes a reusable business concept,
not only one literal spelling. Later semantically equivalent Query phrases may
reuse that rule without being stored one by one.

The authority contract is:

```text
category       = user.query_binding_rule
source         = data_agent_conversation
user_id        = owning user
memory_key     = normalized confirmed anchor alias
content        = QueryBindingRuleContent(alias, target, evidence)
content trust  = user_confirmed
status         = ACTIVE
```

Example:

```text
confirmed rule: 销售额 -> 实付金额
later phrases:  销售金额 / 成交额
result:         semantically recall the rule, then resolve 实付金额 exactly in
                the current DDL before binding
```

Do not create a rule table, synonym table, new index, new embedding path or
per-phrase alias record. Reuse existing ES/Qdrant hybrid recall and MySQL
authority readback.

## 2. Module boundaries

### Long-term Memory module

Long-term Memory owns rule creation, typed content, identity, version, lifecycle,
tenant scope, projection and authoritative recall. Add:

```python
class QueryBindingRuleContent(ContractModel):
    trust: Literal["user_confirmed"]
    alias: str
    target: str
    supporting_user_quote: str
    evidence_message_uids: list[str]
    confirmed_assistant_message_uid: str | None
```

`build_memory_text()` emits a bounded, self-contained projection containing both
alias and target, so lexical and vector search can retrieve the rule for nearby
business expressions. Search continues to use:

```text
MySQL exact baseline + Elasticsearch + Qdrant
  -> RRF
  -> same-user MySQL ACTIVE/version/hash authority readback
```

No separate Memory application method is required. The existing
`MemorySearchService.search()` already accepts source, user, category and limit
and returns authoritative hits plus degraded targets.

### Query module

Query owns two narrow interfaces:

```python
class QueryBindingRuleCandidate(ContractModel):
    alias: str
    target: str
    memory_uid: str
    record_version: int
    content_hash: str
    score: float
    signals: list[str]


class QueryBindingRuleRecall(ContractModel):
    candidates: list[QueryBindingRuleCandidate]
    degraded_targets: list[str]


class QueryBindingRulePort(Protocol):
    async def recall(
        self,
        user_id: str,
        query_text: str,
    ) -> QueryBindingRuleRecall: ...


class QueryRuleDecision(ContractModel):
    slot_quote: str
    memory_uid: str


class QueryRuleMatcherPort(Protocol):
    async def match(
        self,
        slot_quotes: list[str],
        rules: list[QueryBindingRuleCandidate],
    ) -> list[QueryRuleDecision]: ...
```

The recall adapter converts only same-user, ACTIVE, current-version,
content-hash-valid `user.query_binding_rule` rows. It never exposes supporting
quotes or arbitrary Memory details to Query.

The matcher is a zero-temperature structured-output adapter. It may select an
existing memory UID for an existing QueryIntent slot quote or abstain. It cannot
invent a target, object ID, slot, SQL fragment or confidence score. Production
and in-memory test adapters share the same Query-owned interfaces.

### Conversation module

Conversation owns evidence-message membership and asynchronous extraction. The
model proposes `key=anchor alias`, `value=target`; deterministic validation
requires both to occur in the same owned user quote before constructing
`QueryBindingRuleContent`. Assistant proposals still require a later explicit
user restatement; ambiguous confirmations remain invalid.

## 3. Rule creation

Add `QUERY_BINDING_RULE` to `UserMemoryCategory` and
`user.query_binding_rule` to `BuiltinMemoryCategory`.

For this category only:

- `ExtractionCandidate.key` is the confirmed anchor alias;
- `ExtractionCandidate.value` is the exact target Meta name or alias;
- key and value must both occur verbatim in `supporting_user_quote`;
- code constructs `QueryBindingRuleContent`; generic `UserMemoryContent` is not
  accepted under this category;
- `memory_key` uses the existing normalized key convention;
- category policy is user-scoped, permanent and uses
  `user.query_binding_rule.v1`.

Existing `agent_memory.active_key` guarantees one active version per user and
anchor alias. New confirmations use existing update/supersede/history/outbox
semantics. No schema migration is required because category, content schema and
content JSON are already extensible.

## 4. Query data flow

```text
QueryRequest
  -> parse DDL
  -> claim Conversation turn
  -> load pending Query clarification chain
  -> parse and validate exact-evidence QueryIntent
  -> build one bounded recall text from all existing QueryIntent slot quotes
  -> QueryBindingRulePort.recall(user_id, recall_text)
       -> existing hybrid Memory search
       -> same-user MySQL authority readback
       -> Query-owned candidates + degraded state
  -> scoped Meta recall for normal slot texts and recalled rule targets
  -> normal exact Meta binding
       unique -> keep current binding
       non-unique -> rule path
  -> rule path
       exact alias rule -> select deterministically
       otherwise healthy semantic recall -> Rule Matcher selects or abstains
       selected rule target uniquely resolves in current DDL/slot kind
         -> bind and retain rule proof
       otherwise -> clarification or retryable infrastructure error
  -> unchanged planner / AST / EXPLAIN / readiness / SELECT flow
```

The semantic recall text includes only already validated QueryIntent slot quotes.
Recalled alias/target text is never appended to `evidence_messages`, QueryIntent,
SQL parameters, reverse-coverage input or user prompt history.

## 5. Rule selection and binding algorithm

For every existing slot `(slot, quote, allowed_kinds)`:

1. Build `normal_matches` exactly as today from current-DDL authoritative Meta
   candidates using `quote == candidate.name|alias`.
2. If `len(normal_matches) == 1`, bind it and ignore historical rules.
3. Otherwise search recalled rules for normalized `quote == rule.alias`. If one
   exists, select it without model matching.
4. If no exact alias exists and Memory recall reports any degraded target, fail
   with retryable `query_rule_unavailable`; an incomplete semantic candidate set
   must not trigger repeated clarification or unsafe auto-binding.
5. For remaining non-unique slots, call Rule Matcher once with all slot quotes
   and the bounded authoritative rule allowlist.
6. Validate every decision in code: quote belongs to those slots, UID belongs to
   recalled rules, one decision per slot, no unknown fields, and one selected
   rule per slot. Invalid output is `query_rule_match_invalid`, not a fallback.
7. Match selected `rule.target` exactly against authoritative scoped Meta
   `name/aliases`, current DDL allowlist and `allowed_kinds`; continue excluding
   natural-language Metric candidates under the existing non-executable-metric
   rule.
8. Exactly one target binds the original quote and stores a rule proof. Zero or
   multiple targets return the existing clarification.

The matcher decides semantic equivalence only between an exact user-evidenced
slot quote and a small authoritative rule allowlist. All execution-relevant
target/object choices remain deterministic after that selection.

## 6. QueryContext proof

Add:

```python
class QueryBindingRuleProof(ContractModel):
    target: str
    memory_uid: str
    record_version: int
    content_hash: str
```

`QueryContext.rule_proofs` maps original QueryIntent quote to proof. Existing
`bindings` remains `original quote -> object ID`, preserving planner and SQL
validation contracts.

`bindings_are_authoritative()` uses the original quote for normal bindings and
the proof target for rule-assisted bindings. It must still return the same
object ID and kind from current scoped Meta authority.

The Memory version is a point-in-time authority snapshot. A later user update
creates a new active version but does not retroactively invalidate an already
started Query. Do not log alias, target, supporting quote or content. If needed,
log only a hash of `memory_uid:record_version`.

## 7. Failure behavior

| Condition | Result |
|---|---|
| Exact alias rule and unique current target | Auto-bind without model matching |
| Semantically equivalent phrase and matcher selects one healthy recalled rule | Auto-bind without another user confirmation |
| Matcher abstains or several meanings remain plausible | Existing clarification |
| ES/Qdrant degradation with no exact alias | Retryable `query_rule_unavailable`, no SQL |
| Rule matcher transport failure | Retryable `query_rule_match_failed`, no SQL |
| Matcher returns unknown quote/UID or duplicate decision | `query_rule_match_invalid`, no SQL |
| Rule target absent or non-unique in current DDL | Existing clarification |
| Rule target is disallowed kind or natural-language Metric | Existing clarification |
| Generic `user.business_rule` hit | Ignore; it is not a Query Binding Rule |
| Rule changes after context construction | Current Query uses captured version; later Query sees new version |

## 8. Safety examples

- “销售额” confirmed as “实付金额”; later “销售金额”“成交额” may select the same
  rule and auto-bind.
- “销售税额” must not be selected merely because it contains “销售额”; Rule
  Matcher may abstain, and the deterministic target gate cannot invent a tax
  field mapping.
- If the user has active rules “销售额 -> 实付金额” and “含税销售额 -> 含税金额”,
  the matcher receives both authoritative candidates and must select exactly one
  or abstain.
- A rule target can select only an existing QueryIntent slot. It cannot turn
  “查询订单” into an aggregate, add a status filter or choose a time range.

## 9. Compatibility and rollback

- This is an unmerged V1 branch. Add the category and behavior directly; do not
  add migrations, backfills, dual reads or compatibility shims.
- Existing generic user memories and Query HTTP/NDJSON contracts remain valid.
- Rollback removes the Query recall/matcher usage and new extraction category;
  no separate database object or index must be removed.

## 10. Alternatives

### Exact aliases only

Rejected after product feedback. It remembers spelling rather than reusable
business semantics and causes repeated clarification for every nearby phrase.

### Pass all Conversation memories into Query

Rejected. Free text and ordinary context cannot become execution authority.

### Auto-bind directly from vector score

Rejected. A nearest neighbor is a recall signal, not a binding decision. The
bounded matcher must select an authoritative rule and the target must still
resolve exactly in current Meta.

### New rule/synonym table

Rejected. It duplicates Long-term Memory scope, history, lifecycle, projections
and deletion while forcing manual alias maintenance.

### Store permanent Meta object IDs

Rejected. User rules are cross-conversation while object IDs are current-schema
coordinates; exact target text is re-resolved against current authority.

## 11. No ADR decision

No ADR is required. The design reuses existing Memory projections and Query LLM
infrastructure, adds no persistence technology and remains reversible in V1.
