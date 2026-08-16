from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Iterator, Optional, Protocol, runtime_checkable


@dataclass
class SourceSpec:
    """What integrators send instead of file paths — identifies an adapter and its params."""
    adapter: str
    params: dict[str, Any] = field(default_factory=dict)
    service: Optional[str] = None
    env: Optional[str] = None


@dataclass
class TimeWindow:
    start: datetime
    end: datetime


@dataclass
class LogStreamRef:
    """A concrete stream resolved from a SourceSpec (a log group, label set, or file)."""
    adapter: str
    stream_id: str
    cursor: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawLogLine:
    """One unparsed line plus its provenance, ready for the parser → normalization pipeline.

    Adapters may fill `default_service` / `default_environment` / `default_host` from
    source metadata (Loki stream labels, k8s pod fields). Ingestion uses them only
    when the parsed line and SourceSpec do not already set those fields — core stays
    source-agnostic.
    """
    text: str
    source_ref: str
    received_at: Optional[datetime] = None
    default_service: Optional[str] = None
    default_environment: Optional[str] = None
    default_host: Optional[str] = None


@runtime_checkable
class SourceAdapter(Protocol):
    name: str

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        """Resolve a spec into concrete streams (log groups, label sets, files)."""
        ...

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        """Yield raw lines for a stream within a time window; must be resumable via ref.cursor."""
        ...
