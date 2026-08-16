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
    """One unparsed line plus its provenance, ready for the parser → normalization pipeline."""

    text: str
    source_ref: str
    received_at: Optional[datetime] = None
    # Adapter-inferred fallbacks (e.g. k8s path / kubectl prefix). The line's own
    # parsed fields win; these fill in when the payload has no service/env/host.
    default_service: Optional[str] = None
    default_environment: Optional[str] = None
    default_host: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    name: str

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        """Resolve a spec into concrete streams (log groups, label sets, files)."""
        ...

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        """Yield raw lines for a stream within a time window; must be resumable via ref.cursor."""
        ...
