# Code review

Review the pending diff in raglogs (`git diff main...HEAD` or `--staged`), not
the whole repo. Report findings most-severe first with file:line, the problem,
and the concrete failure it causes. Separate blocking issues from suggestions.
If it's clean, say so — don't invent problems.

## Check

- **Correctness**: time-window boundary math (`--since`/`--baseline`,
  `src/utils/time.py`), clustering, diffing; async correctness and DB session
  lifecycle (`src/db/session.py`); error handling on LLM/DB/network calls.
- **Pipeline contracts**: core stays source-agnostic (new inputs are adapters
  yielding `ParsedLogLine`); all model access goes through
  `src/core/llm/provider.py` and the `noop` provider still works; schema changes
  add a new Alembic migration and leave applied ones untouched.
- **Config & security**: no hard-coded secrets/URLs/paths (config from
  `RAGLOGS_*` via `src/config/settings.py`); no secrets in the diff; untrusted
  log input never builds SQL/shell strings unsafely.
- **Tests & style**: functional changes include tests; normalization changes
  include a before/after case; type annotations present. Run `make lint` and
  `make test-unit`.
