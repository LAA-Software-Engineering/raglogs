# CLAUDE.md

Project guidance for Claude Code. The full stack, setup, architecture, style,
and boundaries live in **@AGENTS.md** — read it first. This file only adds
Claude-specific notes and points to the skills.

## Quick reference

- Install: `make install-dev` · Migrate: `raglogs init` · Demo: `make demo`
- Before finishing any change: `make lint` and `make test-unit` must pass.
- Unit tests need no DB; integration tests need `docker compose up postgres -d`.

## Skills

Task workflows live in `.claude/skills/`. Invoke with a slash command or let
them auto-activate by intent:

- `/implement-feature` — add a feature end-to-end (adapter, normalization
  rule, CLI/API command, pipeline stage) with tests.
- `/code-review` — review pending changes for correctness, pipeline
  contracts, and test coverage.
- `/bugfix` — reproduce with a failing test, fix the root cause, verify.

## Working agreements

- Match the surrounding code's style and idioms; don't introduce new patterns
  or dependencies without reason.
- Keep the core pipeline source-agnostic — new inputs are adapters in
  `src/adapters/`, not special cases in core.
- Route all model calls through `src/core/llm/provider.py`; keep the `noop`
  provider working so the tool runs without an API key.
- Add a new Alembic migration rather than editing an applied one.
