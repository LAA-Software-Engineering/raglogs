---
name: code-review
description: Review pending changes in raglogs for correctness, pipeline contract violations, security, and test coverage. Use when the user asks to review code, review a diff, review a PR, or check changes before merging.
---

# Code review for raglogs

Review the pending diff, not the whole repo. Start with:

```bash
git diff main...HEAD        # or: git diff --staged
```

Report findings most-severe first. For each: file:line, what's wrong, and the
concrete failure it causes. Distinguish blocking issues from suggestions. Don't
invent problems — if it's clean, say so.

## What to check

**Correctness**
- Off-by-one and boundary bugs in time-window handling (`--since`, `--baseline`,
  `src/utils/time.py`), clustering, and diffing.
- Async correctness: no blocking calls in async paths; sessions opened/closed
  correctly (`src/db/session.py`).
- Error handling on external calls (LLM, DB, network) — wrapped with `tenacity`
  where the surrounding code does.

**Pipeline contracts**
- Core stays source-agnostic — new inputs are adapters yielding `ParsedLogLine`,
  not branches in core.
- All model access goes through `src/core/llm/provider.py`, and the `noop`
  provider still works (tool runs without an API key).
- Schema changes ship a new Alembic migration; applied migrations are untouched.

**Config & security**
- No hard-coded secrets, URLs, or paths — config comes from `RAGLOGS_*` env via
  `src/config/settings.py`. No secrets in the diff.
- Untrusted log input isn't used to build SQL or shell strings unsafely.

**Tests & style**
- Functional changes include tests; normalization changes include a before/after
  case in `tests/unit/test_normalization.py`.
- Type annotations present. `make lint` and `make test-unit` pass — run them.
