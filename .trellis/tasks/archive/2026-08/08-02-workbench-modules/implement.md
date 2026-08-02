---
goal: Deepen Workbench internal state modules and replace implementation-coupled tests without changing visual or transport contracts
version: 1.0
date_created: 2026-08-02
last_updated: 2026-08-02
owner: zwb
status: Planned
tags: [frontend, workbench, refactor, testing, accessibility]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

This plan extracts real Workbench state owners, preserves the existing console design and authority contracts, and replaces callback-history tests with public module and user-observable behavior tests.

## 1. Requirements & Constraints

- **REQ-001**: Restore, submission, job subscription, clarification, and chat each have one explicit internal owner and public feature seam.
- **REQ-002**: Preserve URL, React state, refs, session/local storage, and backend authority rules.
- **REQ-003**: Preserve HTTP/SSE payloads, `ApiError`, native reconnect/polling, idempotent submission, and same-turn chat retry.
- **REQ-004**: Feature tests must not obtain or invoke callbacks through mocked adapter call history.
- **REQ-005**: Preserve the current visual system, semantic controls, focus visibility, live status/error behavior, reduced motion, and mobile stacking.
- **CON-001**: No backend contract, legacy frontend, visual redesign, new feature, or global state dependency.
- **GUD-001**: Use red-green-replace and delete duplicate adapter-mechanism tests when observable seam coverage is green.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Establish pure recovery rules and job lifecycle ownership.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-001 | Add failing pure tests for persisted submission/deep-link reconciliation, non-replayable legacy attempts, and stale continuation ownership. |  |  |
| TASK-002 | Extract `submissionRecovery` without changing storage keys, URL format, fingerprints, or acceptance windows. |  |  |
| TASK-003 | Add failing hook seam tests for subscription watch/stop, authoritative waiting-input refresh, terminal cleanup, and stale-job rejection. |  |  |
| TASK-004 | Extract `useJobSubscription` with full StrictMode/unmount cleanup and stable transport injection. |  |  |

### Implementation Phase 2

- **GOAL-002**: Extract restore, submission, and chat orchestration while keeping WorkbenchPage as composition entry.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-005 | Extract `useJobRestore` and prove retry/not-found locks, foreign-coordinate fallback, source/DDL reset, and stale continuation guards. |  |  |
| TASK-006 | Move submission orchestration behind a feature hook/module while preserving URL-before-POST, abort, pending markers, busy ownership, and idempotency. |  |  |
| TASK-007 | Extract `useChatSession` and prove same-turn retry, navigation gate, conversation 404 recreation, deterministic release, and draft snapshot protection. |  |  |
| TASK-008 | Reduce `WorkbenchPage` to state composition and rendering without changing DOM semantics, copy, or layout classes. |  |  |

### Implementation Phase 3

- **GOAL-003**: Replace bloated and duplicate tests with seam-oriented coverage.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-009 | Add narrow scenario factories for preview/submission and in-memory job/chat lifecycle drivers. |  |  |
| TASK-010 | Replace all `connectJobEvents.mock.calls` callback driving with hook/module harnesses or user-visible page outcomes. |  |  |
| TASK-011 | Remove duplicate SSE reconnect/poll/malformed/close-race coverage from feature tests while retaining `api/jobEvents.test.ts`. |  |  |
| TASK-012 | Retain only payload/call-count assertions that prove idempotency, retry budget, or public request contracts. |  |  |

### Implementation Phase 4

- **GOAL-004**: Complete accessibility/design review and repository verification.

| Task | Description | Completed | Date |
|---|---|---|---|
| TASK-013 | Run `web-design-guidelines`, fix verified accessibility/UX/performance findings, and repeat the review. |  |  |
| TASK-014 | Run npm install lock check, lint, typecheck, focused/full tests, build, Python frontend tests, callback-history static search, and diff checks. |  |  |
| TASK-015 | Update proven frontend specs and verification record, run Trellis check, fix findings, and complete task lifecycle after authorized commit. |  |  |

## 3. Alternatives

- **ALT-001**: Introduce a global state library. Rejected because current ownership is local and the dependency would exceed the problem.
- **ALT-002**: Split every handler into a hook. Rejected because shallow one-use interfaces reduce locality.
- **ALT-003**: Keep adapter callback driving in page tests. Rejected because API adapter tests already own those mechanisms and the tests couple to implementation history.
- **ALT-004**: Redesign the page while extracting modules. Rejected because visual changes would obscure behavioral equivalence and are out of scope.

## 4. Dependencies

- **DEP-001**: Parent frontend flow and approved test-seam research.
- **DEP-002**: Frontend state-management, hook, component, type-safety, API/deployment, and quality specifications.
- **DEP-003**: Node/npm toolchain and local browser/build environment.

## 5. Files

- **FILE-001**: `frontend/src/workbench/WorkbenchPage.tsx` — feature composition and rendering entry.
- **FILE-002**: `frontend/src/workbench/submissionRecovery.ts` and Workbench hooks/modules — explicit internal state owners.
- **FILE-003**: `frontend/src/workbench/WorkbenchPage.test.tsx` and new hook/module tests — observable seam coverage.
- **FILE-004**: `frontend/src/api/jobEvents.test.ts` — transport/state-machine mechanism authority.
- **FILE-005**: `.trellis/spec/frontend/` and task verification artifacts — implementation-proven rules only.

## 6. Testing

- **TEST-001**: Pure persisted-submission and ownership decisions.
- **TEST-002**: Job subscription/restore lifecycle and cleanup.
- **TEST-003**: Chat retry/conversation recovery/draft snapshot ownership.
- **TEST-004**: Workbench user-observable restore/submission/clarification/chat behavior.
- **TEST-005**: API/SSE adapter mechanism tests with no duplicate feature coverage.
- **TEST-006**: Lint, typecheck, Vitest, build, Python frontend compatibility, design review, and static callback-history search.

## 7. Risks & Assumptions

- **RISK-001**: Hook extraction can create stale closures or miss cleanup under StrictMode; explicit ownership and unmount tests are mandatory.
- **RISK-002**: Moving restore/submission logic can change URL-before-POST or pending-attempt semantics; pure decision and reload tests are mandatory.
- **RISK-003**: Test reduction can remove behavior coverage if adapter and feature responsibilities are not mapped first.
- **RISK-004**: Seemingly harmless markup changes can regress keyboard or live-region behavior; design-guideline review is mandatory.
- **ASSUMPTION-001**: Existing visual direction and backend contracts remain unchanged.

## 8. Related Specifications / Further Reading

- `AGENTS.md`
- `.trellis/spec/frontend/index.md`
- `.trellis/spec/frontend/directory-structure.md`
- `.trellis/spec/frontend/component-guidelines.md`
- `.trellis/spec/frontend/hook-guidelines.md`
- `.trellis/spec/frontend/state-management.md`
- `.trellis/spec/frontend/type-safety.md`
- `.trellis/spec/frontend/api-deployment-contract.md`
- `.trellis/spec/frontend/quality-guidelines.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/onboarding-frontend-flow.md`
- `.trellis/tasks/08-02-align-project-structure-tests/research/test-seam-map.md`
