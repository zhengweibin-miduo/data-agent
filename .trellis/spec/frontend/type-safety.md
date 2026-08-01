# Frontend Type Safety

## Current Boundary

The browser code is TypeScript compiled by `tsc -b`. FastAPI/Pydantic remains
the authority for HTTP contracts; `frontend/src/api/types.ts` is a checked client
projection, not an independent schema authority.

## Rules

- Centralize HTTP failure projection in `ApiError` and treat missing envelope
  fields as an opaque HTTP failure.
- Keep job status/stage values aligned with `data_agent.models.jobs`.
- Do not use SSE `JobEventData` as an answer-submission contract: it lacks
  `question_set_id`; fetch `JobRecord` first.
- Preserve response nullability and empty states instead of assuming result,
  questions, history, or error data exists.
- Keep `strict` TypeScript enabled and run `npm run typecheck` in CI.
- Do not use `any` to bypass contract mismatches. Update the Pydantic authority
  first when a real server contract changes, then update client types and tests.
