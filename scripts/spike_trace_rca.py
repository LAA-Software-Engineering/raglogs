#!/usr/bin/env python3
"""SPIKE (#118, Phase C): does trace anomaly localize the RE3 root cause where
logs cannot (esp. propagation faults / train-ticket)?

Cheap, standalone — no src/core changes. RCAEval traces have no statusCode
(100% null), so failure is inferred from *change vs a pre-injection baseline*
(BARO-style) over trace-derived per-service signals:

  rate      — spans/sec, incident vs baseline (a fault often makes the injected
              service get retried/hammered while downstream traffic collapses)
  duration  — p95 span duration, incident vs baseline (latency anomaly)

Candidates are noise-filtered (min incident spans) so low-sample services don't
win on a spurious ratio. Predictions:

  rate      — argmax rate-ratio
  dur       — argmax duration-p95-ratio
  combined  — argmax max(rate-ratio, dur-ratio)

compared to the labelled root cause, with a per-system (leave-one-system-out)
breakdown against the 28.9% logs volume baseline.

Usage:
    python scripts/spike_trace_rca.py [--limit N] [--pre 300] [--post 600] [--min-spans 20]
"""
from __future__ import annotations

import argparse
import statistics
from collections import Counter, defaultdict

from src.eval.rcaeval import parse_case_dir_name, parse_inject_time

REPO_ID = "phamquiluan/RCAEval"


def _list_re3_cases():
    from huggingface_hub import HfApi

    files = HfApi().list_repo_files(REPO_ID, repo_type="dataset")
    return sorted({f.split("/")[0] for f in files if f.startswith("re3") and "/" in f})


def _p95(xs):
    if not xs:
        return 0.0
    if len(xs) >= 20:
        return statistics.quantiles(xs, n=20)[18]
    return float(max(xs))


def _load(case: str, pre: int, post: int):
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    inj = parse_inject_time(
        open(hf_hub_download(REPO_ID, repo_type="dataset", filename=f"{case}/inject_time.txt")).read()
    ).timestamp()
    tp = hf_hub_download(REPO_ID, repo_type="dataset", filename=f"{case}/traces.parquet")
    t = pq.read_table(tp, columns=["serviceName", "startTimeMillis", "duration"]).to_pylist()

    b_cnt: Counter = Counter()
    i_cnt: Counter = Counter()
    b_dur = defaultdict(list)
    i_dur = defaultdict(list)
    for r in t:
        try:
            sec = int(r["startTimeMillis"]) / 1000.0
            d = int(r["duration"])
        except (TypeError, ValueError):
            continue
        s = r["serviceName"]
        if not s:
            continue
        if inj - pre <= sec < inj:
            b_cnt[s] += 1
            b_dur[s].append(d)
        elif inj <= sec <= inj + post:
            i_cnt[s] += 1
            i_dur[s].append(d)
    return b_cnt, i_cnt, b_dur, i_dur, pre, post


def _predict(case, pre, post, min_spans):
    b_cnt, i_cnt, b_dur, i_dur, pre, post = _load(case, pre, post)
    rate, dur, comb = {}, {}, {}
    for s in set(i_cnt):
        if i_cnt[s] < min_spans:
            continue
        br = b_cnt.get(s, 0) / pre
        ir = i_cnt[s] / post
        rate_ratio = (ir + 1e-9) / (br + 1e-9)
        dur_ratio = (_p95(i_dur.get(s, [])) + 1) / (_p95(b_dur.get(s, [])) + 1)
        rate[s] = rate_ratio
        dur[s] = dur_ratio
        comb[s] = max(rate_ratio, dur_ratio)

    def amax(d):
        return max(d, key=d.get) if d else None

    return amax(rate), amax(dur), amax(comb)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pre", type=int, default=300)
    ap.add_argument("--post", type=int, default=600)
    ap.add_argument("--min-spans", type=int, default=20)
    ap.add_argument("--per-system-limit", type=int, default=0,
                    help="cap cases per system (ob/ss/tt) for a fast balanced subset")
    args = ap.parse_args()

    cases = _list_re3_cases()
    if args.per_system_limit:
        seen: Counter = Counter()
        picked = []
        for c in cases:
            try:
                sysk = parse_case_dir_name(c).system
            except ValueError:
                continue
            if seen[sysk] < args.per_system_limit:
                seen[sysk] += 1
                picked.append(c)
        cases = picked
    if args.limit:
        cases = cases[: args.limit]

    n = 0
    ok = {"rate": 0, "dur": 0, "comb": 0}
    by_sys = defaultdict(lambda: {"n": 0, "rate": 0, "dur": 0, "comb": 0})
    for case in cases:
        try:
            meta = parse_case_dir_name(case)
        except ValueError:
            continue
        truth = meta.service
        try:
            pr, pd, pc = _predict(case, args.pre, args.post, args.min_spans)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {type(e).__name__} {str(e)[:80]}")
            continue
        n += 1
        bs = by_sys[meta.suite + meta.system]
        bs["n"] += 1
        for k, pred in (("rate", pr), ("dur", pd), ("comb", pc)):
            hit = pred == truth
            ok[k] += hit
            bs[k] += hit

    if not n:
        print("no cases")
        return
    print(f"\n=== trace-anomaly RCA spike — RE3, {n} cases (pre={args.pre} post={args.post} min_spans={args.min_spans}) ===\n")
    print("logs volume baseline (ref):  ~28.9%")
    for k in ("rate", "dur", "comb"):
        print(f"trace {k:5s}: {ok[k]/n:5.1%}  ({ok[k]}/{n})")
    print("\nper system (n | rate | dur | comb):")
    for sysk, d in sorted(by_sys.items()):
        dn = d["n"]
        print(f"  {sysk}: n={dn} | rate {d['rate']/dn:5.1%} | dur {d['dur']/dn:5.1%} | comb {d['comb']/dn:5.1%}")


if __name__ == "__main__":
    main()
