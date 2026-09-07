"""Convert the RCAEval RE2/RE3 corpus into eval-harness cases.

RCAEval (https://github.com/phamquiluan/RCAEval, MIT) ships one directory per
failure case named ``{benchmark}{system}_{service}_{fault}_{instance}`` (e.g.
``re2ob_adservice_cpu_1``), each containing:

- ``inject_time.txt`` — the fault-injection Unix timestamp (the trigger label).
- ``logs.csv`` — ``time, service, message`` rows.

The case id encodes the ground-truth root-cause **service** and **fault type**,
so we derive labels from the directory name + ``inject_time.txt`` and never need
the (Parquet) metadata index. Conversion is pure and unit-tested; the download
(~3.4 GB) lives in ``scripts/eval_data.py`` and is never committed.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Window around the injection timestamp. RCAEval doesn't state a fixed
# observation duration, so this is a documented starting heuristic (refine
# empirically): a few minutes before to give a baseline, ~10 minutes after to
# capture the fault's effects.
DEFAULT_PRE_SECONDS = 300
DEFAULT_POST_SECONDS = 600

# RCAEval fault families -> the harness trigger taxonomy
# (deploy|config|dependency|resource|code|none). RE2 faults are resource/network
# stressors; RE3 faults are code-level. None of them announce themselves in the
# log stream — that is the point of the dataset.
_FAULT_TYPE = {
    "cpu": "resource",
    "mem": "resource",
    "memory": "resource",
    "disk": "resource",
    "io": "resource",
    "socket": "resource",
    "delay": "dependency",
    "latency": "dependency",
    "loss": "dependency",
    "network": "dependency",
    "code": "code",
}


@dataclass
class RCACase:
    case_id: str
    suite: str  # "re2" | "re3"
    system: str  # ob | ss | tt
    service: str
    fault: str
    instance: str


def parse_case_dir_name(name: str) -> RCACase:
    """Parse ``re2ob_adservice_cpu_1`` into its ground-truth components."""
    parts = name.split("_")
    if len(parts) < 4:
        raise ValueError(f"unexpected RCAEval case name: {name!r}")
    benchmark = parts[0]
    suite = benchmark[:3]
    system = benchmark[3:]
    if suite not in ("re2", "re3"):
        raise ValueError(f"unexpected RCAEval benchmark prefix in {name!r}")
    instance = parts[-1]
    fault = parts[-2]
    service = "_".join(parts[1:-2])
    if not service:
        raise ValueError(f"could not parse service from {name!r}")
    return RCACase(
        case_id=name,
        suite=suite,
        system=system,
        service=service,
        fault=fault,
        instance=instance,
    )


def trigger_type_for_fault(fault: str) -> str:
    """Map an RCAEval fault name to the harness trigger taxonomy."""
    key = fault.lower()
    for token, kind in _FAULT_TYPE.items():
        if token in key:
            return kind
    return "resource"


def parse_inject_time(text: str) -> datetime:
    """Parse ``inject_time.txt`` — a Unix timestamp (seconds, possibly float)."""
    value = float(text.strip())
    # Guard against millisecond timestamps.
    if value > 1e12:
        value /= 1000.0
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _parse_log_time(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        value = float(raw)
        if value > 1e12:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except ValueError:
        pass
    from src.utils.time import parse_iso

    try:
        return parse_iso(raw)
    except Exception:
        return None


def _infer_level(message: str) -> str:
    m = message.lower()
    if any(k in m for k in ("error", "exception", "traceback", "fatal", "panic")):
        return "error"
    if "warn" in m:
        return "warn"
    return "info"


def convert_logs_csv(text: str) -> list[dict]:
    """Convert RCAEval ``logs.csv`` text into raglogs JSONL ingest records.

    Reads ``time, service, message`` by header when present, else positionally.
    A ``level`` is inferred from the message so error lines feed clustering and
    the baseline arm. Rows with an unparseable timestamp are skipped.
    """
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []

    header = [c.strip().lower() for c in rows[0]]
    if {"time", "service", "message"} <= set(header):
        ti, si, mi = header.index("time"), header.index("service"), header.index("message")
        body = rows[1:]
    else:
        ti, si, mi = 0, 1, 2
        body = rows

    records: list[dict] = []
    for row in body:
        if len(row) <= max(ti, si, mi):
            continue
        ts = _parse_log_time(row[ti])
        if ts is None:
            continue
        message = row[mi].strip()
        records.append(
            {
                "timestamp": ts.isoformat(),
                "service": row[si].strip() or None,
                "message": message,
                "level": _infer_level(message),
            }
        )
    return records


def window_around(
    inject_time: datetime,
    pre_seconds: int = DEFAULT_PRE_SECONDS,
    post_seconds: int = DEFAULT_POST_SECONDS,
) -> tuple[datetime, datetime]:
    return (
        inject_time - timedelta(seconds=pre_seconds),
        inject_time + timedelta(seconds=post_seconds),
    )


def build_case_yaml(
    meta: RCACase,
    inject_time: datetime,
    window: tuple[datetime, datetime],
) -> dict:
    """Build the harness ``case.yaml`` dict for one RCAEval case."""
    start, end = window
    # No `logs` key: the loader defaults to logs.jsonl in the case dir, which is
    # exactly where convert_case writes it.
    return {
        "id": meta.case_id,
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "root_cause": {"service": meta.service},
        "trigger": {
            "timestamp": inject_time.isoformat(),
            "type": trigger_type_for_fault(meta.fault),
        },
        "expect_explanation": True,
        "notes": f"RCAEval {meta.suite} {meta.system} fault={meta.fault} instance={meta.instance}",
    }


def convert_case(
    src_dir: Path,
    out_dir: Path,
    pre_seconds: int = DEFAULT_PRE_SECONDS,
    post_seconds: int = DEFAULT_POST_SECONDS,
) -> bool:
    """Convert one RCAEval case directory into an eval-harness case directory.

    Writes ``case.yaml`` + ``logs.jsonl`` under ``out_dir``. Returns False (and
    writes nothing) when the source is missing required files.
    """
    import yaml

    src_dir = Path(src_dir)
    logs_csv = src_dir / "logs.csv"
    inject_txt = src_dir / "inject_time.txt"
    if not logs_csv.exists() or not inject_txt.exists():
        return False

    meta = parse_case_dir_name(src_dir.name)
    inject_time = parse_inject_time(inject_txt.read_text())
    records = convert_logs_csv(logs_csv.read_text())
    if not records:
        return False

    window = window_around(inject_time, pre_seconds, post_seconds)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "logs.jsonl").open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    (out_dir / "case.yaml").write_text(
        yaml.safe_dump(build_case_yaml(meta, inject_time, window), sort_keys=False)
    )
    return True
