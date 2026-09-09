"""Generate a deploy-class incident corpus from the OpenTelemetry Demo (#79).

RCAEval injects resource/code faults; no public dataset contains the *deploy
broke it* incident raglogs is built around, because generating one requires
controlling the deploy. The OTel Demo ships failure scenarios as runtime feature
flags (flagd/OpenFeature) plus, via Chaos Mesh, infrastructure faults — and the
ground truth is correct **by construction** because we cause the fault and record
its exact time.

This module is the reusable, testable core: the flag→scenario table, the flagd
variant patch, and building/writing a harness ``case`` (``case.yaml`` +
``logs.jsonl`` / ``spans.jsonl`` / ``metrics.jsonl``) from a captured window. The
live orchestration (baseline → flip → incident → emit → flip back) is
:func:`generate_incident`, with telemetry capture and flag control injected so it
is unit-testable with fakes and runnable against a real demo.

**Why this corpus matters (see docs/eval-otel-demo.md):** it is the *independent*
corpus for **frozen external validation** — train the ranker + calibrator on
RCAEval, freeze the exact artifact, and run it here with no retraining, no
threshold/feature/calibration fitting. It also exercises what RCAEval cannot:
healthy **negative** cases (raglogs must abstain) and **confounded** cases (an
unrelated deploy near the real fault), which now matter as much as top-1.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from src.eval.rcaeval import _metric_to_jsonl, _span_to_jsonl

# Harness trigger taxonomy: deploy | config | dependency | resource | code | none.


@dataclass(frozen=True)
class FlagScenario:
    flag: str  # flagd flag name
    service: str  # ground-truth root-cause service (must match telemetry service.name)
    trigger_type: str
    incident_class: str
    variant_on: str = "on"


# The demo's built-in failure flags (opentelemetry.io/docs/demo/feature-flags).
# ``service`` is the injected root cause; verify the string matches the running
# demo's ``service.name`` (it has changed across demo versions) — a generation-time
# check, like RCAEval's service canonicalisation.
FLAG_SCENARIOS: tuple[FlagScenario, ...] = (
    FlagScenario("productCatalogFailure", "product-catalog", "dependency",
                 "Downstream dependency errors on a specific code path"),
    FlagScenario("paymentServiceFailure", "payment", "code",
                 "Service-level error on a critical path"),
    FlagScenario("paymentServiceUnreachable", "payment", "dependency",
                 "Payment dependency unreachable"),
    FlagScenario("cartServiceFailure", "cart", "code",
                 "Error on every EmptyCart call"),
    FlagScenario("recommendationServiceCacheFailure", "recommendation", "resource",
                 "Memory growth / slow-burn degradation"),
    FlagScenario("kafkaQueueProblems", "kafka", "resource",
                 "Queue overload + consumer lag"),
    FlagScenario("adHighCpu", "ad", "resource", "CPU saturation / latency degradation"),
    FlagScenario("imageSlowLoad", "frontend", "resource", "Image load latency degradation"),
    FlagScenario("loadGeneratorFloodHomepage", "frontend", "resource",
                 "Traffic-driven saturation"),
)

SCENARIOS_BY_FLAG: dict[str, FlagScenario] = {s.flag: s for s in FLAG_SCENARIOS}


# ── flagd control ──────────────────────────────────────────────────────────────


def patch_flag_variant(config: dict, flag: str, variant: str) -> dict:
    """Return a copy of a flagd config with ``flag``'s ``defaultVariant`` set to
    ``variant``. Pure, so the read→patch→write flip is unit-testable."""
    flags = config.get("flags")
    if not isinstance(flags, dict) or flag not in flags:
        raise KeyError(f"flag {flag!r} not in flagd config")
    new_flags = {k: dict(v) for k, v in flags.items()}
    new_flags[flag]["defaultVariant"] = variant
    return {**config, "flags": new_flags}


def set_flag_variant(base_url: str, flag: str, variant: str, *, timeout: float = 10.0) -> None:
    """Flip a flag on the running demo: GET the flag config, patch the default
    variant, POST it back (opentelemetry.io/docs/demo/feature-flags)."""
    import httpx

    with httpx.Client(base_url=base_url, timeout=timeout) as client:
        config = client.get("/feature/api/read").raise_for_status().json()
        client.post("/feature/api/write", json=patch_flag_variant(config, flag, variant)).raise_for_status()


# ── case building ────────────────────────────────────────────────────────────


def build_incident_case(
    case_id: str,
    inject_time: datetime,
    *,
    scenario: Optional[FlagScenario],
    baseline_seconds: int,
    post_seconds: int,
    confounding_deploy_at: Optional[datetime] = None,
    notes: str = "",
) -> dict:
    """Build a harness ``case.yaml`` dict. ``scenario=None`` is a **negative**
    (healthy) case: raglogs must return insufficient-evidence, so it carries no
    root cause and ``expect_explanation: false``. ``confounding_deploy_at`` records
    an unrelated deploy inside the window (the ground-truth trigger stays the flag
    flip) so trigger selection is tested against a distractor."""
    incident_start = inject_time
    end = inject_time + timedelta(seconds=post_seconds)
    note_parts = [notes] if notes else []
    if confounding_deploy_at is not None:
        note_parts.append(f"confounder: unrelated deploy at {confounding_deploy_at.isoformat()}")

    doc: dict = {
        "id": case_id,
        "window": {"start": incident_start.isoformat(), "end": end.isoformat()},
        "baseline": f"{baseline_seconds}s",
    }
    if scenario is None:
        doc["expect_explanation"] = False
        doc["notes"] = "; ".join(["healthy negative case (no fault injected)", *note_parts])
        return doc

    doc["root_cause"] = {"service": scenario.service}
    doc["trigger"] = {"timestamp": inject_time.isoformat(), "type": scenario.trigger_type}
    doc["expect_explanation"] = True
    doc["notes"] = "; ".join([f"OTel Demo flag={scenario.flag} ({scenario.incident_class})", *note_parts])
    return doc


def write_case(out_dir: Path, case_doc: dict, *, logs=None, spans=None, metrics=None) -> Path:
    """Write ``case.yaml`` (+ any telemetry sidecars) into ``out_dir``."""
    import yaml

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "case.yaml").write_text(yaml.safe_dump(case_doc, sort_keys=False))
    with (out_dir / "logs.jsonl").open("w") as f:
        for rec in logs or []:
            f.write(json.dumps(rec) + "\n")
    if spans:
        with (out_dir / "spans.jsonl").open("w") as f:
            for s in spans:
                f.write(json.dumps(_span_to_jsonl(s)) + "\n")
    if metrics:
        with (out_dir / "metrics.jsonl").open("w") as f:
            for m in metrics:
                f.write(json.dumps(_metric_to_jsonl(m)) + "\n")
    return out_dir


# ── orchestration ────────────────────────────────────────────────────────────

# capture(window_start, window_end) -> (logs, spans, metrics)
CaptureFn = Callable[[datetime, datetime], tuple[list, list, list]]


def generate_incident(
    out_dir: Path,
    case_id: str,
    *,
    scenario: Optional[FlagScenario],
    capture: CaptureFn,
    flip: Optional[Callable[[str, str], None]] = None,
    sleep: Callable[[float], None],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    baseline_seconds: int = 300,
    post_seconds: int = 600,
) -> Path:
    """Run one generation loop: collect the baseline window, flip the flag (record
    the exact inject time), collect the incident window, emit the case, flip back.

    ``capture`` / ``flip`` / ``sleep`` are injected so this is unit-testable with
    fakes and runnable against a live demo. For a negative case pass
    ``scenario=None`` (no flag is flipped)."""
    sleep(baseline_seconds)  # let the baseline window accrue
    inject_time = now()
    if scenario is not None and flip is not None:
        flip(scenario.flag, scenario.variant_on)
    try:
        sleep(post_seconds)  # let the incident window accrue
        window_start = inject_time - timedelta(seconds=baseline_seconds)
        window_end = inject_time + timedelta(seconds=post_seconds)
        logs, spans, metrics = capture(window_start, window_end)
    finally:
        if scenario is not None and flip is not None:
            flip(scenario.flag, "off")

    doc = build_incident_case(
        case_id, inject_time, scenario=scenario,
        baseline_seconds=baseline_seconds, post_seconds=post_seconds,
    )
    return write_case(out_dir, doc, logs=logs, spans=spans, metrics=metrics)
