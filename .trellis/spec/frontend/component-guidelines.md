# Frontend Component Guidelines

## Current Scope

There is no component framework. `index.html` owns stable page structure and
`app.js` creates repeated task, chat, and memory records with native DOM APIs.

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
- Build user-provided content with `textContent`, never `innerHTML`.
