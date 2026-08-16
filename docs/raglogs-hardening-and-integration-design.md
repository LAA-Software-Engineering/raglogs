# raglogs — Hardening & Integration Design

- **Status:** Draft / proposal
- **Author:** Leonardo (with Claude Code)
- **Date:** 2026-07-28
- **Audience:** raglogs maintainers, prospective integrators
- **Related:** [raglogs](https://github.com/LAA-Software-Engineering/raglogs)

---

## 1. Purpose & scope

[raglogs](https://github.com/LAA-Software-Engineering/raglogs) is a log-analysis tool that ingests logs for a time window, clusters/fingerprints them, compares against a baseline, detects triggers (deploys, restarts, circuit breakers, token-expiry bursts), and emits an **evidence-grounded** incident explanation. Its defining property — "the LLM never sees raw logs, only curated facts" — makes it a strong candidate to sit behind an incident-management system and auto-explain incidents.

Today raglogs is an early single-user CLI/POC. This document specifies the concrete features it must implement to become:

1. **Easier to integrate** — safe to call as a network service from another system.
2. **More robust** — safe to run as a shared, always-on production service during incidents.

Scope is limited to raglogs itself. The consumer-side wiring (invoking the API, storing evidence, notifying responders, pre-populating post-incident reviews) is covered only where it constrains the raglogs API contract.

## 2. Current state (baseline)

As of this writing, raglogs ships:

- **Stack:** Python 3.10+, FastAPI/Uvicorn, Typer CLI, SQLAlchemy + Alembic, Pydantic settings, PostgreSQL 14+ with pgvector, Docker Compose.
- **Source layout:** `src/{adapters/file, api/routes, cli/commands, config, core/*, db, utils}` where `core/` holds `clustering, compare, explain, ingestion, llm, normalization, parsing, retrieval, timeline`.
- **Pipeline:** file adapter → parser → normalization → SHA-256 fingerprint (16-char cluster key) → Postgres storage → clustering → baseline compare → importance ranking → evidence assembly → LLM or deterministic template output.
- **HTTP API:** `GET /health`, `POST /ingestions`, `GET /ingestions/{job_id}`, `POST /query/{explain,ask,clusters,timeline,compare}`, `GET /config`.
- **LLM:** OpenAI (`gpt-4.1-mini` default), Ollama, or any OpenAI-compatible endpoint; `--no-llm` deterministic fallback.

### 2.1 Gap summary

| # | Gap | Category | Impact |
|---|-----|----------|--------|
| G1 | File-only ingestion; no CloudWatch/Loki/Datadog/K8s adapters | Integrate | Every integrator must build an export-to-file shim |
| G2 | No API authentication | Integrate + Robust | Cannot expose over a network |
| G3 | No API versioning / published OpenAPI artifact | Integrate | No safe client codegen; breaking changes silent |
| G4 | Batch-file ingestion only; no push/stream | Integrate | Re-upload files per query |
| G5 | Poll-only async jobs; no completion callback | Integrate | Awkward orchestration from another service |
| G6 | No ingest idempotency / dedup | Integrate + Robust | Retries double-count clusters |
| G7 | Prose-oriented output; no stable JSON evidence schema | Integrate | Consumers scrape text |
| G8 | Single-tenant; no scope isolation | Robust | Cross-incident log leakage |
| G9 | No rate limiting / backpressure | Robust | Ingest & LLM overwhelm under load |
| G10 | No LLM retry/timeout/auto-fallback | Robust | Incident-time failures |
| G11 | Keyword-only `ask`; pgvector semantic retrieval unbuilt | Robust (value) | No similar-incident recall |
| G12 | Only `/health`; no metrics/tracing | Robust | Blind when raglogs itself degrades |
| G13 | No data retention / lifecycle | Robust | Unbounded Postgres growth |
| G14 | Env/flag-only config; limited per-request override | Robust | No per-tenant tuning |

## 3. Goals & non-goals

### Goals

- Make the HTTP API safe and pleasant to call from another service (including non-Python consumers).
- Preserve raglogs' core design invariant: **the LLM only ever sees curated facts.**
- Keep the deterministic (`--no-llm`) path as a first-class, always-available fallback.
- Ship incrementally; each phase is independently useful.

### Non-goals

- Rewriting the clustering/fingerprint/evidence core (it is the well-done part).
- Building a full multi-org SaaS control plane. "Multi-tenancy" here means **scope isolation**, not billing/orgs.
- Replacing dashboards or search tools. raglogs still "explains incidents"; it is not Grafana or grep.

## 4. Design principles

1. **Additive, versioned changes.** New behavior lands behind `/v1` and feature flags; existing CLI behavior is preserved.
2. **Fail safe, not silent.** LLM/adapters failing must degrade to deterministic output, and must be observable.
3. **Scope is mandatory in the service, optional in the CLI.** The single-user CLI experience stays simple; the networked service requires an explicit scope on every write and read.
4. **Machine output is the contract; prose is a rendering.** Every query returns structured JSON; human text is an optional field derived from it.

---

## 5. Detailed feature designs

Each feature below has: motivation, design, interface/schema changes, data-model impact, config, and failure modes.

### 5.1 Source adapters (G1) — *the top integration blocker*

**Motivation.** raglogs is only as useful as the logs it can reach. File-only ingestion forces every integrator to export logs to disk first. Most real deployments already have logs in CloudWatch, Loki, or Datadog; the highest-leverage adapters are pull-based sources.

**Design.** Introduce a pluggable `SourceAdapter` interface alongside the existing `adapters/file/`:

```python
# src/adapters/base.py
class SourceAdapter(Protocol):
    name: str  # "file" | "cloudwatch" | "loki" | ...

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        """Resolve a spec into concrete streams (log groups, label sets, files)."""

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        """Yield raw lines for a stream within a time window; must be resumable."""
```

`RawLogLine` feeds directly into the existing parser → normalization → fingerprint pipeline, so adapters do not touch the core.

Adapters to build, in priority order:

1. **CloudWatch Logs** — pull by log group + optional filter pattern, using `FilterLogEvents`/`StartQuery` (Logs Insights) for larger windows. Auth via standard AWS credential chain (IRSA in-cluster). A common first deliverable for AWS-hosted consumers.
2. **Loki** — pull via `query_range` with LogQL label selectors; native pagination.
3. **Datadog / Kubernetes** — roadmap items already named upstream; same interface.

**SourceSpec** is what integrators send instead of file paths:

```json
{
  "adapter": "cloudwatch",
  "params": {
    "log_group": "/aws/lambda/my-service",
    "filter_pattern": "?ERROR ?panic ?timeout",
    "region": "us-east-1"
  },
  "service": "my-service",
  "env": "ci"
}
```

**Data model.** Add `source_adapter` and `source_ref` columns to the ingestion job + log rows for provenance ("which stream did this cluster come from").

**Config.** Per-adapter blocks under `ADAPTER_CLOUDWATCH_*`, `DATADOG_*`, etc. Adapters are optional; absent credentials disable the adapter and surface a clear `/health` sub-status.

**Failure modes.** Adapter unreachable → job fails with a typed error (`ADAPTER_UNAVAILABLE`) and does not partially ingest silently. Partial reads (rate-limited AWS API) → resumable via `LogStreamRef` cursor; job reports `partial: true`.

### 5.2 API authentication (G2)

**Motivation.** The HTTP API currently has no auth. It cannot be exposed to another service as-is.

**Design.** Two layers:

1. **Service-to-service:** static API keys via `Authorization: Bearer <key>`, keys stored hashed (argon2) in a `api_keys` table, each key bound to a **scope** (see 5.8) and a role (`ingest`, `query`, `admin`). This is enough for a server-to-server integration.
2. **Optional OIDC:** validate a JWT from an identity provider when a key is not used, for human/API-explorer access. Behind a flag; not required for a basic service integration.

Middleware runs before every route except `/health` and `/metrics`. Rejected requests return `401`/`403` with a typed error body.

**Config.**

```
AUTH_ENABLED=true
AUTH_MODE=api_key            # api_key | oidc | both
OIDC_ISSUER=...              # when oidc enabled
```

**Failure modes.** `AUTH_ENABLED=false` is allowed only when bound to loopback; binding to `0.0.0.0` with auth disabled logs a loud warning and (optionally) refuses to start.

### 5.3 API versioning + published OpenAPI (G3)

**Motivation.** Consumers want a generated client and a stable contract. FastAPI already produces an OpenAPI spec — it just needs to be versioned and published as a build artifact.

**Design.**

- Move all query/ingest routes under `/v1/`. Keep unversioned routes as deprecated aliases for one release.
- Emit `openapi.json` as a CI artifact on every tagged release; publish to the repo's releases.
- Add codegen targets (e.g. a `make client-go` running `oapi-codegen`, plus a typed Python client) so consumers can vendor a generated client instead of hand-rolling HTTP.
- Adopt an explicit compatibility policy: additive changes within `v1`; breaking changes require `v2`.

**Interface.** No behavior change beyond path prefix; the win is contract stability and codegen.

### 5.4 Push / streaming ingestion (G4)

**Motivation.** Batch-file ingestion means re-uploading files per analysis. A service behind a live incident tool wants to push lines directly and/or keep a source tailing.

**Design.** Two additions:

1. **Direct push endpoint** — `POST /v1/ingestions/lines` accepting NDJSON of pre-parsed or raw lines, for callers that already hold the logs (e.g. forwarding a captured buffer). Bounded by rate limiting (5.9).
2. **Tail mode** — a long-lived ingestion job that periodically re-runs a `SourceAdapter.read` from its last cursor, for continuous sources. Managed by the worker; controllable via `POST /v1/ingestions/{id}:pause|resume|stop`.

**Data model.** Ingestion job gains `mode` (`batch` | `push` | `tail`), `cursor`, and `last_polled_at`.

**Failure modes.** Backpressure (5.9) applies; when the queue is full, push returns `429` with `Retry-After`. Tail jobs that error N consecutive times auto-pause and surface in `/health`.

### 5.5 Completion callbacks / webhooks-out (G5)

**Motivation.** Async ingestion is poll-only (`GET /ingestions/{job_id}`). Polling from another service is wasteful and racy.

**Design.** Optional `callback_url` on `POST /v1/ingestions`. On terminal state (`succeeded`/`failed`/`partial`) raglogs POSTs a signed payload:

```json
{
  "job_id": "ing_01H...",
  "status": "succeeded",
  "scope": "incident:INC-1234",
  "counts": { "lines": 48213, "clusters": 37 },
  "partial": false,
  "signature": "sha256=..."   // HMAC over body with per-key secret
}
```

Delivery uses bounded retries with exponential backoff (max ~5 attempts, jittered); failures are logged and the job status remains queryable so polling still works as a fallback. HMAC signing lets the consumer verify authenticity.

**Config.** `WEBHOOK_MAX_RETRIES`, `WEBHOOK_TIMEOUT`.

### 5.6 Ingest idempotency & dedup (G6)

**Motivation.** Callers commonly retry. Re-ingesting an overlapping window must not double-count clusters, or every count/change-ratio becomes wrong.

**Design.** Two mechanisms:

1. **Request idempotency:** `Idempotency-Key` header on `POST /v1/ingestions`. A repeat within a TTL returns the original job rather than starting a new one.
2. **Content dedup:** each log row already normalizes to a fingerprint; add a per-scope unique constraint on `(scope, source_ref, original_line_hash, timestamp)` so re-reading the same physical lines is a no-op upsert. Cluster counts derive from distinct rows, making ingestion **idempotent by construction**.

**Data model.** New unique index; `original_line_hash` (SHA-256 of the raw line, distinct from the normalized fingerprint) added to log rows.

**Failure modes.** Hash collision risk is negligible at SHA-256; the timestamp+source_ref tuple further disambiguates.

### 5.7 Stable JSON evidence schema (G7)

**Motivation.** `explain`/`timeline` output is prose-first (with optional `format: text`). Consumers need to store discrete fields (confidence, trigger, clusters) and render their own UI, not regex prose.

**Design.** Every `/v1/query/*` response returns a versioned structured body; rendered text becomes an optional `rendered_text` field derived from it. Canonical `explain` schema:

```json
{
  "schema_version": "1.0",
  "scope": "incident:INC-1234",
  "window": { "from": "2026-07-28T14:17:00Z", "to": "2026-07-28T14:32:00Z" },
  "confidence": { "label": "medium-high", "score": 0.72 },
  "summary": "Primary error spike in payment-svc following a deploy at 14:29.",
  "trigger": {
    "detected": true,
    "type": "deploy",
    "service": "payment-svc",
    "at": "2026-07-28T14:29:11Z",
    "correlation": "precedes_primary_spike"
  },
  "primary_cluster": {
    "fingerprint": "a1b2c3d4e5f60718",
    "template": "connection refused to <HOST>:<PORT>",
    "count": 2145,
    "baseline_count": 12,
    "change_ratio": 165.6,
    "services": ["payment-svc"],
    "levels": ["error"],
    "first_seen": "2026-07-28T14:29:40Z",
    "last_seen": "2026-07-28T14:31:58Z"
  },
  "secondary_clusters": [ "...same shape..." ],
  "evidence": [
    { "kind": "trigger", "ref": "deploy", "detail": "...", "source_ref": "cw:/aws/lambda/..." }
  ],
  "llm": { "used": true, "provider": "openai", "model": "gpt-4.1-mini", "fell_back": false },
  "rendered_text": "..."
}
```

`timeline` and `compare` get analogous versioned schemas (buckets/events for timeline; per-cluster deltas with `+ / - / ↑ / ↓ / +⚡` markers for compare). `schema_version` lets consumers pin.

**Non-negotiable:** the `llm` block always reports whether the LLM was used and whether it fell back to deterministic templates, so consumers know the provenance of every summary.

### 5.8 Scope isolation / "multi-tenancy" (G8)

**Motivation.** A shared service serving many incidents must not let one incident's logs contaminate another's baseline or analysis.

**Design.** Introduce a mandatory `scope` string on every write and read in the **service** (optional/`default` in the CLI). Recommended convention: `incident:<id>`, `service:<name>`, or `env:<name>`. Scope is:

- Stored on ingestion jobs and log rows.
- Bound to each API key (a key may be pinned to one scope or allowed to pass scope explicitly).
- Enforced in every query's `WHERE scope = ...` — including baseline comparison, so a baseline is always drawn from the same scope.

**Data model.** `scope` column + composite indexes `(scope, timestamp)`, `(scope, service, env, fingerprint)` — extending the existing indexes rather than replacing them.

**Migration.** Existing rows get `scope = 'default'`. Backward compatible.

**Failure modes.** A service request without a resolvable scope → `400 SCOPE_REQUIRED`. Prevents accidental global reads.

### 5.9 Rate limiting & backpressure (G9)

**Motivation.** A large incident dumps a lot of logs; unbounded ingest and LLM fan-out risk overload and cost blowups.

**Design.**

- **API rate limiting:** token-bucket per API key on ingest and query routes; `429` + `Retry-After` when exceeded.
- **Ingest backpressure:** a bounded work queue; when full, `POST /ingestions` returns `429` rather than accepting unbounded work. Tail jobs respect the same ceiling.
- **LLM concurrency cap:** a semaphore around LLM calls with a global max in flight, independent of API concurrency, so a burst of `explain` calls cannot fan out unbounded requests to the provider.

**Config.** `RATELIMIT_*`, `INGEST_QUEUE_MAX`, `LLM_MAX_CONCURRENCY`.

### 5.10 LLM resilience (G10)

**Motivation.** External LLM calls fail, hang, and cost money. raglogs already has the ideal fallback — deterministic templates — but only via a manual `--no-llm` flag.

**Design.**

- **Timeout + bounded retries** (exponential backoff, jitter) around every LLM call.
- **Automatic fallback:** on timeout/error/exhausted retries, the pipeline falls back to the deterministic template output and sets `llm.fell_back = true` in the response. The request still succeeds. This turns the existing offline mode into an automatic safety net.
- **Cost/token ceiling:** hard per-request `max_tokens` and an estimated-cost guard; requests exceeding the ceiling either trim evidence (respecting `MAX_EVIDENCE_ITEMS`) or fall back.
- **Circuit breaker:** after N consecutive LLM failures, open the breaker and serve deterministic output for a cool-down period, surfaced in `/health`.

**Config.** `LLM_TIMEOUT`, `LLM_MAX_RETRIES`, `LLM_MAX_TOKENS`, `LLM_BREAKER_THRESHOLD`.

**Invariant preserved:** fallback output is still fact-curated; degradation lowers polish, never grounding.

### 5.11 Semantic retrieval & cluster merging (G11) — *the value unlock*

**Motivation.** `ask` is keyword-only and embeddings are disabled by default, despite pgvector being in the stack. Semantic retrieval + cluster merging is what enables cross-incident recall: "have we seen this failure signature before?" — the single most valuable capability for post-incident review.

**Design.**

1. **Enable embeddings pipeline:** on ingest (opt-in `--with-embeddings` already exists), embed cluster templates using OpenAI `text-embedding-3-small` or a local model; store vectors in the existing pgvector column.
2. **Semantic `ask`:** replace/augment keyword retrieval with pgvector nearest-neighbor over cluster templates + evidence.
3. **Semantic cluster merging:** post-fingerprint, merge clusters whose templates are near-duplicates in embedding space (e.g. same error with differently-normalized dynamic parts), reducing cluster fragmentation.
4. **Cross-incident similarity endpoint:** `POST /v1/query/similar` — given a scope's primary cluster(s), return prior incidents (other scopes, subject to key permissions) with nearby fingerprints. This is what powers "we saw this in INC-1188."

**Data model.** Ensure the vector column is indexed (IVFFlat/HNSW) for ANN; add a `cluster_embedding` table keyed by `(scope, fingerprint)`.

**Failure modes.** Embeddings provider down → fall back to keyword retrieval (mirror of the LLM fallback pattern); `similar` degrades to fingerprint-equality matching.

### 5.12 Self-observability (G12)

**Motivation.** During an incident, if raglogs is slow, its consumer is blind. It needs to expose its own health.

**Design.**

- **Structured JSON logs** with request IDs and scope.
- **Prometheus `/metrics`:** ingest latency & line counts, cluster counts, query latency by endpoint, LLM latency/cost/fallback rate, breaker state, queue depth.
- **Tracing:** OpenTelemetry spans across ingest → cluster → explain, with trace IDs returned in responses so consumers can correlate.
- **Richer `/health`:** sub-statuses for DB, each configured adapter, LLM provider, and breaker state.

### 5.13 Data retention & lifecycle (G13)

**Motivation.** Ingested logs + vectors in Postgres grow unbounded and become an operational liability.

**Design.**

- **TTL per scope:** configurable retention (default e.g. 30 days for raw log rows; longer for cluster summaries + embeddings, which are small and power similar-incident recall).
- **Tiering:** keep compact cluster/evidence summaries and embeddings after raw lines are purged, so historical "have we seen this?" still works cheaply.
- **Purge job:** a scheduled worker task; emits metrics on rows reclaimed.

**Config.** `RETENTION_RAW`, `RETENTION_SUMMARY`.

### 5.14 Per-request config overrides (G14)

**Motivation.** Config is env/flag/`.env` only; a shared service wants per-call tuning (baseline window, max clusters) without redeploys.

**Design.** Promote the key tunables to first-class, validated fields on the query request body, overriding server defaults per call:

```json
{
  "scope": "incident:INC-1234",
  "window": { "from": "...", "to": "..." },
  "baseline_window": "24h",
  "max_clusters": 10,
  "max_evidence_items": 8,
  "llm": { "provider": "openai", "enabled": true }
}
```

Server defaults (the existing env vars) apply when a field is omitted. Precedence: request field > per-key default > server default.

---

## 6. Target architecture

```
                       Integrating system (any language)
   incident worker ──POST /v1/ingestions {scope, SourceSpec, callback_url}──▶ raglogs API
        │  ◀──────────── HMAC-signed completion webhook ──────────────────────  (FastAPI /v1)
        │  ──POST /v1/query/explain {scope, window} ─────────────────────────▶     │
        │  ◀── structured evidence JSON (confidence, trigger, clusters) ──────     │
        ▼                                                                          ▼
  incident record + PIR draft                                          ┌──────────────────┐
  + responder notification                                             │  auth │ ratelimit │
                                                                       ├──────────────────┤
                                    SourceAdapters                     │ ingest worker /   │
   CloudWatch ◀──┐                  (pluggable)                        │ tail scheduler    │
   Loki       ◀──┼── read(window) ──▶ parse ▶ normalize ▶ fingerprint ─┤ clustering        │
   Datadog/K8s◀──┘                                                     │ baseline/compare  │
                                                                       │ evidence assembly │
                                          curated facts only ─────────▶│ LLM (retry/breaker│
                                                                       │  → deterministic) │
                                                                       ├──────────────────┤
                                                                       │ Postgres+pgvector │
                                                                       │ scope-partitioned │
                                                                       │ + embeddings (ANN)│
                                                                       └──────────────────┘
                                     /metrics /health ──▶ Prometheus / Grafana
```

Core pipeline (parse → normalize → fingerprint → cluster → compare → evidence) is unchanged; all new work is at the edges (adapters, auth, scope, resilience, observability) and in finishing the embeddings path.

## 7. Phased rollout

Each phase is independently shippable and useful.

| Phase | Theme | Features | Outcome |
|-------|-------|----------|---------|
| **0** | Contract foundation | G2 auth, G3 `/v1` + OpenAPI/client codegen, G7 JSON schema, G8 scope | raglogs is safely callable with a stable, isolated contract |
| **1** | First real integration | G1 CloudWatch adapter, G5 callbacks, G6 idempotency | A consumer can auto-ingest logs on incident creation and get a grounded `explain` |
| **2** | Production hardening | G9 rate limit/backpressure, G10 LLM resilience, G12 observability | Safe to run always-on during real incidents |
| **3** | The value unlock | G11 semantic retrieval + cluster merging + `/similar`, G4 push/tail | Similar-incident recall powers PIR drafting |
| **4** | Operability | G13 retention, G14 per-request overrides, Loki/Datadog adapters | Sustainable, tunable, multi-source |

**Minimum viable integration = Phase 0 + Phase 1.** That is the smallest set that lets a consumer call raglogs on incident creation and attach a grounded summary.

## 8. Security considerations

- **AuthN/Z:** hashed API keys, scope-bound, least-privilege roles (`ingest`/`query`/`admin`). No unauthenticated network exposure.
- **Secrets:** LLM/adapter credentials from the environment/secret store, never persisted in logs or the evidence packet.
- **Data sensitivity:** logs may contain PII. raglogs' normalization already strips UUIDs/IPs/emails/tokens before fingerprinting — but raw rows are stored; retention (5.13) and scope isolation (5.8) bound exposure. Consider a "store-normalized-only" mode for high-sensitivity scopes.
- **Webhook authenticity:** HMAC-signed callbacks; the consumer verifies before acting.
- **LLM egress:** the "curated facts only" invariant already limits what leaves the trust boundary; the automatic-fallback path means an LLM outage never forces raw-log exposure as a workaround.

## 9. Testing strategy

- **Unit** (extend existing `tests/unit`): each adapter's parse/cursor logic, scope enforcement, idempotency upsert, LLM fallback trigger, schema serialization.
- **Integration** (extend `tests/integration`): full ingest→explain per adapter against a Postgres+pgvector container; idempotent re-ingest asserts stable counts; breaker/fallback under injected LLM failure.
- **Contract tests:** validate every response against its `schema_version` JSON schema; a consumer-side contract test consumes the published OpenAPI to catch breaking changes in CI.
- **Load:** ingest a large synthetic incident to validate backpressure and rate limiting return `429` rather than degrading.

## 10. Open questions

1. **Deployment locus:** raglogs as a shared cluster service, or one instance per environment? Per-env is simpler for scope/credentials; shared is cheaper.
2. **Baseline source for serverless logs:** what is a good "healthy window" baseline for spiky serverless traffic — trailing 24h (default) or same-time-yesterday?
3. **`/similar` cross-scope permissions:** should similar-incident recall cross scopes by default, or require an explicit `admin`-scoped key? (Privacy vs. usefulness.)
4. **Upstream vs. fork:** land these features upstream in `LAA-Software-Engineering/raglogs`, or maintain a fork? The mismatched clone URLs in the README suggest the ownership story needs clarifying first.
5. **Embeddings provider:** OpenAI `text-embedding-3-small` (managed, egress) vs. a local model (no egress, more ops). Sensitivity of log content likely decides this.

## 11. Appendix — feature-to-gap traceability

| Feature § | Gap | Phase | Category |
|-----------|-----|-------|----------|
| 5.1 Source adapters | G1 | 1/4 | Integrate |
| 5.2 Auth | G2 | 0 | Both |
| 5.3 Versioning/OpenAPI | G3 | 0 | Integrate |
| 5.4 Push/stream | G4 | 3 | Integrate |
| 5.5 Callbacks | G5 | 1 | Integrate |
| 5.6 Idempotency | G6 | 1 | Both |
| 5.7 JSON schema | G7 | 0 | Integrate |
| 5.8 Scope | G8 | 0 | Robust |
| 5.9 Rate limit | G9 | 2 | Robust |
| 5.10 LLM resilience | G10 | 2 | Robust |
| 5.11 Semantic retrieval | G11 | 3 | Robust/value |
| 5.12 Observability | G12 | 2 | Robust |
| 5.13 Retention | G13 | 4 | Robust |
| 5.14 Per-request config | G14 | 4 | Robust |
