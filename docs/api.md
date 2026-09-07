# HTTP API


raglogs exposes a FastAPI server for integrations and future tooling.

```bash
uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload
# or
make api
```

`make api` still binds `0.0.0.0`. With the default `AUTH_ENABLED=false` that logs a loud startup warning. Bind loopback instead with `make api API_BIND_HOST=127.0.0.1`, or enable auth (below).

### HTTP API authentication

Auth is **off by default** so local demo and existing clients keep working. Set `AUTH_ENABLED=true` before exposing the API on a network (including Docker Compose on `0.0.0.0`).

```bash
export AUTH_ENABLED=true
raglogs keys create --role admin --name "local"
# copy the rlk_… API key and the whsec_… webhook secret from the panels — each is shown only once

curl -X POST http://localhost:8000/v1/query/explain \
  -H "Authorization: Bearer rlk_…" \
  -H "Content-Type: application/json" \
  -d '{"since": "30m", "no_llm": true}'
```

| Role | Allowed |
|---|---|
| `ingest` | `POST /v1/ingestions`, `POST /v1/ingestions/lines`, tail lifecycle `pause` / `resume` / `stop` (and the deprecated `/ingestions` aliases) |
| `query` | `GET /v1/ingestions*`, `POST /v1/query/*`, web UI (`GET /`, `/static`), OpenAPI (`/docs`) |
| `admin` | everything, including `GET /v1/config` |

`GET /health` and `GET /metrics` are always unauthenticated. `/docs` is **not** exempt. `/metrics` returns Prometheus text (`text/plain`) and is not rate-limited.

Missing or invalid `Authorization: Bearer` returns **401**:

```json
{"error_code": "AUTH_UNAUTHORIZED", "message": "…"}
```

A valid key with the wrong role returns **403**:

```json
{"error_code": "AUTH_FORBIDDEN", "message": "…"}
```

Keys are stored argon2-hashed with a short indexed prefix. Each key is **pinned** to a `scope` (default `default`) and every service ingest/query is filtered by that scope — including baseline comparison — so one incident's logs cannot contaminate another. Mint with `--allow-scope-override` to let the caller pass a request `scope`. A service request with no resolvable scope returns **400**:

```json
{"error_code": "SCOPE_REQUIRED", "message": "…"}
```

A pinned key that sends a different non-empty scope returns **403** `SCOPE_MISMATCH`. The CLI is scope-optional and defaults to `default` (`raglogs ingest --scope incident:INC-9`, `raglogs explain --scope …`). Each new key also gets a `whsec_…` webhook signing secret (shown once; `keys list` shows `whsec_****` only).

**`POST /v1/query/similar` cross-scope permissions.** Similar-incident search can look across isolation scopes ("we saw this in INC-1188") when the caller is allowed to see those scopes. Matches from a scope the caller cannot see are never returned.

| Caller | Cross-scope similar |
|---|---|
| `admin` keys | Yes by default. Pass `"cross_scope": false` to pin to the resolved scope. |
| `query` keys that are pinned | Same-scope only. `"cross_scope": true` is ignored. |
| `query` keys minted with `--allow-scope-override` | Same-scope unless the body sets `"cross_scope": true`. |
| `AUTH_ENABLED=false` (local CLI / demo) | Cross-scope allowed by default. Pass `"cross_scope": false` to pin. |

Optional OIDC: set `AUTH_MODE=oidc` or `both` and `OIDC_ISSUER`. A JWT (three dotted segments) is validated via JWKS (`iss`, `exp`, and `aud` when `OIDC_AUDIENCE` is set). Role comes from claim `raglogs_role` or `roles`, defaulting to `query`. When `AUTH_MODE=api_key`, JWTs are rejected.

If auth is disabled and the process binds a non-loopback address (`0.0.0.0`, `::`, a public IP), raglogs logs a warning. Set `AUTH_REFUSE_INSECURE_BIND=true` to refuse startup instead.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Service and DB health check (unversioned). Includes `adapters`, `tail_jobs`, `llm: {provider, status}`, and `llm_breaker` (`closed` / `open` / `half_open`). Open breaker → `status: degraded`, still HTTP 200. |
| `GET` | `/metrics` | Prometheus scrape (unversioned, unauthenticated). Ingest/query latency, ingest line counts, cluster counts, LLM latency/fallback/tokens, breaker state, worker queue depth. |
| `POST` | `/v1/ingestions` | Enqueue a batch ingest job (`adapter`: `file`, `cloudwatch`, `datadog`, `loki`, or `k8s`). Set `"mode": "tail"` for pull adapters to start a long-lived tail job. |
| `POST` | `/v1/ingestions/lines` | Push NDJSON of raw or pre-parsed log lines (sync persist) |
| `POST` | `/v1/ingestions/{id}:pause` | Pause a tail job |
| `POST` | `/v1/ingestions/{id}:resume` | Resume a paused tail job |
| `POST` | `/v1/ingestions/{id}:stop` | Stop a tail job (terminal; cannot resume) |
| `GET` | `/v1/ingestions` | List recent completed ingestion jobs, newest first |
| `GET` | `/v1/ingestions/{job_id}` | Fetch ingestion job detail |
| `GET` | `/v1/ingestions/latest` | ID of the most recently completed ingestion job, if any |
| `POST` | `/v1/query/explain` | Explain a time window |
| `POST` | `/v1/query/ask` | Answer a natural language question |
| `POST` | `/v1/query/clusters` | List top clusters |
| `POST` | `/v1/query/timeline` | Reconstruct incident timeline for a window |
| `POST` | `/v1/query/compare` | Diff two time windows (same semantics as `raglogs compare`) |
| `POST` | `/v1/query/similar` | Prior incidents with nearby fingerprints ("we saw this in INC-1188") |
| `GET` | `/v1/config` | Read effective configuration |

**Push NDJSON.** `POST /v1/ingestions/lines` accepts newline-delimited lines (`Content-Type: application/x-ndjson`, `application/jsonl`, or `text/plain`). Each line is a raw log string or a JSON object with at least `message` / `raw` / `text` (optional `timestamp`, `service`, `level`, `host`, `env`). Cap is `INGEST_PUSH_MAX_LINES` (default 5000); over the cap returns 400.

```bash
curl -X POST http://localhost:8000/v1/ingestions/lines \
  -H "Content-Type: application/x-ndjson" \
  --data-binary $'{"message":"timeout talking to payments","level":"error","service":"api"}\nplain syslog line\n'
```

**Tail jobs.** `POST /v1/ingestions` with `"mode": "tail"` (adapters `cloudwatch`, `datadog`, or `loki` only) creates a long-lived job. The worker re-runs the adapter from the saved cursor about every `TAIL_POLL_INTERVAL` seconds (default 30). Pause, resume, or stop with:

```bash
curl -X POST http://localhost:8000/v1/ingestions \
  -H "Content-Type: application/json" \
  -d '{"adapter":"loki","params":{"query":"{app=\\"api\\"}"},"mode":"tail"}'
# → { "ingestion_job_id": "...", "mode": "tail", "status": "running" }

curl -X POST http://localhost:8000/v1/ingestions/$ID:pause
curl -X POST http://localhost:8000/v1/ingestions/$ID:resume
curl -X POST http://localhost:8000/v1/ingestions/$ID:stop
```

`stop` is terminal. After `TAIL_ERROR_THRESHOLD` consecutive poll failures (default 5) a tail job auto-pauses; `/health` reports `tail_jobs.running` and `tail_jobs.paused`.

**Backpressure and rate limiting.** Two independent 429s:

- **Queue depth.** When pending worker jobs ≥ `INGEST_QUEUE_MAX` (default 100), `POST /v1/ingestions` and `POST /v1/ingestions/lines` return **429** with `Retry-After` (`INGEST_RETRY_AFTER_SECONDS`, default 5) and body `{"error_code":"INGEST_QUEUE_FULL","message":"..."}`. Tail ticks skip the same ceiling.
- **API token bucket.** `POST /v1/ingestions*` (writes) and `/v1/query*` (plus unversioned aliases) are limited per API key (`request.state.auth_principal.key_id`, or a single `anonymous` bucket when `AUTH_ENABLED=false`). Exceeding the bucket returns **429** with `Retry-After` (`RATELIMIT_RETRY_AFTER_SECONDS`, default 1) and body `{"error_code":"RATE_LIMITED","message":"..."}`. Defaults (`RATELIMIT_INGEST_RPS` / `RATELIMIT_QUERY_RPS` / `RATELIMIT_BURST` = 100) are high enough for local demo and tests; `0` rps means unlimited for that category. `/health`, `/metrics`, `/docs`, static UI, and `/config` are not limited. Buckets are in-memory per process.

LLM calls are separately capped by `LLM_MAX_CONCURRENCY` (default 4) so a burst of `explain` cannot fan out unbounded provider requests. Timeouts, retries, automatic template fallback (`llm.fell_back`), and the process-local circuit breaker are described under [LLM integration](../README.md#llm-integration).

**Observability.** Every response echoes `X-Request-Id` (honors incoming `X-Request-Id` / `X-Request-ID`, otherwise a UUID) and W3C `traceparent` plus `X-Trace-Id`. Structured JSON logs (structlog) include `request_id` and resolved `scope` via contextvars; secrets and bearer tokens are never logged. `GET /metrics` names:

| Metric | Type | Meaning |
|---|---|---|
| `raglogs_ingest_duration_seconds` | histogram | Pipeline ingest wall time |
| `raglogs_ingest_lines_total` | counter (`result`) | Lines inserted / deduped / error |
| `raglogs_ingest_request_duration_seconds` | histogram | HTTP ingest write latency |
| `raglogs_cluster_count` | histogram | Clusters produced per run |
| `raglogs_query_request_duration_seconds` | histogram (`endpoint`) | HTTP query latency |
| `raglogs_llm_request_duration_seconds` | histogram | LLM provider call latency |
| `raglogs_llm_fallback_total` | counter | G10 template fallbacks |
| `raglogs_llm_estimated_tokens_total` | counter | Estimated input tokens (UTF-8 chars/4, not USD) |
| `raglogs_llm_breaker_state` | gauge | `0` closed, `1` half_open, `2` open |
| `raglogs_worker_queue_depth` | gauge | Pending worker jobs (omitted until a successful scrape; left stale if DB fails) |
| `raglogs_purge_rows_total` | counter (`kind`) | Rows reclaimed by retention purge: `raw`, `summary`, or `embedding` |

OpenTelemetry spans cover ingest → cluster → explain (and the HTTP request). The default exporter is none; set `OTEL_EXPORTER_OTLP_ENDPOINT` to export, or `OTEL_SDK_DISABLED=true` to skip the SDK. Trace ids stay on response headers so `/v1/query/*` JSON (`schema_version` 1.0) is unchanged.

**Idempotency-Key.** `POST /v1/ingestions` (batch enqueue and tail create; also the deprecated `/ingestions` alias) honors an `Idempotency-Key` header (max 256 characters). A repeat **in the same isolation scope** within `INGEST_IDEMPOTENCY_TTL_SECONDS` (default 86400) returns the original **202** job — the same `worker_job_id` for batch, the same `ingestion_job_id` for tail — instead of starting a new one. Reusing another scope's key returns **409** `IDEMPOTENCY_SCOPE_CONFLICT`. Empty keys return **400**. GET routes ignore the header. `POST /v1/ingestions/lines` does not use the header; duplicate push/tail lines are handled by content dedup instead.

```bash
curl -X POST http://localhost:8000/v1/ingestions \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: incident-123-retry" \
  -d '{"paths":["/var/log/app"]}'
```

**Content dedup.** Every persist path (`ingest_files`, `ingest_from_source` including tail ticks, and push `/lines`) stores `original_line_hash` (SHA-256 of the **raw** line, distinct from the normalized fingerprint) and upserts on `(scope, source_ref, original_line_hash, timestamp)`. Re-reading the same physical lines is a no-op, so cluster counts stay stable across overlapping windows and tail/push retries. Missing `source_ref` is stored as `""` so uniqueness works (Postgres NULLs are distinct). `scope` defaults to `"default"` (CLI) or is resolved from the API key / request (service). Queries, ingest lists, and baseline comparison are filtered by the same scope. Duplicate lines are skipped, not errors.

**Completion callbacks.** Optional `callback_url` on `POST /v1/ingestions` (http or https only; `file:` and empty hosts are rejected). When a **batch** worker job reaches a terminal state (`done` / `failed`), raglogs POSTs an HMAC-SHA256-signed JSON body to that URL. Delivery is fail-open: retries with jittered exponential backoff (`WEBHOOK_MAX_RETRIES`, default 5 extra attempts) on 5xx, 429, and connect errors; 4xx other than 429 are not retried. Failures are logged and **do not** change ingest status — poll `GET /v1/ingestions/jobs/{worker_job_id}` still works.

`job_id` in the payload is the **ingestion_job_id** when ingest created a row; if the worker failed before that, it is the `worker_job_id`. `scope` is the resolved isolation scope (API key pin, request override when allowed, or `"default"` when auth is off). `counts.clusters` is `0` (clustering is not part of ingest). Worker `done` maps to `"succeeded"` (or `"partial"` when `error_count > 0`); `failed` maps to `"failed"`.

```bash
curl -X POST http://localhost:8000/v1/ingestions \
  -H "Content-Type: application/json" \
  -d '{"paths":["/var/log/app"],"callback_url":"https://example.com/hooks/raglogs"}'
```

Example body:

```json
{
  "job_id": "a1b2c3d4-…",
  "status": "succeeded",
  "scope": "default",
  "counts": { "lines": 48213, "clusters": 0 },
  "partial": false
}
```

The signature is **not** inside the JSON. It is sent as:

```
X-Raglogs-Signature: sha256=<hex>
```

Verify by HMAC-SHA256 of the **raw request body** with the webhook secret, then compare to the header (constant-time). Use the per-key `whsec_…` from `raglogs keys create` when the ingest request was authenticated with that API key; otherwise `WEBHOOK_SECRET`. Do not HMAC the `rlk_…` bearer token — it is not stored.

Python:

```python
import hmac, hashlib

def verify(secret: str, body: bytes, header: str) -> bool:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    expected = "sha256=" + digest
    return hmac.compare_digest(expected, header)
```

Tail jobs and `POST /v1/ingestions/lines` (sync push) do **not** fire callbacks in this release — tail jobs are long-lived (poll or `:stop`); push completes in the HTTP response.

Overlapping tail poll windows can **double-count** the same physical lines until content dedup (G6) lands. Prefer a single tail job per source and avoid also batch-ingesting the same window.

Unversioned `/ingestions`, `/query/*`, and `/config` remain as **deprecated aliases** for one release. They behave the same as the `/v1` paths and send `Deprecation: true` plus a `Link: </v1/...>; rel="successor-version"` header. `/health`, `/metrics`, the web UI (`/`), and `/static` stay unversioned.

**Compatibility policy.** Additive changes stay in `v1`. Breaking path or method removals require `v2`. `/v1/query/*` JSON bodies are **schema_version 1.0**: structured fields plus an `llm` provenance block (`used`, `provider`, `model`, `fell_back`). Prose is `rendered_text` (and `format: text` still adds a `text` alias; `format: markdown` still adds `markdown`). Additive fields may appear in 1.x; a breaking body change requires `schema_version` 2.0 / `v2`.

**OpenAPI and clients.** Export the spec with `make openapi` (`clients/openapi.json`). CI uploads that file as a workflow artifact and attaches it to GitHub Releases on tags. A thin typed Python client ships as `src.clients.v1.RaglogsClient` (targets `/v1`). `make client-go` runs [oapi-codegen](https://github.com/oapi-codegen/oapi-codegen) into `clients/go/` when the binary is installed; otherwise it prints the install command and exits 0. See `clients/README.md`.

**Example**

```bash
curl -X POST http://localhost:8000/v1/query/explain \
  -H "Content-Type: application/json" \
  -d '{"since": "30m", "no_llm": true}'
```

```json
{
  "schema_version": "1.0",
  "scope": "default",
  "window": {"from": "2026-03-12T22:00:00+00:00", "to": "2026-03-12T22:30:00+00:00"},
  "confidence": {"label": "medium-high", "score": 0.72},
  "summary": "Stripe signature verification failed for endpoint /webhooks/stripe",
  "trigger": {"detected": false, "type": null, "service": null, "at": null, "correlation": null},
  "primary_cluster": {
    "fingerprint": "a1b2c3d4",
    "template": "Stripe signature verification failed for endpoint /webhooks/stripe",
    "count": 184,
    "baseline_count": 0,
    "change_ratio": 185.0,
    "services": ["billing-worker"],
    "levels": ["error"]
  },
  "evidence": [
    {"kind": "log", "detail": "184 similar errors in billing-worker"}
  ],
  "llm": {"used": false, "provider": "disabled", "model": "gpt-4.1-mini", "fell_back": false},
  "rendered_text": "Incident summary\n\nWindow: ...",
  "cached": false,
  "total_logs": 464
}
```

**Explain** — `POST /v1/query/explain` accepts the same window filters as the CLI. Optional `"format": "markdown"` adds a paste-ready `markdown` incident report field alongside the JSON payload (same shape as `raglogs explain --format markdown`).

### Per-request query overrides

`POST /v1/query/*` bodies accept optional tunables that override server defaults for that call only:

```json
{
  "since": "30m",
  "baseline_window": "24h",
  "max_clusters": 10,
  "max_evidence_items": 8,
  "llm": { "provider": "openai", "enabled": true }
}
```

| Field | Bounds | Server default |
|---|---|---|
| `baseline_window` | duration parsed like CLI (`30m`, `24h`, `7d`) | `DEFAULT_BASELINE_WINDOW` (`24h`) |
| `max_clusters` | 1–100 | `MAX_CLUSTERS_FOR_EXPLAIN` (`10`) |
| `max_evidence_items` | 1–50 | `MAX_EVIDENCE_ITEMS` (`8`) |
| `llm.provider` | `openai` / `ollama` / `claude` / `disabled` | `LLM_PROVIDER` |
| `llm.enabled` | bool; `false` acts like `no_llm` | inferred from `LLM_PROVIDER` |

**Precedence:** request field > per-key default (`api_keys.config_json`) > server env default. Omitted fields fall through. When `AUTH_ENABLED=false` the per-key layer is skipped. `llm.provider` does not persist globally; openai without `OPENAI_API_KEY` and claude without `ANTHROPIC_API_KEY` still use the noop provider. The explain cache key includes the **resolved** overrides so different `max_clusters` values do not share an entry.

Invalid values return **400**:

```json
{"error_code": "INVALID_OVERRIDE", "message": "max_clusters must be between 1 and 100", "field": "max_clusters", "min": 1, "max": 100}
```

Applies to explain, timeline, compare, clusters, ask, and similar (similar uses `max_clusters` when clustering; `top` remains the match count). Clusters still accepts `top` as an alias for `max_clusters` when `max_clusters` is omitted.

**Timeline** — `POST /v1/query/timeline` accepts the same window filters as the CLI (`since` or `from_time`/`to_time`, optional `service`, `env`, `all_ingestions`, `ingestion_job_id`). Set `"format": "text"` to include plain-text `rendered_text` (and a `text` alias) alongside `events`. Timeline is rules-only (`llm.used` is always false).

```bash
curl -X POST http://localhost:8000/v1/query/timeline \
  -H "Content-Type: application/json" \
  -d '{"since": "2h", "format": "json"}'
```

**Compare** — `POST /v1/query/compare` matches `raglogs compare`: either `"since"` + `"baseline"` (durations, window A ends at request time) or explicit `window_a_from` / `window_a_to` / `window_b_from` / `window_b_to`. Optional `"format": "text"` adds `rendered_text` (and a `text` alias). Cluster diffs include a `marker` (`+` / `-` / `↑` / `↓`; triggers use `+⚡` / `-⚡`). Compare is rules-only.

**Similar** — `POST /v1/query/similar` finds prior incidents whose cluster templates are near the current window's primary fingerprint(s). Pass `since` / `from_time` / `to_time` or `ingestion_job_id` to cluster the current incident, or skip clustering with `"fingerprint"` / `"fingerprints"`. Optional `"top"` (default 10) and `"cross_scope"` (see permissions above). Response `retrieval_mode` is `"semantic"` when pgvector ANN over `cluster_embeddings` hits, otherwise `"fingerprint"` (exact fingerprint equality, never HTTP 500 when embeddings are down). Rules-only (`llm.used` is always false).

```bash
curl -X POST http://localhost:8000/v1/query/similar \
  -H "Content-Type: application/json" \
  -d '{"since": "1h", "cross_scope": true}'
```

Published JSON Schema files live in `clients/jsonschema/` (`explain.v1.json`, `timeline.v1.json`, `compare.v1.json`, `ask.v1.json`, `clusters.v1.json`, `similar.v1.json`). Export with `make jsonschema`.

```bash
curl -X POST http://localhost:8000/v1/query/compare \
  -H "Content-Type: application/json" \
  -d '{"since": "30m", "baseline": "24h"}'
```

---
