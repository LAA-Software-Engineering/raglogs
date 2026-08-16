# Bugfix

Diagnose and fix a bug in raglogs: reproduce, fix the root cause, verify.

## Steps

1. **Reproduce first.** Get the exact trigger (command + flags, or a log
   sample). Write a **failing** unit test in `tests/unit/` that captures the bug
   before touching source; confirm it fails. Use `tests/integration/` if it only
   reproduces against Postgres.
2. **Find the root cause.** Trace the pipeline (ingest → normalize → fingerprint
   → cluster → baseline compare → evidence → explain/timeline/compare) to the
   stage that's actually wrong — don't patch a downstream symptom. Common
   culprits: time-window math (`src/utils/time.py`), over/under-normalization
   (`src/core/normalization/patterns.py`), timestamp/field parsing
   (`src/core/parsing/`), async session lifecycle (`src/db/session.py`).
3. **Fix.** Smallest change at the root cause; match surrounding style; don't
   refactor unrelated code. Keep core source-agnostic and the `noop` provider
   working.
4. **Verify.** New test passes; `make test-unit` and `make lint` pass; run
   `make test-int` if DB paths are touched. State root cause, fix, and how you
   verified. Keep it focused on the one bug.
