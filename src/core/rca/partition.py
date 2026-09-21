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


@dataclass(frozen=True)
class EquivalenceClass:
    """One ``~_O`` class: the shared usable ``signature``, the ``members`` (sorted by id), and
    ``d_missing`` — the coordinates that *would* separate the members but were not usable this
    incident (uncollectable, UNKNOWN, or below threshold). ``d_missing`` is empty for a singleton."""

    signature: tuple[tuple[str, tuple], ...]
    members: tuple[Hypothesis, ...]
    d_missing: frozenset[str]

    @property
    def is_singleton(self) -> bool:
        return len(self.members) == 1


@dataclass(frozen=True)
class Partition:
    """The full **structural** result: the surviving ``classes`` (deterministically ordered by
    signature) and the ``f_usable`` set they were computed over. ``eliminated`` are the hypotheses
    a usable observation hard-contradicted.

    This is the machinery only — it does **not** assign an outcome. Reading a partition as
    IDENTIFIED / NON_IDENTIFIABLE / UNCERTAIN is Phase E's job (#183); how Phase E treats a
    zero-``classes`` partition is defined there, not here.

    The value type **enforces its own invariant**: a partition describes at least one hypothesis, so
    an empty ``classes`` is only valid alongside a non-empty ``eliminated``. That makes
    :attr:`no_surviving_hypothesis` mean exactly "every hypothesis was hard-eliminated" on any
    construction path — a hollow ``Partition(classes=(), eliminated=())`` cannot masquerade as it."""

    classes: tuple[EquivalenceClass, ...]
    f_usable: tuple[str, ...]
    eliminated: tuple[Hypothesis, ...] = ()

    def __post_init__(self) -> None:
        if not self.classes and not self.eliminated:
            raise ValueError(
                "Partition describes no hypotheses: an empty `classes` is valid only when `eliminated` "
                "is non-empty (every hypothesis was hard-eliminated)"
            )

    @property
    def no_surviving_hypothesis(self) -> bool:
        """True when no hypothesis survived hard-incompatibility filtering (``classes`` is empty, so
        by the type invariant everything is in ``eliminated``). A structural fact Phase E consumes;
        this module does not label it an outcome."""
        return not self.classes


def _coord_behavior(h: Hypothesis) -> dict[str, tuple]:
    """The hypothesis's full structural behavior per coordinate: ``{f: (predicted_state, hard_states)}``
    where ``predicted_state`` is its categorical prediction (or ``None``) and ``hard_states`` is the
    sorted tuple of states of ``f`` that would hard-eliminate it. Both halves of the Phase C model
    matter for distinguishability — measuring a coordinate can separate two hypotheses either because
    they predict it differently *or* because it eliminates one and not the other."""
    contra = {
        c.observable_id: tuple(sorted(c.states))
        for c in (h.observation_model.contradictions if h.observation_model else ())
    }
    coords = set(h.predictions) | set(contra)
    return {f: (h.predictions.get(f), contra.get(f, ())) for f in coords}


def _behavior(h: Hypothesis) -> tuple:
    """A hypothesis's complete, id/localization-free behavioral fingerprint (predictions + hard rules).
    Two hypotheses with the same fingerprint are indistinguishable under *every* observation."""
    return tuple(sorted(_coord_behavior(h).items()))


def structural_signature(h: Hypothesis, f_usable: tuple[str, ...]) -> tuple[tuple[str, tuple], ...]:
    """``S_O(C)`` — the hypothesis's full structural behavior ``(predicted_state, hard_states)``
    projected over ``F_usable``, sorted by id. This is the **single** equivalence relation: it drives
    class membership, and its complement over non-usable coordinates is :func:`_d_missing`. Using the
    full behavior (not predictions alone) means a *usable* hard-rule difference creates distinct
    classes, while an *unavailable* one becomes a missing distinguisher — so a multi-member class can
    never have an empty ``D_missing``."""
    behavior = _coord_behavior(h)
    return tuple((f, behavior[f]) for f in f_usable if f in behavior)


def _distinct(hypotheses: Iterable[Hypothesis]) -> list[Hypothesis]:
    """Materialize the hypothesis *set* ``H`` at the boundary, deterministically. A causal ``id`` may
    appear more than once only as the *same* hypothesis: a literal repeat, or a distinct object with
    identical behavior **and** identical ranking provenance, is canonicalized to one; anything else
    sharing an id is rejected — differing behavior is a conflicting definition, and differing
    ``source`` is ambiguous provenance (``Hypothesis.__eq__`` ignores ``source``, so silently keeping
    the first copy would make which score reaches Phase G input-order-dependent). Two *distinct* ids
    with an identical full behavioral model are rejected as degenerate — no observation could ever
    separate them, so they would form a multi-member class with an empty D_missing, the invalid
    state #183 forbids. Input multiplicity must not become causal cardinality."""
    by_id: dict[str, Hypothesis] = {}
    for h in hypotheses:
        prev = by_id.get(h.id)
        if prev is None:
            by_id[h.id] = h
        elif prev is h:
            continue  # literal repeated object — canonicalize
        elif prev != h:
            raise ValueError(
                f"partition: conflicting hypotheses share id {h.id!r} — a causal id must have one "
                "definition"
            )
        elif prev.source != h.source:
            raise ValueError(
                f"partition: hypotheses share id {h.id!r} but carry different ranking provenance — "
                "provenance cannot be silently discarded"
            )
        # else: a distinct object with identical behavior AND provenance — a harmless duplicate.
    seen: dict[tuple, str] = {}
    for h in by_id.values():
        fingerprint = _behavior(h)
        if fingerprint in seen:
            raise ValueError(
                f"partition: hypotheses {seen[fingerprint]!r} and {h.id!r} have identical behavioral "
                "models — no observation could distinguish them (a degenerate hypothesis set)"
            )
        seen[fingerprint] = h.id
    return list(by_id.values())


def _d_missing(members: tuple[Hypothesis, ...], f_usable: frozenset[str]) -> frozenset[str]:
    """Coordinates on which some pair of members' full structural behavior — categorical prediction
    *or* hard-elimination rule — differs and which are NOT usable: exactly the distinguishers that
    were not collected. Members already agree over every usable coordinate (that is why they share a
    class), so any behavioral disagreement is on a non-usable one."""
    missing: set[str] = set()
    behaviors = [_coord_behavior(m) for m in members]
    default = (None, ())
    for ba, bb in combinations(behaviors, 2):
        for f in set(ba) | set(bb):
            if f not in f_usable and ba.get(f, default) != bb.get(f, default):
                missing.add(f)
    return frozenset(missing)


def partition(
    hypotheses: Iterable[Hypothesis],
    observations: Iterable[Observable],
    policy: UsabilityPolicy = DEFAULT_POLICY,
) -> Partition:
    """Partition ``hypotheses`` (a non-empty set) into ``~_O`` equivalence classes over the usable
    observations, after dropping any hypothesis a usable observation hard-contradicts. **Reads no
    scores** — the result is identical for any ranking (Invariant 6). Raises if the set is empty or
    carries conflicting/degenerate definitions; a zero-class result means every hypothesis was
    hard-eliminated (:attr:`Partition.no_surviving_hypothesis`). Assigning an outcome to the partition
    is Phase E's responsibility (#183), not this function's."""
    observations = list(observations)
    # Derive the ONE usable set and reuse it for signatures, D_missing, AND hard elimination — so a
    # below-threshold observation that is excluded from F_usable also cannot eliminate a hypothesis.
    usable = usable_observations(observations, policy)
    f_usable = tuple(sorted(o.id for o in usable))
    f_usable_set = frozenset(f_usable)

    distinct = _distinct(hypotheses)
    if not distinct:
        # An empty hypothesis set is a caller error, not a structural outcome: reject it so that a
        # zero-class Partition can only ever mean "all hypotheses were hard-eliminated".
        raise ValueError("partition requires at least one hypothesis")

    survivors: list[Hypothesis] = []
    eliminated: list[Hypothesis] = []
    for h in distinct:
        (eliminated if h.hard_incompatibility(usable) else survivors).append(h)

    grouped: dict[tuple[tuple[str, tuple], ...], list[Hypothesis]] = {}
    for h in survivors:
        grouped.setdefault(structural_signature(h, f_usable), []).append(h)

    classes = tuple(
        EquivalenceClass(
            signature=sig,
            members=(members := tuple(sorted(hs, key=lambda h: h.id))),
            d_missing=_d_missing(members, f_usable_set),
        )
        # sort by repr: a signature value may contain None (an unpredicted but hard-ruled coordinate),
        # which is not orderable against a str, but its repr is a stable deterministic key.
        for sig, hs in sorted(grouped.items(), key=lambda kv: repr(kv[0]))
    )
    # Sort eliminated by (now-unique) id too, so the entire Partition is permutation-invariant.
    eliminated_sorted = tuple(sorted(eliminated, key=lambda h: h.id))
    return Partition(classes=classes, f_usable=f_usable, eliminated=eliminated_sorted)


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
