"""Class scoring — the ranker as one signal, never a membership decider (#185, Phase G of #177).

Phase D (:mod:`~src.core.rca.partition`) decides **class membership** from the observation model
(categorical prediction signature over ``F_usable``), invariant under any ranking score (Invariant 6).
Phase E (:mod:`~src.core.rca.outcome`) labels the partition. This phase adds the one thing those two
deliberately withhold: an **ordering over the surviving classes** for presentation. It is a small,
explicit, discrete evidence algebra — *not* a calibrated probability — and it can never move the
partition:

    relation              score
    hard contradiction    eliminate   (already applied in Phase D; never re-decided here)
    strong support        +3
    support               +2
    weak support          +1
    UNKNOWN / unobserved   0           (Invariant 1: a missing measurement is neutral)
    not-required mismatch  0
    weak contradiction    -1
    soft contradiction    -2

The relation for a soft :class:`~src.core.rca.expectations.ExpectedObservation` is fixed by its
``Strength`` and whether the **usable** observed state matches the prediction (see ``_MATCH`` /
``_MISMATCH``). A hypothesis's score is the sum over its expectations; a class's score is
``max`` over its members (``score([C]_O) = max_{C_i ∈ [C]_O} score(C_i)`` — an explicitly simple
aggregation, chosen so a class is as strong as its best-supported member).

The existing multimodal RCA ranker plugs in as **one additional class-ranking signal only**
(``ranker_signal`` = the max provenance ``RootCauseCandidate.score`` among a class's members). It is a
*secondary* ordering key: it breaks ties between classes of equal discrete score and never promotes a
lower-scored class above a higher-scored one, so the ranker informs presentation without ever deciding
membership (its acceptance criterion) or masquerading as a probability.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from src.core.rca.expectations import Strength
from src.core.rca.hypothesis import Hypothesis
from src.core.rca.observable import Observable, usable_observables
from src.core.rca.partition import EquivalenceClass, Partition


class Support(IntEnum):
    """The discrete evidence levels (#185). Integer weights, deliberately **not** probabilities — they
    sum and can be negative or exceed 1; nothing here normalizes them to ``[0, 1]``."""

    STRONG_SUPPORT = 3
    SUPPORT = 2
    WEAK_SUPPORT = 1
    NEUTRAL = 0
    WEAK_CONTRADICTION = -1
    SOFT_CONTRADICTION = -2


# A soft expectation's relation is fixed by its strength and whether the observed state matched. The
# penalty side is deliberately milder than the support side: not having (or mismatching) a soft symptom
# is weak evidence, consistent with "you cannot refute a cause by a missing symptom" (Invariant 3;
# hard elimination is the ONLY strong negative, and it is Phase D's job, not this algebra's).
_MATCH: Mapping[Strength, Support] = {
    Strength.USUALLY: Support.STRONG_SUPPORT,
    Strength.OFTEN: Support.SUPPORT,
    Strength.MAYBE: Support.WEAK_SUPPORT,
    Strength.NOT_REQUIRED: Support.NEUTRAL,
}
_MISMATCH: Mapping[Strength, Support] = {
    Strength.USUALLY: Support.SOFT_CONTRADICTION,
    Strength.OFTEN: Support.WEAK_CONTRADICTION,
    Strength.MAYBE: Support.WEAK_CONTRADICTION,
    Strength.NOT_REQUIRED: Support.NEUTRAL,  # a not-required mismatch is inert by construction
}


def _usable_states(observations: Iterable[Observable]) -> dict[str, str]:
    """``{id: state}`` for the usable (collectable ∧ OBSERVED) observations only — the same gate the
    observation model uses, so UNKNOWN / uncollectable coordinates never enter scoring (Invariant 1)."""
    return {o.id: o.state for o in usable_observables(list(observations)) if o.state is not None}


def _score_from_states(hypothesis: Hypothesis, observed: Mapping[str, str]) -> int:
    """Discrete evidence score of one hypothesis given the already-usable observed states. An
    unobserved expectation is neutral (never a penalty); a hypothesis with no observation model
    scores 0. Reads no ranker score."""
    model = hypothesis.observation_model
    if model is None:
        return 0
    total = 0
    for e in model.expected:
        state = observed.get(e.observable_id)
        if state is None:
            continue  # not measured this incident → neutral (Invariant 1)
        total += int(_MATCH[e.strength] if state == e.expected_state else _MISMATCH[e.strength])
    return total


def score_hypothesis(hypothesis: Hypothesis, observations: Iterable[Observable]) -> int:
    """Discrete evidence score of ``hypothesis`` over ``observations`` (usable ones only). Pure, no DB,
    no ranker score — the soft-evidence algebra of #185."""
    return _score_from_states(hypothesis, _usable_states(observations))


def score_class(members: Iterable[Hypothesis], observations: Iterable[Observable]) -> int:
    """Class score = ``max`` over member scores (a class is as strong as its best-supported member).
    Raises on an empty class — a class always has ≥1 member."""
    observed = _usable_states(observations)
    scores = [_score_from_states(h, observed) for h in members]
    if not scores:
        raise ValueError("score_class requires at least one member")
    return max(scores)


def _ranker_signal(members: Iterable[Hypothesis]) -> Optional[float]:
    """The class's ranker-ranking signal: the max provenance ``RootCauseCandidate.score`` among the
    members, or ``None`` when no member carries ranker provenance. A *secondary* ordering signal only —
    it never enters the discrete score and never decides membership."""
    scores = [h.source.score for h in members if h.source is not None]
    return max(scores) if scores else None


@dataclass(frozen=True)
class ScoredClass:
    """A surviving equivalence class with its discrete evidence ``score`` and optional secondary
    ``ranker_signal``, plus the per-member breakdown so Phase I can show *why*. ``score`` is a discrete
    evidence sum (Phase G algebra), **not** a probability."""

    hypothesis_ids: tuple[str, ...]
    localizations: tuple[str, ...]
    signature: tuple[tuple[str, str], ...]
    score: int
    ranker_signal: Optional[float]
    member_scores: tuple[tuple[str, int], ...]  # (hypothesis_id, score), member order preserved

    def __post_init__(self) -> None:
        object.__setattr__(self, "hypothesis_ids", tuple(self.hypothesis_ids))
        object.__setattr__(self, "localizations", tuple(self.localizations))
        object.__setattr__(self, "signature", tuple(tuple(p) for p in self.signature))
        object.__setattr__(self, "member_scores", tuple((str(i), int(s)) for i, s in self.member_scores))


def _scored_class(cls: EquivalenceClass, observed: Mapping[str, str], *, use_ranker: bool) -> ScoredClass:
    member_scores = tuple((h.id, _score_from_states(h, observed)) for h in cls.members)
    return ScoredClass(
        hypothesis_ids=tuple(h.id for h in cls.members),
        localizations=tuple(dict.fromkeys(h.localization for h in cls.members)),
        signature=cls.signature,
        score=max(s for _, s in member_scores),
        ranker_signal=_ranker_signal(cls.members) if use_ranker else None,
        member_scores=member_scores,
    )


def rank_classes(partition: Partition, *, use_ranker: bool = True) -> tuple[ScoredClass, ...]:
    """Score and order the partition's surviving classes for presentation, highest score first.

    Scoring uses the partition's **own** authoritative usable snapshot
    (:attr:`~src.core.rca.partition.Partition.usable_states`), so a caller cannot rewrite the facts
    behind the score, exactly as :func:`~src.core.rca.outcome.resolve` does for evidence. Ordering is
    deterministic: primary discrete ``score`` desc, then ``ranker_signal`` desc (a secondary tie-break
    that can never lift a lower-scored class above a higher-scored one — ``None`` sorts last), then the
    class ``signature`` for a stable total order.

    Invariant 6 preserved: this reads the partition read-only and never re-partitions — the returned
    classes are exactly ``partition.classes`` reordered, with membership untouched."""
    observed = partition.usable_states
    scored = [_scored_class(c, observed, use_ranker=use_ranker) for c in partition.classes]
    scored.sort(key=lambda s: (
        -s.score,
        -(s.ranker_signal if s.ranker_signal is not None else float("-inf")),
        s.signature,
    ))
    return tuple(scored)
