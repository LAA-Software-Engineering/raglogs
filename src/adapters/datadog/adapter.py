from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

import httpx
import orjson
from dateutil import parser as dateutil_parser
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.adapters.base import LogStreamRef, RawLogLine, SourceSpec, TimeWindow
from src.core.errors import AdapterUnavailableError

PAGE_SIZE_MAX = 1000
PAGE_SIZE_DEFAULT = 1000
MAX_ROWS_DEFAULT = 10000
SEARCH_PATH = "/api/v2/logs/events/search"

_TRACE_KEYS = (
    "trace_id",
    "traceId",
    "dd.trace_id",
    "dd.traceId",
)
_REQUEST_KEYS = (
    "request_id",
    "requestId",
    "req_id",
    "correlation_id",
    "correlationId",
    "http.request_id",
    "http.requestId",
)


def api_base_url(site: str) -> str:
    """Build the Datadog API origin from a site name or full URL.

    Accepts ``datadoghq.com``, ``us3.datadoghq.com``, ``app.datadoghq.eu``,
    ``api.datadoghq.com``, or a full ``https://api...`` origin.
    """
    site = (site or "").strip().rstrip("/")
    if not site:
        site = "datadoghq.com"
    if site.startswith("https://") or site.startswith("http://"):
        return site
    if site.startswith("app."):
        site = site[4:]
    if site.startswith("api."):
        return f"https://{site}"
    return f"https://api.{site}"


def _lookup(data: dict[str, Any], *keys: str) -> Any:
    """Return the first present key, including dotted paths (``dd.trace_id``)."""
    for key in keys:
        if key in data:
            return data[key]
        if "." not in key:
            continue
        current: Any = data
        found = True
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                found = False
                break
        if found:
            return current
    return None


def _tag_value(tags: list[Any], name: str) -> Optional[str]:
    prefix = f"{name}:"
    for tag in tags:
        if isinstance(tag, str) and tag.startswith(prefix):
            value = tag[len(prefix) :]
            if value:
                return value
    return None


def _as_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def map_datadog_event(event: dict[str, Any]) -> dict[str, Any]:
    """Map a Datadog v2 log event onto raglogs JSON field aliases.

    Datadog reserved attributes (``status``, ``service``, ``host``, ``message``,
    ``timestamp``) plus ``env`` / trace / request IDs from tags or nested
    custom attributes. Unknown envelope fields are dropped so core parsing
    stays source-agnostic.
    """
    attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    nested = attrs.get("attributes") if isinstance(attrs.get("attributes"), dict) else {}
    tags = attrs.get("tags") if isinstance(attrs.get("tags"), list) else []

    env = (
        _as_str(attrs.get("env"))
        or _tag_value(tags, "env")
        or _tag_value(tags, "environment")
        or _as_str(_lookup(nested, "env", "environment"))
    )
    payload: dict[str, Any] = {}
    timestamp = attrs.get("timestamp")
    if timestamp is not None:
        payload["timestamp"] = timestamp
    message = attrs.get("message")
    if message is not None:
        payload["message"] = message
    status = attrs.get("status")
    if status is not None:
        payload["level"] = status
    service = attrs.get("service")
    if service is not None:
        payload["service"] = service
    host = attrs.get("host")
    if host is not None:
        payload["host"] = host
    if env is not None:
        payload["env"] = env
    trace_id = _as_str(_lookup(nested, *_TRACE_KEYS) or _lookup(attrs, *_TRACE_KEYS))
    if trace_id is not None:
        payload["trace_id"] = trace_id
    request_id = _as_str(_lookup(nested, *_REQUEST_KEYS) or _lookup(attrs, *_REQUEST_KEYS))
    if request_id is not None:
        payload["request_id"] = request_id
    return payload


def parse_event_timestamp(event: dict[str, Any]) -> Optional[datetime]:
    """Parse the Datadog event timestamp for RawLogLine.received_at."""
    attrs = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    raw = attrs.get("timestamp")
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(raw, str):
        try:
            dt = dateutil_parser.parse(raw)
        except (ValueError, OverflowError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _iso8601(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _int_param(params: dict[str, Any], key: str, default: int) -> int:
    raw = params.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _clamp_page_size(value: int) -> int:
    return max(1, min(value, PAGE_SIZE_MAX))


def _indexes_param(params: dict[str, Any]) -> list[str]:
    raw = params.get("indexes")
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    if isinstance(raw, list):
        return [str(part).strip() for part in raw if str(part).strip()]
    return []


def _is_retryable(exc: BaseException) -> bool:
    """Retry 429 / 5xx and transport failures — not 4xx auth or validation errors."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


def _client() -> httpx.Client:
    return httpx.Client(timeout=60.0)


class DatadogSourceAdapter:
    """SourceAdapter over the Datadog Logs Search API (v2).

    Auth via ``RAGLOGS_ADAPTER_DATADOG_API_KEY`` + ``RAGLOGS_ADAPTER_DATADOG_APP_KEY``
    — keys are never accepted as SourceSpec params.
    """

    name = "datadog"

    def __init__(
        self,
        api_key: str = "",
        app_key: str = "",
        site: str = "datadoghq.com",
        page_size: int = PAGE_SIZE_DEFAULT,
        max_rows: int = MAX_ROWS_DEFAULT,
    ):
        self.api_key = api_key
        self.app_key = app_key
        self.site = site or "datadoghq.com"
        self.page_size = _clamp_page_size(page_size)
        self.max_rows = max(1, max_rows)

    def check_available(self) -> None:
        """Raise AdapterUnavailableError if API/app keys are missing.

        Local-only — used by /health. Does not call Datadog.
        """
        if not self.api_key or not self.app_key:
            raise AdapterUnavailableError(
                "datadog adapter requires RAGLOGS_ADAPTER_DATADOG_API_KEY "
                "and RAGLOGS_ADAPTER_DATADOG_APP_KEY"
            )

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        query = spec.params.get("query")
        if query is None or (isinstance(query, str) and not query.strip()):
            query = "*"
        else:
            query = str(query)
        indexes = _indexes_param(spec.params)
        page_size = _clamp_page_size(_int_param(spec.params, "page_size", self.page_size))
        max_rows = max(1, _int_param(spec.params, "max_rows", self.max_rows))
        site = spec.params.get("site") or self.site
        stream_id = f"{','.join(indexes)}|{query}" if indexes else query
        yield LogStreamRef(
            adapter=self.name,
            stream_id=stream_id,
            metadata={
                "query": query,
                "indexes": indexes,
                "page_size": page_size,
                "max_rows": max_rows,
                "site": site,
            },
        )

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        self.check_available()
        query = ref.metadata.get("query") or ref.stream_id or "*"
        indexes = ref.metadata.get("indexes") or []
        page_size = _clamp_page_size(int(ref.metadata.get("page_size") or self.page_size))
        max_rows = max(1, int(ref.metadata.get("max_rows") or self.max_rows))
        site = ref.metadata.get("site") or self.site
        url = api_base_url(str(site)) + SEARCH_PATH
        cursor = ref.cursor
        yielded = 0

        client = _client()
        try:
            while yielded < max_rows:
                remaining = max_rows - yielded
                payload: dict[str, Any] = {
                    "filter": {
                        "query": query,
                        "from": _iso8601(window.start),
                        "to": _iso8601(window.end),
                    },
                    "sort": "timestamp",
                    "page": {"limit": min(page_size, remaining)},
                }
                if indexes:
                    payload["filter"]["indexes"] = indexes
                if cursor:
                    payload["page"]["cursor"] = cursor

                try:
                    page = self._search_logs(client, url, payload)
                except httpx.HTTPError as e:
                    raise AdapterUnavailableError(
                        f"Datadog Logs API unavailable for {ref.stream_id}: {e}"
                    ) from e

                events = page.get("data")
                if not events:
                    ref.cursor = None
                    break

                for event in events:
                    if not isinstance(event, dict):
                        continue
                    mapped = map_datadog_event(event)
                    text = orjson.dumps(mapped).decode()
                    yield RawLogLine(
                        text=text,
                        source_ref=ref.stream_id,
                        received_at=parse_event_timestamp(event),
                    )
                    yielded += 1
                    if yielded >= max_rows:
                        break

                after = (page.get("meta") or {}).get("page", {}).get("after")
                if not after or after == cursor or yielded >= max_rows:
                    # Keep the next-page cursor when we hit max_rows so --resume-job
                    # can continue; clear it when the window is exhausted.
                    ref.cursor = after if yielded >= max_rows and after and after != cursor else None
                    break
                cursor = after
                ref.cursor = cursor
        finally:
            client.close()

    def _headers(self) -> dict[str, str]:
        return {
            "DD-API-KEY": self.api_key,
            "DD-APPLICATION-KEY": self.app_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _search_logs(self, client: httpx.Client, url: str, payload: dict[str, Any]) -> dict:
        response = client.post(url, headers=self._headers(), json=payload)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as e:
            raise AdapterUnavailableError(f"Datadog Logs API returned invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise AdapterUnavailableError("Datadog Logs API returned a non-object body")
        return data
