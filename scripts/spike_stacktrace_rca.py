#!/usr/bin/env python3
"""SPIKE (#118): does stack-trace *content* localize the RE3 root cause better
than error *volume*?

Deliberately cheap and standalone — no src/core changes, no schema/config/UI.
For each RCAEval RE3 case it downloads logs.parquet, restricts to the incident
window [inject, inject+post], and computes three top-1 predictions:

  volume  — service with the most error-level log lines (proxy for the trivial
            most-frequent-error baseline, ~28.9%)
  stack   — service owning the most *originating* stack-trace lines (a thrown
            exception with real frames, not a relayed 500 / JSON error body)
  both    — prefer services with originating stack traces, tie-break by volume

and compares each to the labelled root cause (the case dir name). Prints the
confusion matrix (volume vs stack) and the three accuracies.

Kill criterion (set by the maintainer): if `stack` and `both` are within noise
of `volume` (~a couple cases), stack traces only describe where the failure
*surfaced*; stop and pivot #118 (logs = manifestation/evidence, traces/metrics
= causal localization). A jump to ~40%+ would mean the 3-case grounding was
misleading and there is a real logs-only signal.

Usage:
    python scripts/spike_stacktrace_rca.py [--limit N] [--post 600]
"""
from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict

from src.core.normalization.fingerprint import fingerprint_message
from src.eval.rcaeval import _infer_level, parse_case_dir_name, parse_inject_time

REPO_ID = "phamquiluan/RCAEval"

# error/fatal levels for the volume arm (mirrors the trivial baseline filter)
_ERROR_RE = re.compile(r"\b(error|fatal|critical|exception|panic|\b5\d\d\b)", re.IGNORECASE)

# An *originating* stack trace: a thrown exception with actual frames. Relays
# (front-end's `{"status":500,"exception":"..."}` JSON, `POST /orders 500`) have
# no frame and must NOT match.
_STACK_RE = re.compile(
    r"\n\s*at\s+\S"                       # Java/Scala "\n\tat pkg.Cls.method(File:line)"
    r"|\bat\s[\w.$]+\([\w.$ ]*:\d+\)"     # same, single-line rendering
    r"|Traceback \(most recent call last\)"  # Python
    r'|\n\s*File "[^"]+", line \d+'       # Python frame
    r"|\bpanic:\s"                        # Go
    r"|\bgoroutine\s+\d+\s+\["            # Go stack
    r"|Exception in thread"               # Java uncaught
    r"|nested exception is [\w.$]+",      # Spring
    re.IGNORECASE,
)


def _is_error(msg: str) -> bool:
    return bool(_ERROR_RE.search(msg))


def _is_originating_stack(msg: str) -> bool:
    return bool(_STACK_RE.search(msg))


def _list_re3_cases():
    from huggingface_hub import HfApi

    files = HfApi().list_repo_files(REPO_ID, repo_type="dataset")
    cases = sorted({f.split("/")[0] for f in files if f.startswith("re3") and "/" in f})
    return cases


def _load_case(case: str, post: int):
    import pyarrow.parquet as pq
    from huggingface_hub import hf_hub_download

    inj_path = hf_hub_download(REPO_ID, repo_type="dataset", filename=f"{case}/inject_time.txt")
    inject = parse_inject_time(open(inj_path).read())
    start = inject.timestamp()
    end = start + post

    lp = hf_hub_download(REPO_ID, repo_type="dataset", filename=f"{case}/logs.parquet")
    t = pq.read_table(lp, columns=["timestamp", "container_name", "message"])
    ts = t.column("timestamp").to_pylist()
    svc = t.column("container_name").to_pylist()
    msg = t.column("message").to_pylist()

    # volume arm == trivial baseline: top (service, fingerprint) error group.
    err_by_svc_fp: Counter = Counter()
    stack_by_svc: Counter = Counter()
    for ti, s, m in zip(ts, svc, msg):
        try:
            tf = float(ti)
            if tf > 1e12:
                tf /= 1000.0
        except (TypeError, ValueError):
            continue
        if not (start <= tf <= end) or not s or m is None:
            continue
        ms = str(m)
        if _infer_level(ms) == "error":
            fp = fingerprint_message(ms)[1]
            err_by_svc_fp[(s, fp)] += 1
        if _is_originating_stack(ms):
            stack_by_svc[s] += 1
    return err_by_svc_fp, stack_by_svc


def _argmax(counter: Counter):
    return counter.most_common(1)[0][0] if counter else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max cases (0 = all)")
    ap.add_argument("--post", type=int, default=600, help="incident window seconds after inject")
    args = ap.parse_args()

    cases = _list_re3_cases()
    if args.limit:
        cases = cases[: args.limit]

    cm = defaultdict(int)  # (volume_ok, stack_ok) -> n
    n = vol_ok = stack_ok = both_ok = 0
    per_service_flip = []  # (case, truth, volume_pred, stack_pred)
    by_system = defaultdict(lambda: {"n": 0, "vol": 0, "stack": 0, "both": 0})

    for case in cases:
        try:
            meta = parse_case_dir_name(case)
        except ValueError:
            continue
        truth = meta.service
        try:
            err_by_svc_fp, stack_by_svc = _load_case(case, args.post)
        except Exception as e:  # noqa: BLE001
            print(f"  skip {case}: {type(e).__name__} {str(e)[:80]}")
            continue

        # trivial baseline: service owning the largest single (service, fp) group
        top_group = err_by_svc_fp.most_common(1)
        volume_pred = top_group[0][0][0] if top_group else None
        stack_pred = _argmax(stack_by_svc)
        # combined: prefer a service that owns originating stack traces, else volume
        both_pred = stack_pred if stack_by_svc else volume_pred

        v = volume_pred == truth
        s = stack_pred == truth
        b = both_pred == truth
        n += 1
        vol_ok += v
        stack_ok += s
        both_ok += b
        cm[(v, s)] += 1
        sysk = meta.suite + meta.system  # e.g. re3ob / re3ss / re3tt
        bs = by_system[sysk]
        bs["n"] += 1
        bs["vol"] += v
        bs["stack"] += s
        bs["both"] += b
        if v != s:
            per_service_flip.append((case, truth, volume_pred, stack_pred))

    if not n:
        print("no cases scored")
        return

    print(f"\n=== stack-trace RCA spike — RE3, {n} cases (post={args.post}s) ===\n")
    print(f"volume  (most error lines/service):   {vol_ok/n:5.1%}  ({vol_ok}/{n})")
    print(f"stack   (most originating frames):    {stack_ok/n:5.1%}  ({stack_ok}/{n})")
    print(f"both    (stack first, volume tiebrk): {both_ok/n:5.1%}  ({both_ok}/{n})")
    print("\nconfusion (volume vs stack):")
    print(f"  both correct:               {cm[(True, True)]}")
    print(f"  volume correct, stack wrong:{cm[(True, False)]}")
    print(f"  volume wrong, stack correct:{cm[(False, True)]}")
    print(f"  both wrong:                 {cm[(False, False)]}")
    print("\nper system (n | volume | stack | both):")
    for sysk, d in sorted(by_system.items()):
        dn = d["n"]
        print(f"  {sysk}: n={dn} | vol {d['vol']/dn:5.1%} | stack {d['stack']/dn:5.1%} | both {d['both']/dn:5.1%}")
    print("\ncases where volume and stack disagree (case | truth | volume | stack):")
    for case, truth, vp, sp in per_service_flip[:40]:
        print(f"  {case} | {truth} | {vp} | {sp}")


if __name__ == "__main__":
    main()
