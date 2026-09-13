"""Sealed DEV / TEST split by fault family — the eval discipline for the trace-
localization arc (#118 / #79).

The rule (user-directed): before touching the algorithm, split a captured corpus into a
**DEV** subset (algorithm/feature selection allowed) and a **SEALED TEST** subset that is
*never inspected during tuning*, and hold out **whole fault families / services** — not
random cases — so the test asks whether the trace logic *transfers* rather than memorises a
particular injected failure. The frozen model then gets exactly one look at TEST.

This module is the guard rail. :func:`make_split` assigns cases deterministically (a case is
TEST iff its fault family or root-cause service is in the held-out set); :func:`write_manifest`
seals it to ``split.yaml`` with a fingerprint of the test set; and the loaders refuse to hand
back TEST ids unless the caller *explicitly* unseals — so a dev-loop eval can only ever score
DEV, and reading TEST is a deliberate, auditable act.

Pure over ``case.yaml`` dicts (no DB), so it works for any corpus — OTel flag cases, Chaos
Mesh cases, or the synthetic mechanism benchmark — and is unit-testable.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

MANIFEST_NAME = "split.yaml"

_FLAG_RE = re.compile(r"flag=([A-Za-z0-9_]+)")


class SealedError(RuntimeError):
    """Raised when TEST cases are requested without an explicit unseal, or the sealed
    test set has been tampered with (fingerprint mismatch)."""


def fault_family(doc: dict) -> str:
    """Derive a case's fault *family* from its ``case.yaml`` dict, most specific first.

    A family groups cases that share a failure mechanism, so holding one out tests
    transfer. Precedence: an explicit ``fault_family``; the synthetic corpus's
    ``trace_localization.fault_type``; the OTel flag name in ``notes`` (``flag=<name>``);
    the ``trigger.type``; ``negative`` for healthy cases; else ``unknown``.
    """
    if doc.get("fault_family"):
        return str(doc["fault_family"])
    tl = doc.get("trace_localization") or {}
    if tl.get("fault_type"):
        return str(tl["fault_type"])
    m = _FLAG_RE.search(str(doc.get("notes", "")))
    if m:
        return m.group(1)
    if doc.get("expect_explanation") is False:
        return "negative"
    trig = doc.get("trigger") or {}
    if trig.get("type"):
        return str(trig["type"])
    return "unknown"


def root_cause_service(doc: dict) -> str | None:
    rc = doc.get("root_cause") or {}
    return rc.get("service")


@dataclass
class Split:
    dev: list[str]
    test: list[str]
    holdout_families: list[str] = field(default_factory=list)
    holdout_services: list[str] = field(default_factory=list)

    @property
    def test_fingerprint(self) -> str:
        return _fingerprint(self.test)


def _fingerprint(ids: list[str]) -> str:
    """Stable hash of the test-id set — detects a test split silently changing under a
    frozen result (the seal's integrity check)."""
    joined = "\n".join(sorted(ids)).encode()
    return hashlib.sha256(joined).hexdigest()


def make_split(
    cases: dict[str, dict],
    *,
    holdout_families: list[str] | None = None,
    holdout_services: list[str] | None = None,
) -> Split:
    """Assign each case to DEV or TEST. A case is TEST iff its fault family is in
    ``holdout_families`` or its root-cause service is in ``holdout_services`` — whole
    families/services move together, never individual cases. Deterministic; ids sorted."""
    hf = set(holdout_families or [])
    hs = set(holdout_services or [])
    if not hf and not hs:
        raise ValueError("a sealed split must hold out at least one fault family or service")
    dev, test = [], []
    for cid in sorted(cases):
        doc = cases[cid]
        is_test = fault_family(doc) in hf or (root_cause_service(doc) in hs if hs else False)
        (test if is_test else dev).append(cid)
    return Split(dev=dev, test=test, holdout_families=sorted(hf), holdout_services=sorted(hs))


def write_manifest(corpus_dir: Path, split: Split) -> Path:
    """Seal a split to ``<corpus_dir>/split.yaml`` (refuses to clobber an existing one —
    a seal is written once; re-sealing would let TEST be reshuffled after peeking)."""
    import yaml

    path = Path(corpus_dir) / MANIFEST_NAME
    if path.exists():
        raise SealedError(f"{path} already exists; refusing to re-seal (delete it deliberately to re-split)")
    doc = {
        "sealed": True,
        "holdout_families": split.holdout_families,
        "holdout_services": split.holdout_services,
        "test_fingerprint": split.test_fingerprint,
        "dev": split.dev,
        "test": split.test,
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return path


def load_manifest(corpus_dir: Path) -> dict:
    import yaml

    path = Path(corpus_dir) / MANIFEST_NAME
    if not path.exists():
        raise FileNotFoundError(f"no sealed split at {path}; create one with make_split + write_manifest")
    doc = yaml.safe_load(path.read_text()) or {}
    recomputed = _fingerprint(list(doc.get("test", [])))
    if doc.get("test_fingerprint") != recomputed:
        raise SealedError(
            f"{path}: test fingerprint mismatch — the sealed TEST set was modified "
            f"(expected {doc.get('test_fingerprint')}, got {recomputed})"
        )
    return doc


def dev_ids(manifest: dict) -> list[str]:
    """The DEV case ids — the only ones a tuning loop may score."""
    return list(manifest.get("dev", []))


def test_ids(manifest: dict, *, unseal: bool = False) -> list[str]:
    """The SEALED TEST case ids. Raises unless ``unseal=True`` — reading TEST is a
    deliberate, one-shot act (the frozen model's single look), never part of dev scoring."""
    if not unseal:
        raise SealedError(
            "TEST cases are sealed. Pass unseal=True only for the one-shot frozen "
            "evaluation — never during model/feature development."
        )
    return list(manifest.get("test", []))
