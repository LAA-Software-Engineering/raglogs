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
    """A surviving equivalence class, projected for presentation: the member ids/kinds/localizations,
    the class's ``d_missing`` (prediction distinguishers not collected), and its per-member
    ``elimination_checks``. ``is_irreducible`` marks a multi-member class with empty ``d_missing``
    (legal even inside an ``UNCERTAIN`` partition)."""

    hypothesis_ids: tuple[str, ...]
    kinds: tuple[str, ...]
    localizations: tuple[str, ...]
    d_missing: frozenset[str]
    elimination_checks: tuple[EliminationCheck, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis_ids", tuple(self.hypothesis_ids))
        object.__setattr__(self, "kinds", tuple(self.kinds))
        object.__setattr__(self, "localizations", tuple(self.localizations))
        object.__setattr__(self, "d_missing", frozenset(self.d_missing))
        object.__setattr__(self, "elimination_checks", tuple(self.elimination_checks))
        if not self.hypothesis_ids:
            raise ValueError("ClassView must describe at least one hypothesis")

    @property
    def is_singleton(self) -> bool:
        return len(self.hypothesis_ids) == 1

    @property
    def is_irreducible(self) -> bool:
        return len(self.hypothesis_ids) > 1 and not self.d_missing


@dataclass(frozen=True)
class StructuralResult:
    """The deterministic evidence packet (#183). ``localization`` is the surviving classes' member
    localizations (empty for ``NO_COMPATIBLE_HYPOTHESIS`` — it never claims one). ``d_missing`` and
    ``potential_elimination_checks`` are the union over surviving classes; ``eliminated`` are the
    hard-contradicted hypothesis ids; ``integration_gaps`` are uncollectable observables (only when
    observations were supplied). Ranking/soft-scoring is deliberately absent — that is Phase G."""

    outcome: Outcome
    localization: tuple[str, ...]
    classes: tuple[ClassView, ...]
    d_missing: frozenset[str]
    potential_elimination_checks: tuple[EliminationCheck, ...]
    eliminated: tuple[str, ...] = ()
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


def _class_view(cls: EquivalenceClass, f_usable: frozenset[str]) -> ClassView:
    return ClassView(
        hypothesis_ids=tuple(h.id for h in cls.members),
        kinds=tuple(h.kind.value for h in cls.members),
        localizations=tuple(dict.fromkeys(h.localization for h in cls.members)),  # distinct, ordered
        d_missing=cls.d_missing,
        elimination_checks=_elimination_checks(cls, f_usable),
    )


def resolve(partition: Partition, observations: Iterable[Observable] = ()) -> StructuralResult:
    """Assemble the deterministic evidence packet for ``partition``. ``observations`` is optional and
    used only to derive ``integration_gaps`` (uncollectable coordinates — a wire-this-up
    recommendation); it never affects the outcome, which is a pure function of the partition."""
    outcome = classify(partition)
    f_usable = frozenset(partition.f_usable)
    views = tuple(_class_view(c, f_usable) for c in partition.classes)

    localization: tuple[str, ...] = tuple(
        dict.fromkeys(loc for v in views for loc in v.localizations)
    )
    d_missing = frozenset().union(*(v.d_missing for v in views)) if views else frozenset()
    checks = tuple(sorted(
        {chk for v in views for chk in v.elimination_checks},
        key=lambda e: (e.hypothesis_id, e.observable_id),
    ))
    gaps = tuple(integration_gaps(list(observations)))
    return StructuralResult(
        outcome=outcome,
        localization=localization,
        classes=views,
        d_missing=d_missing,
        potential_elimination_checks=checks,
        eliminated=tuple(h.id for h in partition.eliminated),
        integration_gaps=gaps,
    )
