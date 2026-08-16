# Implement feature

Implement a new raglogs feature end-to-end, with tests. Keep the core pipeline
source-agnostic (ingest → normalize → fingerprint → cluster → baseline compare →
evidence → explain / timeline / compare). See `AGENTS.md`.

## Steps

1. **Locate the seam** and read the neighbors first:
   - New log source → adapter in `src/adapters/` yielding `ParsedLogLine` (no
     source-specific branches in core).
   - New error family / better clustering → `src/core/normalization/patterns.py`.
   - New CLI command → `src/cli/commands/<name>.py`, registered in
     `src/cli/main.py`.
   - New API route → `src/api/routes/<name>.py`, wired in `src/api/app.py`.
   - Schema change → new Alembic migration (`alembic revision`); never edit an
     applied one.
2. **Implement**: type-annotate signatures, match surrounding style, read config
   via `get_settings()` (`RAGLOGS_` env prefix), route model calls through
   `src/core/llm/provider.py`, keep the `noop` provider working.
3. **Test**: unit tests in `tests/unit/` (no DB); normalization changes get a
   before/after case in `tests/unit/test_normalization.py`; DB-backed behavior in
   `tests/integration/`.
4. **Verify**: `make lint` and `make test-unit` must pass. Update docs if
   user-facing. Keep the change focused on one concern.
