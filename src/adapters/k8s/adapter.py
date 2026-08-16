"""Kubernetes log-export adapter.

Ingests offline cluster captures — concatenated `kubectl logs` output, Fluent Bit /
Vector JSON lines, CRI (kubelet) node logs, and tarballs of those files — through
the SourceAdapter protocol. Live cluster API access is out of scope.

Path conventions (kubelet):
  /var/log/pods/<namespace>_<pod>_<uid>/<container>/<n>.log
  /var/log/containers/<pod>_<namespace>_<container>-<id>.log
"""

from __future__ import annotations

import gzip
import re
import tarfile
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from src.adapters.base import LogStreamRef, RawLogLine, SourceSpec, TimeWindow
from src.adapters.file.adapter import discover_files, read_lines
from src.core.errors import AdapterUnavailableError
from src.core.parsing.text_parser import CRI_PATTERN

ARCHIVE_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
LOG_SUFFIXES = (".log", ".json", ".jsonl", ".ndjson", ".txt")

# /var/log/pods/<namespace>_<pod>_<uid>/<container>/<restart>.log
PODS_PATH = (
    r"(?:^|/)pods/(?P<namespace>[^/_]+)_(?P<pod>[^/]+)_(?P<uid>[0-9a-fA-F-]+)"
    r"/(?P<container>[^/]+)/(?P<restart>\d+)\.log$"
)
# /var/log/containers/<pod>_<namespace>_<container>-<containerid>.log
CONTAINERS_PATH = (
    r"(?:^|/)containers/(?P<pod>[^/_]+)_(?P<namespace>[^/]+)_"
    r"(?P<container>[^/]+)-(?P<cid>[0-9a-fA-F]+)\.log$"
)

_PODS_RE = re.compile(PODS_PATH)
_CONTAINERS_RE = re.compile(CONTAINERS_PATH)


def build_k8s_params(
    params: Optional[dict[str, Any]] = None,
    paths: Optional[list[str]] = None,
    recursive: bool = False,
) -> dict[str, Any]:
    """Merge top-level ingest paths/recursive into adapter params.

    CLI positional paths, API `paths`, and `--param paths=` all land here so
    discover() has a single contract.
    """
    merged: dict[str, Any] = dict(params or {})
    if paths and not merged.get("paths") and not merged.get("path"):
        merged["paths"] = list(paths)
    if recursive and "recursive" not in merged:
        merged["recursive"] = True
    return merged


def _strip_gzip_log_suffix(path: str) -> str:
    """Drop a trailing .gz from a log path, but not from tar archives."""
    normalized = path.replace("\\", "/")
    lower = normalized.lower()
    if any(lower.endswith(suffix) for suffix in ARCHIVE_SUFFIXES):
        return normalized
    if lower.endswith(".gz"):
        return normalized[:-3]
    return normalized


def infer_k8s_meta_from_path(path: str) -> dict[str, str]:
    """Pull namespace / pod / container from a kubelet log path, if present."""
    normalized = _strip_gzip_log_suffix(path)
    match = _PODS_RE.search(normalized)
    if match:
        return {
            "namespace": match.group("namespace"),
            "pod": match.group("pod"),
            "pod_uid": match.group("uid"),
            "container": match.group("container"),
        }
    match = _CONTAINERS_RE.search(normalized)
    if match:
        return {
            "namespace": match.group("namespace"),
            "pod": match.group("pod"),
            "container": match.group("container"),
        }
    return {}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes")
    return bool(value)


def _normalize_paths(params: dict[str, Any]) -> list[str]:
    raw = params.get("paths") or params.get("path")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p.strip() for p in raw.split(",") if p.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(p) for p in raw if p]
    return []


def _is_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def _is_gzip_log(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".gz") and not _is_archive(path)


def _is_log_member(name: str) -> bool:
    stripped = _strip_gzip_log_suffix(name)
    lower = stripped.lower()
    if any(lower.endswith(suffix) for suffix in LOG_SUFFIXES):
        return True
    return bool(_PODS_RE.search(stripped) or _CONTAINERS_RE.search(stripped))


def _list_tar_log_members(path: Path) -> list[str]:
    try:
        with tarfile.open(path, "r:*") as tar:
            return [
                m.name
                for m in tar.getmembers()
                if m.isfile() and _is_log_member(m.name)
            ]
    except tarfile.TarError as e:
        raise AdapterUnavailableError(f"Cannot read archive {path}: {e}") from e


def _read_tar_member(archive: Path, member: str) -> Iterator[str]:
    try:
        with tarfile.open(archive, "r:*") as tar:
            handle = tar.extractfile(member)
            if handle is None:
                return
            with handle:
                if _is_gzip_log(Path(member)):
                    with gzip.open(
                        handle, "rt", encoding="utf-8", errors="replace"
                    ) as gz:
                        for line in gz:
                            yield line.rstrip("\n\r")
                    return
                for raw in handle:
                    yield raw.decode("utf-8", errors="replace").rstrip("\n\r")
    except (tarfile.TarError, OSError) as e:
        raise AdapterUnavailableError(f"Cannot read {archive}!{member}: {e}") from e


def _read_gzip(path: Path) -> Iterator[str]:
    try:
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line.rstrip("\n\r")
    except OSError as e:
        raise AdapterUnavailableError(f"Cannot read {path}: {e}") from e


def _defaults_from_meta(meta: dict[str, Any]) -> dict[str, Optional[str]]:
    container = meta.get("container")
    return {
        "service": container,
        "environment": meta.get("namespace"),
        "host": meta.get("pod"),
    }


def _source_ref(meta: dict[str, Any], fallback: str) -> str:
    namespace = meta.get("namespace")
    pod = meta.get("pod")
    container = meta.get("container")
    if pod and container:
        if namespace:
            return f"{namespace}/{pod}/{container}"
        return f"{pod}/{container}"
    return fallback


def _k8s_extra(meta: dict[str, Any]) -> dict[str, Any]:
    keys = ("namespace", "pod", "container", "pod_uid")
    kubernetes = {key: meta[key] for key in keys if meta.get(key)}
    return {"kubernetes": kubernetes} if kubernetes else {}


def _reassemble_cri_fragments(lines: Iterable[str]) -> Iterator[str]:
    """Join CRI P (partial) fragments with the following F (full) into one line.

    Kubelet splits a single container log line across CRI records at a byte
    boundary. Concatenate without inserting separators so fingerprints stay
    one-to-one with the original message.
    """
    pending_parts: list[str] = []
    pending_ts: Optional[str] = None
    pending_stream: Optional[str] = None

    def take_joined() -> Optional[str]:
        nonlocal pending_parts, pending_ts, pending_stream
        if not pending_parts:
            return None
        joined = f"{pending_ts} {pending_stream} F {''.join(pending_parts)}"
        pending_parts = []
        pending_ts = None
        pending_stream = None
        return joined

    for line in lines:
        cri = CRI_PATTERN.match(line)
        if cri is None:
            joined = take_joined()
            if joined is not None:
                yield joined
            yield line
            continue

        tag = cri.group("tag")
        ts = cri.group("ts")
        stream = cri.group("stream")
        msg = cri.group("msg")

        if tag == "P":
            if pending_stream is not None and pending_stream != stream:
                joined = take_joined()
                if joined is not None:
                    yield joined
            if not pending_parts:
                pending_ts = ts
                pending_stream = stream
            pending_parts.append(msg)
            continue

        # tag == "F": last (or only) fragment of this log line
        if pending_parts:
            if pending_stream is not None and pending_stream != stream:
                joined = take_joined()
                if joined is not None:
                    yield joined
                yield line
                continue
            pending_parts.append(msg)
            joined = take_joined()
            if joined is not None:
                yield joined
        else:
            yield line

    joined = take_joined()
    if joined is not None:
        yield joined


class KubernetesExportAdapter:
    """SourceAdapter over Kubernetes log exports (files, .gz, and tar archives)."""

    name = "k8s"

    def discover(self, spec: SourceSpec) -> Iterable[LogStreamRef]:
        paths = _normalize_paths(spec.params)
        if not paths:
            raise AdapterUnavailableError(
                "k8s adapter requires 'paths' (or 'path') in params"
            )

        recursive = _as_bool(spec.params.get("recursive", False))
        files = discover_files(paths, recursive=recursive)

        for path in files:
            if _is_archive(path):
                for member in _list_tar_log_members(path):
                    meta = infer_k8s_meta_from_path(member)
                    yield LogStreamRef(
                        adapter=self.name,
                        stream_id=f"{path}!{member}",
                        metadata={
                            "kind": "tar",
                            "archive": str(path),
                            "member": member,
                            **meta,
                        },
                    )
                continue

            meta = infer_k8s_meta_from_path(str(path))
            kind = "gzip" if _is_gzip_log(path) else "file"
            yield LogStreamRef(
                adapter=self.name,
                stream_id=str(path),
                metadata={"kind": kind, **meta},
            )

    def read(self, ref: LogStreamRef, window: TimeWindow) -> Iterator[RawLogLine]:
        # Export files are already a bounded capture — ignore window, same as FileSourceAdapter.
        meta = ref.metadata or {}
        defaults = _defaults_from_meta(meta)
        extra = _k8s_extra(meta)
        source_ref = _source_ref(meta, ref.stream_id)

        kind = meta.get("kind")
        if kind == "tar":
            lines: Iterable[str] = _read_tar_member(
                Path(meta["archive"]), meta["member"]
            )
        elif kind == "gzip":
            lines = _read_gzip(Path(ref.stream_id))
        else:
            lines = read_lines(Path(ref.stream_id))

        for line in _reassemble_cri_fragments(lines):
            yield RawLogLine(
                text=line,
                source_ref=source_ref,
                default_service=defaults.get("service"),
                default_environment=defaults.get("environment"),
                default_host=defaults.get("host"),
                extra=extra,
            )
