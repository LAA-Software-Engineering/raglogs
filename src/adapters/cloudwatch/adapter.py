from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.adapters.base import LogStreamRef, RawLogLine, SourceSpec, TimeWindow
from src.core.errors import AdapterUnavailableError

PAGE_SIZE = 10000  # CloudWatch FilterLogEvents max per page
RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "LimitExceededException",
}


def _client(region: str):
    try:
        return boto3.client("logs", region_name=region)
    except Exception as e:
        raise AdapterUnavailableError(f"Could not create CloudWatch Logs client: {e}") from e


def _is_retryable(exc: BaseException) -> bool:
    """Retry throttling/5xx only — not e.g. ResourceNotFoundException, which won't
    succeed on retry and would otherwise burn 3 attempts before failing anyway."""
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
        return error.get("Code") in RETRYABLE_ERROR_CODES or status >= 500
    return isinstance(exc, BotoCoreError)


class CloudWatchSourceAdapter:
    """SourceAdapter over AWS CloudWatch Logs. Auth via the standard AWS credential chain
    (env vars, shared config, IRSA in-cluster) — no credentials are accepted as params."""

    name = "cloudwatch"

    def __init__(self, region: Optional[str] = None):
        self.region = region or "us-east-1"

    def check_available(self) -> None:
        """Raise AdapterUnavailableError if no AWS credentials resolve via the default
        chain. Cheap, local-only check — used by /health."""
        session = boto3.Session(region_name=self.region)
        if session.get_credentials() is None:
            raise AdapterUnavailableError(
                "no AWS credentials resolved via the default credential chain"
            )

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        log_groups = spec.params.get("log_groups")
        if not log_groups:
            log_group = spec.params.get("log_group")
            if not log_group:
                raise AdapterUnavailableError(
                    "cloudwatch adapter requires 'log_group' or 'log_groups' in params"
                )
            log_groups = [log_group]

        filter_pattern = spec.params.get("filter_pattern", "")
        region = spec.params.get("region") or self.region
        for log_group in log_groups:
            yield LogStreamRef(
                adapter=self.name,
                stream_id=log_group,
                metadata={"filter_pattern": filter_pattern, "region": region},
            )

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        client = _client(ref.metadata.get("region") or self.region)
        filter_pattern = ref.metadata.get("filter_pattern", "")
        start_ms = int(window.start.timestamp() * 1000)
        end_ms = int(window.end.timestamp() * 1000)
        next_token = ref.cursor

        while True:
            kwargs: dict[str, Any] = {
                "logGroupName": ref.stream_id,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": PAGE_SIZE,
            }
            if filter_pattern:
                kwargs["filterPattern"] = filter_pattern
            if next_token:
                kwargs["nextToken"] = next_token

            try:
                page = self._filter_log_events(client, kwargs)
            except (BotoCoreError, ClientError) as e:
                raise AdapterUnavailableError(
                    f"CloudWatch Logs unavailable for {ref.stream_id}: {e}"
                ) from e

            for event in page.get("events", []):
                ts = event.get("timestamp")
                received_at = (
                    datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts is not None else None
                )
                yield RawLogLine(
                    text=event.get("message", ""),
                    source_ref=ref.stream_id,
                    received_at=received_at,
                )

            new_token = page.get("nextToken")
            # CloudWatch returns the same token once there are no more events for the window
            if not new_token or new_token == next_token:
                ref.cursor = None
                break
            next_token = new_token
            ref.cursor = next_token  # caller may persist this between runs to resume

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def _filter_log_events(self, client, kwargs: dict) -> dict:
        return client.filter_log_events(**kwargs)
