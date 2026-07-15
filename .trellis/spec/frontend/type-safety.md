# Frontend Type Safety

## Current Scope

Not applicable yet. The repository has no TypeScript compiler configuration,
frontend type declarations, runtime schema library for browser data, or
generated API types.

Python type checking is established for the backend through Pyright and is
documented in `.trellis/spec/backend/quality-guidelines.md`; it must not be
recast as a frontend TypeScript convention. When a frontend package is added,
record the actual compiler settings, type ownership, boundary validation, and
forbidden escape hatches here.

## Evidence

There is no `tsconfig.json`, `package.json`, `.ts`, or `.tsx` file in the
tracked project or current application tree.
