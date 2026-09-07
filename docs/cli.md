# CLI reference


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
| `--with-embeddings` | Persist pgvector embeddings on `log_embeddings` for semantic `ask` (requires `EMBEDDINGS_PROVIDER`) |
| `--adapter` | `file` (default), `cloudwatch`, `datadog`, `loki`, or `k8s` |
| `--param` | Adapter param as `key=value` (repeatable) |
| `--since` | Window for pull adapters, e.g. `30m`, `1h`, `24h` (default last 1h) |
| `--from` / `--to` | Explicit ISO 8601 window bounds (pull adapters) |
| `--resume-job` | Prior ingestion job UUID to resume pagination cursors from |
| `--scope` | Isolation scope (CLI default `default`). Service requests require a resolvable scope. |

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
curl -X POST http://localhost:8000/v1/ingestions \
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
raglogs explain --since 1h --format markdown > postmortem.md
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

**Markdown incident report**

`--format markdown` writes a paste-ready GitHub-flavored markdown file for tickets and postmortems. Redirect stdout to save it:

```bash
raglogs explain --since 1h --format markdown > postmortem.md
```

The report includes:

- Title (from the primary cluster message, or the first useful summary line)
- Metadata: window (ISO start/end + duration), services, environment (if filtered), total logs, confidence, mode (`rules` / `llm`)
- Summary (the same explanation text as `text` / JSON — not re-parsed)
- Primary cluster (fingerprint, count, importance, representative message) when present
- Secondary clusters and trigger candidates when present
- Evidence bullets
- A Reproduce footer with the exact `raglogs explain ... --format markdown` invocation

Output is written as raw markdown on stdout (Rich markup is not applied), so shell redirection stays valid GFM. Progress and errors go to stderr.

**Output structure** (default `--format text`)

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

Answer a natural language question about your logs. Retrieval is **semantic-first** when embeddings exist and a provider is enabled, then keyword search, then a window-level error/warn fallback.

```bash
raglogs ask "why did login fail?"
raglogs ask "what changed before latency increased?" --since 2h
raglogs ask "what happened in billing?" --since 1h
raglogs ask "why are checkouts failing?" --format json
raglogs ask "why did login fail?" --ingestion-job <uuid>
raglogs ask "why did login fail?" --all-ingestions
```

Like `explain` / `timeline` / `compare`, `ask` defaults to the latest completed ingestion. Pass `--ingestion-job` to target a specific job, or `--all-ingestions` to search every ingested log.

| Flag | Description |
|---|---|
| `--since` | Relative window: `30m`, `1h`, `24h` |
| `--service` | Filter to one service |
| `--format` | `text` or `json` |
| `--ingestion-job` | Scope to a specific ingestion job UUID |
| `--all-ingestions` | Search all historical ingestions, not just the latest |

**How retrieval works**

1. **Semantic** — if `EMBEDDINGS_PROVIDER` is `openai` or `local` *and* log lines were ingested with `--with-embeddings`, the question is embedded and nearest neighbors are fetched from pgvector (`ASK_SEMANTIC_TOP_K`, `ASK_SEMANTIC_MIN_SIMILARITY`). Paraphrases that keyword search would miss (e.g. "why are payments being declined?" vs "Stripe signature verification failed") can still match.
2. **Keyword** — if embeddings are disabled, the provider errors, or semantic search returns nothing above the similarity threshold, `ask` matches tokens against `normalized_message` as before.
3. **Fallback** — if keyword search is also empty, the top error/warn lines in the window are used.

Hits are still grouped with `cluster_logs` so answers stay grounded in counts, `first_seen`, and `last_seen`. JSON output includes `retrieval_mode` (`semantic` | `keyword` | `fallback`). Default `EMBEDDINGS_PROVIDER=disabled` keeps `ask` fully keyword-based — no API key required.

Populate vectors at ingest time:

```bash
raglogs ingest ./logs --with-embeddings
```

Vectors are stored in `log_embeddings` (1536-d). Because that column width is fixed, a non-disabled provider with `EMBEDDINGS_DIMENSIONS ≠ 1536` — or a `local` model whose native width isn't 1536 — is a configuration error that can never persist a vector. It is surfaced, not silently skipped: **`ingest --with-embeddings` hard-exits** (non-zero) with an actionable message before ingesting, and **API startup logs the misconfig at `error` and keeps serving** the non-embedding features (explain / timeline / compare / keyword `ask`) — coupling the whole API's availability to an optional feature would be worse, especially during an incident. Transient provider failures *during* ingest are still skipped fail-open; the log lines land regardless. (With `EMBEDDINGS_PROVIDER=local`, both the startup check and ingest load the SentenceTransformer model, which may download it on first use.)

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

### `raglogs purge`

Expire raw log rows (and cascaded log-line embeddings / cluster membership) while keeping cluster summaries and `cluster_embeddings` so `POST /v1/query/similar` still works. After `RETENTION_SUMMARY` those summaries expire too. Per-scope TTLs override env defaults via the `scope_retention` table; missing override → `RETENTION_RAW` / `RETENTION_SUMMARY`. `0`, empty, or `off` skips that tier.

The background worker (`raglogs worker`) also enqueues a purge job on idle poll about every `PURGE_INTERVAL_SECONDS` (default 3600). Purge uses `SELECT FOR UPDATE SKIP LOCKED` like ingest and deletes in `created_at`-ordered chunks (time-in-store) so ingest is not starved. Raw expiry uses `log_entries.created_at`, not the event timestamp, so historical dumps are not wiped on ingest.

```bash
raglogs purge              # every scope with data
raglogs purge --scope default
raglogs purge --dry-run    # count only
```

---

### `raglogs keys`

Mint, list, revoke, and set per-key query defaults for HTTP API keys. The API bearer token is printed **once** at create time and is never stored or logged — only an argon2 hash and a short prefix are kept. A separate **webhook signing secret** (`whsec_…`) is also printed once; it is stored server-side so ingest completion callbacks can be HMAC-signed. It is not the bearer token. `raglogs keys list` shows `whsec_****` when a signing secret exists (legacy keys minted before this column fall back to `WEBHOOK_SECRET`).

```bash
raglogs keys create --role query --scope default --name "ci"
raglogs keys create --role query --scope incident:INC-9 --allow-scope-override --name "ci-override"
raglogs keys create --role query --max-clusters 5 --baseline-window 12h
raglogs keys set-defaults <key-uuid> --max-clusters 8 --max-evidence-items 4 --llm-provider ollama
raglogs keys set-defaults <key-uuid> --clear
raglogs keys list
raglogs keys revoke <key-uuid>
```

| Flag | Description |
|---|---|
| `--role` | `ingest`, `query`, or `admin` (default `query`) |
| `--scope` | Pin the key to this isolation scope (enforced on every service read/write). Default `default`. Convention: `incident:<id>`, `service:<name>`, `env:<name>`. |
| `--allow-scope-override` | Allow the caller to pass a request `scope` other than the key's pin. Pinned by default. |
| `--name` | Optional label |
| `--max-clusters` | Per-key default `max_clusters` (1–100) stored on `api_keys.config_json` |
| `--max-evidence-items` | Per-key default `max_evidence_items` (1–50) |
| `--baseline-window` | Per-key default baseline duration (e.g. `24h`) |
| `--llm-provider` | Per-key default `openai` / `ollama` / `claude` / `disabled` |
| `--llm-enabled` / `--no-llm-enabled` | Per-key default for whether the LLM is used |

`raglogs keys set-defaults` merges flags into the key's `config_json`. `--clear` removes all per-key query defaults. Requires a migrated database (`raglogs init`). See [HTTP API authentication](api.md#http-api-authentication) and [per-request overrides](api.md#per-request-query-overrides).

---
