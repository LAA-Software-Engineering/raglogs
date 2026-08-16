from typing import Any, Iterable, Iterator, Optional

from tenacity import retry, stop_after_attempt, wait_exponential

from src.adapters.base import LogStreamRef, RawLogLine, SourceSpec, TimeWindow
from src.core.errors import AdapterUnavailableError

PAGE_SIZE = 10000  # CloudWatch FilterLogEvents max per page


def _client(region: str):
    try:
        import boto3
    except ImportError as e:
        raise AdapterUnavailableError(
            "boto3 is not installed — install the 'cloudwatch' extra to use this adapter"
        ) from e

    try:
        return boto3.client("logs", region_name=region)
    except Exception as e:
        raise AdapterUnavailableError(f"Could not create CloudWatch Logs client: {e}") from e


class CloudWatchSourceAdapter:
    """SourceAdapter over AWS CloudWatch Logs. Auth via the standard AWS credential chain
    (env vars, shared config, IRSA in-cluster) — no credentials are accepted as params."""

    name = "cloudwatch"

    def __init__(self, region: Optional[str] = None):
        self.region = region or "us-east-1"

    def check_available(self) -> None:
        """Raise AdapterUnavailableError if boto3 isn't installed or no credentials
        resolve via the default chain. Cheap, local-only check — used by /health."""
        try:
            import boto3
        except ImportError as e:
            raise AdapterUnavailableError(
                "boto3 is not installed — install the 'cloudwatch' extra to use this adapter"
            ) from e

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
        for log_group in log_groups:
            yield LogStreamRef(
                adapter=self.name,
                stream_id=log_group,
                metadata={"filter_pattern": filter_pattern},
            )

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        from botocore.exceptions import BotoCoreError, ClientError

        client = _client(self.region)
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
                yield RawLogLine(text=event.get("message", ""), source_ref=ref.stream_id)

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
        reraise=True,
    )
    def _filter_log_events(self, client, kwargs: dict) -> dict:
        return client.filter_log_events(**kwargs)
