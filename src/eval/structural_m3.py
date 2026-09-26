"""#209 M3 — the frozen out-of-sample evaluator for the structural model.

M3 is the generalization test for M1 (span ``sig``), M2a (``edge:`` observables) and M2b (``util:``
observables), run **once** on a corpus captured after the model was frozen. The protocol — corpus,
metrics, decision thresholds — was pre-registered on #209 before any capture existed; this module
encodes it so the measurement cannot be shaped by the data:

* Three nested arms over the *same* inputs: ``M1`` (sig only), ``M2a`` (+ edge), ``M2a+M2b``
  (+ util, the frozen model). Each arm is :func:`~src.core.rca.structural.resolve_structural` on a
  trimmed :class:`~src.core.rca.structural_model.StructuralInputs` — the product's own resolve step.
* Inputs are the persisted rows the product path reads
  (:func:`~src.core.rca.structural.load_structural_rows`), so series identity is exercised exactly
  as it is in production, not on a hand-built sample list.
* The verdicts (:func:`decide`) apply the thresholds fixed in the protocol; none of them is a tunable.
* Each gated statistic is computed exactly as the otel-fresh reference numbers the thresholds came
  from: the service universe is every service seen in span *or* metric rows; a positive is
  *generated* when any hypothesis exists; selectivity is over generated positives (an empty
  compatible set counts as 0); healthy abstention is *no hypothesis generated*.

Pure: no DB. ``scripts/eval/m3_structural.py`` ingests a corpus and feeds rows in."""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.core.rca.metric_series import instance_identity
from src.core.rca.observable import State
from src.core.rca.partition import discretize_rate, discretize_ratio
from src.core.rca.structural import resolve_structural
from src.core.rca.structural_model import (
    StructuralInputs,
    structural_signals,
    util_metric_class,
)

ARMS = ("M1", "M2a", "M2a+M2b")
FULL_ARM = "M2a+M2b"

# Pre-registered on #209 (M3 protocol), from the otel-fresh results; fixed before capture.
TRUTH_RETAINED_MIN = 0.50       # cause-has-telemetry truth retained (M2a's trace-only ceiling)
CANDIDATE_FRACTION_MAX = 0.33   # median |localization| / |services| over GENERATED positives (empty = 0); literal
HEALTHY_ABSTENTION_MIN = 0.58   # healthy windows with NO HYPOTHESIS GENERATED (7/12 on otel-fresh);
                                # not the ungated healthy_no_claim, which also counts NO_COMPATIBLE
MIN_HEALTHY_NEGATIVES = 12
# Positives whose cause is a resource fault — the cases M2b exists to recover.
RESOURCE_FAULT_SCENARIOS = ("adHighCpu", "recommendationCpuStress")


def arm_inputs(inp: StructuralInputs, arm: str) -> StructuralInputs:
    """The inputs one arm sees: M1 drops edge + util observables, M2a drops util, the full arm keeps all.
    Candidate generation from the incident call graph (``edges``) is M1 behaviour and stays in every arm."""
    if arm == "M1":
        return inp._replace(edge_signals={}, util_signals={})
    if arm == "M2a":
        return inp._replace(util_signals={})
    if arm == FULL_ARM:
        return inp
    raise ValueError(f"unknown arm {arm!r}")


@dataclass(frozen=True)
class ArmResult:
    outcome: str                      # Outcome value, or "no_candidates" when no hypothesis was generated
    localization: tuple[str, ...]

    @property
    def generated(self) -> bool:
        """Did this arm generate any hypothesis? Not generating is the (reference) abstention."""
        return self.outcome != "no_candidates"


@dataclass(frozen=True)
class CaseEval:
    case_id: str
    cause: Optional[str]              # ground-truth root-cause service; None for a healthy window
    positive: bool
    n_services: int
    cause_has_telemetry: bool
    arms: dict[str, ArmResult]
    # PRESENT observable families *about the cause* under the full inputs: sig / edge (into it) / util
    cause_families: tuple[str, ...] = ()
    util_measured_for_cause: bool = False
    util_present: tuple[str, ...] = ()
    edges_measured: int = 0           # edges whose sig is PRESENT or ABSENT
    edges_present: int = 0            # ... PRESENT (error >= cutoff OR latency >= 2x)
    edges_latency_measured: int = 0   # edges with a measured latency branch
    edges_latency_high: int = 0       # ... at >= 2x, whether or not the error branch also fired
    edges_error_present: int = 0      # edges whose explicit-status error rate crossed the cutoff
    util_samples: int = 0             # utilization-class metric samples in the rows
    util_samples_named: int = 0       # ... of which name their reporting instance
    notes: list[str] = field(default_factory=list)

    def retained(self, arm: str) -> bool:
        return self.cause is not None and self.cause in self.arms[arm].localization

    @property
    def retained_via(self) -> tuple[str, ...]:
        """PRESENT families about the retained truth under the frozen model — what *witnessed* it, not
        what retained it (see :attr:`retained_by_util`); ``topology`` when no PRESENT observable concerns
        the cause (a callee of an unexplained anomaly)."""
        if not self.retained(FULL_ARM):
            return ()
        return self.cause_families or ("topology",)

    @property
    def retained_by_util(self) -> bool:
        """Retained *via* ``util:`` — the arm delta: the frozen model retains the cause, the same inputs
        without util (M2a) do not, and a ``util:`` observable on the cause is PRESENT. A measured util
        observable is already a named-instance series (``summarize_utilization`` will not emit
        PRESENT/ABSENT without one)."""
        return self.retained(FULL_ARM) and not self.retained("M2a") and "util" in self.cause_families


def scenario_of(case_id: str) -> str:
    """``otel_adHighCpu`` / ``otel_chaos_recommendationCpuStress`` -> the scenario name."""
    return case_id.rsplit("_", 1)[-1]


def evaluate_case(case_id: str, cause: Optional[str], positive: bool, spans: list, metrics: list,
                  window_start: datetime) -> CaseEval:
    """Run every arm on one case's rows (spans + metrics over ``[baseline_start, window_end]``)."""
    inp = structural_signals(spans, metrics, window_start)
    # every service seen in span OR metric rows (the reference denominator)
    universe = ({sp.service for sp in spans if sp.service}
                | {m.service for m in metrics if getattr(m, "service", None)})
    arms: dict[str, ArmResult] = {}
    for arm in ARMS:
        resolved = resolve_structural(arm_inputs(inp, arm))
        if resolved is None:
            arms[arm] = ArmResult("no_candidates", ())
        else:
            result, _part = resolved
            arms[arm] = ArmResult(result.outcome.value, tuple(result.localization))

    families: list[str] = []
    if cause is not None:
        sig = inp.signals.get(cause)
        if sig is not None and sig.sig_state == State.PRESENT:
            families.append("sig")
        if any(e.callee == cause and e.sig_state == State.PRESENT for e in inp.edge_signals.values()):
            families.append("edge")
        if any(u.service == cause and u.sig_state == State.PRESENT for u in inp.util_signals.values()):
            families.append("util")

    edges = list(inp.edge_signals.values())
    latency_high = [e for e in edges if e.latency_measured and discretize_ratio(e.latency_ratio) == State.HIGH]
    error_present = [e for e in edges if e.error_measured and discretize_rate(e.error_rate) == State.PRESENT]
    util_rows = [m for m in metrics if util_metric_class(getattr(m, "metric", None)) is not None]
    named = [m for m in util_rows if instance_identity(getattr(m, "attributes", None)) is not None]
    return CaseEval(
        case_id=case_id, cause=cause, positive=positive,
        n_services=len(universe), cause_has_telemetry=cause is not None and cause in universe,
        arms=arms, cause_families=tuple(families),
        util_measured_for_cause=cause is not None and any(
            u.service == cause and u.sig_state is not None for u in inp.util_signals.values()),
        util_present=tuple(sorted(u.id for u in inp.util_signals.values() if u.sig_state == State.PRESENT)),
        edges_measured=sum(e.sig_state is not None for e in edges),
        edges_present=sum(e.sig_state == State.PRESENT for e in edges),
        edges_latency_measured=sum(e.latency_measured for e in edges),
        edges_latency_high=len(latency_high),
        edges_error_present=len(error_present),
        util_samples=len(util_rows), util_samples_named=len(named),
    )


def _rate(k: int, n: int) -> Optional[float]:
    return k / n if n else None


def summarize_arm(cases: list[CaseEval], arm: str) -> dict:
    """The pre-registered per-arm metrics."""
    pos = [c for c in cases if c.positive]
    neg = [c for c in cases if not c.positive]
    pos_tel = [c for c in pos if c.cause_has_telemetry]
    generated = [c for c in pos if c.arms[arm].generated]
    fractions = [len(c.arms[arm].localization) / max(c.n_services, 1) for c in generated]
    identified = [c for c in cases if c.arms[arm].outcome == "identified"]
    ident_correct = [c for c in identified if c.positive and c.arms[arm].localization == (c.cause,)]
    return {
        "positives": len(pos),
        "generated": len(generated),
        "truth_retained_all": [sum(c.retained(arm) for c in pos), len(pos)],
        "truth_retained_cause_has_telemetry": [sum(c.retained(arm) for c in pos_tel), len(pos_tel)],
        "truth_retained_cause_has_telemetry_rate": _rate(sum(c.retained(arm) for c in pos_tel), len(pos_tel)),
        "median_candidates": statistics.median([len(c.arms[arm].localization) for c in generated])
        if generated else None,
        "median_candidate_fraction": statistics.median(fractions) if fractions else None,
        "negatives": len(neg),
        "healthy_abstained": [sum(not c.arms[arm].generated for c in neg), len(neg)],
        "healthy_abstention_rate": _rate(sum(not c.arms[arm].generated for c in neg), len(neg)),
        # secondary, not gated: healthy windows with no localization claim at all (incl. NO_COMPATIBLE)
        "healthy_no_claim": [sum(not c.arms[arm].localization for c in neg), len(neg)],
        "outcomes": dict(sorted(Counter(c.arms[arm].outcome for c in cases).items())),
        "identified_precision": [len(ident_correct), len(identified)],
        "identified_on_healthy": sum(not c.positive for c in identified),
    }


def edge_specificity(cases: list[CaseEval]) -> dict:
    """Healthy-window edge flags at the unchanged M2a cutoffs (reported, never recalibrated here). The
    >=2x latency rate is counted on its own — including edges whose error branch also fired — so the
    rate the protocol asks for is exact; the error branch and the combined PRESENT are reported too."""
    neg = [c for c in cases if not c.positive]
    return {
        "healthy_windows_with_latency_high_edge": [sum(c.edges_latency_high > 0 for c in neg), len(neg)],
        "healthy_latency_high_over_latency_measured_edges": [sum(c.edges_latency_high for c in neg),
                                                              sum(c.edges_latency_measured for c in neg)],
        "healthy_windows_with_error_present_edge": [sum(c.edges_error_present > 0 for c in neg), len(neg)],
        "healthy_windows_with_present_edge": [sum(c.edges_present > 0 for c in neg), len(neg)],
        "healthy_present_over_measured_edges": [sum(c.edges_present for c in neg),
                                                sum(c.edges_measured for c in neg)],
    }


def identity_coverage(cases: list[CaseEval]) -> dict:
    """Share of utilization-class samples that name their reporting instance — an M2b prerequisite."""
    total = sum(c.util_samples for c in cases)
    named = sum(c.util_samples_named for c in cases)
    return {"util_samples": total, "named_instance": named, "rate": _rate(named, total)}


def decide(cases: list[CaseEval]) -> dict:
    """Apply the pre-registered decision rules to the frozen model (the full arm)."""
    full = summarize_arm(cases, FULL_ARM)
    retained = full["truth_retained_cause_has_telemetry_rate"]
    fraction = full["median_candidate_fraction"]
    abstention = full["healthy_abstention_rate"]
    criteria = {
        "truth_retained_cause_has_telemetry": [retained, TRUTH_RETAINED_MIN, ">="],
        "median_candidate_fraction": [fraction, CANDIDATE_FRACTION_MAX, "<="],
        "healthy_abstention": [abstention, HEALTHY_ABSTENTION_MIN, ">="],
    }
    if retained is None or abstention is None:
        generalizes = "untestable"      # no positive with cause telemetry, or no healthy window
    elif (retained >= TRUTH_RETAINED_MIN and abstention >= HEALTHY_ABSTENTION_MIN
          and fraction is not None and fraction <= CANDIDATE_FRACTION_MAX):
        generalizes = "generalizes"
    else:
        generalizes = "does_not_generalize"

    # M2b, as frozen: not validated if util fires on a healthy window; untestable only when series-
    # identity coverage is ~0 (exactly: no named utilization sample — otel-fresh was 0/37282);
    # validated iff a resource-fault positive is retained via util (the arm delta); else not validated.
    resource = [c for c in cases if c.positive and scenario_of(c.case_id) in RESOURCE_FAULT_SCENARIOS]
    healthy_util = sorted(c.case_id for c in cases if not c.positive and c.util_present)
    coverage = identity_coverage(cases)["rate"]
    if healthy_util:
        m2b = "not_validated"
    elif coverage is None or coverage == 0.0:
        m2b = "untestable"
    elif any(c.retained_by_util for c in resource):
        m2b = "validated"
    else:
        m2b = "not_validated"

    neg = [c for c in cases if not c.positive]
    deviations = []
    if len(neg) < MIN_HEALTHY_NEGATIVES:
        deviations.append(f"{len(neg)} healthy negatives < {MIN_HEALTHY_NEGATIVES} pre-registered")
    missing = [s for s in RESOURCE_FAULT_SCENARIOS if not any(scenario_of(c.case_id) == s for c in cases)]
    if missing:
        deviations.append(f"resource-fault scenarios absent from corpus: {missing}")
    return {
        "generalization": generalizes,
        "generalization_criteria": criteria,
        "m2b": m2b,
        "m2b_identity_coverage": coverage,
        "m2b_resource_cases": {c.case_id: {"util_measured_for_cause": c.util_measured_for_cause,
                                           "retained_M2a": c.retained("M2a"),
                                           "retained_full": c.retained(FULL_ARM),
                                           "retained_by_util": c.retained_by_util,
                                           "retained_via": list(c.retained_via)} for c in resource},
        "m2b_healthy_util_present": healthy_util,
        "protocol_deviations": deviations,
    }


def build_m3_report(cases: list[CaseEval]) -> dict:
    return {
        "arms": {arm: summarize_arm(cases, arm) for arm in ARMS},
        "edge_specificity": edge_specificity(cases),
        "identity_coverage": identity_coverage(cases),
        "verdicts": decide(cases),
        "cases": [
            {"id": c.case_id, "cause": c.cause, "positive": c.positive, "n_services": c.n_services,
             "cause_has_telemetry": c.cause_has_telemetry,
             "arms": {a: {"outcome": r.outcome, "localization": list(r.localization),
                          "retained": c.retained(a)} for a, r in c.arms.items()},
             "retained_via": list(c.retained_via), "retained_by_util": c.retained_by_util,
             "util_present": list(c.util_present)}
            for c in cases
        ],
    }


def _frac(pair: list) -> str:
    k, n = pair
    return f"{k}/{n}" + (f" ({k / n:.0%})" if n else "")


def _num(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def render_markdown(report: dict, *, provenance: dict) -> str:
    """The as-is results post for #209."""
    lines = ["## M3 — frozen out-of-sample result", ""]
    lines += [f"- {k}: `{v}`" for k, v in provenance.items()]
    v = report["verdicts"]
    lines += ["", f"**Generalization:** `{v['generalization']}` · **M2b:** `{v['m2b']}`", ""]
    for name, (value, bound, op) in v["generalization_criteria"].items():
        lines.append(f"- {name}: {_num(value)} (pre-registered {op} {bound})")
    if v["protocol_deviations"]:
        lines += ["", "**Protocol deviations:** " + "; ".join(v["protocol_deviations"])]
    lines += ["", "| metric | " + " | ".join(ARMS) + " |", "|---|" + "---|" * len(ARMS)]
    rows = [
        ("generated / positives", lambda a: f"{a['generated']}/{a['positives']}"),
        ("truth retained (all)", lambda a: _frac(a["truth_retained_all"])),
        ("truth retained (cause has telemetry)", lambda a: _frac(a["truth_retained_cause_has_telemetry"])),
        ("median candidates", lambda a: str(a["median_candidates"])),
        ("median candidate fraction", lambda a: _num(a["median_candidate_fraction"])),
        ("healthy abstention (no hypothesis)", lambda a: _frac(a["healthy_abstained"])),
        ("healthy no localization claim", lambda a: _frac(a["healthy_no_claim"])),
        ("IDENTIFIED precision", lambda a: f"{a['identified_precision'][0]}/{a['identified_precision'][1]}"),
        ("IDENTIFIED on healthy", lambda a: str(a["identified_on_healthy"])),
        ("outcomes", lambda a: ", ".join(f"{k}={n}" for k, n in a["outcomes"].items())),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(report["arms"][arm]) for arm in ARMS) + " |")
    es, ic = report["edge_specificity"], report["identity_coverage"]
    lines += [
        "",
        f"- Edge specificity (healthy), latency >= 2x: windows "
        f"{_frac(es['healthy_windows_with_latency_high_edge'])}, edges "
        f"{_frac(es['healthy_latency_high_over_latency_measured_edges'])} of latency-measured; error "
        f"branch: windows {_frac(es['healthy_windows_with_error_present_edge'])}; any PRESENT edge: windows "
        f"{_frac(es['healthy_windows_with_present_edge'])}, edges "
        f"{_frac(es['healthy_present_over_measured_edges'])} of measured",
        f"- Series-identity coverage: {ic['named_instance']}/{ic['util_samples']} utilization samples name "
        f"their instance ({_num(ic['rate'])})",
        "",
        "Unobservable = no telemetry from the cause service at all; listed as such, not a model failure.",
        "",
        "| case | cause | cause telemetry | " + " | ".join(ARMS) + " | witnessed by | util PRESENT |",
        "|---|---|---|" + "---|" * len(ARMS) + "---|---|",
    ]
    for c in report["cases"]:
        cells = []
        for arm in ARMS:
            r = c["arms"][arm]
            mark = ("✓ " if r["retained"] else "✗ ") if c["positive"] else ""
            cells.append(f"{mark}{r['outcome']} ({len(r['localization'])})")
        tel = "—" if not c["positive"] else ("yes" if c["cause_has_telemetry"] else "**UNOBSERVABLE**")
        via = ", ".join(c["retained_via"]) + (" (util retained it)" if c["retained_by_util"] else "")
        lines.append(f"| {c['id']} | {c['cause'] or '—'} | {tel} | " + " | ".join(cells)
                     + f" | {via or '—'} | {', '.join(c['util_present']) or '—'} |")
    return "\n".join(lines) + "\n"
