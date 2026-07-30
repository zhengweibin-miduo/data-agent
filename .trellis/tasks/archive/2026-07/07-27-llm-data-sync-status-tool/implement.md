# Implementation Plan

## 1. Dependency Gate

- [x] Land or explicitly stack on the final
  `07-27-sync-conversation-metadata-dw` commit.
- [x] Re-read the final `data_sync` contracts and update this plan if symbols
  changed.
- [x] Confirm the branch diff contains the dependency intentionally and no
  copied placeholder implementation.

## 2. Typed Intent Contracts

- [x] Add bounded `AnswerDataDependency`, `AnswerReadinessIntent`, gate-decision,
  target-catalog, and tool-result contracts with Chinese field descriptions.
- [x] Encode deterministic consistency rules between the boolean and dependency
  list.
- [x] Add tests for valid no-wait, source-scoped, aggregate, duplicate,
  hallucinated, missing-dependency, and oversized results.

Validation:

```powershell
uv run pytest tests/unit/answer_readiness/test_models.py
uv run pytest tests/unit/test_model_descriptions.py
```

## 3. Independent Intent Recognition

- [x] Reuse `LLMClient`; do not add another model client.
- [x] Build a bounded structured prompt from the current question and accepted
  target catalog.
- [x] Validate the first structured result deterministically.
- [x] Permit exactly one structured repair; fail closed after the second
  invalid result.
- [x] Ensure prompts/logs contain no source credentials or data rows.

Validation:

```powershell
uv run pytest tests/unit/answer_readiness/test_classifier.py
```

## 4. Read-Only Readiness Tool

- [x] Add a repository query that selects status without claiming or mutating
  tasks.
- [x] Implement source-scoped and all-source target semantics.
- [x] Map only `streaming` to ready; missing or any other phase is not ready.
- [x] Expose a LangChain-compatible async tool whose result contains only
  `ready`.
- [x] Add integration assertions that task phase, lease, attempts, cursors, and
  timestamps are unchanged after a tool call.

Validation:

```powershell
uv run pytest tests/unit/answer_readiness/test_tool.py
uv run pytest tests/integration/answer_readiness
```

## 5. Deterministic Gate Service

- [x] Route no-wait intent directly to `PROCEED` without database access.
- [x] Route wait intent through the tool and require every dependency to be
  ready.
- [x] Return `DATA_PREPARING` with exactly
  `数据准备中，请稍后重试` when not ready.
- [x] Return `INTENT_UNRESOLVED` after failed repair; never continue to a
  business answer.
- [x] Keep progress, table/source identity, phases, and errors out of the
  user-facing result.

Validation:

```powershell
uv run pytest tests/unit/answer_readiness/test_service.py
```

## 6. Specifications and Quality Gate

- [x] Update backend directory, database/tool-integration, error, logging, and
  quality specs with executable contracts.
- [x] Run Trellis full-scope check and root `code_review.md` review.
- [x] Confirm no `ConversationService` wiring, public HTTP route, general answer
  entrypoint, or actual user-answer flow was added.

Validation:

```powershell
uv sync --locked
uv lock --check
uv run ruff check src tests
uv run pyright src tests
uv run python -m compileall -q src tests
uv run python -m data_agent.settings
uv run pytest -m "not tei"
git diff --check
```

## Rollback

- Before dependency landing, keep this task in planning.
- After implementation, removal is code/test/spec-only; no database rollback is
  required.
