"""Structural outcome — the deterministic label + evidence packet (#183, Phase E of the #177 epic).

Phase D (#182) produces a :class:`~src.core.rca.partition.Partition` — a prediction-only equivalence
partition that assigns *no* outcome. Phase E turns that partition into one of five outcomes and a
deterministic evidence packet. The mapping is a **pure, total function of the partition's class
cardinality** — never of ranker scores, and with no ``ε`` threshold anywhere (see
``docs``/#183 and the epic amendment on #177):

    | classes | shape                                    | outcome                   |
    |---------|------------------------------------------|---------------------------|
    | 0       | (all hard-eliminated)                    | NO_COMPATIBLE_HYPOTHESIS  |
    | 1       | singleton                                | IDENTIFIED                |
    | 1       | ≥2 members, D_missing ≠ ∅                 | NON_IDENTIFIABLE          |
    | 1       | ≥2 members, D_missing = ∅                 | IRREDUCIBLE               |
    | >1      | any                                      | UNCERTAIN                 |

``NON_IDENTIFIABLE`` says *collect these observables*; ``IRREDUCIBLE`` says *extend the model* — the
members share a prediction signature and no modeled **prediction** distinguisher exists outside
``F_usable``. Because ``~_O`` is prediction-only, a hard-contradiction rule could still eliminate a
member under some observation; those are surfaced as ``potential_elimination_checks`` in the packet,
never folded into ``~_O`` or ``D_missing``. ``NO_COMPATIBLE_HYPOTHESIS`` never claims a localization.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.core.rca.observable import Observable, integration_gaps
from src.core.rca.partition import EquivalenceClass, Partition


class Outcome(str, Enum):
    """The five structural outcomes (#183). A closed set: the total function over a partition's class
    cardinality maps to exactly one of these."""

    NO_COMPATIBLE_HYPOTHESIS = "no_compatible_hypothesis"  # 0 classes — every hypothesis eliminated
    IDENTIFIED = "identified"                              # 1 class, 1 member
    NON_IDENTIFIABLE = "non_identifiable"                  # 1 class, ≥2 members, D_missing ≠ ∅
    IRREDUCIBLE = "irreducible"                            # 1 class, ≥2 members, D_missing = ∅
    UNCERTAIN = "uncertain"                                # >1 classes


def classify(partition: Partition) -> Outcome:
    """The deterministic outcome of a partition — a pure, total function of class cardinality (and,
    for a single multi-member class, whether ``D_missing`` is empty). Reads no scores; has no
    ``ε`` threshold. Total: every partition Phase D can build maps to exactly one outcome."""
    if partition.no_surviving_hypothesis:
        return Outcome.NO_COMPATIBLE_HYPOTHESIS
    if len(partition.classes) > 1:
        return Outcome.UNCERTAIN
    (cls,) = partition.classes
    if cls.is_singleton:
        return Outcome.IDENTIFIED
    return Outcome.NON_IDENTIFIABLE if cls.d_missing else Outcome.IRREDUCIBLE


class Relation:
    """How an observed fact relates to a hypothesis's expectation (string constants, extensible)."""

    SUPPORTS = "supports"                # observed state matches the predicted state
    MISMATCH = "mismatch"                # observed state differs (soft — does not eliminate)
    HARD_ELIMINATES = "hard_eliminates"  # observed state satisfies a hard-contradiction rule


@dataclass(frozen=True)
class EvidenceItem:
    """One deterministic, observed fact and how it bears on a hypothesis/class: the usable
    ``observable_id`` and its ``observed_state``, the ``expected_state`` the hypothesis predicted (or
    ``None`` when the relation is a hard elimination), and the ``relation``. This is what Phase I / the
    LLM cites — no invented evidence, no ranker score."""

    observable_id: str
    observed_state: str
    expected_state: Optional[str]
    relation: str


@dataclass(frozen=True)
class MemberView:
    """A class/eliminated member, keeping id, kind, and user-facing localization **associated** (never
    flattened into parallel arrays that lose the mapping when localizations repeat)."""

    id: str
    kind: str
    localization: str


@dataclass(frozen=True)
class EliminationCheck:
    """A hard-rule discriminator surfaced separately from ``~_O`` and ``D_missing``: observing
    ``observable_id`` in one of ``eliminating_states`` would hard-eliminate ``hypothesis_id``. Only
    checks on coordinates **not** in ``F_usable`` are reported (a usable one would already have been
    applied during filtering) — i.e. *what to collect to rule this hypothesis out*."""

    hypothesis_id: str
    observable_id: str
    eliminating_states: tuple[str, ...]


@dataclass(frozen=True)
class ClassView:
    """A surviving equivalence class, projected for presentation: its ``members`` (structured views),
    the prediction ``signature`` that defines the class, the observed ``evidence`` that produced it,
    the class's ``d_missing`` (prediction distinguishers not collected), and its per-member
    ``elimination_checks``. ``is_irreducible`` marks a multi-member class with empty ``d_missing``
    (legal even inside an ``UNCERTAIN`` partition)."""

    members: tuple[MemberView, ...]
    signature: tuple[tuple[str, str], ...]
    d_missing: frozenset[str]
    evidence: tuple[EvidenceItem, ...]
    elimination_checks: tuple[EliminationCheck, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "members", tuple(self.members))
        object.__setattr__(self, "signature", tuple(tuple(pair) for pair in self.signature))
        object.__setattr__(self, "d_missing", frozenset(self.d_missing))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "elimination_checks", tuple(self.elimination_checks))
        if not self.members:
            raise ValueError("ClassView must describe at least one hypothesis")

    @property
    def hypothesis_ids(self) -> tuple[str, ...]:
        return tuple(m.id for m in self.members)

    @property
    def localizations(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(m.localization for m in self.members))  # distinct, ordered

    @property
    def is_singleton(self) -> bool:
        return len(self.members) == 1

    @property
    def is_irreducible(self) -> bool:
        return len(self.members) > 1 and not self.d_missing


@dataclass(frozen=True)
class EliminatedView:
    """A hard-contradicted hypothesis and the usable observation(s) that eliminated it — so the
    ``NO_COMPATIBLE_HYPOTHESIS`` packet can cite *why* each candidate was ruled out."""

    member: MemberView
    evidence: tuple[EvidenceItem, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", tuple(self.evidence))


@dataclass(frozen=True)
class StructuralResult:
    """The deterministic evidence packet (#183). ``localization`` is the distinct surviving-member
    localizations (empty for ``NO_COMPATIBLE_HYPOTHESIS`` — it never claims one). Each ``ClassView``
    carries its defining signature and the observed evidence that produced it; ``d_missing`` and
    ``potential_elimination_checks`` are the union over surviving classes; ``eliminated`` carries each
    ruled-out hypothesis with its eliminating observation; ``integration_gaps`` are uncollectable
    observables. Ranking/soft-scoring is deliberately absent — that is Phase G."""

    outcome: Outcome
    localization: tuple[str, ...]
    classes: tuple[ClassView, ...]
    d_missing: frozenset[str]
    potential_elimination_checks: tuple[EliminationCheck, ...]
    eliminated: tuple[EliminatedView, ...] = ()
    integration_gaps: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "localization", tuple(self.localization))
        object.__setattr__(self, "classes", tuple(self.classes))
        object.__setattr__(self, "d_missing", frozenset(self.d_missing))
        object.__setattr__(self, "potential_elimination_checks", tuple(self.potential_elimination_checks))
        object.__setattr__(self, "eliminated", tuple(self.eliminated))
        object.__setattr__(self, "integration_gaps", tuple(self.integration_gaps))
        if self.outcome == Outcome.NO_COMPATIBLE_HYPOTHESIS and self.localization:
            raise ValueError("NO_COMPATIBLE_HYPOTHESIS must not claim a localization")


def _member_view(h) -> MemberView:
    return MemberView(id=h.id, kind=h.kind.value, localization=h.localization)


def _observed_states(observations: list[Observable], f_usable: frozenset[str]) -> dict[str, str]:
    """The observed state of each usable coordinate (from the same observations the partition used)."""
    return {o.id: o.state for o in observations
            if o.id in f_usable and o.state is not None}


def _elimination_checks(cls: EquivalenceClass, f_usable: frozenset[str]) -> tuple[EliminationCheck, ...]:
    checks: list[EliminationCheck] = []
    for h in cls.members:
        model = h.observation_model
        if model is None:
            continue
        for c in model.contradictions:
            if c.observable_id not in f_usable:  # a usable one would already have been applied
                checks.append(EliminationCheck(h.id, c.observable_id, tuple(sorted(c.states))))
    return tuple(sorted(checks, key=lambda e: (e.hypothesis_id, e.observable_id)))


def _class_evidence(cls: EquivalenceClass, observed: dict[str, str]) -> tuple[EvidenceItem, ...]:
    """Supporting evidence for a class: over the class's (shared) prediction signature, each usable
    coordinate's observed state and whether it supports or (softly) mismatches the prediction."""
    items: list[EvidenceItem] = []
    for f, predicted in cls.signature:
        obs = observed.get(f)
        if obs is None:
            continue
        relation = Relation.SUPPORTS if obs == predicted else Relation.MISMATCH
        items.append(EvidenceItem(f, obs, predicted, relation))
    return tuple(items)


def _eliminated_view(h, observed: dict[str, str]) -> EliminatedView:
    items: list[EvidenceItem] = []
    model = h.observation_model
    if model is not None:
        for c in model.contradictions:
            st = observed.get(c.observable_id)
            if st is not None and st in c.states:  # the usable observation that eliminated it
                items.append(EvidenceItem(c.observable_id, st, None, Relation.HARD_ELIMINATES))
    return EliminatedView(member=_member_view(h),
                          evidence=tuple(sorted(items, key=lambda e: e.observable_id)))


def _class_view(cls: EquivalenceClass, f_usable: frozenset[str], observed: dict[str, str]) -> ClassView:
    return ClassView(
        members=tuple(_member_view(h) for h in cls.members),
        signature=cls.signature,
        d_missing=cls.d_missing,
        evidence=_class_evidence(cls, observed),
        elimination_checks=_elimination_checks(cls, f_usable),
    )


def resolve(partition: Partition, observations: Iterable[Observable] = ()) -> StructuralResult:
    """Assemble the deterministic evidence packet for ``partition``. Pass the **same observations** the
    partition was built from: they supply the observed states behind each class's supporting evidence,
    the eliminating fact for each ruled-out hypothesis, and the ``integration_gaps``. They never affect
    the outcome, which is a pure function of the partition."""
    observations = list(observations)
    outcome = classify(partition)
    f_usable = frozenset(partition.f_usable)
    observed = _observed_states(observations, f_usable)
    views = tuple(_class_view(c, f_usable, observed) for c in partition.classes)

    localization: tuple[str, ...] = tuple(
        dict.fromkeys(m.localization for v in views for m in v.members)
    )
    d_missing = frozenset().union(*(v.d_missing for v in views)) if views else frozenset()
    checks = tuple(sorted(
        {chk for v in views for chk in v.elimination_checks},
        key=lambda e: (e.hypothesis_id, e.observable_id),
    ))
    return StructuralResult(
        outcome=outcome,
        localization=localization,
        classes=views,
        d_missing=d_missing,
        potential_elimination_checks=checks,
        eliminated=tuple(_eliminated_view(h, observed) for h in partition.eliminated),
        integration_gaps=tuple(integration_gaps(observations)),
    )
