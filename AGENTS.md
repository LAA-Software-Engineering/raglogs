# AGENTS.md

Guidance for AI coding agents working in **raglogs** — an incident-explanation
tool that analyzes a bounded time window of logs and produces a short,
evidence-backed explanation of what happened. CLI + FastAPI, Python, Postgres.

This file is the shared source of truth. `CLAUDE.md` and `.cursor/rules/`
defer to it.

## Tech stack

- Python **3.10+**
- Typer (CLI), FastAPI + Uvicorn (HTTP API)
- SQLAlchemy 2.x (sync sessions) + Alembic, Postgres with `pgvector`
- Pydantic v2 / pydantic-settings, structlog, rich
- numpy + scikit-learn (clustering), tenacity (retries), httpx
- pytest + pytest-asyncio (`asyncio_mode = auto`)

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # or: make install-dev
cp .env.example .env           # set DB_URL at minimum
docker compose up postgres -d  # or: make db-up
raglogs init                   # run migrations (alembic upgrade head)
```

Sanity check: `make demo` (starts db, migrates, ingests sample logs, runs
`explain` / `timeline` / `compare`).

## Commands

```bash
make test-unit    # pytest tests/unit — no database required
make test-int     # pytest tests/integration — needs live Postgres
make test-cov     # unit tests with coverage
make lint         # ruff check src/ tests/
make format       # ruff format src/ tests/
make api          # uvicorn dev server on :8000 (docs at /docs)
make worker       # background ingestion worker
```

Run a single test: `pytest tests/unit/test_clustering.py -v`

## Architecture

Pipeline: **ingest → normalize → fingerprint → cluster → baseline compare →
evidence assembly → explain / timeline / compare**. Everything after ingestion
is source-agnostic.

```
src/core/
  parsing/        JSON + text parsers, timestamp + field-alias resolution
  normalization/  message normalization, fingerprinting, TRIGGER_PATTERNS
  clustering/     fingerprint grouping, importance scoring, baseline compare
  compare/        window diffing (new / disappeared / increased / decreased)
  explain/        evidence assembly, confidence, templates, summarizer
  timeline/       causal timeline reconstruction
  retrieval/      keyword-based question routing
  llm/            provider abstraction (OpenAI, Ollama, noop)
  ingestion/      orchestration + batch persistence
src/adapters/     log source adapters — each yields ParsedLogLine
src/cli/commands/ one file per CLI command
src/api/routes/   FastAPI route handlers
src/db/           SQLAlchemy models + sync session
src/config/       settings (env-driven)
```

## Code style

- Type annotations on all function signatures. Prefer explicit over clever;
  avoid abstractions that exist only to save lines.
- Settings come from env vars via `src/config/settings.py` — never
  hard-code config or secrets.
- New log sources are adapters in `src/adapters/` that yield `ParsedLogLine`;
  keep the core pipeline source-agnostic.
- New normalization/trigger rules live in `src/core/normalization/patterns.py`.
  A trigger pattern must be specific enough to avoid false positives and
  general enough to match variants across formats.
- LLM calls go through `src/core/llm/provider.py` — do not call providers
  directly elsewhere. Code must run end-to-end with the `noop` provider.

## Testing

- Add tests with every functional change. Unit tests (`tests/unit/`) must not
  require a database; pipeline tests needing Postgres go in `tests/integration/`.
- Normalization changes: add a before/after case to
  `tests/unit/test_normalization.py`.
- `make lint` and `make test-unit` must pass before any PR.

## PRs & commits

- Branch from `main`; keep PRs focused (one concern per PR).
- Commit summary under 72 chars, imperative mood; explain *why* in the body
  when it isn't obvious. Reference the related issue.
- No purely cosmetic reformatting PRs.

## Evaluation & scope policy

raglogs has far more operational surface than verified analysis quality, so the
project measures before it extends (issue #88, epic #74). The tooling now
exists: `make eval` scores raglogs and a trivial baseline over labeled cases
and reports the **lift**; `make eval-data` / `make loghub` download external
corpora (RCAEval, Loghub-2.0).

- **Any PR that changes `src/core/`** — normalization, clustering, evidence,
  explain, timeline, retrieval — **must state the eval delta in its
  description**: metric before, metric after, on which corpus. "No change" is
  an acceptable answer; "not measured" is not.
- **Net-new platform surface is frozen** until raglogs shows measurable lift
  over the baseline on at least two independent corpora: no new source
  adapters, auth modes, API surface, client languages, or operational
  subsystems. Bug fixes, making existing features work as documented, and
  anything that improves explanation quality with an eval delta attached are
  always allowed.

## Boundaries — do not touch

- `.env` (real secrets), `migrations/versions/*` already applied (add a new
  migration instead of editing), `sample_data/` fixtures.
- Never commit secrets or real credentials.
- Don't edit generated artifacts (`*.egg-info`, `__pycache__`, `.venv`).
