# raglogs

Ask your logs what happened.

```bash
$ make demo
```

<img width="1468" height="933" alt="image" src="https://github.com/user-attachments/assets/93a58a8b-0ecb-4dd4-9901-8faaab3ccea3" />

## What raglogs does

raglogs analyzes a bounded time window of logs and produces a short
incident explanation backed by evidence.

It is designed for answering one question quickly:

**What happened, why do you think that, and what evidence supports it?**

grep finds lines.
Datadog shows dashboards.
raglogs explains incidents.

---

## The killer commands

```bash
raglogs explain --since 2h
```

```
╭──────────────────────────────────────────────────────── raglogs explain  ─────────────────────────────────────────────────────────╮
│ Incident summary                                                                                                                  │
│                                                                                                                                   │
│ Window: 2026-03-12T22:33:30 to 2026-03-12T23:33:30                                                                                │
│                                                                                                                                   │
│ Services affected: billing-worker, api                                                                                            │
│                                                                                                                                   │
│ Primary issue: A surge of 184 signature verification failures occurred in the billing-worker service, starting about 2 minutes   │
│ after deployment of billing-worker version v2.4.1.                                                                                │
│                                                                                                                                   │
│ Secondary effects: Following the primary failures, the api service returned 39 requests with 500 Internal Server Errors due to    │
│ upstream errors, along with 25 requests showing high latency.                                                                     │
│                                                                                                                                   │
│ Likely trigger: Deployment of billing-worker version v2.4.1 at 22:38:29, immediately followed by application start, appears to    │
│ have introduced the failures.                                                                                                     │
│                                                                                                                                   │
│ Confidence: high                                                                                                                  │
╰───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

```bash
raglogs timeline --since 2h
```

```
  22:38:29  deploy     Deploy completed for billing-worker version v2.4.1 · deployment-controller
  22:38:30  startup    Application started billing-worker v2.4.1 on port 8080 · billing-worker

  22:40:31  error ↑    signature verification failed
             184 events · billing-worker · 49 min span

  22:42:00  effect     500 Internal Server Error — upstream error
             39 events · api · 48 min span

  22:45:29  effect     high latency detected
             25 events · api · 44 min span
```

```bash
raglogs compare --since 30m --baseline 24h
```

```
Incident comparison

  Window A (now):      2026-03-16 15:17:42 UTC → 2026-03-16 15:47:42 UTC
  Window B (baseline): 2026-03-15 15:17:42 UTC → 2026-03-15 15:47:42 UTC

New error clusters
  + signature verification failed                            86 events
  + 500 Internal Server Error — upstream error               20 events
  + Retry events (24 distinct events, 24 total)              24 events

Triggers in A not seen in B
  +⚡ Deploy completed for billing-worker version v2.4.1 · deployment-controller
```

`explain` answers **what happened**.
`timeline` shows **how it unfolded**.
`compare` shows **what changed**.

Together they work like `git log`, `git blame`, `git diff` — but for incidents.

The narrative above is derived from **cluster relationships** — counts, timing,
level escalation, baseline deltas — not from any fixture-specific vocabulary, so
the same shapes hold on your own logs. `timeline` and `compare` are fully
deterministic (no LLM). `explain` and `ask` render the same curated evidence
with deterministic templates by default; with an LLM configured they add the
prose polish shown here. (Outputs above are representative of the bundled sample
incident.)

---

## Why raglogs

Most log tools are good at search and filtering. raglogs is built for a different job: taking a bounded time window and explaining it.

**The problem with raw LLM approaches**

Sending thousands of log lines to an LLM produces vague summaries, hallucinated causes, and no grounding in actual counts or timing. Context windows fill up. Results are inconsistent.

**What raglogs does instead**

1. Normalizes log messages to remove dynamic noise (UUIDs, IDs, IPs, timestamps)
2. Fingerprints normalized messages into stable cluster keys
3. Groups logs into clusters by fingerprint
4. Compares cluster volumes against a configurable baseline window
5. Detects trigger events (deploys, restarts, config reloads)
6. Assembles a structured evidence packet from actual counts, timing, and baseline deltas
7. Either passes that evidence to an LLM for polish, or renders it with deterministic templates

The LLM never sees raw logs. It only sees curated facts. The explanation is grounded in evidence, not inference.

See [docs/architecture.md](docs/architecture.md) for the pipeline internals.

---

## Quick start

**Prerequisites:** Docker, Python 3.10+

```bash
# Clone and install
git clone https://github.com/LAA-Software-Engineering/raglogs
cd raglogs
pip install -e .

# Start Postgres with pgvector
docker compose up postgres -d

# Initialize schema (Alembic migrations + the pgvector extension)
raglogs init

# Run the demo
raglogs ingest ./sample_data/sample_incident
raglogs explain --since 1h
raglogs timeline --since 2h
raglogs compare --since 30m --baseline 24h
raglogs ask 'why did the deploy fail?'
```

Or with Make:

```bash
make demo
```

**Requirements:** Python 3.10+, PostgreSQL 14+ with the
[pgvector](https://github.com/pgvector/pgvector) extension, and (optionally)
Docker for the bundled Compose setup. Configure with `cp .env.example .env` and
set `DB_URL` at minimum.

---

## Reference

| Topic | Where |
| --- | --- |
| Every CLI command and flag | [docs/cli.md](docs/cli.md) |
| HTTP API, auth, OIDC, rate limiting | [docs/api.md](docs/api.md) |
| Log sources, adapters, formats | [docs/adapters.md](docs/adapters.md) |
| Every setting (generated from the model) | [docs/configuration.md](docs/configuration.md) |
| Pipeline internals | [docs/architecture.md](docs/architecture.md) |

### Configuration

All settings are read from `.env`, environment variables, or CLI flags.
Priority: CLI > env var > `.env` file > defaults. The complete, authoritative
list of every setting (env var, type, default) is
[`docs/configuration.md`](docs/configuration.md), generated from the `Settings`
model by `make config-docs` so it can't drift. The settings you're most likely
to touch:

| Variable | Default | Description |
|---|---|---|
| `DB_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/raglogs` | PostgreSQL connection URL |
| `LLM_PROVIDER` | `disabled` | `disabled`, `openai`, `ollama`, `claude` |
| `LLM_MODEL` | `gpt-4.1-mini` | LLM model name. Use `claude-haiku-4-5` when `LLM_PROVIDER=claude` |
| `EMBEDDINGS_PROVIDER` | `disabled` | `disabled`, `openai`, `local`. Cluster merge, semantic `ask`, and `/similar` ANN skip when `disabled` |
| `DEFAULT_BASELINE_WINDOW` | `24h` | How far back to compare for baseline |
| `MAX_EVIDENCE_ITEMS` | `8` | Max evidence lines in output |
| `AUTH_ENABLED` | `false` | Require `Authorization: Bearer` on the HTTP API. Keep `false` for local demo; set `true` in production |

### LLM integration

raglogs is fully useful without any LLM — `--no-llm` (or `LLM_PROVIDER=disabled`)
uses deterministic template summaries. When configured, the LLM receives only a
small curated evidence packet — never raw logs — and the prompt prohibits
fabrication. On timeout, HTTP error, exhausted retries, or an over-budget
payload, explain/ask **fall back to the same deterministic templates** and set
`llm.fell_back=true`; a process-local circuit breaker skips the provider after
repeated failures. Fallback never invents — it only renders the curated evidence.

```env
# OpenAI (or any OpenAI-compatible endpoint via OPENAI_BASE_URL)
LLM_PROVIDER=openai
LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-...

# Ollama (fully local)
LLM_PROVIDER=ollama
LLM_MODEL=llama3
OLLAMA_BASE_URL=http://localhost:11434

# Claude (Anthropic Messages API)
LLM_PROVIDER=claude
LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=sk-ant-...
```

An empty provider key falls back to the deterministic template provider. See
[docs/configuration.md](docs/configuration.md) for every LLM tuning knob
(`LLM_TIMEOUT`, `LLM_MAX_RETRIES`, `LLM_MAX_CONCURRENCY`, breaker settings).

### Web UI

A minimal browser dashboard for the same explain / timeline / compare / ask
flows the CLI exposes — server-rendered (Jinja2 + vanilla JS/CSS, no node/npm),
served by the API itself and calling the same `/v1/query/*` endpoints.

```bash
make web   # Postgres + migrations + a fresh sample incident, served at http://localhost:8000/
```

`make web` reseeds a fresh sample incident each run (like `make demo`). Use
`make web-serve` to serve without reseeding, or `make api` if Postgres is
already running and migrated. Pick a time window, then switch between the
**Explain**, **Timeline**, **Compare**, and **Ask** tabs; the ingestion dropdown
scopes queries to a specific ingestion or "All ingestions". With default
`AUTH_ENABLED=false` the UI is open (fine for local dev); see
[docs/api.md](docs/api.md) for serving it with auth on.

---

## Development

```bash
pip install -e ".[dev]"   # runtime + dev tooling
make test-unit            # unit tests (no DB needed)
make test-int             # integration tests (requires running Postgres)
make api                  # API with hot reload
make lint                 # ruff check
make format               # ruff format
make openapi              # export OpenAPI spec (clients/openapi.json)
make clean
```

`make bench` ingests synthetic log lines and explains the window, reporting wall
time and query counts per phase so regressions are visible (runs on a schedule
and on `main` via `.github/workflows/bench.yml`). The working target is
**explain a window in under 10s**; treat the benchmark output, not this
sentence, as the source of truth. See issue #85 for the remaining perf work.

New log sources are adapters in `src/adapters/` — see
[docs/adapters.md](docs/adapters.md). Project conventions and boundaries live in
[AGENTS.md](AGENTS.md).

**Project structure**

```
src/
├── adapters/            Log source adapters (file, cloudwatch, datadog, loki, k8s)
├── api/                 FastAPI routes + auth (API keys, roles, OIDC, bind guard)
├── cli/commands/        Typer CLI commands
├── config/              Pydantic settings
├── core/
│   ├── clustering/      Fingerprint grouping, semantic merge, importance scoring
│   ├── compare/         Window diffing — new, disappeared, increased, decreased
│   ├── embeddings/      Provider abstraction + ingest persist helper
│   ├── explain/         Evidence assembly, templates, confidence, summarizer
│   ├── ingestion/       Ingestion orchestration, webhooks, batch persistence
│   ├── llm/             Provider abstraction (OpenAI, Ollama, Claude, noop)
│   ├── normalization/   Message normalization, fingerprinting, trigger patterns
│   ├── parsing/         JSON and text parsers, field extractors, timestamps
│   ├── retrieval/       Semantic + keyword question answering
│   ├── retention/       Per-scope TTL, scheduled purge of raw vs summary tiers
│   └── timeline/        Causal timeline reconstruction
├── db/                  SQLAlchemy models, session management
└── utils/               Time window parsing, hashing helpers
```

---

## License

MIT
