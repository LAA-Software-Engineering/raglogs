#!/usr/bin/env python3
"""Seal a DEV / TEST split over an eval corpus, holding out whole fault families / services.

The one command you run **once, before touching the algorithm** (see
``src/eval/sealed_split.py``): it reads every ``case.yaml`` in a corpus, assigns cases to DEV
or SEALED TEST by fault family (or root-cause service), and writes ``split.yaml``. After that
the dev loop scores DEV only; TEST is read exactly once, for the frozen result.

    # inspect the fault families present (does not seal anything)
    python scripts/seal_corpus_split.py data/eval-cases/trace-loc --list-families

    # seal: hold out whole families for TEST (transfer test, not memorisation)
    python scripts/seal_corpus_split.py data/eval-cases/trace-loc \
        --holdout-family symptom_only --holdout-family latency_only
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path


def _load_docs(corpus: Path) -> dict[str, dict]:
    import yaml

    docs: dict[str, dict] = {}
    for case_yaml in sorted(corpus.glob("*/case.yaml")):
        docs[case_yaml.parent.name] = yaml.safe_load(case_yaml.read_text()) or {}
    return docs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--holdout-family", action="append", default=[], dest="families")
    ap.add_argument("--holdout-service", action="append", default=[], dest="services")
    ap.add_argument("--list-families", action="store_true", help="print fault families + counts and exit")
    args = ap.parse_args()

    from src.eval.sealed_split import fault_family, make_split, write_manifest

    docs = _load_docs(args.corpus)
    if not docs:
        print(f"no case.yaml under {args.corpus}")
        return 1

    fams = Counter(fault_family(d) for d in docs.values())
    if args.list_families:
        print(f"{len(docs)} cases; fault families:")
        for fam, n in fams.most_common():
            print(f"  {fam:20s} {n}")
        return 0

    split = make_split(docs, holdout_families=args.families, holdout_services=args.services)
    path = write_manifest(args.corpus, split)
    print(f"sealed split -> {path}")
    print(f"  DEV : {len(split.dev)} cases")
    print(f"  TEST: {len(split.test)} cases (held out: families={split.holdout_families} services={split.holdout_services})")
    print(f"  test fingerprint: {split.test_fingerprint[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
