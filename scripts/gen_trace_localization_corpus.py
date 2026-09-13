#!/usr/bin/env python3
"""Generate the trace-localization benchmark (#118 / #79).

A deliberately small, adversarial, **synthetic** corpus for the question RCAEval cannot
answer (100% oracle, no ERROR spans): can trace topology + ERROR/latency onset tell an
upstream *cause* from a downstream propagated *symptom*? Each case is a microservice call
graph with an injected fault; the telemetry is constructed so the trivial
"loudest-error / busiest-caller" baseline is *wrong* on the adversarial fault types, while
the causal signal (the cause degrades **first**, upstream of the loud symptom) is present
in the traces. Ground-truth causal labels (root cause, first-failing, propagation path,
symptom services, edges, fault type) are written into each ``case.yaml``'s
``trace_localization`` block — see ``src/eval/trace_localization.py``.

Deterministic (seeded), so the "benchmark" is reproducible from this script; output goes to
a gitignored ``data/`` dir by default (like the RCAEval feature cache), regenerate with:

    python scripts/gen_trace_localization_corpus.py            # -> data/eval-cases/trace-loc

Fault types (the case variety):
  callee_fail   cause is a downstream callee; it errors first, its caller errors later
                (loud symptom). Log + trace signal both present.
  symptom_only  cause (callee) errors only in **trace status** (no error logs); the caller
                emits the error **logs** (loud) -> log-only localization points at the caller.
  latency_only  no ERROR status anywhere; the cause's spans inflate latency first, callers
                inflate later. Tests latency-onset localization.
  caller_fail   cause is an upstream caller that fails first (cause == loudest; the easy,
                non-adversarial control).
"""
from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- topologies: service -> list of callees; the first key is the entry point ----------
TOPOLOGIES: dict[str, dict[str, list[str]]] = {
    "shop": {
        "frontend": ["checkout", "recommendation"],
        "checkout": ["payment", "cart"],
        "recommendation": ["product-catalog"],
        "payment": [], "cart": [], "product-catalog": [],
    },
    "orders": {
        "gateway": ["orders"],
        "orders": ["inventory", "billing"],
        "billing": ["ledger"],
        "inventory": [], "ledger": [],
    },
    "media": {
        "edge": ["api"],
        "api": ["auth", "media"],
        "media": ["storage", "transcoder"],
        "auth": [], "storage": [], "transcoder": [],
    },
}

# Per topology: a deep callee (for callee/symptom/latency faults) and an upstream service
# (for caller_fail). The callee's immediate caller is the loud "symptom".
FAULT_TARGETS = {
    "shop": {"callee": "payment", "caller": "checkout"},
    "orders": {"callee": "ledger", "caller": "orders"},
    "media": {"callee": "storage", "caller": "media"},
}

BASELINE_S = 60
INCIDENT_S = 120
ONSET_GAP_S = 12  # symptom (caller) starts failing this long after the cause

# Which signals the CAUSE vs the SYMPTOM exhibit, per fault type. This is what makes the
# benchmark adversarial: in `symptom_only` the cause is visible ONLY in trace ERROR
# status (no error logs, no error-rate metric, flat span-rate), so log/metric ranking
# points at the loud symptom and only trace-status + onset can localise the cause.
#   status      span status_code = ERROR (2)
#   log         emits error-level log lines
#   metric_err  error_rate metric elevated
#   latency     span duration + latency_ms metric inflated (no error signal)
SIGNALS: dict[str, dict[str, set[str]]] = {
    "callee_fail":  {"cause": {"status", "log", "metric_err"}, "symptom": {"status", "log", "metric_err"}},
    "symptom_only": {"cause": {"status"},                      "symptom": {"status", "log", "metric_err"}},
    "latency_only": {"cause": {"latency"},                     "symptom": {"latency"}},
    "caller_fail":  {"cause": {"status", "log", "metric_err"}, "symptom": set()},
}


def _edges(topo: dict[str, list[str]]) -> list[tuple[str, str]]:
    return [(a, b) for a, callees in topo.items() for b in callees]


def _parent_of(topo: dict[str, list[str]]) -> dict[str, str]:
    return {b: a for a, callees in topo.items() for b in callees}


def _entry(topo: dict[str, list[str]]) -> str:
    return next(iter(topo))


def _path_to(topo: dict[str, list[str]], target: str) -> list[str]:
    """entry -> ... -> target (the unique caller chain in these trees)."""
    parent = _parent_of(topo)
    chain = [target]
    while chain[-1] in parent:
        chain.append(parent[chain[-1]])
    return list(reversed(chain))


def _leaves(topo: dict[str, list[str]]) -> list[str]:
    return [s for s, c in topo.items() if not c]


def _random_path(topo: dict[str, list[str]], rng: random.Random) -> list[str]:
    """A root-to-leaf request path (random walk down the call graph)."""
    node = _entry(topo)
    path = [node]
    while topo[node]:
        node = rng.choice(topo[node])
        path.append(node)
    return path


def _hex(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(n))


def _gen_case(topo_name: str, fault_type: str, rng: random.Random, t0: datetime) -> dict:
    topo = TOPOLOGIES[topo_name]
    tgt = FAULT_TARGETS[topo_name]
    cause = tgt["caller"] if fault_type == "caller_fail" else tgt["callee"]
    cause_path = _path_to(topo, cause)  # entry -> ... -> cause
    symptom = _parent_of(topo).get(cause) if fault_type != "caller_fail" else None
    symptom_services = [symptom] if (symptom and symptom != cause) else []

    spans: list[dict] = []
    logs: list[dict] = []
    metric_rows: list[tuple[str, str, float, datetime]] = []
    sig = SIGNALS[fault_type]

    def _signals_for(svc: str, elapsed: float) -> set[str]:
        """Active signals for ``svc`` at ``elapsed`` seconds past inject. The cause
        degrades from inject; the symptom (caller) ONSET_GAP later (propagated)."""
        if svc == cause and elapsed >= 0:
            return sig["cause"]
        if symptom and svc == symptom and elapsed >= ONSET_GAP_S:
            return sig["symptom"]
        return set()

    def emit_trace(start: datetime, path: list[str]):
        trace_id = _hex(rng, 32)
        parent_span = None
        depth = len(path)
        for i, svc in enumerate(path):
            span_id = _hex(rng, 16)
            svc_start = start + timedelta(milliseconds=2 * i)
            elapsed = (svc_start - t0).total_seconds()
            active = _signals_for(svc, elapsed)
            base_ms = rng.uniform(5, 25)
            dur = base_ms + 2 * (depth - i)
            if "latency" in active:
                dur = base_ms * rng.uniform(8, 15)  # latency excursion, no error status
            err = "status" in active
            spans.append({
                "trace_id": trace_id, "span_id": span_id, "parent_span_id": parent_span,
                "service": svc, "operation": f"{svc} handle",
                "start_time": svc_start.isoformat(),
                "duration_ms": round(dur, 3),
                "status_code": "2" if err else "0",
            })
            parent_span = span_id
            yield svc, svc_start, active

    def emit_log(ts: datetime, svc: str, level: str, msg: str):
        logs.append({"timestamp": ts.isoformat(), "service": svc, "message": msg, "level": level})

    # --- traffic: baseline then incident. A few requests per second. ------------------
    total = BASELINE_S + INCIDENT_S
    for sec in range(total):
        ts = t0 + timedelta(seconds=sec - BASELINE_S)
        for _ in range(rng.randint(2, 4)):
            # bias traffic toward the faulted path so the cause is well exercised
            path = cause_path if rng.random() < 0.55 else _random_path(topo, rng)
            jitter = timedelta(milliseconds=rng.uniform(0, 900))
            for svc, svc_start, active in emit_trace(ts + jitter, path):
                if "log" in active:
                    emit_log(svc_start, svc, "error", f"{svc}: request failed")
                elif rng.random() < 0.15:
                    emit_log(svc_start, svc, "info", f"{svc}: ok")
        # metrics: per-service latency + error-rate gauges, once per 10s. Crucially, the
        # cause's error-rate stays flat under `symptom_only`/`latency_only` — the whole
        # point is that only the trace signal betrays it.
        if sec % 10 == 0:
            for svc in topo:
                active = _signals_for(svc, sec - BASELINE_S)
                lat = rng.uniform(10, 20) * (5.0 if "latency" in active else 1.0)
                errrate = 0.4 if "metric_err" in active else 0.01
                metric_rows.append((svc, "latency_ms", round(lat, 2), ts))
                metric_rows.append((svc, "error_rate", round(errrate, 3), ts))

    labels = {
        "root_cause": cause,
        "fault_type": fault_type,
        "first_failing": cause,
        "propagation_path": cause_path[::-1],  # cause -> ... -> entry (failure travels up)
        "symptom_services": symptom_services,
        "edges": [[a, b] for a, b in _edges(topo)],
    }
    return {"spans": spans, "logs": logs, "metrics": metric_rows, "labels": labels,
            "cause": cause, "topo": topo_name}


def _write_case(out: Path, case_id: str, data: dict, t0: datetime) -> None:
    d = out / case_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "spans.jsonl").write_text("\n".join(json.dumps(s) for s in data["spans"]) + "\n")
    (d / "logs.jsonl").write_text("\n".join(json.dumps(m) for m in data["logs"]) + "\n")
    (d / "metrics.jsonl").write_text(
        "\n".join(json.dumps({"service": s, "metric": m, "value": v, "ts": ts.isoformat()})
                  for s, m, v, ts in data["metrics"]) + "\n"
    )
    win_start, win_end = t0, t0 + timedelta(seconds=INCIDENT_S)
    import yaml

    case_yaml = {
        "id": case_id,
        "window": {"start": win_start.isoformat(), "end": win_end.isoformat()},
        "baseline": f"{BASELINE_S}s",
        "root_cause": {"service": data["cause"]},
        "trigger": {"timestamp": win_start.isoformat(), "type": "code"},
        "expect_explanation": True,
        "notes": f"synthetic trace-localization case: {data['topo']} / {data['labels']['fault_type']}",
        "trace_localization": data["labels"],
    }
    (d / "case.yaml").write_text(yaml.safe_dump(case_yaml, sort_keys=False))


def generate(out: Path, seed: int = 0) -> int:
    rng = random.Random(seed)
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fault_types = ["callee_fail", "symptom_only", "latency_only", "caller_fail"]
    n = 0
    for topo_name in TOPOLOGIES:
        for fault_type in fault_types:
            for variant in range(2):  # two seeded variants each -> 3x4x2 = 24 cases
                case_rng = random.Random(rng.randint(0, 2**31))
                data = _gen_case(topo_name, fault_type, case_rng, t0)
                case_id = f"tl_{topo_name}_{fault_type}_{variant}"
                _write_case(out, case_id, data, t0)
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parents[1] / "data" / "eval-cases" / "trace-loc")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    n = generate(args.out, args.seed)
    print(f"wrote {n} trace-localization cases -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
