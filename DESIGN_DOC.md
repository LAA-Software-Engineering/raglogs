# Design Document: Self-Hosted Open Source Log Explanation Tool

## 1. Document Purpose

This document specifies the MVP for an open source, self-hosted developer tool that ingests application logs and lets users query them in natural language, with a primary focus on **bounded-window incident explanation with evidence**.

This is not a generic "chat with logs" product.

This is an **incident explanation tool**.

Core product promise:

> **Ask your logs what happened, and get a root-cause summary with evidence.**

This document is written to be implementation-ready for an AI coding agent or engineer.

---

## 2. Product Summary

### Working concept

The tool ingests logs from files and other future sources, stores both structured metadata and semantic representations, clusters similar events, detects meaningful changes over a specified time window, and generates short evidence-based explanations.

### Core differentiator

Most log tools are good at:

* search
* filtering
* dashboards

This tool should be good at:

* narrative
* correlation
* evidence
* bounded incident explanation

### MVP killer command

```bash
raglogs explain --since 30m
```

Expected output shape:

```text
Incident summary

Window: 22:00 → 22:30
Services affected: api, billing-worker
Primary error cluster: Stripe signature verification failed
Secondary effects: retry queue growth, checkout 500s
Likely trigger: deploy of billing-worker at 21:58

Evidence:
- 184 similar errors in billing-worker
- first occurrence 2m after deploy
- same webhook endpoint affected in 96% of failures
- no comparable error volume before deploy
```

That output is the product's main hook.

---

## 3. Goals and Non-Goals

## 3.1 Goals

The MVP must:

1. Ingest local log files
2. Support JSON logs and plain text logs
3. Parse and normalize logs into a structured store
4. Fingerprint similar log lines
5. Cluster log events within a bounded window
6. Rank clusters by likely importance
7. Provide evidence-based explanations
8. Support natural-language Q&A over logs
9. Be fully self-hostable
10. Be open source under MIT
11. Work with optional LLMs and optional embeddings
12. Provide useful output even with no LLM configured

## 3.2 Non-Goals for MVP

The MVP does not need to:

1. Replace Datadog, ELK, Grafana, or Sentry
2. Be a full observability suite
3. Support tracing/metrics in depth
4. Perform real-time stream processing at scale
5. Build a complex RBAC/multi-tenant SaaS layer
6. Have a polished web UI
7. Support massive distributed ingestion on day one
8. Guarantee perfect root cause detection
9. Handle every log format automatically
10. Train custom ML anomaly models

---

## 4. Product Positioning

### Wrong positioning

* "RAG for logs"
* "Vector search for logs"
* "AI log search"

### Correct positioning

* "Ask your logs what happened"
* "Explain production incidents from logs"
* "Root-cause summaries from logs with evidence"
* "Bounded-window incident explanation for developers"

### Target user

Initial target users:

* backend engineers
* DevOps / platform engineers
* SREs
* indie hackers running self-hosted systems
* startup teams with messy logs but no formal incident tooling

### Ideal first use case

A developer has a folder of production logs after an incident and wants to answer:

* What changed?
* What failed first?
* Which service was affected?
* What looks like the likely trigger?
* What evidence supports that conclusion?

---

## 5. MVP User Stories

### Story 1: Ingest logs from files

As a developer, I want to ingest one or more local log files so I can analyze them later.

Example:

```bash
raglogs ingest ./logs/*.log
```

### Story 2: Explain a time-bounded incident

As a developer, I want to ask what happened during the last 30 minutes so I get a short, evidence-based summary.

Example:

```bash
raglogs explain --since 30m
```

### Story 3: Ask a natural language question

As a developer, I want to ask a question like "why did login fail?" so the system can retrieve relevant evidence and summarize likely causes.

Example:

```bash
raglogs ask "why did login fail?"
```

### Story 4: Inspect top clusters

As a developer, I want to see the top log clusters in a window so I can understand the dominant events.

Example:

```bash
raglogs clusters --since 1h
```

### Story 5: Work without an LLM

As a developer, I want useful structured outputs even if I do not configure an LLM or external API.

Example:

```bash
raglogs explain --since 1h --no-llm
```

---

## 6. High-Level Architecture

## 6.1 Main components

* **CLI**: Typer
* **API**: FastAPI
* **Database**: PostgreSQL + pgvector
* **Job system**: DB-backed jobs table + worker process
* **Embeddings provider**: pluggable
* **LLM provider**: pluggable
* **Parser layer**: source adapters
* **Core analysis engine**: normalization, fingerprinting, clustering, ranking, explanation input assembly

## 6.2 Architecture overview

```text
Log Sources
  ↓
Ingestion Layer
  ↓
Parsing / Normalization
  ↓
Structured Log Store (Postgres)
  ↓
Fingerprinting / Clustering / Ranking
  ↓
Semantic Layer (pgvector)
  ↓
Retriever / Evidence Builder
  ↓
LLM (optional)
  ↓
Deterministic Incident Summary
```

## 6.3 Design principles

1. Structured fields first, semantic retrieval second
2. Clustering before LLM
3. Evidence before explanation
4. Deterministic output format
5. LLM optional, not mandatory
6. Self-hosted by default
7. Simple deployment path
8. Reasonable local-first experience

---

## 7. Technical Stack

## 7.1 Language

Python 3.11+

## 7.2 Core libraries

* **Typer** for CLI
* **FastAPI** for HTTP API
* **SQLAlchemy 2.x** for ORM / persistence
* **Alembic** for migrations
* **psycopg** or asyncpg for Postgres connectivity
* **pgvector** for vector storage and similarity search
* **Pydantic v2** for schema validation
* **Uvicorn** for ASGI server
* **Rich** for CLI output
* **orjson** for faster JSON handling
* **httpx** for external HTTP integrations
* **tenacity** for retry logic
* **numpy / scikit-learn** for clustering and similarity
* **python-dateutil** for parsing relative time windows
* **structlog** or standard logging for internal app logs

## 7.3 Optional model providers

Embeddings:

* OpenAI-compatible endpoint
* local sentence-transformers model
* Ollama embeddings if supported later

LLM:

* OpenAI-compatible chat/completions endpoint
* Ollama / local model
* no-LLM mode

---

## 8. MVP Scope

## 8.1 Included in MVP

### Ingestion

* local file ingestion
* recursive folder ingestion optional
* JSON logs
* plain text logs
* line-by-line ingestion
* source tracking

### Parsing

* basic JSON field extraction
* plain-text line parsing
* timestamp extraction
* service/environment/level extraction where possible
* fallback behavior for unknown formats

### Analysis

* normalization
* fingerprinting
* cluster generation
* cluster stats
* window filtering
* baseline comparison
* evidence selection
* incident explanation
* natural language retrieval

### CLI

* init
* ingest
* ask
* explain
* clusters
* config
* status

### API

* health
* ingest
* query
* explain
* clusters
* jobs

### Storage

* structured logs
* embeddings
* ingestion jobs
* clusters/results cache
* config

### Deployment

* docker compose
* local dev mode
* database migrations
* sample dataset

## 8.2 Excluded from MVP but planned later

* Datadog adapter
* Loki adapter
* MongoDB adapter
* Postgres-table adapter
* Kubernetes adapter
* web UI
* report exports
* timeline charts
* compare windows command
* Slack integration
* alerting

---

## 9. Core Product Behaviors

## 9.1 `explain` command behavior

This is the most important behavior in the product.

### Inputs

* bounded time window (`--since`, `--from/--to`)
* optional service filter
* optional environment filter
* optional source filter
* optional no-LLM mode
* optional max clusters
* optional output format (text/json/markdown)

### Processing pipeline

1. Resolve the requested time window
2. Retrieve logs in the window using structured filters
3. Normalize and fingerprint messages
4. Group logs into clusters by fingerprint and similarity
5. Rank clusters by importance
6. Compare against baseline window when available
7. Correlate cluster events with metadata
8. Assemble evidence packets
9. Generate summary:

   * with LLM if configured
   * deterministic rules-only summary if no LLM
10. Output in a stable format

### Output requirements

The output must be:

* short
* evidence-based
* deterministic in structure
* useful even when uncertainty exists

### Output structure

```text
Incident summary

Window: ...
Services affected: ...
Primary error cluster: ...
Secondary effects: ...
Likely trigger: ...

Evidence:
- ...
- ...
- ...

Confidence: low | medium | high
```

### Confidence rules

Confidence is based on:

* cluster volume
* change from baseline
* metadata consistency
* timing correlation
* presence of trigger events (deploy/restart/config change)
* retrieval agreement if LLM is used

This should not be fake certainty. If uncertain, say so.

---

## 9.2 `ask` command behavior

This is a secondary feature, not the main hook.

Example:

```bash
raglogs ask "why did login fail?"
```

### Behavior

1. Convert question into retrieval strategy
2. Use structured search heuristics first:

   * match keywords
   * infer service names
   * infer severity bias
3. Retrieve related log clusters and raw examples
4. Optionally use embeddings for semantic expansion
5. Build evidence set
6. Return concise answer with references to services, time patterns, and clusters

### Output shape

```text
Most likely cause of login failures:
database timeout in auth-service

Evidence:
- 73 timeout errors in auth-service
- failures began at 14:03
- related 500s observed in login endpoint
- no similar pattern in the prior 6h baseline
```

---

## 9.3 `clusters` command behavior

Example:

```bash
raglogs clusters --since 1h
```

### Behavior

List the highest-impact clusters in the window.

For each cluster show:

* cluster ID
* representative normalized message
* count
* service(s)
* first seen
* last seen
* severity distribution
* change vs baseline if available

This is both a debugging tool and a no-LLM fallback.

---

## 10. Data Model

## 10.1 Conceptual model

There are two core data layers:

### Structured store

Stores parsed logs and metadata for filtering, ranking, and baseline comparison.

### Semantic store

Stores embeddings for normalized messages or cluster summaries.

Do not rely only on vectors. Structured filtering is primary.

---

## 10.2 Database entities

### `sources`

Represents logical ingestion sources.

Fields:

* `id`
* `name`
* `type` (`file`, future: `datadog`, `loki`, etc.)
* `config_json`
* `created_at`

### `ingestion_jobs`

Tracks ingestion jobs.

Fields:

* `id`
* `source_id`
* `status` (`pending`, `running`, `completed`, `failed`)
* `started_at`
* `finished_at`
* `file_count`
* `line_count`
* `error_count`
* `metadata_json`

### `log_entries`

Main structured log table.

Fields:

* `id` (UUID or bigserial)
* `source_id`
* `ingestion_job_id`
* `timestamp`
* `service`
* `environment`
* `level`
* `trace_id`
* `request_id`
* `host`
* `raw_message`
* `normalized_message`
* `fingerprint`
* `parser_type`
* `extra_json`
* `created_at`

Indexes:

* timestamp
* service
* environment
* level
* trace_id
* request_id
* fingerprint
* composite `(timestamp, service)`

### `log_embeddings`

Stores per-message embeddings.

Fields:

* `id`
* `log_entry_id`
* `embedding vector(...)`
* `model_name`
* `created_at`

### `cluster_runs`

Tracks cluster analyses per window.

Fields:

* `id`
* `window_start`
* `window_end`
* `service_filter`
* `environment_filter`
* `algorithm`
* `status`
* `created_at`

### `clusters`

Represents cluster outputs.

Fields:

* `id`
* `cluster_run_id`
* `cluster_key`
* `representative_message`
* `fingerprint`
* `count`
* `services_json`
* `levels_json`
* `first_seen`
* `last_seen`
* `baseline_count`
* `change_ratio`
* `importance_score`
* `cluster_summary`
* `created_at`

### `cluster_members`

Many-to-one mapping between logs and clusters.

Fields:

* `id`
* `cluster_id`
* `log_entry_id`

### `cluster_embeddings`

Optional semantic embeddings for cluster summaries.

Fields:

* `id`
* `cluster_id`
* `embedding vector(...)`
* `model_name`

### `explanations`

Caches generated explanations.

Fields:

* `id`
* `window_start`
* `window_end`
* `service_filter`
* `environment_filter`
* `mode` (`llm`, `rules`)
* `prompt_hash`
* `result_text`
* `result_json`
* `confidence`
* `created_at`

### `app_config`

Key-value configuration table.

Fields:

* `key`
* `value_json`

---

## 10.3 Suggested SQL schema notes

Use:

* `TIMESTAMPTZ` for timestamps
* `JSONB` for flexible metadata
* `VECTOR(n)` for embeddings
* `GIN` indexes on JSONB only if necessary later
* B-tree indexes for timestamp/service/fingerprint

---

## 11. Log Parsing and Normalization

## 11.1 Supported input formats in MVP

### JSON logs

Examples:

```json
{"timestamp":"2026-03-12T22:01:10Z","level":"error","service":"billing-worker","message":"Stripe signature verification failed for endpoint /webhooks/stripe"}
```

### Plain text logs

Examples:

```text
2026-03-12T22:01:10Z ERROR billing-worker Stripe signature verification failed for endpoint /webhooks/stripe
```

## 11.2 Parsing strategy

### JSON logs

Attempt to extract common fields:

* timestamp
* level
* message
* service
* environment
* trace_id
* request_id
* host

Support common aliases:

* timestamp fields: `timestamp`, `ts`, `time`, `@timestamp`
* message fields: `message`, `msg`, `log`
* level fields: `level`, `severity`, `log_level`
* service fields: `service`, `app`, `logger`, `component`

### Plain text logs

Use regex-based heuristics to extract:

* timestamp
* severity
* service
* remaining message

If service is unavailable, leave null or infer from filename when appropriate.

---

## 11.3 Normalization

Normalization is critical.

Purpose:

* reduce noisy dynamic values
* improve clustering
* improve deduplication
* make evidence human-readable

### Replace dynamic values

Examples:

```text
User 123 failed login
User 456 failed login
```

becomes

```text
User <*> failed login
```

### Replace patterns such as:

* UUIDs
* numeric IDs
* IP addresses
* URLs with query params
* hashes
* timestamps
* long hex strings
* email addresses
* request IDs
* tokens
* file paths if overly specific

### Preserve meaningful domain tokens

Do not erase:

* endpoint names
* exception names
* status codes
* service names
* operation names
* database table names if useful

---

## 11.4 Fingerprinting

Fingerprinting groups messages with the same normalized pattern.

Example:

```text
Stripe signature verification failed for endpoint /webhooks/stripe request_id=req_123
Stripe signature verification failed for endpoint /webhooks/stripe request_id=req_456
```

normalized:

```text
Stripe signature verification failed for endpoint /webhooks/stripe request_id=<*>
```

fingerprint:

* deterministic hash of normalized message
* maybe SHA-256 truncated

This fingerprint is the primary grouping key.

---

## 12. Clustering

## 12.1 Why clustering matters

Without clustering, the LLM gets flooded with repeated log lines and noise.

Clustering transforms:

* thousands of raw lines
  into
* a few meaningful event families

This is essential.

## 12.2 MVP clustering strategy

Use a two-stage approach.

### Stage 1: exact grouping by fingerprint

This captures most duplicate/similar logs cheaply.

### Stage 2: optional semantic merge

For fingerprints that are similar but not identical, merge using:

* string similarity
* embedding similarity
* token overlap
* same service and close time range

For MVP, exact fingerprint grouping alone may be enough to start.

## 12.3 Cluster metadata

Each cluster should compute:

* representative message
* count
* first seen timestamp
* last seen timestamp
* involved services
* involved levels
* baseline count
* change ratio
* percentage of total window
* top raw examples

## 12.4 Importance ranking

Rank clusters by a weighted score.

Suggested score components:

* severity weight
* count weight
* increase vs baseline
* recency
* multi-service spread
* whether cluster looks like an error/warn
* whether cluster co-occurs with trigger patterns (deploy, restart, config change)

Example pseudo-score:

```text
importance_score =
  severity_weight
  + log(count + 1)
  + change_ratio_weight
  + spread_weight
  + trigger_correlation_weight
```

---

## 13. Baseline Comparison

## 13.1 Why baseline matters

A cluster that occurs 200 times may be normal.
A cluster that occurs 5 times but usually occurs 0 times may be important.

The system must compare current behavior to recent baseline behavior.

## 13.2 MVP baseline strategy

For a requested window `[T1, T2]`, define a baseline window immediately before it of equal or greater length.

Example:

* explain last 30m
* baseline = previous 6h or previous 24h, configurable

Default suggestion:

* if window <= 1h, baseline = previous 24h
* if window > 1h, baseline = previous 7d where available

## 13.3 Baseline metrics

For each fingerprint/cluster:

* baseline_count
* average occurrences per equivalent window
* first-time-seen flag
* change ratio
* new-cluster flag

Evidence should reference this.

Example:

* "no comparable error volume before deploy"
* "baseline was near zero in prior 24h"

---

## 14. Trigger Detection

## 14.1 Purpose

The tool should identify likely trigger events that precede major failures.

## 14.2 MVP trigger patterns

Detect likely trigger logs such as:

* deploy started / completed
* application restart
* pod restart
* configuration reloaded
* DB connection errors
* migration started/completed
* queue saturation
* timeout spikes
* circuit breaker open
* webhook secret/config mismatch
* auth token expiration bursts

This can start as simple pattern matching.

## 14.3 Trigger heuristics

A trigger is more likely if:

* it occurs shortly before a major error cluster
* it is rare relative to baseline
* it is linked to the same service
* downstream clusters begin after it

---

## 15. Evidence Assembly

## 15.1 Why evidence matters

The product fails if it only produces vague AI prose.

Every explanation must be backed by explicit evidence.

## 15.2 Evidence items

Each evidence item should be derived, not invented.

Possible evidence types:

* count spike
* first occurrence timing
* same endpoint repeated
* same service repeated
* correlated downstream errors
* trigger-before-failure sequence
* baseline near zero
* only one affected service
* same trace/request pattern if available

## 15.3 Evidence packet format

Internal representation should be structured JSON.

Example:

```json
{
  "window": {
    "start": "2026-03-12T22:00:00Z",
    "end": "2026-03-12T22:30:00Z"
  },
  "primary_cluster": {
    "message": "Stripe signature verification failed for endpoint /webhooks/stripe",
    "count": 184,
    "service": "billing-worker",
    "first_seen": "2026-03-12T22:02:10Z",
    "baseline_count": 1,
    "change_ratio": 184.0
  },
  "secondary_clusters": [
    {
      "message": "checkout returned 500",
      "count": 39,
      "service": "api"
    }
  ],
  "trigger_candidates": [
    {
      "message": "deploy completed for billing-worker",
      "timestamp": "2026-03-12T21:58:15Z"
    }
  ],
  "evidence": [
    "184 similar errors in billing-worker",
    "first occurrence 2m after deploy",
    "same webhook endpoint affected in 96% of failures",
    "no comparable error volume before deploy"
  ]
}
```

This structured evidence packet should drive both LLM and rules-only summaries.

---

## 16. LLM Integration

## 16.1 LLM philosophy

LLMs are optional polish and summarization layers, not the source of truth.

Truth comes from:

* structured counts
* clustering
* timing
* baseline comparison
* retrieved examples

## 16.2 Modes

### Mode 1: no-LLM

Use deterministic templates and evidence ranking.

### Mode 2: LLM enabled

Send only curated evidence, not raw log dumps.

## 16.3 Prompt strategy

Never send thousands of logs.

Send:

* top cluster summaries
* counts
* time correlations
* trigger candidates
* top evidence points
* constraints on output structure

## 16.4 Prompt requirements

The prompt must enforce:

* short output
* no fabricated claims
* mention uncertainty when needed
* use only supplied evidence
* fixed sections
* no markdown unless requested

### Example system prompt

```text
You are analyzing production logs. Use only the supplied evidence.
Do not invent causes or events not present in the evidence.
Return a short incident summary with the following sections exactly:
1. Incident summary
2. Services affected
3. Primary issue
4. Secondary effects
5. Likely trigger
6. Evidence
7. Confidence

If evidence is insufficient, say that clearly.
```

### Example user payload

Supply JSON evidence packet and ask for deterministic summary.

## 16.5 Supported provider interface

Abstract interface:

```python
class LLMProvider(Protocol):
    def generate_summary(self, evidence_packet: dict) -> str: ...
```

Possible implementations:

* OpenAI-compatible
* Ollama
* mock provider
* no-op provider

---

## 17. Embeddings Integration

## 17.1 Purpose

Embeddings are secondary support for:

* semantic cluster merge
* question retrieval
* cluster-summary retrieval

They are not the primary storage or grouping mechanism.

## 17.2 Where to embed

Prefer embedding:

* normalized messages
* cluster summaries

Avoid embedding:

* highly noisy raw messages without normalization

## 17.3 Provider abstraction

```python
class EmbeddingProvider(Protocol):
    def embed_texts(self, texts: list[str]) -> list[list[float]]: ...
```

Implementations:

* OpenAI-compatible embeddings
* sentence-transformers local
* disabled mode

---

## 18. CLI Specification

## 18.1 Command list

### `raglogs init`

Initializes local configuration.

Example:

```bash
raglogs init
```

Behavior:

* create config file
* test database connection optional
* store provider settings
* optionally install schema

### `raglogs ingest`

Ingest one or more files.

Examples:

```bash
raglogs ingest ./logs/*.log
raglogs ingest ./sample-incident --recursive
```

Flags:

* `--recursive`
* `--source-name`
* `--service`
* `--env`
* `--format json|text|auto`
* `--with-embeddings/--no-embeddings`

### `raglogs explain`

Main killer command.

Examples:

```bash
raglogs explain --since 30m
raglogs explain --service billing-worker --since 2h
raglogs explain --from 2026-03-12T22:00:00Z --to 2026-03-12T22:30:00Z
raglogs explain --since 1h --no-llm
```

Flags:

* `--since`
* `--from`
* `--to`
* `--service`
* `--env`
* `--source`
* `--max-clusters`
* `--baseline-window`
* `--format text|json|markdown`
* `--no-llm`

### `raglogs ask`

Natural language query.

Examples:

```bash
raglogs ask "why did login fail?"
raglogs ask "what changed before latency increased?" --since 2h
```

Flags similar to explain.

### `raglogs clusters`

Show top clusters.

Example:

```bash
raglogs clusters --since 1h
```

### `raglogs status`

Show DB/job/provider status.

### `raglogs config`

Inspect or set config values.

---

## 18.2 CLI UX requirements

* clean output
* color via Rich
* plain text by default
* JSON output mode for automation
* meaningful error messages
* progress indicators for ingestion
* summary stats after ingestion

Example ingestion output:

```text
Ingestion complete

Files processed: 3
Lines read: 124,503
Parsed logs: 121,948
Skipped lines: 2,555
New services detected: api, billing-worker, redis-worker
Embeddings created: 12,000
Duration: 18.2s
```

---

## 19. API Specification

## 19.1 Purpose

The API supports local/self-hosted use and future integrations.

## 19.2 Endpoints

### `GET /health`

Returns service health.

### `POST /ingestions`

Create ingestion job.

Request:

```json
{
  "paths": ["./logs/app.log"],
  "recursive": false,
  "format": "auto"
}
```

### `GET /ingestions/{job_id}`

Check ingestion job status.

### `POST /query/explain`

Main explanation endpoint.

Request:

```json
{
  "since": "30m",
  "service": "billing-worker",
  "env": "prod",
  "no_llm": false,
  "format": "json"
}
```

Response:

```json
{
  "window": {...},
  "summary": "...",
  "confidence": "medium-high",
  "evidence": [...],
  "clusters": [...]
}
```

### `POST /query/ask`

Natural language question endpoint.

### `POST /query/clusters`

Return cluster list.

### `GET /config`

Read effective config.

---

## 20. Background Jobs

## 20.1 Why DB-backed jobs first

Do not add Redis/Celery for MVP.
Too much overhead.

Use:

* jobs table
* simple worker loop
* polling or cooperative processing

## 20.2 Job types

* ingestion
* embedding generation
* clustering cache generation
* explanation cache generation

## 20.3 Worker process

Separate command:

```bash
raglogs worker
```

It:

* polls pending jobs
* marks running/completed/failed
* stores metadata/errors
* supports retry count

---

## 21. Repository Structure

Suggested repository structure:

```text
raglogs/
  README.md
  pyproject.toml
  .env.example
  docker-compose.yml
  Makefile

  alembic.ini
  migrations/

  raglogs/
    __init__.py
    config/
      settings.py
      models.py

    cli/
      main.py
      commands/
        init.py
        ingest.py
        explain.py
        ask.py
        clusters.py
        status.py
        config.py
        worker.py

    api/
      app.py
      deps.py
      routes/
        health.py
        ingestions.py
        explain.py
        ask.py
        clusters.py
        config.py

    db/
      base.py
      models.py
      session.py
      repositories/

    core/
      ingestion/
        service.py
        file_loader.py
        job_runner.py

      parsing/
        json_parser.py
        text_parser.py
        field_extractors.py
        timestamp.py

      normalization/
        normalize.py
        fingerprint.py
        patterns.py

      clustering/
        clusterer.py
        scoring.py
        baseline.py
        merge.py

      retrieval/
        filters.py
        semantic.py
        question_router.py

      explain/
        evidence.py
        summarizer.py
        templates.py
        confidence.py

      embeddings/
        provider.py
        openai_provider.py
        local_provider.py
        service.py

      llm/
        provider.py
        openai_provider.py
        ollama_provider.py
        noop_provider.py

      outputs/
        text.py
        json.py
        markdown.py

    adapters/
      file/
        adapter.py

      # future
      datadog/
      loki/
      mongo/
      postgres/
      kubernetes/

    utils/
      time.py
      hashing.py
      strings.py
      logging.py

  sample_data/
    sample_incident/
      api.log
      billing-worker.log
      deploy.log

  tests/
    unit/
    integration/
    fixtures/
```

---

## 22. Configuration

## 22.1 Config requirements

Configuration sources:

* `.env`
* config file
* CLI flags
* environment variables

Priority:
CLI > env > config file > defaults

## 22.2 Config values

Examples:

```env
RAGLOGS_DB_URL=postgresql+psycopg://postgres:postgres@localhost:5432/raglogs
RAGLOGS_EMBEDDINGS_PROVIDER=openai
RAGLOGS_EMBEDDINGS_MODEL=text-embedding-3-small
RAGLOGS_LLM_PROVIDER=openai
RAGLOGS_LLM_MODEL=gpt-4.1-mini
RAGLOGS_OPENAI_BASE_URL=
RAGLOGS_OPENAI_API_KEY=
RAGLOGS_DEFAULT_BASELINE_WINDOW=24h
RAGLOGS_MAX_EVIDENCE_ITEMS=8
RAGLOGS_MAX_CLUSTERS_FOR_EXPLAIN=10
```

Support:

* no provider configured
* local provider configured
* OpenAI-compatible base URL override

---

## 23. No-LLM Mode

## 23.1 Importance

This matters for adoption.
The tool must still be useful without any model APIs.

## 23.2 Behavior

In no-LLM mode:

* still ingest logs
* still normalize/fingerprint/cluster
* still rank clusters
* still compare baseline
* still produce templated summary

Example:

```text
Incident summary

Window: 22:00 → 22:30
Services affected: billing-worker, api
Primary issue: Stripe signature verification failed
Secondary effects: checkout 500s
Likely trigger: deploy event detected 2m before first error spike

Evidence:
- 184 matching logs in billing-worker
- baseline count in prior 24h: 1
- first checkout 500 occurred after webhook error spike
- 96% of matching logs referenced /webhooks/stripe

Confidence: medium
```

No hallucination risk. Lower polish, still useful.

---

## 24. Demo Dataset

## 24.1 Why it matters

A great demo is crucial for GitHub traction.

## 24.2 Required demo story

The sample dataset should clearly show:

1. deploy event
2. new error family starts
3. retries increase
4. downstream service failures appear
5. likely trigger inferred

## 24.3 Suggested files

* `deploy.log`
* `billing-worker.log`
* `api.log`

## 24.4 Expected demo command

```bash
docker compose up
raglogs ingest ./sample_data/sample_incident
raglogs explain --since 30m
```

Expected output must be impressive and deterministic.

---

## 25. FastAPI Design Notes

## 25.1 Why include API in MVP even if CLI-first

Because:

* easier future integrations
* easier local service architecture
* clear separation of concerns
* future UI becomes easier

CLI can call internal services directly first, not necessarily HTTP.

## 25.2 Suggested architecture choice

For MVP:

* CLI uses core Python services directly
* API is a thin wrapper over same services

Do not make CLI depend on local HTTP server unless necessary.

---

## 26. Implementation Phases

## Phase 1: Core local MVP

### Scope

* local file ingestion
* JSON and text parsing
* normalization
* fingerprinting
* Postgres persistence
* optional embeddings
* cluster generation
* explain
* ask
* clusters
* no-LLM mode
* LLM mode
* CLI UX

### Deliverable

A developer can run:

```bash
raglogs init
raglogs ingest ./example_logs
raglogs explain --since 1h
```

and get a useful evidence-based output.

---

## Phase 2: Self-hosted runtime polish

### Scope

* Docker Compose
* FastAPI server
* worker process
* API endpoints
* better config management
* cached explanations
* cluster run persistence

---

## Phase 3: Connectors and richer incident analysis

### Scope

* Datadog adapter
* Loki adapter
* MongoDB adapter
* Postgres adapter
* Kubernetes export ingestion
* incident timeline
* compare windows
* `what-changed` command
* markdown report export

---

## 27. Algorithms and Heuristics

## 27.1 Time window parsing

Support inputs like:

* `30m`
* `1h`
* `24h`
* ISO timestamps

## 27.2 Severity weighting

Suggested weights:

* fatal: 5
* error: 4
* warn: 3
* info: 1
* debug: 0.5

## 27.3 Change ratio

```text
change_ratio = (current_count + 1) / (baseline_count + 1)
```

Use smoothing to avoid divide-by-zero explosions.

## 27.4 Trigger correlation heuristic

A trigger is relevant if:

* same service or adjacent service
* occurs within N minutes before first major cluster spike
* matches known trigger patterns
* baseline rarity is high

## 27.5 Cluster importance threshold

Only top N clusters go into explanation.
Default:

* top 5 or top 10 by importance

---

## 28. Error Handling and Failure Modes

## 28.1 Expected failures

* malformed log lines
* missing timestamps
* unsupported file encodings
* provider API failures
* missing embeddings model
* database unavailable
* too little data in window
* empty query results
* baseline unavailable

## 28.2 Handling principles

* never crash whole ingestion because of bad lines
* log parser failures as skipped lines
* degrade gracefully when providers fail
* produce best-effort no-LLM summary if LLM fails
* expose uncertainty clearly

Example when no meaningful result exists:

```text
Incident summary

Insufficient evidence to identify a likely issue in the requested window.

Evidence:
- 12 total logs matched filters
- no error-level clusters detected
- no cluster showed meaningful deviation from baseline

Confidence: low
```

---

## 29. Testing Strategy

## 29.1 Unit tests

Must cover:

* JSON parsing
* text parsing
* normalization rules
* fingerprinting stability
* time window resolution
* baseline calculations
* cluster ranking
* evidence assembly
* no-LLM summary templates

## 29.2 Integration tests

Must cover:

* full ingest flow
* explain command on sample incident
* ask command on sample incident
* API endpoints
* Postgres + pgvector integration
* provider mocks

## 29.3 Golden output tests

Important for CLI credibility.

Store expected outputs for sample datasets:

* `explain --since 30m`
* `clusters --since 30m`
* `ask "what happened?"`

Allow small flexible fields such as durations/timestamps where necessary.

---

## 30. Performance Expectations for MVP

## 30.1 Acceptable MVP targets

For local files:

* ingest 100k lines in under ~30 seconds on a normal dev machine
* explain over 100k-500k stored lines in a few seconds to tens of seconds
* no-LLM explain should be noticeably faster than LLM explain

## 30.2 Performance priorities

1. fingerprint grouping
2. indexed window filtering
3. limited cluster set for explanations
4. avoid embedding every line immediately if not needed
5. batch inserts
6. batch embeddings

---

## 31. Security and Privacy

## 31.1 Principles

Since this is self-hosted:

* default to local-first operation
* avoid sending logs externally unless provider is configured
* document clearly when embeddings/LLM APIs send data off-box

## 31.2 MVP requirements

* redact secrets during normalization where possible
* do not print raw tokens/secrets in explanations
* document provider data flow
* allow no-LLM and local-model operation

---

## 32. Output Formats

## 32.1 Text

Best for humans.

## 32.2 JSON

Best for automation and integration.

## 32.3 Markdown

Best for reports and GitHub demo content.

Example JSON output should include:

* window
* primary cluster
* secondary clusters
* trigger candidates
* evidence
* confidence
* summary text

---

## 33. Naming Recommendation

Better than "RAG logs":

Strong directions:

* logsage
* logwise
* rootlog
* logbrief
* logtell
* whathappened

Recommended bias:
Pick a name that implies explanation, not vectors.

My strongest picks:

* **logbrief**
* **logtell**
* **whathappened**
* **logsage**

---

## 34. MVP Acceptance Criteria

The MVP is done when all of the following are true:

1. A user can initialize the project locally
2. A user can ingest local JSON/text logs
3. Parsed logs are stored in Postgres
4. Fingerprints are generated deterministically
5. Clusters can be listed for a time window
6. `raglogs explain --since 30m` produces a stable evidence-based output
7. The output references actual counts/timing/baseline comparisons
8. The tool works in no-LLM mode
9. The tool works with at least one LLM provider
10. The sample incident demo works out of the box
11. Docker Compose setup is included
12. README shows the 3-command wow path:

* start
* ingest
* explain

---

## 35. Suggested Build Order

Build in this exact order:

### Step 1

Project scaffolding, config, DB models, migrations

### Step 2

File ingestion and parser layer

### Step 3

Normalization and fingerprinting

### Step 4

Persist logs and query by time/service/env

### Step 5

Cluster generation and ranking

### Step 6

No-LLM `clusters` output

### Step 7

No-LLM `explain` output

### Step 8

Natural language `ask` with structured retrieval only

### Step 9

Embeddings integration

### Step 10

LLM summary generation over evidence packets

### Step 11

FastAPI endpoints

### Step 12

Docker Compose and sample incident demo

That order keeps the product useful early.

---

## 36. Concrete Example End-to-End Flow

### Input logs

* deploy log: deploy completed for billing-worker
* billing-worker logs: webhook signature verification failures
* api logs: checkout 500s

### User command

```bash
raglogs explain --since 30m
```

### Internal flow

1. resolve time window
2. query logs in window
3. normalize messages
4. fingerprint messages
5. group clusters
6. compare cluster counts to baseline
7. detect deploy trigger
8. correlate secondary cluster after primary error spike
9. build evidence packet
10. generate summary

### Output

```text
Incident summary

Window: 22:00 → 22:30
Services affected: billing-worker, api
Primary issue: Stripe signature verification failures in billing-worker
Secondary effects: checkout 500s in api
Likely trigger: deploy completed for billing-worker at 21:58

Evidence:
- 184 matching failures in billing-worker
- first spike occurred 2m after deploy
- 96% of failures referenced /webhooks/stripe
- checkout 500s began after webhook failures
- baseline for this error family was near zero in the prior 24h

Confidence: medium-high
```

---

## 37. Final Product Rule

The product must always prioritize this hierarchy:

1. evidence
2. structure
3. clarity
4. speed
5. polish

Never let LLM polish replace actual evidence.

The thing that makes this project interesting is not that it can "chat with logs".

It is that it can take a bounded time window and answer:

> **What happened, why do you think that, and what evidence supports it?**

That is the MVP.
