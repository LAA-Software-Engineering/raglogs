---
name: bugfix
description: Diagnose and fix a bug in raglogs — reproduce with a failing test, fix the root cause, verify. Use when the user reports something broken, a crash, wrong output, a failing test, or asks to debug or fix a bug.
---

# Bugfix workflow for raglogs

## 1. Reproduce first

- Get the exact trigger: command + flags, or a log sample. Bugs with a
  reproducing log sample are easiest — ask for one if missing.
- Write a **failing** unit test in `tests/unit/` that captures the bug before
  changing any source. This is the regression guard. If it only reproduces
  against Postgres, put it in `tests/integration/`.
- Confirm it fails: `pytest tests/unit/test_<area>.py -v`.

## 2. Find the root cause

Trace through the pipeline to the stage that's actually wrong — don't patch a
downstream symptom:

ingest → normalize → fingerprint → cluster → baseline compare → evidence →
explain / timeline / compare.

Common culprits:
- Time-window math (`src/utils/time.py`, `--since` / `--baseline` parsing).
- Over/under-normalization producing too many or merged clusters
  (`src/core/normalization/patterns.py`).
- Timestamp/field parsing across formats (`src/core/parsing/`).
- Async session lifecycle (`src/db/session.py`).

## 3. Fix

- Smallest change that fixes the root cause. Match surrounding style; don't
  refactor unrelated code in the same change.
- Keep core source-agnostic and the `noop` LLM provider working.

## 4. Verify

- The new test passes; `make test-unit` and `make lint` pass. Run `make test-int`
  if the fix touches DB-backed paths.
- State the root cause, the fix, and how you verified it. Keep the PR focused on
  the one bug.
