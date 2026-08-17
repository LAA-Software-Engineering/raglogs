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
- [Roadmap](#roadmap)

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

### `raglogs init`

Initializes local configuration and runs database migrations.

```bash
raglogs init
raglogs init --db-url postgresql+psycopg://user:pass@host/raglogs
raglogs init --no-migrate   # skip migrations
```

---

### `raglogs ingest`

Ingests logs into the database from local files, a pull adapter (CloudWatch, Datadog, Loki), or Kubernetes log exports. File mode supports JSON and plain-text formats, single files, directories, and glob patterns.

```bash
raglogs ingest ./logs/app.log
raglogs ingest ./logs/
raglogs ingest ./logs/*.log
raglogs ingest ./logs/ --recursive
raglogs ingest ./logs/ --service api --env production
raglogs ingest ./logs/ --format json

# CloudWatch Logs (AWS credential chain; no keys on the CLI)
raglogs ingest --adapter cloudwatch --param log_group=/aws/lambda/my-service --since 1h

# Datadog Logs Search API (keys via env; see Configuration)
raglogs ingest --adapter datadog --param query='service:billing-worker status:error' --since 1h
```

**Kubernetes log exports** (`--adapter k8s`) — concatenated `kubectl logs`, Fluent Bit / Vector JSON lines, CRI (kubelet) node logs, `.gz` files, and tarballs. Namespace / pod / container map to environment / host / service.

```bash
# kubectl capture (prefix + timestamps give pod/container and event time)
kubectl logs -n production -l app=billing-worker --all-containers \
  --prefix --timestamps --since=1h > /tmp/billing-export.log
raglogs ingest --adapter k8s /tmp/billing-export.log

# kubelet /var/log/pods dump (path supplies namespace, pod, container)
raglogs ingest --adapter k8s --recursive ./var/log/pods
raglogs ingest --adapter k8s ./node-logs.tar.gz
```

| Flag | Description |
|---|---|
| `--recursive` / `-r` | Recurse into subdirectories |
| `--source-name` | Logical name for this ingestion source |
| `--service` | Default service name when not in logs |
| `--env` | Default environment |
| `--format` | `json`, `text`, or `auto` (default) |
| `--with-embeddings` | Generate vector embeddings (requires embeddings provider) |
| `--adapter` | `file` (default), `cloudwatch`, `datadog`, `loki`, or `k8s` |
| `--param` | Adapter param as `key=value` (repeatable) |
| `--since` | Window for pull adapters, e.g. `30m`, `1h`, `24h` (default last 1h) |
| `--from` / `--to` | Explicit ISO 8601 window bounds (pull adapters) |
| `--resume-job` | Prior ingestion job UUID to resume pagination cursors from |

**Loki**

Pull a bounded window from Grafana Loki via LogQL — no intermediate files.
Auth and the Loki origin come from env (`LOKI_*`); tenant and query can be
passed per ingest. Stream labels are mapped onto `service` / `environment` /
`host` (`app`/`service`/`job`, `namespace`/`env`, `pod`/`instance`).

`query_range`'s `limit` is global across matching streams. Paginating by
advancing `start` to the latest timestamp in a full page can skip earlier
lines from other streams in a wide selector — prefer a narrow LogQL query
when completeness matters.

```bash
export LOKI_URL=http://localhost:3100
raglogs ingest --adapter loki --param query='{app="api"}' --since 1h
raglogs ingest --adapter loki --param query='{namespace="prod"}' \
  --from 2026-03-12T22:00:00+00:00 --to 2026-03-12T22:30:00+00:00
```

```bash
curl -X POST http://localhost:8000/ingestions \
  -H "Content-Type: application/json" \
  -d '{"adapter":"loki","params":{"query":"{app=\"api\"}"},"since":"1h"}'
```

| Param | Description |
|---|---|
| `query` / `queries` | LogQL selector (required unless `LOKI_QUERY` is set) |
| `tenant` | Override `LOKI_TENANT` (`X-Scope-OrgID`) |
| `limit` | Page size for `query_range` (default 5000, Loki max) |

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

### `raglogs timeline`

Reconstructs the causal sequence of events in an incident window. Shows deploys, service restarts, the primary error spike, downstream effects, and system-level symptoms — sorted chronologically and grouped by causal role.

```bash
raglogs timeline --since 30m
raglogs timeline --since 2h
raglogs timeline --from 2026-03-12T22:00:00Z --to 2026-03-12T22:30:00Z
raglogs timeline --since 2h --service billing-worker
raglogs timeline --since 1h --format json
```

| Flag | Description |
|---|---|
| `--since` | Relative window: `30m`, `1h`, `24h`, `7d` |
| `--from` | Start of window (ISO 8601) |
| `--to` | End of window (ISO 8601) |
| `--service` | Filter to one service |
| `--env` | Filter to one environment |
| `--format` | `text` or `json` |

**Event categories**

| Label | Meaning |
|---|---|
| `deploy` | Deploy, release, or rollout event |
| `startup` | Service start or port binding |
| `trigger` | Other pre-error event (config change, migration) |
| `error ↑` | Primary error cluster — the root cause |
| `effect` | Downstream failure caused by the primary error |
| `symptom` | System-level degradation (queue growth, backlog) |

**Output**

```
Incident timeline  2026-03-12 21:58:00 UTC → 2026-03-12 23:58:00 UTC

  21:58:14  deploy     Deploy completed for billing-worker version v2.4.1 · deployment-controller
  21:58:15  startup    Application started billing-worker v2.4.1 on port 8080 · billing-worker

  22:00:10  error ↑    Stripe signature verification failed for endpoint /webhooks/stripe
                       184 events · billing-worker · 49 min span

  22:01:27  effect     POST /api/checkout 200 OK latency=<duration> (high latency detected)
                       25 events · api · 43 min span

  22:01:49  effect     Webhook retries (2 retry events)
                       2 events · billing-worker

  22:02:56  effect     POST /api/checkout 500 Internal Server Error — upstream billing error
                       39 events · api · 45 min span

  22:11:25  symptom    Webhook queue growing, 251 events pending processing
                       2 events · billing-worker · 30 min span
```

Point-in-time events (deploys, startups) show the service inline. Volumetric events show a sub-line with event count, service, and cluster duration. Blank lines separate events more than 60 seconds apart. Repeated webhook retry events are deduplicated into a single line.

No LLM required. The timeline is assembled entirely from cluster timestamps and causal classification.

---

### `raglogs compare`

Diffs two time windows by their cluster sets. Shows exactly which error patterns appeared, disappeared, intensified, or resolved between a current window and a baseline.

```bash
raglogs compare --since 30m --baseline 24h
raglogs compare --since 1h --baseline 7d
raglogs compare --since 2h --baseline 24h --service billing-worker
raglogs compare \
  --window-a-from 2026-03-16T14:00:00Z --window-a-to 2026-03-16T14:30:00Z \
  --window-b-from 2026-03-15T14:00:00Z --window-b-to 2026-03-15T14:30:00Z
raglogs compare --since 30m --baseline 24h --format json
```

`--since 30m --baseline 24h` compares the last 30 minutes against the equivalent 30-minute window from 24 hours ago — the most useful form during an active incident.

| Flag | Description |
|---|---|
| `--since` | Incident window size, e.g. `30m`, `1h` |
| `--baseline` | Offset to baseline window, e.g. `24h`, `7d` |
| `--window-a-from/to` | Explicit start/end for window A (ISO 8601) |
| `--window-b-from/to` | Explicit start/end for window B (ISO 8601) |
| `--service` | Filter both windows to one service |
| `--env` | Filter both windows to one environment |
| `--format` | `text` or `json` |

**Output sections**

| Symbol | Meaning |
|---|---|
| `+` | New cluster — present in A, absent in B |
| `-` | Disappeared — present in B, gone in A |
| `↑` | Increased — in both, count grew by more than 50% |
| `↓` | Decreased — in both, count shrank by more than 50% |
| `+⚡` | New trigger — deploy or restart only seen in A |
| `-⚡` | Dropped trigger — deploy or restart only seen in B |

**Output**

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

Individual webhook retry events (`evt_XXXXXX`) and queue-depth lines are deduplicated into single entries before diffing. No LLM required.

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

Note: `ask` uses structured keyword retrieval, not semantic search. It works without an embeddings provider. Semantic retrieval via pgvector is planned for a future release.

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
| `DB_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/raglogs` | PostgreSQL connection URL |
| `LLM_PROVIDER` | `disabled` | `disabled`, `openai`, `ollama` |
| `LLM_MODEL` | `gpt-4.1-mini` | LLM model name |
| `OPENAI_API_KEY` | _(empty)_ | API key for OpenAI or compatible endpoint |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Base URL for OpenAI-compatible API |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `EMBEDDINGS_PROVIDER` | `disabled` | `disabled`, `openai`, `local`. Semantic cluster merge is skipped when `disabled` |
| `EMBEDDINGS_MODEL` | `text-embedding-3-small` | Embeddings model name |
| `EMBEDDINGS_DIMENSIONS` | `1536` | Vector size passed to the OpenAI embeddings API |
| `CLUSTER_MERGE_SIMILARITY_THRESHOLD` | `0.92` | Cosine similarity at or above which fingerprint clusters merge. High on purpose so distinct errors stay separate |
| `CLUSTER_MERGE_MIN_COUNT` | `1` | Minimum `count` for a cluster to participate in a merge |
| `DEFAULT_BASELINE_WINDOW` | `24h` | How far back to compare for baseline |
| `MAX_CLUSTERS_FOR_EXPLAIN` | `10` | Max clusters sent to the explain pipeline |
| `MAX_EVIDENCE_ITEMS` | `8` | Max evidence lines in output |
| `ADAPTER_CLOUDWATCH_REGION` | `us-east-1` | AWS region for `--adapter cloudwatch` |
| `DATADOG_API_KEY` | _(empty)_ | Datadog API key (`logs_read_data`) |
| `DATADOG_APP_KEY` | _(empty)_ | Datadog application key |
| `DATADOG_SITE` | `datadoghq.com` | Datadog site (`us3.datadoghq.com`, `datadoghq.eu`, …) |
| `DATADOG_PAGE_SIZE` | `1000` | Logs per Datadog page (API max 1000) |
| `DATADOG_MAX_ROWS` | `10000` | Max events pulled in one Datadog ingest run |

---

## LLM integration

raglogs is fully useful without any LLM. The `--no-llm` flag (or `LLM_PROVIDER=disabled`) activates deterministic template-based summaries.

When an LLM is configured, it receives only a small curated evidence packet — not raw logs. The prompt enforces fixed output structure, prohibits fabrication, and requires explicit uncertainty statements when evidence is insufficient.

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
Semantic merge (optional)
(embed cluster representatives; merge pairs with cosine ≥ threshold)
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
Incident Summary · Timeline · Diff
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

### Semantic cluster merging

Fingerprinting can still split one incident across multiple clusters when wording differs enough that normalized templates diverge (for example two Stripe error strings that mean the same failure). After fingerprint grouping, raglogs can embed each cluster's representative message and merge near-duplicates.

- **Disabled (default).** `EMBEDDINGS_PROVIDER=disabled` skips the merge pass entirely. Clustering is fingerprint-only and deterministic — the same logs always produce the same clusters.
- **Enabled.** With `openai` or `local`, representatives are embedded at analysis time (in memory; not written to pgvector). Pairs with cosine similarity ≥ `CLUSTER_MERGE_SIMILARITY_THRESHOLD` (default **0.92**) are merged via connected components. Merged `count` is the sum of member counts; services and levels are summed; `first_seen` is the earliest timestamp and `last_seen` the latest; importance is recomputed. The canonical fingerprint is the member with the highest importance score. `ClusterRun.algorithm` is `fingerprint+semantic` when embeddings were used, even if no pair crossed the threshold.
- **Fail open.** If the embeddings backend is missing, raises, or returns unusable vectors, clustering continues with the fingerprint-only set.
- **Not in this release.** Semantic `ask` and similar-incident search over historical clusters remain separate work. Compare still applies its own heuristic collapse for webhook retries / queue growth after clustering.

Local embeddings require the optional extra: `pip install 'raglogs[local-embeddings]'` (`sentence-transformers`). If that import fails, merge is skipped.

### Baseline comparison

For every cluster in the incident window, raglogs computes a change ratio against the baseline window:

```
change_ratio = (current_count + 1) / (baseline_count + 1)
```

A cluster that fires 200 times and usually fires 180 is probably normal. A cluster that fires 5 times but has never appeared before has a change ratio of 6 and ranks much higher. The smoothing term prevents divide-by-zero explosions on new clusters.

Default baseline window is the 24 hours before the incident window. Configurable with `--baseline-window` or `DEFAULT_BASELINE_WINDOW`.

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

### Timeline reconstruction

`raglogs timeline` assembles events into three causal buckets without any ML or LLM:

1. **Pre-error** — trigger candidates (deploys, startups) sorted by timestamp
2. **Error** — the primary cluster at its first occurrence
3. **Post-error** — secondary clusters (effects, symptoms) sorted by first occurrence

Secondary clusters are classified by message content: queue/backlog growth becomes `symptom`, 500 errors and latency spikes become `effect`. Repeated webhook retry events (individual `evt_XXXXXX` lines) are deduplicated into a single count. Effects that appear to have started before the primary error — due to data noise — are floored to the primary's first occurrence to preserve causal ordering.

### Window diffing

`raglogs compare` runs clustering independently on both windows, then diffs the resulting fingerprint sets. Before diffing, each cluster set is collapsed: all `evt_XXXXXX` retry clusters merge into a single entry, and all queue-depth lines merge into one. The collapsed maps are then diffed by fingerprint, with counts compared to determine direction (new, disappeared, increased, decreased). Trigger candidates are normalized by message prefix to handle version strings, so `v2.4.1` and `v2.3.9` both resolve as "deploy" without creating spurious diffs.

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
| `POST` | `/ingestions` | Enqueue an ingest job (`adapter`: `file`, `cloudwatch`, or `datadog`) |
| `GET` | `/ingestions` | List recent completed ingestion jobs, newest first |
| `GET` | `/ingestions/{job_id}` | Poll ingestion job status |
| `GET` | `/ingestions/latest` | ID of the most recently completed ingestion job, if any |
| `POST` | `/query/explain` | Explain a time window |
| `POST` | `/query/ask` | Answer a natural language question |
| `POST` | `/query/clusters` | List top clusters |
| `POST` | `/query/timeline` | Reconstruct incident timeline for a window |
| `POST` | `/query/compare` | Diff two time windows (same semantics as `raglogs compare`) |
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

**Timeline** — `POST /query/timeline` accepts the same window filters as the CLI (`since` or `from_time`/`to_time`, optional `service`, `env`, `all_ingestions`, `ingestion_job_id`). Set `"format": "text"` to include a plain-text `text` field alongside `events`.

```bash
curl -X POST http://localhost:8000/query/timeline \
  -H "Content-Type: application/json" \
  -d '{"since": "2h", "format": "json"}'
```

**Compare** — `POST /query/compare` matches `raglogs compare`: either `"since"` + `"baseline"` (durations, window A ends at request time) or explicit `window_a_from` / `window_a_to` / `window_b_from` / `window_b_to`. Optional `"format": "text"` adds a rendered `text` field.

```bash
curl -X POST http://localhost:8000/query/compare \
  -H "Content-Type: application/json" \
  -d '{"since": "30m", "baseline": "24h"}'
```

---

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

Pick a time window (presets or a duration like `2h`), then switch between the
**Explain**, **Timeline**, **Compare**, and **Ask** tabs. The **ingestion**
dropdown in the top bar lists your 25 most recent completed ingestions (via
`GET /ingestions`) and defaults to the latest one, matching the CLI; pick a
different ingestion or "All ingestions" to change what a query is scoped to.

The UI is server-rendered (Jinja2 + vanilla JS/CSS, no CORS, no node/npm) and
calls the same `/query/*` JSON endpoints listed above.

There is no authentication — fine for local dev. If you deploy raglogs
anywhere reachable outside your machine, put it behind a reverse proxy
(nginx, Caddy, etc.) that handles auth.

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
│   ├── adapters/            Log source adapters (file, cloudwatch, datadog, loki, k8s)
│   ├── api/routes/          FastAPI route handlers
│   ├── cli/commands/        Typer CLI commands
│   ├── config/              Pydantic settings
│   ├── core/
│   │   ├── clustering/      Fingerprint grouping, semantic merge, importance scoring
│   │   ├── compare/         Window diffing — new, disappeared, increased, decreased
│   │   ├── embeddings/      Provider abstraction (OpenAI, local, disabled)
│   │   ├── explain/         Evidence assembly, templates, confidence, summarizer
│   │   ├── ingestion/       Ingestion orchestration and batch persistence
│   │   ├── llm/             Provider abstraction (OpenAI, Ollama, noop)
│   │   ├── normalization/   Message normalization, fingerprinting, trigger patterns
│   │   ├── parsing/         JSON and text parsers, field extractors, timestamps
│   │   ├── retrieval/       Keyword-based question answering
│   │   └── timeline/        Causal timeline reconstruction
│   ├── db/                  SQLAlchemy models, session management
│   └── utils/               Time window parsing, hashing helpers
├── migrations/              Alembic migration scripts
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

## Roadmap

- Markdown incident report export (`raglogs explain --format markdown > postmortem.md`)

---

## License

MIT
