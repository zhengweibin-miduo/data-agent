# Frontend Directory Structure

## Current Scope

This repository has no frontend application. There is no `package.json`,
JavaScript or TypeScript source tree, frontend build configuration, static asset
directory, page directory, or component directory.

The top-level `app/` directory is a Python backend package, not a web frontend.
Its contents are documented under `.trellis/spec/backend/`.

## Current Layout

No frontend layout or naming convention exists to document. Do not create
`src/components`, `pages`, `hooks`, or similar directories merely to match this
template. Until frontend files exist, there is no repository evidence from
which to derive a framework or layout rule.

## Evidence

- `pyproject.toml` is the only application dependency manifest.
- `.github/workflows/ci.yml` runs only Python and backend integration checks.
- `git ls-files` contains no `.js`, `.jsx`, `.ts`, `.tsx`, HTML, CSS, or
  frontend manifest files.
