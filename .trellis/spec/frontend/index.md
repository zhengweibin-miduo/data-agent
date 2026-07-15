# Frontend Development Guidelines

> Current frontend scope and explicit non-applicability boundaries.

## Overview

This repository currently contains no frontend application. These files record
that fact so future agents do not invent framework, component, hook, state,
TypeScript, or frontend quality conventions while working on the Python backend.

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Evidence that no frontend tree exists | Current scope documented |
| [Component Guidelines](./component-guidelines.md) | No component framework or conventions yet | Current scope documented |
| [Hook Guidelines](./hook-guidelines.md) | No hook or frontend data-fetching patterns yet | Current scope documented |
| [State Management](./state-management.md) | No frontend state layer yet | Current scope documented |
| [Quality Guidelines](./quality-guidelines.md) | No frontend quality commands yet | Current scope documented |
| [Type Safety](./type-safety.md) | No TypeScript boundary yet | Current scope documented |

## Re-evaluation Trigger

Re-evaluate these guides only after frontend source or tooling exists. Derive
any later convention from the actual package manifest, source, tests, and CI
rather than selecting a framework or workflow in advance.

## Pre-Development Checklist

- Check whether the task actually touches a frontend source tree or frontend
  manifest. The current repository has neither.
- For backend-only work, treat the frontend guides as a non-applicability
  boundary; do not introduce framework, component, hook, state, or TypeScript
  rules.
- If frontend files have appeared, inspect those files before changing this
  index because the current absence statements may no longer be true.

## Quality Check

- Confirm that every frontend guide still describes only repository evidence
  and does not name a framework, library, command, or test that is absent.
- Do not report npm, pnpm, yarn, browser, accessibility, or frontend build
  checks as executed while no corresponding manifest command exists.
- Verify that all six links in the guidelines index resolve and that any future
  status change is backed by actual frontend files.

---

**Language**: All documentation should be written in **English**.
