# Log sources & formats

raglogs ingests from local files and from pull adapters. Everything after
ingestion — normalization, fingerprinting, clustering, and the explain
pipeline — is **source-agnostic**: an adapter's only job is to yield
`RawLogLine` objects that the parser maps onto `ParsedLogLine`.

## Adapters

| Adapter | `--adapter` | Source |
| --- | --- | --- |
| File | `file` (default) | Local files, directories, glob patterns |
| CloudWatch | `cloudwatch` | AWS CloudWatch Logs (AWS credential chain) |
| Datadog | `datadog` | Datadog Logs Search API |
| Loki | `loki` | Grafana Loki |
| Kubernetes | `k8s` | `kubectl`-style pod log exports |

See [cli.md](cli.md) for `raglogs ingest` usage and [api.md](api.md) for the
`POST /v1/ingestions` params and tail jobs.

### Adding a source adapter

New source adapters go in `src/adapters/` and implement `SourceAdapter`
(`discover` / `read`), yielding `RawLogLine` objects that the existing parser
maps onto `ParsedLogLine`. The normalization, fingerprinting, storage,
clustering, and explain pipeline stays untouched — keep the core pipeline
source-agnostic and put all source-specific handling in the adapter.

### Datadog adapter limits

- **Auth:** `DD-API-KEY` + `DD-APPLICATION-KEY` (application key needs
  `logs_read_data`). Keys come from env only — never CLI `--param`.
- **Endpoint:** `POST https://api.<site>/api/v2/logs/events/search` with an
  absolute `from`/`to` window (relative ranges drop events while paginating).
- **Pagination:** cursor from `meta.page.after`; resume with `--resume-job`.
- **Page size:** default 1000, Datadog hard max 1000 (`--param page_size=N` or
  `DATADOG_PAGE_SIZE`).
- **Max rows per run:** default 10000 (`--param max_rows=N` or
  `DATADOG_MAX_ROWS`). Hitting the cap saves the next cursor for resume.
- **Rate limits:** HTTP 429 and 5xx are retried up to 3 times with exponential
  backoff; a persistent failure marks the job `ADAPTER_UNAVAILABLE` (or
  `partial: true` if some events already landed).
- **Field mapping:** Datadog `status` → `level`; `service` / `host` / `message`
  / `timestamp` pass through; `env` from the `env:` tag or attributes;
  `trace_id` / `request_id` from nested custom attributes when present. Other
  nested Datadog attributes are dropped so core parsing stays source-agnostic.

## Log formats

By default (`--format auto`) raglogs samples the first non-empty line of each
file to detect JSON vs plain text. Override with `--format json` or
`--format text`.

### JSON logs

raglogs accepts structured JSON logs and resolves common field aliases
automatically.

```json
{"timestamp": "2026-03-12T22:01:10Z", "level": "error", "service": "billing-worker", "message": "connection refused"}
{"ts": "2026-03-12T22:01:10Z", "severity": "ERROR", "app": "api", "msg": "upstream returned 500"}
{"@timestamp": "2026-03-12T22:01:10Z", "log_level": "WARN", "logger": "worker", "log": "Queue depth exceeded threshold"}
```

Supported field aliases:

| Field | Accepted names |
| --- | --- |
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
2026-03-12T22:01:10Z ERROR billing-worker connection refused
[2026-03-12T22:01:10Z] [WARN] High memory usage detected on worker-3
```

raglogs uses regex heuristics to extract timestamp, level, service, and message
from common plain-text formats. If service is not found in the line, it can be
provided with `--service` or inferred from the filename.
