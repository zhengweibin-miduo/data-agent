# Frontend Component Guidelines

## Current Scope

React components live under `frontend/src`. `App.tsx` owns navigation;
`WorkbenchPage` and `KnowledgePage` own feature orchestration; smaller components
render lineage and public task state.

## Rules

- Prefer semantic HTML (`form`, `button`, `a`, `label`, headings, lists) before
  ARIA or click handlers on generic elements.
- Every control has a visible label, meaningful `name`, keyboard access, and a
  visible `:focus-visible` state.
- Dynamic status uses the existing polite live region; actionable errors use a
  nearby error target and a concrete next step.
- Status must use text in addition to color. Destructive memory deletion keeps
  the native confirmation dialog.
- Structure labels such as `DDL`, `TRACE`, and `RECORD` describe real content;
  do not add decorative sequence numbers.
- Render user-provided content as React text children; never use
  `dangerouslySetInnerHTML` for API or user content.
- In fixed-height desktop layouts, clarification content owns an internally
  scrollable bounded row so every question and the submit action remain
  keyboard-reachable on short laptop viewports.
