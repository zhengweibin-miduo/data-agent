# Frontend Hook Guidelines

## Current Scope

Not applicable. The frontend has no React dependency, hooks, query cache, or
framework lifecycle. Stateful behavior is implemented with small functions and
native browser events in `app.js`.

Do not introduce a hook abstraction or framework merely to organize the current
static UI. Add hook conventions only if an approved implementation introduces a
real hook runtime and tests.
