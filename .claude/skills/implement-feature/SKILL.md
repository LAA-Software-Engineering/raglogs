---
name: implement-feature
description: Implement a new feature in raglogs end-to-end — a log source adapter, normalization/trigger rule, CLI command, API route, or pipeline stage — with tests. Use when the user asks to add, build, or implement functionality in this repo.
---

# Implement a feature in raglogs

Follow the pipeline: ingest → normalize → fingerprint → cluster → baseline
compare → evidence → explain / timeline / compare. Keep the core
source-agnostic. See `AGENTS.md` for the full map.

## 1. Locate the seam

Identify which layer the feature belongs to and read the neighbors first:

- **New log source** → adapter in `src/adapters/` that yields `ParsedLogLine`.
  Do not add source-specific branches to core.
- **Better clustering / new error family** → normalization + trigger patterns
  in `src/core/normalization/patterns.py`.
- **New CLI command** → `src/cli/commands/<name>.py`, registered in
  `src/cli/main.py`.
- **New API endpoint** → `src/api/routes/<name>.py`, wired in `src/api/app.py`.
- **Schema change** → new Alembic migration (`alembic revision`), never edit an
  applied one.

## 2. Implement

- Type-annotate all signatures. Match surrounding style; don't add dependencies
  without a concrete need.
- Config via `get_settings()` (`src/config/settings.py`, `RAGLOGS_` env prefix)
  — no hard-coded values.
- Route any model call through `src/core/llm/provider.py`; keep the `noop`
  provider working so the feature runs without an API key.

## 3. Test

- Add unit tests in `tests/unit/` (no DB). Normalization changes get a
  before/after case in `tests/unit/test_normalization.py`. Pipeline behavior
  needing Postgres goes in `tests/integration/`.
- Run `make lint` and `make test-unit` — both must pass. For DB-backed work:
  `docker compose up postgres -d && make test-int`.

## 4. Wrap up

Update the README/docs if user-facing. Summarize what changed, why, and how you
verified it. Keep the change focused — one concern.
