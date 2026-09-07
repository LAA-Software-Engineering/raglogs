# How it works


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
- **Fail open.** If the embeddings backend is missing, raises, or returns unusable vectors, clustering continues with the fingerprint-only set. The same fail-open applies when upserting `cluster_embeddings` for similar-incident search: a provider outage never fails the cluster run.
- **Cluster template persist.** After clustering, raglogs upserts each cluster's representative template into `cluster_embeddings` keyed by `(scope, fingerprint)` when the embeddings provider is available. Similar-incident search queries those rows (not raw `log_embeddings`). Skip happens automatically when `EMBEDDINGS_PROVIDER=disabled`.
- **Ask vs merge vs similar.** Semantic `ask` uses the *stored* `log_embeddings` table (populated by `raglogs ingest --with-embeddings`) and `ASK_SEMANTIC_MIN_SIMILARITY` (default **0.75**). Cluster merge still uses its own in-memory pass and threshold. Similar-incident search (`POST /v1/query/similar`) uses the `cluster_embeddings` table (upserted at analysis time when an embeddings provider is available) and `SIMILAR_SEMANTIC_MIN_SIMILARITY` (default **0.80**). Compare still applies its own heuristic collapse for webhook retries / queue growth after clustering.

Local embeddings require the optional extra: `pip install 'raglogs[local-embeddings]'` (`sentence-transformers`) **and** a model that emits exactly 1536 dims to match the stored column. The common models don't (`all-MiniLM-L6-v2` = 384, `all-mpnet-base-v2` = 768), so `EMBEDDINGS_PROVIDER=local` with one of them is rejected rather than silently producing no embeddings: `ingest --with-embeddings` hard-exits, and API startup logs it at `error` and continues. On the analysis-time merge hot path a missing extra or bad config degrades to skip (logged at `error`), never a crash.

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
