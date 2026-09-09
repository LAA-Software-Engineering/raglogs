"""Eval case format: a directory per case with ground-truth labels.

```
tests/eval/cases/<case-id>/
  case.yaml           # ground truth (see EvalCase)
  logs.jsonl          # optional; the case's logs (or point elsewhere via `logs:`)
```

The layout is source-agnostic: `logs` may name a file, directory, or glob
(relative to the repo root) so a case can reuse an existing fixture instead of
duplicating it.
"""

from __future__ import annotations

import glob
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.utils.time import parse_iso

# src/eval/case.py -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Valid `trigger.type` values (documentation, not enforced hard).
TRIGGER_TYPES = {"deploy", "config", "dependency", "resource", "code", "none"}


@dataclass
class RootCause:
    service: str
    fingerprint_hint: Optional[str] = None


@dataclass
class Trigger:
    timestamp: Optional[datetime]
    type: str = "none"


@dataclass
class EvalCase:
    id: str
    window_start: datetime
    window_end: datetime
    expect_explanation: bool
    root_cause: Optional[RootCause] = None
    trigger: Optional[Trigger] = None
    notes: str = ""
    logs_paths: list[Path] = field(default_factory=list)
    # Optional baseline-window duration (e.g. "5m") so change-vs-baseline has a
    # real pre-incident period; None uses the pipeline default. For corpora with
    # a short capture (e.g. RCAEval), point this at the pre-injection window.
    baseline_window: Optional[str] = None
    # Optional telemetry sidecars (#118 multi-modal RCA): a `spans.jsonl` /
    # `metrics.jsonl` in the case dir, ingested under the same scope as the logs.
    # Absent for logs-only cases; the pipeline runs unchanged without them.
    spans_path: Optional[Path] = None
    metrics_path: Optional[Path] = None


def _resolve_logs(case_dir: Path, raw: Optional[object]) -> list[Path]:
    """Resolve the `logs` field (or default) to concrete existing paths.

    `logs` may be a single string or a list of strings; each is a path, dir, or
    glob relative to the repo root. When absent, fall back to a `logs.jsonl` in
    the case directory.
    """
    patterns: list[str]
    if raw is None:
        patterns = [str(case_dir / "logs.jsonl")]
    elif isinstance(raw, str):
        patterns = [raw]
    elif isinstance(raw, list):
        patterns = [str(p) for p in raw]
    else:
        raise ValueError(f"`logs` must be a string or list of strings, got {type(raw)!r}")

    resolved: list[Path] = []
    for pat in patterns:
        p = Path(pat)
        if not p.is_absolute():
            p = _REPO_ROOT / p
        if any(ch in pat for ch in "*?["):
            matches = sorted(Path(m) for m in glob.glob(str(p)))
            if not matches:
                raise FileNotFoundError(f"`logs` glob matched nothing: {pat}")
            resolved.extend(matches)
        else:
            if not p.exists():
                raise FileNotFoundError(f"`logs` path does not exist: {p}")
            resolved.append(p)
    return resolved


def _root_cause(raw: Optional[dict]) -> Optional[RootCause]:
    if not raw:
        return None
    service = raw.get("service")
    if not service:
        raise ValueError("root_cause.service is required when root_cause is present")
    return RootCause(service=str(service), fingerprint_hint=raw.get("fingerprint_hint"))


def _trigger(raw: Optional[dict]) -> Optional[Trigger]:
    if not raw:
        return None
    ts = raw.get("timestamp")
    return Trigger(
        timestamp=parse_iso(str(ts)) if ts else None,
        type=str(raw.get("type", "none")),
    )


def load_case(case_dir: Path) -> EvalCase:
    """Load and validate one case directory."""
    import yaml  # lazy: only the eval path needs PyYAML

    case_yaml = case_dir / "case.yaml"
    if not case_yaml.exists():
        raise FileNotFoundError(f"missing case.yaml in {case_dir}")
    raw = yaml.safe_load(case_yaml.read_text()) or {}

    window = raw.get("window") or {}
    if "start" not in window or "end" not in window:
        raise ValueError(f"{case_yaml}: window.start and window.end are required")

    expect_explanation = bool(raw.get("expect_explanation", True))
    root_cause = _root_cause(raw.get("root_cause"))
    if expect_explanation and root_cause is None:
        raise ValueError(
            f"{case_yaml}: root_cause is required unless expect_explanation is false"
        )

    spans = case_dir / "spans.jsonl"
    metrics = case_dir / "metrics.jsonl"
    return EvalCase(
        id=str(raw.get("id") or case_dir.name),
        window_start=parse_iso(str(window["start"])),
        window_end=parse_iso(str(window["end"])),
        expect_explanation=expect_explanation,
        root_cause=root_cause,
        trigger=_trigger(raw.get("trigger")),
        notes=str(raw.get("notes", "")),
        logs_paths=_resolve_logs(case_dir, raw.get("logs")),
        baseline_window=(str(raw["baseline"]) if raw.get("baseline") else None),
        spans_path=spans if spans.exists() else None,
        metrics_path=metrics if metrics.exists() else None,
    )


def load_cases(cases_dir: Path) -> list[EvalCase]:
    """Load every case subdirectory (one with a case.yaml), sorted by id."""
    cases_dir = Path(cases_dir)
    if not cases_dir.exists():
        raise FileNotFoundError(f"cases directory not found: {cases_dir}")
    dirs = sorted(d for d in cases_dir.iterdir() if d.is_dir() and (d / "case.yaml").exists())
    return [load_case(d) for d in dirs]
