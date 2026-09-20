"""Structural partitioning — the deterministic core of the #177 epic (#182, Phase D).

Given a set of causal hypotheses and the incident's observations, group the hypotheses into
**equivalence classes** by their *categorical* predictions over the **usable** observables, after
removing the ones a usable observation logically contradicts. The whole point is that this is
**provably independent of ranker scores**: nothing here reads a score, so partition membership cannot
depend on one (Invariant 6). Ranking is a separate, later signal (Phase G).

The machinery, exactly as #182 specifies it::

    F_usable   = { f : collectable(f) ∧ availability(f)=OBSERVED ∧ measurement_confidence(f) ≥ τ_f }
    S_O(C)     = { (f, prediction(C, f)) : f ∈ F_usable }        # categorical predictions only
    C_i ~_O C_j  ⇔  S_O(C_i) = S_O(C_j)
    D(C_i,C_j) = usable observables whose categorical predictions differ  (empty within a class)
    D_missing  = distinguishers that would separate members but are NOT in F_usable
    partition H by ~_O   (after hard-incompatibility filtering)

Predictions entering a signature are **categorical** (``PRESENT`` / ``ABSENT`` / ``HIGH`` / ``LOW`` …);
continuous magnitudes must be discretized first (:func:`discretize_ratio` / :func:`discretize_rate`
apply the pre-D spike's policy — a latency ``≈2×`` gate, error-rate keyed on presence), so two nearby
values (rate ``0.11`` vs ``0.13``) never fall into different classes (Invariant 7).
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from itertools import combinations
from types import MappingProxyType

from src.core.rca.hypothesis import Hypothesis
from src.core.rca.observable import Observable, State
from src.core.rca.observable import usable_observables as _collectable_observed

_EMPTY_TAU: Mapping[str, float] = MappingProxyType({})


def _finite_in_range(name: str, value: float, lo: float, hi: float) -> None:
    """Reject non-real, non-finite, or out-of-range numbers (booleans are not numbers here) so a
    malformed input can never acquire a structural category or silently erase evidence."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number, got {value!r}")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if not (lo <= value <= hi):
        hi_s = "inf" if hi == math.inf else hi
        raise ValueError(f"{name} must be in [{lo}, {hi_s}], got {value!r}")


@dataclass(frozen=True)
class UsabilityPolicy:
    """The ``F_usable`` confidence gate: an OBSERVED, collectable observable is usable only if its
    ``measurement_confidence`` meets its threshold ``τ_f``. ``default_tau`` applies to any observable
    without a specific entry in ``tau``. The default policy (``0.0``) accepts every OBSERVED
    measurement, matching Phase A."""

    default_tau: float = 0.0
    tau: Mapping[str, float] = field(default_factory=lambda: _EMPTY_TAU)

    def __post_init__(self) -> None:
        # A frozen invalid policy is still invalid: validate every threshold as a real confidence in
        # [0, 1] (consistent with Observable.measurement_confidence) so a malformed policy fails at the
        # boundary rather than silently erasing all evidence via a NaN/out-of-range comparison.
        _finite_in_range("default_tau", self.default_tau, 0.0, 1.0)
        tau = dict(self.tau)
        for k, v in tau.items():
            if not isinstance(k, str) or not k:
                raise ValueError(f"UsabilityPolicy: threshold id must be a non-empty string, got {k!r}")
            _finite_in_range(f"tau[{k!r}]", v, 0.0, 1.0)
        object.__setattr__(self, "tau", MappingProxyType(tau))

    def threshold(self, observable_id: str) -> float:
        return self.tau.get(observable_id, self.default_tau)


DEFAULT_POLICY = UsabilityPolicy()


def usable_observations(
    observations: Iterable[Observable], policy: UsabilityPolicy = DEFAULT_POLICY
) -> list[Observable]:
    """**The** authoritative usable-observation set for a partition: collectable, OBSERVED, and
    meeting the per-observable confidence threshold ``τ_f``. Every downstream use — signatures,
    D_missing, *and* hard elimination — consumes this one set, so an observation cannot be too
    untrustworthy for a signature yet trusted enough to irreversibly kill a hypothesis. Excludes
    UNKNOWN and uncollectable observables (Invariants 1 & 2) and duplicate coordinates."""
    return [o for o in _collectable_observed(observations)
            if o.measurement_confidence >= policy.threshold(o.id)]


def usable_ids(observations: Iterable[Observable], policy: UsabilityPolicy = DEFAULT_POLICY) -> tuple[str, ...]:
    """``F_usable`` — the sorted ids of :func:`usable_observations`."""
    return tuple(sorted(o.id for o in usable_observations(observations, policy)))


def signature(prediction: Mapping[str, str], f_usable: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    """``S_O(C)`` — the hypothesis's categorical predictions projected over ``f_usable``, sorted by
    id; ids the hypothesis does not predict are omitted. This alone decides class membership."""
    return tuple((f, prediction[f]) for f in f_usable if f in prediction)


@dataclass(frozen=True)
class EquivalenceClass:
    """One ``~_O`` class: the shared usable ``signature``, the ``members`` (sorted by id), and
    ``d_missing`` — the coordinates that *would* separate the members but were not usable this
    incident (uncollectable, UNKNOWN, or below threshold). ``d_missing`` is empty for a singleton."""

    signature: tuple[tuple[str, str], ...]
    members: tuple[Hypothesis, ...]
    d_missing: frozenset[str]

    @property
    def is_singleton(self) -> bool:
        return len(self.members) == 1


@dataclass(frozen=True)
class Partition:
    """The full structural result: the surviving ``classes`` (deterministically ordered by
    signature) and the ``f_usable`` set they were computed over. ``eliminated`` are the hypotheses
    a usable observation hard-contradicted."""

    classes: tuple[EquivalenceClass, ...]
    f_usable: tuple[str, ...]
    eliminated: tuple[Hypothesis, ...] = ()


def _distinct(hypotheses: Iterable[Hypothesis]) -> list[Hypothesis]:
    """Materialize the hypothesis *set* ``H`` at the boundary: exact duplicates are canonicalized to
    one, and two entries that share a causal ``id`` but define different behavior are rejected as
    conflicting. Input multiplicity must not become causal cardinality — otherwise ``[h, h]`` would
    turn an IDENTIFIED result into NON_IDENTIFIABLE (with an empty, invalid D_missing) in Phase E."""
    by_id: dict[str, Hypothesis] = {}
    for h in hypotheses:
        prev = by_id.get(h.id)
        if prev is None:
            by_id[h.id] = h
        elif prev != h:
            raise ValueError(
                f"partition: conflicting hypotheses share id {h.id!r} — a causal id must have one "
                "definition"
            )
    return list(by_id.values())


def _d_missing(members: tuple[Hypothesis, ...], f_usable: frozenset[str]) -> frozenset[str]:
    """Coordinates on which some pair of members' categorical predictions differ and which are NOT
    usable — i.e. exactly the distinguishers that were not collected. Members already agree on every
    usable coordinate (that is why they share a class), so any disagreement is on a non-usable one."""
    missing: set[str] = set()
    for a, b in combinations(members, 2):
        pa, pb = a.predictions, b.predictions
        for f in set(pa) | set(pb):
            if f not in f_usable and pa.get(f) != pb.get(f):
                missing.add(f)
    return frozenset(missing)


def partition(
    hypotheses: Iterable[Hypothesis],
    observations: Iterable[Observable],
    policy: UsabilityPolicy = DEFAULT_POLICY,
) -> Partition:
    """Partition ``hypotheses`` into ``~_O`` equivalence classes over the usable observations, after
    dropping any hypothesis a usable observation hard-contradicts. **Reads no scores** — the result
    is identical for any ranking (Invariant 6)."""
    observations = list(observations)
    # Derive the ONE usable set and reuse it for signatures, D_missing, AND hard elimination — so a
    # below-threshold observation that is excluded from F_usable also cannot eliminate a hypothesis.
    usable = usable_observations(observations, policy)
    f_usable = tuple(sorted(o.id for o in usable))
    f_usable_set = frozenset(f_usable)

    survivors: list[Hypothesis] = []
    eliminated: list[Hypothesis] = []
    for h in _distinct(hypotheses):
        (eliminated if h.hard_incompatibility(usable) else survivors).append(h)

    grouped: dict[tuple[tuple[str, str], ...], list[Hypothesis]] = {}
    for h in survivors:
        grouped.setdefault(signature(h.predictions, f_usable), []).append(h)

    classes = tuple(
        EquivalenceClass(
            signature=sig,
            members=(members := tuple(sorted(hs, key=lambda h: h.id))),
            d_missing=_d_missing(members, f_usable_set),
        )
        for sig, hs in sorted(grouped.items())
    )
    return Partition(classes=classes, f_usable=f_usable, eliminated=tuple(eliminated))


# --- Discretization (pre-D spike policy): continuous magnitude -> categorical band -----------------

def discretize_ratio(ratio: float, *, high: float = 2.0, low: float = 0.5) -> str:
    """Discretize an incident/baseline **multiplier** into a categorical band. The ``high=2.0`` gate
    is the spike's load-bearing latency threshold (stable at ``≥2×``, degenerate below); below-``low``
    is a symmetric drop. Nearby values land in the same band, so they never split a class (Invariant 7).

    A ratio must be finite and non-negative and the thresholds coherent (``0 ≤ low ≤ high``): a
    non-finite/invalid magnitude must never acquire a category (that is the collapse this epic
    prevents), so it raises instead of silently reading ``NORMAL``."""
    _finite_in_range("ratio", ratio, 0.0, math.inf)
    _finite_in_range("high", high, 0.0, math.inf)
    _finite_in_range("low", low, 0.0, math.inf)
    if low > high:
        raise ValueError(f"discretize_ratio: low ({low}) must not exceed high ({high})")
    if ratio >= high:
        return State.HIGH
    if ratio <= low:
        return State.LOW
    return State.NORMAL


def discretize_rate(rate: float, *, cutoff: float = 0.05) -> str:
    """Discretize an error **rate** to presence. The spike found the error-rate cutoff is not a
    sensitive knob (error identification keys on span/log presence), so this is a simple threshold:
    ``0.11`` and ``0.13`` both read ``PRESENT`` and cannot form distinct classes (Invariant 7).

    A rate and its cutoff must be finite and in ``[0, 1]``: a non-finite/out-of-range value raises
    rather than being read as ``ABSENT`` (which would fabricate observed absence from missing data)."""
    _finite_in_range("rate", rate, 0.0, 1.0)
    _finite_in_range("cutoff", cutoff, 0.0, 1.0)
    return State.PRESENT if rate >= cutoff else State.ABSENT
