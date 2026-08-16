import os
from pathlib import Path
from typing import Generator, Iterable, Iterator, Optional

from src.adapters.base import LogStreamRef, RawLogLine, SourceSpec, TimeWindow


def discover_files(paths: list[str], recursive: bool = False) -> list[Path]:
    """
    Discover all files from a list of paths (files or directories).
    Supports glob patterns in paths.
    """
    import glob as glob_module

    result: list[Path] = []

    for path_str in paths:
        p = Path(path_str)

        # Direct file
        if p.is_file():
            result.append(p)
            continue

        # Direct directory
        if p.is_dir():
            if recursive:
                for root, _, files in os.walk(p):
                    for f in files:
                        result.append(Path(root) / f)
            else:
                for f in p.iterdir():
                    if f.is_file():
                        result.append(f)
            continue

        # Try glob expansion
        expanded = glob_module.glob(path_str, recursive=recursive)
        if expanded:
            for ep in expanded:
                ep = Path(ep)
                if ep.is_file():
                    result.append(ep)
                elif ep.is_dir() and recursive:
                    for root, _, files in os.walk(ep):
                        for f in files:
                            result.append(Path(root) / f)

    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for p in result:
        resolved = p.resolve()
        if resolved not in seen:
            seen.add(resolved)
            deduped.append(p)

    return deduped


def detect_format(path: Path, hint: Optional[str] = None) -> str:
    """Detect log format: 'json' or 'text'."""
    if hint and hint != "auto":
        return hint

    # Check by extension
    if path.suffix in (".json", ".jsonl", ".ndjson"):
        return "json"

    # Sample first non-empty line
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    if line.startswith("{") and line.endswith("}"):
                        return "json"
                    break
    except OSError:
        pass

    return "text"


def read_lines(path: Path, encoding: str = "utf-8") -> Generator[str, None, None]:
    """Yield lines from a file, handling encoding errors gracefully."""
    try:
        with open(path, "r", encoding=encoding, errors="replace") as f:
            for line in f:
                yield line.rstrip("\n\r")
    except OSError as e:
        raise IOError(f"Cannot read file {path}: {e}") from e


class FileSourceAdapter:
    """SourceAdapter implementation over local files. Wraps discover_files/detect_format/read_lines
    with no behavior change — see src/adapters/base.py for the protocol."""

    name = "file"

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        paths = spec.params.get("paths", [])
        recursive = spec.params.get("recursive", False)
        fmt_hint = spec.params.get("format", "auto")

        for path in discover_files(paths, recursive=recursive):
            yield LogStreamRef(
                adapter=self.name,
                stream_id=str(path),
                metadata={"format": detect_format(path, hint=fmt_hint)},
            )

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        for line in read_lines(Path(ref.stream_id)):
            yield RawLogLine(text=line, source_ref=ref.stream_id)
