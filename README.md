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

## Contents

- [The killer commands](#the-killer-commands)
- [Why raglogs](#why-raglogs)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Commands](#commands)
- [Configuration](#configuration)
- [LLM integration](#llm-integration)
- [Log formats](#log-formats)
- [How it works](#how-it-works)
- [HTTP API](#http-api)
- [Web UI](#web-ui)
- [Development](#development)

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
│ Primary issue: A surge of 184 Stripe signature verification failures occurred in the billing-worker service at the                │
│ /webhooks/stripe endpoint, starting about 2 minutes after deployment of billing-worker version v2.4.1.                            │
│                                                                                                                                   │
│ Secondary effects: Following the primary failures, the api service experienced 39 checkout requests returning 500 Internal Server │
│ Errors due to upstream billing errors, along with 25 checkout requests showing high latency. Additionally, billing-worker logged  │
│ webhook retry attempts for failed events.                                                                                         │
│                                                                                                                                   │
│ Likely trigger: Deployment of billing-worker version v2.4.1 at 22:38:29, immediately followed by application start, appears to    │
│ have introduced the Stripe signature verification failures.                                                                       │
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

  22:40:31  error ↑    Stripe signature verification failed for endpoint /webhooks/stripe
             184 events · billing-worker · 49 min span

  22:42:00  effect     POST /api/checkout 500 Internal Server Error — upstream billing error
             39 events · api · 48 min span
  22:42:50  effect     Webhook retries (2 retry events)
             2 events · billing-worker

  22:45:29  effect     POST /api/checkout 200 OK latency=<duration> (high latency detected)
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
  + Stripe signature verification failed for endpoint /webhooks/stripe         86 events
  + POST /api/checkout 500 Internal Server Error — upstream billing error      20 events
  + Webhook retries (24 distinct events, 24 total)                             24 events
  + Webhook queue growing                                                      13 events

Triggers in A not seen in B
  +⚡ Deploy completed for billing-worker version v2.4.1 · deployment-controller
```

```bash
raglogs ask 'why did stripe fail?'
```

```
╭─────────────────────────────────────────────────────────── raglogs ask ───────────────────────────────────────────────────────────╮
│ Stripe failed because the signature verification for incoming webhook requests to the /webhooks/stripe endpoint failed            │
│ repeatedly. This caused the billing-worker service to reject or fail processing Stripe webhook events, likely disrupting payment  │
│ or billing workflows. The errors were consistently observed between 22:54 and 23:30 UTC on 2026-03-12.                            │
│                                                                                                                                   │
│ Key supporting evidence:                                                                                                          │
│ - 500 errors logged with the message "Stripe signature verification failed for endpoint /webhooks/stripe"                         │
│ - Errors occurred in the billing-worker service                                                                                   │
│ - Time window of errors: 2026-03-12T22:54:49 to 2026-03-12T23:30:29 UTC                                                           │
╰───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

`explain` answers **what happened**.
`timeline` shows **how it unfolded**.
`compare` shows **what changed**.

Together they work like `git log`, `git blame`, `git diff` — but for incidents.

All three outputs are fully deterministic. No LLM required.

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

# Initialize schema
raglogs init

# Run the demo
raglogs ingest ./sample_data/sample_incident
raglogs explain --since 1h
raglogs timeline --since 2h
raglogs compare --since 30m --baseline 24h
raglogs ask 'why did stripe fail?'
```

Or with Make:

```bash
make demo
```

---

## Installation

**Requirements**

- Python 3.10+
- PostgreSQL 14+ with the [pgvector](https://github.com/pgvector/pgvector) extension
- Docker (optional, for the bundled Compose setup)

**Install**

```bash
pip install -e .
```

**Configure**

```bash
cp .env.example .env
# Edit .env — set DB_URL at minimum
```

**Initialize the database**

```bash
raglogs init
```

This runs Alembic migrations and creates all required tables, including the `vector` extension for pgvector.

---

## Commands

The full CLI reference — every command and flag — lives in [docs/cli.md](docs/cli.md).

## Configuration

All settings are read from `.env`, environment variables, or CLI flags. Priority: CLI > env var > `.env` file > defaults.

> **The complete, authoritative list of every setting (env var, type, default) is
> [`docs/configuration.md`](docs/configuration.md), generated from the `Settings`
> model by `make config-docs` so it can't drift.** The table below highlights the
> settings you're most likely to touch.

| Variable | Default | Description |
|---|---|---|
| `DB_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/raglogs` | PostgreSQL connection URL |
| `LLM_PROVIDER` | `disabled` | `disabled`, `openai`, `ollama`, `claude` |
| `LLM_MODEL` | `gpt-4.1-mini` | LLM model name. Use `claude-haiku-4-5` when `LLM_PROVIDER=claude` |
| `OPENAI_API_KEY` | _(empty)_ | API key for OpenAI or compatible endpoint |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compatible API |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `ANTHROPIC_API_KEY` | _(empty)_ | API key for Claude (`LLM_PROVIDER=claude`). Empty falls back to noop |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Anthropic Messages API host |
| `EMBEDDINGS_PROVIDER` | `disabled` | `disabled`, `openai`, `local`. Cluster merge, semantic `ask`, and `/similar` ANN skip when `disabled` |
| `EMBEDDINGS_MODEL` | `text-embedding-3-small` | Embeddings model name |
| `EMBEDDINGS_DIMENSIONS` | `1536` | Vector size. Must be 1536 (the stored column width) for any non-disabled provider; any other value is a hard config error surfaced at startup / before ingest |
| `CLUSTER_MERGE_SIMILARITY_THRESHOLD` | `0.92` | Cosine similarity at or above which fingerprint clusters merge. High on purpose so distinct errors stay separate |
| `CLUSTER_MERGE_MIN_COUNT` | `1` | Minimum `count` for a cluster to participate in a merge |
| `ASK_SEMANTIC_TOP_K` | `100` | Max log lines returned by semantic `ask` |
| `ASK_SEMANTIC_MIN_SIMILARITY` | `0.75` | Minimum cosine similarity for a semantic `ask` hit. Looser than cluster-merge because questions paraphrase |
| `SIMILAR_SEMANTIC_MIN_SIMILARITY` | `0.80` | Minimum cosine similarity for `POST /v1/query/similar`. Falls back to fingerprint equality when embeddings are down |
| `DEFAULT_BASELINE_WINDOW` | `24h` | How far back to compare for baseline |
| `MAX_CLUSTERS_FOR_EXPLAIN` | `10` | Max clusters sent to the explain pipeline |
| `MAX_EVIDENCE_ITEMS` | `8` | Max evidence lines in output |
| `ADAPTER_CLOUDWATCH_REGION` | `us-east-1` | AWS region for `--adapter cloudwatch` |
| `DATADOG_API_KEY` | _(empty)_ | Datadog API key (`logs_read_data`) |
| `DATADOG_APP_KEY` | _(empty)_ | Datadog application key |
| `DATADOG_SITE` | `datadoghq.com` | Datadog site (`us3.datadoghq.com`, `datadoghq.eu`, …) |
| `DATADOG_PAGE_SIZE` | `1000` | Logs per Datadog page (API max 1000) |
| `DATADOG_MAX_ROWS` | `10000` | Max events pulled in one Datadog ingest run |
| `AUTH_ENABLED` | `false` | Require `Authorization: Bearer` on the HTTP API (except `/health` and `/metrics`). Keep `false` for local demo; set `true` in production/Docker |
| `AUTH_MODE` | `api_key` | `api_key`, `oidc`, or `both` |
| `OIDC_ISSUER` | _(empty)_ | JWT issuer when `AUTH_MODE` is `oidc` or `both` |
| `OIDC_AUDIENCE` | _(empty)_ | Optional JWT audience |
| `OIDC_JWKS_URL` | _(empty)_ | Optional JWKS URL; default is `{issuer}/.well-known/openid-configuration` then `{issuer}/.well-known/jwks.json` |
| `API_BIND_HOST` | `127.0.0.1` | Host the startup guard treats as the bind address. `make api` sets this to `0.0.0.0` to match uvicorn |
| `AUTH_REFUSE_INSECURE_BIND` | `false` | If `true`, refuse to start when auth is off and the bind host is not loopback; if `false`, log a warning only |
| `RATELIMIT_ENABLED` | `true` | Token-bucket rate limiting on ingest writes and query routes. In-memory per process (not shared across workers) |
| `RATELIMIT_INGEST_RPS` | `100` | Steady-state tokens/sec for `POST /v1/ingestions*`. `0` = unlimited |
| `RATELIMIT_QUERY_RPS` | `100` | Steady-state tokens/sec for `/v1/query*`. `0` = unlimited |
| `RATELIMIT_BURST` | `100` | Bucket size (max tokens) per API key (or `anonymous` when auth is off) |
| `RATELIMIT_RETRY_AFTER_SECONDS` | `1` | `Retry-After` value on `429 RATE_LIMITED` |
| `INGEST_QUEUE_MAX` | `100` | Pending worker-job ceiling; over this, ingest returns `429 INGEST_QUEUE_FULL` |
| `INGEST_RETRY_AFTER_SECONDS` | `5` | `Retry-After` value on `429 INGEST_QUEUE_FULL` |
| `LLM_MAX_CONCURRENCY` | `4` | Max in-flight LLM provider calls process-wide. `0` = unlimited. Noop does not wait |
| `LLM_TIMEOUT` | `30` | Per-attempt HTTP timeout in seconds for OpenAI/Ollama calls |
| `LLM_MAX_RETRIES` | `2` | Extra attempts after the first (3 total) with jittered exponential backoff |
| `LLM_MAX_TOKENS` | `600` | Completion cap (`max_tokens` / Ollama `num_predict`) |
| `LLM_MAX_INPUT_TOKENS` | `0` | Estimated input-token budget (`chars/4`). `0` derives from `LLM_MAX_TOKENS`. Over budget: trim evidence (respecting `MAX_EVIDENCE_ITEMS`) or fall back |
| `LLM_BREAKER_THRESHOLD` | `5` | Consecutive LLM failures before the process-local breaker opens |
| `LLM_BREAKER_COOLDOWN_SECONDS` | `60` | Seconds the breaker stays open before a half-open probe |
| `WEBHOOK_SECRET` | _(empty)_ | Fallback HMAC secret for ingest completion callbacks when auth is off or the API key has no per-key `whsec_` |
| `WEBHOOK_MAX_RETRIES` | `5` | Extra webhook POST attempts after the first (6 POSTs by default) on 5xx / 429 / connect errors |
| `WEBHOOK_TIMEOUT` | `10` | Per-attempt HTTP timeout in seconds for completion callbacks |
| `INGEST_IDEMPOTENCY_TTL_SECONDS` | `86400` | How long `Idempotency-Key` on `POST /v1/ingestions` is remembered (batch enqueue and tail create) |
| `RETENTION_RAW` | `30d` | How long to keep raw `log_entries` measured by `created_at` (time-in-store; cascaded `log_embeddings` / `cluster_members`). `0` / empty / `off` = never purge. Per-scope override: `scope_retention.raw_interval` |
| `RETENTION_SUMMARY` | `180d` | How long to keep cluster summaries + `cluster_embeddings` after which similar-incident recall for that scope expires. Same `0` / `off` disable |
| `PURGE_INTERVAL_SECONDS` | `3600` | Idle worker poll interval between automatic purge jobs. `0` disables scheduled purge (`raglogs purge` still works) |
| `PURGE_CHUNK_SIZE` | `1000` | Max rows deleted per table per scope per purge job (worker does one chunk; CLI drains) |
| `LOG_FORMAT` | `json` | Structured log renderer: `json` or `console`. API uses this; CLI switches to console on a TTY |
| `OTEL_SDK_DISABLED` | `false` | Skip the OpenTelemetry SDK. Request ids are still generated |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(empty)_ | Optional OTLP HTTP traces endpoint. Empty = no exporter (no collector required) |
| `OTEL_SERVICE_NAME` | `raglogs` | Resource `service.name` on exported spans |

---

## LLM integration

raglogs is fully useful without any LLM. The `--no-llm` flag (or `LLM_PROVIDER=disabled`) activates deterministic template-based summaries.

When an LLM is configured, it receives only a small curated evidence packet — not raw logs. The prompt enforces fixed output structure, prohibits fabrication, and requires explicit uncertainty statements when evidence is insufficient. In-flight provider calls are capped by `LLM_MAX_CONCURRENCY` (CLI and API share the process semaphore; the noop provider does not block).

Every provider call has a timeout (`LLM_TIMEOUT`) and bounded jittered retries (`LLM_MAX_RETRIES`). On timeout, HTTP error, exhausted retries, or an over-budget evidence payload, explain/ask **fall back to the same deterministic templates** and set `llm.fell_back=true` — the request still succeeds. After `LLM_BREAKER_THRESHOLD` consecutive failures a process-local circuit breaker opens for `LLM_BREAKER_COOLDOWN_SECONDS`; while open, raglogs skips the provider entirely and serves templates. `GET /health` exposes `llm_breaker: {state, consecutive_failures, cooldown_remaining_seconds}` (`closed` / `open` / `half_open`). An open breaker marks `status` as `degraded` but still returns HTTP 200 so probes do not fail. Fallback never invents: it only renders the curated evidence packet.

### OpenAI

```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-...
```

### Ollama (fully local)

```env
LLM_PROVIDER=ollama
LLM_MODEL=llama3
OLLAMA_BASE_URL=http://localhost:11434
```

### Claude (Anthropic)

```env
LLM_PROVIDER=claude
LLM_MODEL=claude-haiku-4-5
ANTHROPIC_API_KEY=sk-ant-...
```

Uses the Anthropic Messages API (`POST /v1/messages`) via raw httpx. An empty `ANTHROPIC_API_KEY` falls back to the deterministic template provider. Override `ANTHROPIC_BASE_URL` only for a proxy. The OpenAI default model (`gpt-4.1-mini`) is unchanged when `LLM_PROVIDER=openai`.

### Any OpenAI-compatible endpoint

```env
LLM_PROVIDER=openai
OPENAI_BASE_URL=http://localhost:1234/v1
OPENAI_API_KEY=not-required
```

---

## Log formats

### JSON logs

raglogs accepts structured JSON logs and resolves common field aliases automatically.

```json
{"timestamp": "2026-03-12T22:01:10Z", "level": "error", "service": "billing-worker", "message": "Stripe signature verification failed"}
{"ts": "2026-03-12T22:01:10Z", "severity": "ERROR", "app": "api", "msg": "checkout returned 500"}
{"@timestamp": "2026-03-12T22:01:10Z", "log_level": "WARN", "logger": "worker", "log": "Queue depth exceeded threshold"}
```

Supported field aliases:

| Field | Accepted names |
|---|---|
| Timestamp | `timestamp`, `ts`, `time`, `@timestamp`, `datetime` |
| Message | `message`, `msg`, `log`, `text`, `body` |
| Level | `level`, `severity`, `log_level`, `loglevel`, `lvl` |
| Service | `service`, `app`, `logger`, `component`, `application` |
| Environment | `environment`, `env`, `deployment`, `stage` |
| Trace ID | `trace_id`, `traceId`, `trace` |
| Request ID | `request_id`, `requestId`, `req_id`, `correlation_id` |
| Host | `host`, `hostname`, `server`, `instance`, `pod` |

### Plain text logs

```
2026-03-12T22:01:10Z ERROR billing-worker Stripe signature verification failed
[2026-03-12T22:01:10Z] [WARN] High memory usage detected on worker-3
```

raglogs uses regex heuristics to extract timestamp, level, service, and message from common plain-text formats. If service is not found in the line, it can be provided with `--service` or inferred from the filename.

### Format auto-detection

By default (`--format auto`), raglogs samples the first non-empty line of each file to detect JSON vs plain text. Override with `--format json` or `--format text`.

---

## How it works

The pipeline internals — normalization, clustering, evidence, confidence — are in [docs/architecture.md](docs/architecture.md).

## HTTP API

The full HTTP API reference lives in [docs/api.md](docs/api.md).

## Web UI

A minimal browser dashboard for the same explain / timeline / compare / ask
flows the CLI exposes — no separate build step, served by the API itself.

```bash
make web
# starts Postgres, runs migrations, seeds a fresh sample incident, and
# serves the UI at http://localhost:8000/ — open it and there's already
# something to explain.
```

`make web` always reseeds a fresh sample incident (each run adds a new
ingestion — same as `make demo`). For repeat runs where you don't want
that: `make web-serve` starts Postgres, migrates, and serves without
reseeding. Already have Postgres running and migrated? `make api` starts
just the server.

Pick a time window — relative presets or a duration like `2h` (the default), or
switch to absolute UTC `from`/`to` datetimes — then switch between the
**Explain**, **Timeline**, **Compare**, and **Ask** tabs. The **ingestion**
dropdown in the top bar lists your 25 most recent completed ingestions (via
`GET /v1/ingestions`) and defaults to the latest one, matching the CLI; pick a
different ingestion or "All ingestions" to change what a query is scoped to.

The UI is server-rendered (Jinja2 + vanilla JS/CSS, no CORS, no node/npm) and
calls the same `/v1/query/*` JSON endpoints listed above.

With default `AUTH_ENABLED=false` the UI is open — fine for local dev. When
auth is on, load the UI with a `query` or `admin` bearer token (the browser
does not attach `Authorization` on its own; put raglogs behind a proxy that
injects the header, or keep auth off on loopback). `/health` stays public.

---

## Development

```bash
# Install everything (runtime + dev tooling)
pip install -e ".[dev]"

# Unit tests (no DB needed)
make test-unit

# Integration tests (requires running Postgres)
make test-int

# API with hot reload
make api

# Lint / format
make lint
make format

# Export OpenAPI spec (clients/openapi.json)
make openapi

# Optional generated clients (see clients/README.md)
make client-go
make client-python

# Full clean
make clean
```

### Performance

`make bench` ingests synthetic log lines and explains the window, reporting
wall time and query counts per phase so regressions are visible (it runs on a
schedule and on `main` via `.github/workflows/bench.yml`, uploading
`bench_results.json`). The working target is **explain a window in under 10s**;
the real ceiling on line volume is still being characterized — treat the
benchmark output, not this sentence, as the source of truth. See issue #85 for
the remaining perf work (connection-pool sizing and bulk cluster-member inserts
already landed; time-partitioning and a full load test are open).

**Project structure**

```
raglogs/
├── src/
│   ├── adapters/            Log source adapters (file, cloudwatch, datadog, loki, k8s)
│   ├── api/routes/          FastAPI route handlers
│   ├── api/auth/            API keys, roles, OIDC, bind-host guard
│   ├── cli/commands/        Typer CLI commands
│   ├── clients/             Thin typed HTTP client targeting /v1
│   ├── config/              Pydantic settings
│   ├── core/
│   │   ├── clustering/      Fingerprint grouping, semantic merge, importance scoring
│   │   ├── compare/         Window diffing — new, disappeared, increased, decreased
│   │   ├── embeddings/      Provider abstraction + ingest persist helper
│   │   ├── explain/         Evidence assembly, templates, confidence, summarizer
│   │   ├── ingestion/       Ingestion orchestration, webhooks, batch persistence
│   │   ├── llm/             Provider abstraction (OpenAI, Ollama, noop)
│   │   ├── normalization/   Message normalization, fingerprinting, trigger patterns
│   │   ├── parsing/         JSON and text parsers, field extractors, timestamps
│   │   ├── retrieval/       Semantic + keyword question answering
│   │   ├── retention/       Per-scope TTL, scheduled purge of raw vs summary tiers
│   │   └── timeline/        Causal timeline reconstruction
│   ├── db/                  SQLAlchemy models, session management
│   └── utils/               Time window parsing, hashing helpers
├── migrations/              Alembic migration scripts
├── clients/                 OpenAPI spec + client codegen docs
├── sample_data/             Demo incident logs (deploy, billing, api)
└── tests/
    ├── unit/                Tests — parsers, normalization, clustering, time
    └── integration/         Full ingest → cluster → explain flow (requires DB)
```

**Adding a log source adapter**

New source adapters go in `src/adapters/` and implement `SourceAdapter` (`discover` / `read`), yielding `RawLogLine` objects that the existing parser maps onto `ParsedLogLine`. The normalization, fingerprinting, storage, clustering, and explain pipeline is fully source-agnostic.

**Datadog adapter limits**

- Auth: `DD-API-KEY` + `DD-APPLICATION-KEY` (application key needs `logs_read_data`). Keys come from env only — never CLI `--param`.
- Endpoint: `POST https://api.<site>/api/v2/logs/events/search` with an absolute `from`/`to` window (relative ranges drop events while paginating).
- Pagination: cursor from `meta.page.after`; resume with `--resume-job`.
- Page size: default 1000, Datadog hard max 1000 (`--param page_size=N` or `DATADOG_PAGE_SIZE`).
- Max rows per run: default 10000 (`--param max_rows=N` or `DATADOG_MAX_ROWS`). Hitting the cap saves the next cursor for resume.
- Rate limits: HTTP 429 and 5xx are retried up to 3 times with exponential backoff; a persistent failure marks the job `ADAPTER_UNAVAILABLE` (or `partial: true` if some events already landed).
- Field mapping: Datadog `status` → `level`; `service` / `host` / `message` / `timestamp` pass through; `env` from the `env:` tag or attributes; `trace_id` / `request_id` from nested custom attributes when present. Other nested Datadog attributes are dropped so core parsing stays source-agnostic.

---

## License

MIT
