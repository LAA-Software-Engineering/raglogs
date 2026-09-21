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
        # The packet cannot contradict its own outcome algebra: the outcome must match the class
        # structure, and the top-level summaries must be exactly their class-derived values, so a
        # downstream consumer can trust the object without re-deriving the inference.
        if self.outcome != _expected_outcome(self.classes):
            raise ValueError(
                f"outcome {self.outcome.value!r} does not match the class structure "
                f"(expected {_expected_outcome(self.classes).value!r})"
            )
        if self.localization != _summary_localization(self.classes):
            raise ValueError("localization must be the distinct member localizations of the classes")
        if self.d_missing != _union_d_missing(self.classes):
            raise ValueError("d_missing must be the union of the classes' d_missing")
        if self.potential_elimination_checks != _union_checks(self.classes):
            raise ValueError("potential_elimination_checks must be the union of the classes' checks")
        if self.outcome == Outcome.NO_COMPATIBLE_HYPOTHESIS:
            if not self.eliminated or any(not e.evidence for e in self.eliminated):
                raise ValueError(
                    "NO_COMPATIBLE_HYPOTHESIS requires eliminated hypotheses, each with the "
                    "observation that eliminated it"
                )


def _expected_outcome(classes: tuple[ClassView, ...]) -> Outcome:
    if not classes:
        return Outcome.NO_COMPATIBLE_HYPOTHESIS
    if len(classes) > 1:
        return Outcome.UNCERTAIN
    (cls,) = classes
    if cls.is_singleton:
        return Outcome.IDENTIFIED
    return Outcome.NON_IDENTIFIABLE if cls.d_missing else Outcome.IRREDUCIBLE


def _summary_localization(classes: tuple[ClassView, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(m.localization for v in classes for m in v.members))


def _union_d_missing(classes: tuple[ClassView, ...]) -> frozenset[str]:
    return frozenset().union(*(v.d_missing for v in classes)) if classes else frozenset()


def _union_checks(classes: tuple[ClassView, ...]) -> tuple[EliminationCheck, ...]:
    return tuple(sorted(
        {chk for v in classes for chk in v.elimination_checks},
        key=lambda e: (e.hypothesis_id, e.observable_id),
    ))


def _member_view(h) -> MemberView:
    return MemberView(id=h.id, kind=h.kind.value, localization=h.localization)


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
    """Assemble the deterministic evidence packet for ``partition``. The supporting evidence and each
    elimination fact are derived from the partition's **own** authoritative usable snapshot
    (:attr:`~src.core.rca.partition.Partition.usable_states`), so a caller cannot rewrite the facts
    behind the partition. ``observations`` is optional and used **only** for ``integration_gaps``
    (uncollectable coordinates — a wire-this-up recommendation); it never affects the outcome or the
    evidence."""
    outcome = classify(partition)
    f_usable = frozenset(partition.f_usable)
    observed = partition.usable_states  # authoritative — the facts that produced this partition
    views = tuple(_class_view(c, f_usable, observed) for c in partition.classes)
    return StructuralResult(
        outcome=outcome,
        localization=_summary_localization(views),
        classes=views,
        d_missing=_union_d_missing(views),
        potential_elimination_checks=_union_checks(views),
        eliminated=tuple(_eliminated_view(h, observed) for h in partition.eliminated),
        integration_gaps=tuple(integration_gaps(list(observations))),
    )
