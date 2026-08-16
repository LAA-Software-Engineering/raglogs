# Contributing to raglogs

raglogs is an incident analysis CLI tool. Contributions are welcome — bug fixes, new log adapters, normalization improvements, and documentation are all useful.

This document covers how to get set up, what the codebase expects, and how to submit changes.

---

## Getting started

```bash
git clone https://github.com/leo-aa88/raglogs
cd raglogs
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
cp .env.example .env
# Edit .env — set DB_URL at minimum
docker compose up postgres -d
raglogs init
```

Run the demo to verify everything works:

```bash
raglogs demo
raglogs timeline --since 2h
raglogs compare --since 2h --baseline 24h
```

---

## Project structure

```
src/core/
  clustering/     Fingerprint grouping, importance scoring, baseline comparison
  compare/        Window diffing — new, disappeared, increased, decreased
  explain/        Evidence assembly, confidence, templates, summarizer
  ingestion/      Ingestion orchestration and batch persistence
  llm/            Provider abstraction (OpenAI, Ollama, noop)
  normalization/  Message normalization, fingerprinting, trigger patterns
  parsing/        JSON and text parsers, field alias resolution
  retrieval/      Keyword-based question answering
  timeline/       Causal timeline reconstruction
src/cli/commands/ One file per CLI command
src/api/routes/   FastAPI route handlers
src/db/           SQLAlchemy models and session management
src/utils/        Time window parsing, hashing helpers
tests/unit/       Pure unit tests — no database required
tests/integration/ Full pipeline tests — require running Postgres
```

The pipeline flows: ingest → normalize → fingerprint → cluster → baseline compare → evidence assembly → explain / timeline / compare.

---

## Running tests

Unit tests require no database:

```bash
make test-unit
# or
pytest tests/unit/
```

Integration tests require a running Postgres instance:

```bash
docker compose up postgres -d
make test-int
# or
pytest tests/integration/
```

All tests must pass before a pull request will be merged. If you are adding new functionality, add tests for it.

---

## Code style

- Python 3.11+
- Type annotations on all function signatures
- `ruff` for linting, `black` for formatting

```bash
make lint
make format
```

No hard rules on line length beyond what `black` enforces. Prefer explicit over clever. Avoid abstractions that exist only to save lines.

---

## Areas where contributions are most useful

**Normalization improvements**

The normalization step (`src/core/normalization/patterns.py`) determines clustering quality. If you have real-world log formats that normalize poorly — producing too many clusters for the same error, or collapsing unrelated errors — a fix there has high leverage. Include before/after examples and a test in `tests/unit/test_normalization.py`.

**Log source adapters**

New adapters go in `src/adapters/`. Each adapter yields `ParsedLogLine` objects. The rest of the pipeline is fully source-agnostic. Useful adapters: Datadog, Kubernetes pod logs.

**Trigger patterns**

New trigger event patterns go in `TRIGGER_PATTERNS` in `src/core/normalization/patterns.py`. A good trigger pattern is specific enough to avoid false positives and general enough to match common variants across log formats.

**Bug fixes**

Check the issue tracker. Bugs with a reproducing log sample are easiest to fix.

---

## Submitting a pull request

1. Fork the repository and create a branch from `main`
2. Make your changes with tests
3. Run `make lint` and `make test-unit` — both must pass
4. Open a pull request with a clear description of what changed and why
5. Reference any related issue

Pull requests that are purely cosmetic (reformatting with no functional change) will not be merged.

Keep pull requests focused. A PR that fixes a bug and adds an unrelated feature is harder to review and slower to merge than two separate PRs.

---

## Commit messages

No strict format required. Be clear about what changed and why. One-line messages are fine for small changes. For anything non-trivial:

```
Short summary (under 72 chars)

Longer explanation of what changed and why, if not obvious from the
diff. Reference the issue number if applicable.
```

---

## Questions

Open a GitHub issue with the `question` label. If it is a quick question about whether a contribution would be accepted before you invest time in it, that is a good use of an issue.
