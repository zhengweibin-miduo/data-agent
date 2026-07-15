# Frontend Quality Guidelines

## Current Scope

Not applicable yet. No frontend lint, type-check, unit-test, browser-test,
build, bundle, or accessibility command exists.

`.github/workflows/ci.yml` currently validates only the Python backend with
Ruff, Pyright, `compileall`, configuration loading, and a MySQL integration
test. Do not report npm, pnpm, yarn, frontend build, or accessibility checks as
project quality gates.

## Review Boundary

Until a frontend is introduced, frontend files should not appear in an ordinary
backend task. This guide can name a frontend test or review command only after
that command exists in a project manifest or CI configuration.

AI-generated review findings, including future frontend findings, must be in
Simplified Chinese as required by `AGENTS.md`; code identifiers, paths,
commands, configuration keys, logs, and original error text remain in English.
