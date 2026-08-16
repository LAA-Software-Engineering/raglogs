from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.adapters.base import LogStreamRef, RawLogLine, SourceSpec, TimeWindow
from src.core.errors import AdapterUnavailableError

PAGE_SIZE = 5000  # Loki query_range default max per request
QUERY_PATH = "/loki/api/v1/query_range"


def _query_url(base_url: str) -> str:
    """Join a Loki origin (or an already-suffixed /loki/api/v1 URL) with query_range."""
    base = base_url.rstrip("/")
    if base.endswith("/loki/api/v1"):
        return f"{base}/query_range"
    return f"{base}{QUERY_PATH}"


def _format_labels(stream: dict[str, Any]) -> str:
    parts = [f'{key}="{value}"' for key, value in sorted(stream.items())]
    return "{" + ", ".join(parts) + "}"


def _ns_from_datetime(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    aware = dt.astimezone(timezone.utc)
    return int(aware.timestamp()) * 1_000_000_000 + aware.microsecond * 1_000


def _datetime_from_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc)


def _is_retryable(exc: BaseException) -> bool:
    """Retry throttling/5xx/transport only — 4xx (bad query, auth) will not succeed on retry."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return isinstance(exc, httpx.TransportError)


class LokiSourceAdapter:
    """SourceAdapter over Grafana Loki's query_range HTTP API.

    Auth is settings-only (Bearer token or basic) — never accepted as SourceSpec
    params, matching CloudWatch's credential-chain convention. URL, tenant, and
    default LogQL query may be overridden per request via params.
    """

    name = "loki"

    def __init__(
        self,
        base_url: str = "",
        tenant: str = "",
        bearer_token: str = "",
        username: str = "",
        password: str = "",
        default_query: str = "",
    ):
        self.base_url = base_url
        self.tenant = tenant
        self.bearer_token = bearer_token
        self.username = username
        self.password = password
        self.default_query = default_query

    def check_available(self) -> None:
        """Raise AdapterUnavailableError if no Loki base URL is configured.
        Cheap, local-only check — used by /health."""
        if not (self.base_url or "").strip():
            raise AdapterUnavailableError(
                "loki adapter requires LOKI_URL"
            )

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        queries = spec.params.get("queries")
        if isinstance(queries, str):
            # LogQL uses commas inside selectors — do not split a string on ",".
            queries = [queries] if queries.strip() else []
        if not queries:
            query = spec.params.get("query") or self.default_query
            if isinstance(query, str) and query.strip():
                queries = [query]
            else:
                queries = []
        if not queries:
            raise AdapterUnavailableError(
                "loki adapter requires 'query' or 'queries' in params "
                "(or LOKI_QUERY)"
            )

        url = spec.params.get("url") or spec.params.get("base_url") or self.base_url
        if not (url or "").strip():
            raise AdapterUnavailableError(
                "loki adapter requires a base URL "
                "(LOKI_URL or params.url)"
            )
        tenant = spec.params.get("tenant") or spec.params.get("org_id") or self.tenant
        raw_limit = spec.params.get("limit", PAGE_SIZE)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError) as e:
            raise AdapterUnavailableError(
                f"loki adapter 'limit' must be an integer, got {raw_limit!r}"
            ) from e
        if limit < 1:
            raise AdapterUnavailableError("loki adapter 'limit' must be >= 1")

        for query in queries:
            if not isinstance(query, str) or not query.strip():
                continue
            yield LogStreamRef(
                adapter=self.name,
                stream_id=query.strip(),
                metadata={"url": url.strip(), "tenant": tenant or "", "limit": limit},
            )

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        base_url = ref.metadata.get("url") or self.base_url
        if not (base_url or "").strip():
            raise AdapterUnavailableError(
                "loki adapter requires a base URL "
                "(LOKI_URL or params.url)"
            )
        tenant = ref.metadata.get("tenant") or self.tenant
        limit = int(ref.metadata.get("limit") or PAGE_SIZE)
        url = _query_url(base_url)
        headers = self._headers(tenant)
        auth = self._auth()

        start_ns = int(ref.cursor) if ref.cursor else _ns_from_datetime(window.start)
        end_ns = _ns_from_datetime(window.end)

        while start_ns < end_ns:
            params = {
                "query": ref.stream_id,
                "start": str(start_ns),
                "end": str(end_ns),
                "limit": str(limit),
                "direction": "forward",
            }
            try:
                payload = self._query_range(url, params, headers, auth)
            except httpx.HTTPStatusError as e:
                raise AdapterUnavailableError(
                    f"Loki unavailable for {ref.stream_id}: {e}"
                ) from e
            except httpx.TransportError as e:
                raise AdapterUnavailableError(
                    f"Loki unavailable for {ref.stream_id}: {e}"
                ) from e

            result_type = (payload.get("data") or {}).get("resultType")
            if result_type and result_type != "streams":
                raise AdapterUnavailableError(
                    f"Loki query returned resultType={result_type!r}; "
                    "LogQL must select log streams, not metrics"
                )

            streams = (payload.get("data") or {}).get("result") or []
            page_count = 0
            max_ts = start_ns
            for stream in streams:
                labels = stream.get("stream") or {}
                source_ref = _format_labels(labels) if labels else ref.stream_id
                for ts_raw, line in stream.get("values") or []:
                    try:
                        ts_ns = int(ts_raw)
                    except (TypeError, ValueError):
                        continue
                    page_count += 1
                    max_ts = max(max_ts, ts_ns)
                    yield RawLogLine(
                        text=line,
                        source_ref=source_ref,
                        received_at=_datetime_from_ns(ts_ns),
                    )

            if page_count < limit:
                ref.cursor = None
                break
            next_start = max_ts + 1
            if next_start <= start_ns:
                # Guard against a stuck cursor if Loki repeats the same page.
                ref.cursor = None
                break
            start_ns = next_start
            ref.cursor = str(start_ns)

    def _headers(self, tenant: str) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if tenant:
            headers["X-Scope-OrgID"] = tenant
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _auth(self) -> Optional[tuple[str, str]]:
        if self.bearer_token:
            return None
        if self.username or self.password:
            return (self.username, self.password)
        return None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _query_range(
        self,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
        auth: Optional[tuple[str, str]],
    ) -> dict:
        with httpx.Client(timeout=60) as client:
            response = client.get(url, params=params, headers=headers, auth=auth)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as e:
                if not _is_retryable(e):
                    raise AdapterUnavailableError(
                        f"Loki request failed ({e.response.status_code}): {e.response.text}"
                    ) from e
                raise
            try:
                payload = response.json()
            except ValueError as e:
                raise AdapterUnavailableError(
                    f"Loki returned a non-JSON body: {e}"
                ) from e
        if not isinstance(payload, dict):
            raise AdapterUnavailableError("Loki returned a non-object JSON body")
        if payload.get("status") == "error":
            raise AdapterUnavailableError(
                f"Loki query error: {payload.get('error') or payload.get('errorType') or payload}"
            )
        return payload
