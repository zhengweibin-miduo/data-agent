# Frontend Type Safety

## Current Boundary

The browser code is plain JavaScript with no TypeScript compiler or runtime
schema dependency. FastAPI/Pydantic owns the HTTP contracts; the frontend must
not invent fields that are absent from those models.

## Rules

- Centralize HTTP failure projection in `ApiError` and treat missing envelope
  fields as an opaque HTTP failure.
- Keep job status/stage values aligned with `data_agent.models.jobs`.
- Do not use SSE `JobEventData` as an answer-submission contract: it lacks
  `question_set_id`; fetch `JobRecord` first.
- Preserve response nullability and empty states instead of assuming result,
  questions, history, or error data exists.
- Add TypeScript only with an approved package/tooling change and a real compile
  gate; do not add declarations that are never checked.
