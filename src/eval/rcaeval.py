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


# Fault tokens that may appear in a case name. Fault labels are matched as the
# maximal suffix of these (before the trailing instance number), so a compound
# fault like ``packet_loss`` or ``network_delay`` is captured whole instead of
# leaking its leading token into the root-cause service label.
_FAULT_TOKENS = frozenset(
    {
        "cpu",
        "mem",
        "memory",
        "disk",
        "io",
        "socket",
        "delay",
        "latency",
        "loss",
        "packet",
        "network",
        "code",
        "hog",
        "stress",
        "leak",
    }
)


def parse_case_dir_name(name: str) -> RCACase:
    """Parse ``re2ob_adservice_cpu_1`` into its ground-truth components.

    ``fault`` is the maximal trailing run of known fault tokens (so
    ``re2ob_adservice_packet_loss_1`` yields service ``adservice`` and fault
    ``packet_loss``, not service ``adservice_packet`` / fault ``loss``).
    """
    parts = name.split("_")
    if len(parts) < 4:
        raise ValueError(f"unexpected RCAEval case name: {name!r}")
    benchmark = parts[0]
    suite = benchmark[:3]
    system = benchmark[3:]
    if suite not in ("re2", "re3"):
        raise ValueError(f"unexpected RCAEval benchmark prefix in {name!r}")
    instance = parts[-1]

    middle = parts[1:-1]  # service tokens + fault tokens
    # Peel fault tokens off the right; keep at least one token for the service.
    split = len(middle)
    while split > 1 and middle[split - 1].lower() in _FAULT_TOKENS:
        split -= 1
    if split == len(middle):
        # No recognized fault suffix; fall back to a single trailing token.
        split = len(middle) - 1
    service = "_".join(middle[:split])
    fault = "_".join(middle[split:])
    if not service or not fault:
        raise ValueError(f"could not parse service/fault from {name!r}")
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


def _build_record(
    ts_raw: object,
    service: object,
    message: object,
    window: tuple[datetime, datetime] | None = None,
) -> dict | None:
    """One raglogs ingest record from raw fields, or None to skip.

    Skips unparseable timestamps and, when ``window`` is given, rows outside it.
    """
    ts = _parse_log_time(str(ts_raw))
    if ts is None:
        return None
    if window is not None and not (window[0] <= ts <= window[1]):
        return None
    msg = str(message or "").strip()
    svc = str(service).strip() if service is not None else ""
    return {
        "timestamp": ts.isoformat(),
        "service": svc or None,
        "message": msg,
        "level": _infer_level(msg),
    }


def _pick_column(names: list[str], *candidates: str) -> str | None:
    """Resolve a column by exact (case-insensitive) name, then substring."""
    lower = {n.lower(): n for n in names}
    for cand in candidates:
        if cand in lower:
            return lower[cand]
    for cand in candidates:
        for lc, original in lower.items():
            if cand in lc:
                return original
    return None


def convert_logs_csv(text: str) -> list[dict]:
    """Convert RCAEval ``logs.csv`` text into raglogs JSONL ingest records.

    Reads ``time, service, message`` by header when present, else positionally.
    Kept for the CSV variant of the corpus; the current Hugging Face packaging
    ships ``logs.parquet`` (see :func:`load_parquet_logs`).
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
        rec = _build_record(row[ti], row[si], row[mi])
        if rec is not None:
            records.append(rec)
    return records


def load_parquet_logs(
    path: Path, window: tuple[datetime, datetime] | None = None
) -> list[dict]:
    """Convert an RCAEval ``logs.parquet`` into raglogs ingest records.

    The Hugging Face dataset ships logs as Parquet with columns
    ``timestamp, container_name, message`` (epoch-second timestamps). Column
    names are resolved leniently. When ``window`` is given, only rows inside it
    are kept — the harness explains that window anyway, so this bounds ingest
    (a case can hold tens of thousands of lines).
    """
    import pyarrow.parquet as pq

    table = pq.read_table(str(path))
    names = table.column_names
    tcol = _pick_column(names, "timestamp", "time")
    scol = _pick_column(names, "container_name", "service", "pod_name", "pod", "container")
    mcol = _pick_column(names, "message", "log", "body")
    if tcol is None or mcol is None:
        raise ValueError(f"logs.parquet columns not recognized: {names}")

    records: list[dict] = []
    for row in table.to_pylist():
        rec = _build_record(
            row.get(tcol), row.get(scol) if scol else None, row.get(mcol), window
        )
        if rec is not None:
            records.append(rec)
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
    # The incident window is the post-injection period [inject_time, end]; the
    # pre-injection period [start, inject_time] becomes the change-ratio baseline
    # (logs.jsonl holds both), so raglogs' anomaly signal has something to
    # compare against instead of an empty default 24h window.
    baseline_seconds = max(int((inject_time - start).total_seconds()), 1)
    # RE3 is the code-level-fault suite (faults named f1/f2/f3…), so those are
    # code triggers regardless of the fault token; RE2 maps by fault family.
    trigger_type = "code" if meta.suite == "re3" else trigger_type_for_fault(meta.fault)
    # No `logs` key: the loader defaults to logs.jsonl in the case dir, which is
    # exactly where convert_case writes it.
    return {
        "id": meta.case_id,
        "window": {"start": inject_time.isoformat(), "end": end.isoformat()},
        "baseline": f"{baseline_seconds}s",
        "root_cause": {"service": meta.service},
        "trigger": {
            "timestamp": inject_time.isoformat(),
            "type": trigger_type,
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
    inject_txt = src_dir / "inject_time.txt"
    logs_parquet = src_dir / "logs.parquet"
    logs_csv = src_dir / "logs.csv"
    if not inject_txt.exists() or not (logs_parquet.exists() or logs_csv.exists()):
        return False

    meta = parse_case_dir_name(src_dir.name)
    inject_time = parse_inject_time(inject_txt.read_text())
    window = window_around(inject_time, pre_seconds, post_seconds)
    if logs_parquet.exists():
        records = load_parquet_logs(logs_parquet, window)
    else:
        records = convert_logs_csv(logs_csv.read_text())
    if not records:
        return False

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "logs.jsonl").open("w") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    (out_dir / "case.yaml").write_text(
        yaml.safe_dump(build_case_yaml(meta, inject_time, window), sort_keys=False)
    )
    return True
