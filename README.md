# raglogs

Ask your logs what happened.

```bash
$ raglogs explain --since 30m

Incident summary

Window: 22:00 → 22:30
Services affected: api, billing-worker
Primary issue: Stripe signature verification failed
Likely trigger: Deploy of billing-worker v2.4.1

Evidence:
- 184 similar errors
- first occurrence 2m after deploy
- endpoint '/webhooks/stripe' in 100% of failures

Confidence: medium-high
```

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

- [The killer command](#the-killer-command)
- [Why raglogs](#why-raglogs)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Commands](#commands)
- [Configuration](#configuration)
- [LLM integration](#llm-integration)
- [Log formats](#log-formats)
- [How it works](#how-it-works)
- [HTTP API](#http-api)
- [Development](#development)
- [Roadmap](#roadmap)

---

## The killer command

```bash
raglogs explain --since 30m
```

```
Incident summary

Window: 2026-03-12 22:00:00 UTC → 2026-03-12 22:30:00 UTC
Services affected: api, billing-worker
Primary issue: Stripe signature verification failed for endpoint /webhooks/stripe
Secondary effects: POST /api/checkout 500 Internal Server Error — upstream billing error (39 events)
Likely trigger: Deploy completed for billing-worker v2.4.1 at 21:58:15 UTC

Evidence:
- 184 similar errors in billing-worker
- No comparable error volume in prior 24h baseline
- First error spike occurred 2m after deploy trigger
- Endpoint '/webhooks/stripe' referenced in 100% of primary failures
- 39 checkout 500s in api began after webhook error spike

Confidence: medium-high
```

This output is deterministic. No LLM required.

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
git clone https://github.com/leo-aa88/raglogs
cd raglogs
pip install -e .

# Start Postgres with pgvector
docker compose up postgres -d

# Initialize schema
raglogs init

# Run the demo
raglogs ingest ./sample_data/sample_incident
raglogs explain --since 1h
```

Or with Make:

```bash
make demo
```

---

## Installation

**Requirements**

- Python 3.11+
- PostgreSQL 14+ with the [pgvector](https://github.com/pgvector/pgvector) extension
- Docker (optional, for the bundled Compose setup)

**Install**

```bash
pip install -e .
```

**Configure**

```bash
cp .env.example .env
# Edit .env — set RAGLOGS_DB_URL at minimum
```

**Initialize the database**

```bash
raglogs init
```

This runs Alembic migrations and creates all required tables, including the `vector` extension for pgvector.

---

## Commands

### `raglogs init`

Initializes local configuration and runs database migrations.

```bash
raglogs init
raglogs init --db-url postgresql+psycopg://user:pass@host/raglogs
raglogs init --no-migrate   # skip migrations
```

---

### `raglogs ingest`

Ingests one or more log files into the database. Supports JSON and plain-text formats, single files, directories, and glob patterns.

```bash
raglogs ingest ./logs/app.log
raglogs ingest ./logs/
raglogs ingest ./logs/*.log
raglogs ingest ./logs/ --recursive
raglogs ingest ./logs/ --service api --env production
raglogs ingest ./logs/ --format json
```

| Flag | Description |
|---|---|
| `--recursive` / `-r` | Recurse into subdirectories |
| `--source-name` | Logical name for this ingestion source |
| `--service` | Default service name when not in logs |
| `--env` | Default environment |
| `--format` | `json`, `text`, or `auto` (default) |
| `--with-embeddings` | Generate vector embeddings (requires embeddings provider) |

**Output**

```
Ingestion complete

Files processed:   3
Lines read:        464
Parsed logs:       461
Skipped/errors:    3
Services detected: api, billing-worker, deployment-controller
Duration:          0.4s
```

---

### `raglogs explain`

The main command. Analyzes a time window, clusters the logs, compares against a baseline, and produces a structured incident summary.

```bash
raglogs explain --since 30m
raglogs explain --since 2h --service billing-worker
raglogs explain --from 2026-03-12T22:00:00Z --to 2026-03-12T22:30:00Z
raglogs explain --since 1h --no-llm
raglogs explain --since 1h --format json
raglogs explain --since 1h --format markdown
raglogs explain --since 1h --baseline-window 7d
```

| Flag | Description |
|---|---|
| `--since` | Relative window: `30m`, `1h`, `24h`, `7d` |
| `--from` | Start of window (ISO 8601) |
| `--to` | End of window (ISO 8601) |
| `--service` | Filter to one service |
| `--env` | Filter to one environment |
| `--no-llm` | Skip LLM, use deterministic templates |
| `--max-clusters` | Max clusters to analyze (default: 10) |
| `--baseline-window` | How far back to compare (default: `24h`) |
| `--format` | `text`, `json`, or `markdown` |

**Output structure**

```
Incident summary

Window: ...
Services affected: ...
Primary issue: ...
Secondary effects: ...
Likely trigger: ...

Evidence:
- ...

Confidence: low | medium | medium-high | high
```

Confidence is computed from cluster volume, baseline change ratio, trigger correlation, secondary cluster agreement, and service spread. It is never invented.

No-LLM mode produces the same structure from deterministic templates. Slightly less polished, zero hallucination risk, works fully offline.

---

### `raglogs clusters`

Lists the top log clusters in a time window ranked by importance score. Useful for exploration and understanding dominant event families without running a full explain.

```bash
raglogs clusters --since 1h
raglogs clusters --since 30m --service api
raglogs clusters --since 1h --top 20
raglogs clusters --since 1h --format json
```

**Example output**

```
Top clusters — 2026-03-12 22:00:00 UTC → 2026-03-12 23:00:00 UTC
3 clusters found

 #   Count   Chg    Level   Service(s)           Message
 1   184     184x   error   billing-worker       Stripe signature verification failed for endpoint /webhooks/stripe
 2    39      39x   error   api                  POST /api/checkout 500 Internal Server Error — upstream billing error
 3    10      1.0x  info    deployment-ctrl      Deploy completed for billing-worker version <token> ⚡

⚡ = likely trigger event   Chg = change vs baseline
```

| Flag | Description |
|---|---|
| `--since` | Relative window |
| `--from` / `--to` | Explicit range |
| `--service` | Filter by service |
| `--env` | Filter by environment |
| `--top` / `-n` | Number of clusters to show (default: 15) |
| `--format` | `text` or `json` |

---

### `raglogs ask`

Answer a natural language question about your logs using structured keyword retrieval.

```bash
raglogs ask "why did login fail?"
raglogs ask "what changed before latency increased?" --since 2h
raglogs ask "what happened in billing?" --since 1h
raglogs ask "why are checkouts failing?" --format json
```

**Example output**

```
Most likely cause related to 'why did the webhook fail?':
Stripe signature verification failed for endpoint /webhooks/stripe

In service: billing-worker

Evidence:
- 184 events: 'Stripe signature verification failed...' in billing-worker
- 39 events: 'POST /api/checkout 500...' in api

Total matching log events: 184
```

Note: `ask` uses structured keyword retrieval, not semantic search. It works without an embeddings provider. Semantic retrieval via pgvector is planned for Phase 2.

---

### `raglogs status`

Shows database connectivity, log counts, and provider status.

```bash
raglogs status
```

```
Database:         connected
Log entries:      464
Sources:          1
Ingestion jobs:   1

LLM provider:     disabled
LLM model:        gpt-4.1-mini
Embeddings:       disabled
```

---

### `raglogs config`

Inspect the current effective configuration.

```bash
raglogs config         # show all
raglogs config llm_provider
```

---

## Configuration

All settings are read from `.env`, environment variables, or CLI flags. Priority: CLI > env var > `.env` file > defaults.

| Variable | Default | Description |
|---|---|---|
| `RAGLOGS_DB_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/raglogs` | PostgreSQL connection URL |
| `RAGLOGS_LLM_PROVIDER` | `disabled` | `disabled`, `openai`, `ollama` |
| `RAGLOGS_LLM_MODEL` | `gpt-4.1-mini` | LLM model name |
| `RAGLOGS_OPENAI_API_KEY` | _(empty)_ | API key for OpenAI or compatible endpoint |
| `RAGLOGS_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compatible API |
| `RAGLOGS_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `RAGLOGS_EMBEDDINGS_PROVIDER` | `disabled` | `disabled`, `openai`, `local` |
| `RAGLOGS_EMBEDDINGS_MODEL` | `text-embedding-3-small` | Embeddings model name |
| `RAGLOGS_DEFAULT_BASELINE_WINDOW` | `24h` | How far back to compare for baseline |
| `RAGLOGS_MAX_CLUSTERS_FOR_EXPLAIN` | `10` | Max clusters sent to the explain pipeline |
| `RAGLOGS_MAX_EVIDENCE_ITEMS` | `8` | Max evidence lines in output |

---

## LLM integration

raglogs is fully useful without any LLM. The `--no-llm` flag (or `RAGLOGS_LLM_PROVIDER=disabled`) activates deterministic template-based summaries.

When an LLM is configured, it receives only a small curated evidence packet — not raw logs. The prompt enforces fixed output structure, prohibits fabrication, and requires explicit uncertainty statements when evidence is insufficient.

### OpenAI

```env
RAGLOGS_LLM_PROVIDER=openai
RAGLOGS_LLM_MODEL=gpt-4.1-mini
RAGLOGS_OPENAI_API_KEY=sk-...
```

### Ollama (fully local)

```env
RAGLOGS_LLM_PROVIDER=ollama
RAGLOGS_LLM_MODEL=llama3
RAGLOGS_OLLAMA_BASE_URL=http://localhost:11434
```

### Any OpenAI-compatible endpoint

```env
RAGLOGS_LLM_PROVIDER=openai
RAGLOGS_OPENAI_BASE_URL=http://localhost:1234/v1
RAGLOGS_OPENAI_API_KEY=not-required
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

```
Log Files
    │
    ▼
File Adapter
(discover files, detect format, read lines)
    │
    ▼
Parser
(JSON / text, field aliases, timestamp normalization)
    │
    ▼
Normalization
(replace: UUIDs, IPs, emails, tokens, numeric IDs, paths, timestamps)
(preserve: endpoint names, status codes, exception names, service names)
    │
    ▼
Fingerprinting
(SHA-256 of normalized message → stable 16-char cluster key)
    │
    ▼
PostgreSQL + pgvector
(indexed on timestamp, service, environment, fingerprint)
    │
    ▼
Clustering
(group by fingerprint → count, services, levels, first/last seen)
    │
    ▼
Baseline Comparison
(compare current window to prior window, compute change ratio)
    │
    ▼
Importance Ranking
(severity weight + log(count) + log(change ratio) + service spread + trigger correlation)
    │
    ▼
Evidence Assembly
(trigger detection, timing correlation, primary + secondary cluster selection)
    │
    ▼
LLM (optional) or Deterministic Templates
    │
    ▼
Incident Summary
```

### Normalization

Normalization is the most important step for clustering quality. It strips dynamic values from log messages so semantically identical events get the same fingerprint regardless of which specific user ID, request ID, or IP address was involved.

| Raw message | Normalized |
|---|---|
| `User 12345 failed login from 192.168.1.1` | `User <id> failed login from <ip>` |
| `Request req_abc123 timed out after 3000ms` | `Request <*>=<*> timed out after <duration>` |
| `Processing job 550e8400-e29b-41d4-a716-446655440000` | `Processing job <uuid>` |
| `GET /api/users?page=2&limit=50 200 OK` | `GET /api/users?<params> 200 OK` |

Things deliberately **not** normalized: endpoint paths, HTTP status codes, exception class names, service names, operation names.

### Baseline comparison

For every cluster in the incident window, raglogs computes a change ratio against the baseline window:

```
change_ratio = (current_count + 1) / (baseline_count + 1)
```

A cluster that fires 200 times and usually fires 180 is probably normal. A cluster that fires 5 times but has never appeared before has a change ratio of 6 and ranks much higher. The smoothing term prevents divide-by-zero explosions on new clusters.

Default baseline window is the 24 hours before the incident window. Configurable with `--baseline-window` or `RAGLOGS_DEFAULT_BASELINE_WINDOW`.

### Trigger detection

raglogs scans for log messages matching known trigger patterns in the minutes before the primary error cluster begins. Matched patterns include:

- Deploy started / completed
- Application or service restart
- Pod restart / eviction  
- Configuration reloaded
- Migration started / completed
- Queue saturation
- Circuit breaker open
- Webhook secret or config mismatch
- Auth token expiration bursts

A trigger candidate is promoted to "likely trigger" when it precedes the primary error spike and shares the same or an adjacent service.

### Confidence scoring

Confidence is derived from measurable signals, not from LLM output:

- Cluster volume (more events → higher confidence)
- Baseline change ratio (larger spike → higher confidence)
- Presence of a trigger candidate
- Secondary cluster corroboration
- Multi-service spread
- Total log volume in window

Possible values: `low`, `medium`, `medium-high`, `high`.

---

## HTTP API

raglogs exposes a FastAPI server for integrations and future tooling.

```bash
uvicorn raglogs.api.app:app --host 0.0.0.0 --port 8000 --reload
# or
make api
```

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service and DB health check |
| `POST` | `/ingestions` | Ingest log files |
| `GET` | `/ingestions/{job_id}` | Poll ingestion job status |
| `POST` | `/query/explain` | Explain a time window |
| `POST` | `/query/ask` | Answer a natural language question |
| `POST` | `/query/clusters` | List top clusters |
| `GET` | `/config` | Read effective configuration |

**Example**

```bash
curl -X POST http://localhost:8000/query/explain \
  -H "Content-Type: application/json" \
  -d '{"since": "30m", "no_llm": true}'
```

```json
{
  "window": {"start": "2026-03-12T22:00:00Z", "end": "2026-03-12T22:30:00Z"},
  "summary": "Incident summary\n\nWindow: ...",
  "confidence": "medium-high",
  "mode": "rules",
  "total_logs": 464,
  "services_affected": ["api", "billing-worker"],
  "primary_cluster": {
    "message": "Stripe signature verification failed for endpoint /webhooks/stripe",
    "count": 184,
    "baseline_count": 0,
    "change_ratio": 185.0
  },
  "evidence": ["184 similar errors in billing-worker", "..."]
}
```

---

## Development

```bash
# Install everything
pip install -r requirements.txt && pip install -e .

# Unit tests (no DB needed)
make test-unit

# Integration tests (requires running Postgres)
make test-int

# API with hot reload
make api

# Lint / format
make lint
make format

# Full clean
make clean
```

**Project structure**

```
raglogs/
├── src/
│   ├── adapters/file/       File discovery and line reading
│   ├── api/routes/          FastAPI route handlers
│   ├── cli/commands/        Typer CLI commands
│   ├── config/              Pydantic settings
│   ├── core/
│   │   ├── clustering/      Fingerprint grouping, importance scoring, baseline
│   │   ├── explain/         Evidence assembly, templates, confidence, summarizer
│   │   ├── ingestion/       Ingestion orchestration and batch persistence
│   │   ├── llm/             Provider abstraction (OpenAI, Ollama, noop)
│   │   ├── normalization/   Message normalization, fingerprinting, trigger patterns
│   │   ├── parsing/         JSON and text parsers, field extractors, timestamps
│   │   └── retrieval/       Keyword-based question answering
│   ├── db/                  SQLAlchemy models, session management
│   └── utils/               Time window parsing, hashing helpers
├── migrations/              Alembic migration scripts
├── sample_data/             Demo incident logs (deploy, billing, api)
└── tests/
    ├── unit/                49 tests — parsers, normalization, clustering, time
    └── integration/         Full ingest → cluster → explain flow (requires DB)
```

**Adding a log source adapter**

New source adapters go in `raglogs/adapters/`. Each adapter yields `ParsedLogLine` objects. The normalization, fingerprinting, storage, clustering, and explain pipeline is fully source-agnostic.

---

## Roadmap

**Phase 2 — Runtime polish**
- Docker Compose with API + background worker
- DB-backed job queue and async ingestion
- Cached cluster runs and explanation persistence
- Improved config validation and error messages

**Phase 3 — Connectors and richer analysis**
- Datadog adapter
- Loki adapter
- Kubernetes log export ingestion
- Semantic cluster merging via pgvector
- `raglogs compare` — diff two time windows
- Markdown report export
- Incident timeline visualization
- Web UI

---

## License

MIT
