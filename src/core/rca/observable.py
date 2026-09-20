"""Availability-aware observables (#178, Phase A of the #177 causal-inference epic).

The foundation the epic builds on: make *unavailable telemetry* representable **separately**
from *observed absence*, so a whole class of bugs — treating "we never measured it" the same as
"we measured it and it was fine" — becomes impossible by construction rather than by discipline.

Three states must never collapse:

- ``collectable=False``                       — no integration here can ever produce this evidence;
- ``collectable=True, availability=UNKNOWN``  — it could exist, but no trustworthy measurement was
                                                available for this incident;
- ``availability=OBSERVED, state=ABSENT``     — measured, and the absence is itself evidence.

This module introduces only the model and the invariant-preserving primitives that later phases
(C observation expectations, D structural partitioning, G class scoring) consume. **No ranking
behaviour changes and nothing in the explain path calls this yet** — the milestone is the two
invariants below, enforced by unit tests, not any accuracy change.

The observable **id** is the coordinate identity: a set of observables carries at most one entry
per id (:func:`_by_id` enforces this), so evidence and signatures cannot be corrupted by duplicate
or contradictory coordinates.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Availability(str, Enum):
    """Was a trustworthy measurement of this observable available for this incident? (Closed set.)"""

    UNKNOWN = "unknown"   # not measured / not trustworthy — carries no incident evidence
    OBSERVED = "observed"  # measured — its state (including ABSENT) is evidence


class State:
    """Categorical observable states. **Open-ended by design**: ``state`` is a plain ``str`` so a
    deployment may use its own values (e.g. ``"saturated"``); these named constants are just the
    common ones. Equality of the string is all the model relies on, and any string round-trips."""

    PRESENT = "present"
    ABSENT = "absent"
    HIGH = "high"
    LOW = "low"
    NORMAL = "normal"


@dataclass(frozen=True)
class Observable:
    """One availability-aware observable coordinate.

    The two axes are independent: ``collectable`` is a *deployment* property (can this integration
    ever emit it), ``availability`` is a *per-incident* property (did we get a trustworthy
    measurement this time). ``state`` (a free-form str) is meaningful only when
    ``availability == OBSERVED``; for an UNKNOWN observable it is ``None``.

    The *expected normal* baseline (#178) comes in two honest forms rather than one guess-the-type
    field: ``baseline_state`` for a categorical baseline (e.g. ``NORMAL``) and ``baseline_value`` for
    a numeric one (e.g. ``0.35`` for a CPU-utilization observable whose ``value`` is ``0.92``).
    Either, both, or neither may be set; consumers never have to sniff which representation they got.

    Contradictions are rejected at construction so no downstream code has to defend against them.
    """

    id: str
    collectable: bool = True
    availability: Availability = Availability.UNKNOWN
    state: Optional[str] = None
    value: Optional[float] = None
    baseline_state: Optional[str] = None
    baseline_value: Optional[float] = None
    measurement_confidence: float = 1.0

    def __post_init__(self) -> None:
        # Normalize + validate the *runtime* representation, not just the annotations: this is a
        # frozen dataclass with no static type gate, so `from_dict` (or any dynamic caller) can pass
        # a raw string or a non-string state, and downstream phases must be able to trust the object.
        if not isinstance(self.id, str) or not self.id:
            raise ValueError(f"observable id must be a non-empty string, got {self.id!r}")
        if not isinstance(self.collectable, bool):
            raise ValueError(f"observable {self.id!r}: collectable must be a bool, got {self.collectable!r}")
        # Coerce a raw availability (e.g. the string from from_dict) into the closed enum, or fail.
        if not isinstance(self.availability, Availability):
            try:
                object.__setattr__(self, "availability", Availability(self.availability))
            except ValueError:
                raise ValueError(
                    f"observable {self.id!r}: availability must be one of "
                    f"{[a.value for a in Availability]}, got {self.availability!r}"
                ) from None
        for attr in ("state", "baseline_state"):
            v = getattr(self, attr)
            if v is not None and not isinstance(v, str):
                raise ValueError(f"observable {self.id!r}: {attr} must be a string or None, got {v!r}")
        for attr in ("value", "baseline_value"):
            v = getattr(self, attr)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                raise ValueError(f"observable {self.id!r}: {attr} must be a number or None, got {v!r}")

        if self.availability == Availability.OBSERVED and self.state is None:
            raise ValueError(f"observable {self.id!r}: OBSERVED requires a concrete state")
        if self.availability == Availability.UNKNOWN and self.state is not None:
            raise ValueError(
                f"observable {self.id!r}: UNKNOWN must not carry a state "
                "(that would collapse 'not measured' into an observed value)"
            )
        if not self.collectable and self.availability == Availability.OBSERVED:
            raise ValueError(
                f"observable {self.id!r}: collectable=False cannot be OBSERVED — an integration "
                "that can never produce this evidence cannot have measured it"
            )
        c = self.measurement_confidence
        if isinstance(c, bool) or not isinstance(c, (int, float)) or math.isnan(c) or math.isinf(c) or not (0.0 <= c <= 1.0):
            raise ValueError(
                f"observable {self.id!r}: measurement_confidence must be a finite number in [0, 1], "
                f"got {c!r}"
            )

    @property
    def is_usable(self) -> bool:
        """Usable = collectable here AND actually measured this incident. Only usable observables
        carry incident evidence, define usable IDs, or act as distinguishers. UNKNOWN and
        uncollectable observables are *not* usable (Invariants 1 and 2)."""
        return self.collectable and self.availability == Availability.OBSERVED

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collectable": self.collectable,
            "availability": self.availability.value,
            "state": self.state,
            "value": self.value,
            "baseline_state": self.baseline_state,
            "baseline_value": self.baseline_value,
            "measurement_confidence": self.measurement_confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Observable":
        return cls(
            id=d["id"],
            collectable=d.get("collectable", True),
            availability=d.get("availability", "unknown"),
            state=d.get("state"),
            value=d.get("value"),
            baseline_state=d.get("baseline_state"),
            baseline_value=d.get("baseline_value"),
            measurement_confidence=d.get("measurement_confidence", 1.0),
        )


# Convenience constructors that make the three non-collapsing states impossible to mix up.
def unknown(id: str, *, measurement_confidence: float = 1.0) -> Observable:
    """collectable-but-not-measured (or not trustworthy) this incident. Always collectable — the
    *uncollectable* state has its own constructor (:func:`uncollectable`); this one cannot manufacture
    it, so the three states stay impossible to mix up."""
    return Observable(id=id, collectable=True, availability=Availability.UNKNOWN,
                      measurement_confidence=measurement_confidence)


def observed(id: str, state: str, *, value: Optional[float] = None,
             baseline_state: Optional[str] = None, baseline_value: Optional[float] = None,
             measurement_confidence: float = 1.0) -> Observable:
    """measured this incident; ``state`` (including ABSENT) is evidence. ``baseline_state`` /
    ``baseline_value`` carry the expected-normal categorical / numeric baseline (#178)."""
    return Observable(id=id, collectable=True, availability=Availability.OBSERVED, state=state,
                      value=value, baseline_state=baseline_state, baseline_value=baseline_value,
                      measurement_confidence=measurement_confidence)


def uncollectable(id: str) -> Observable:
    """no integration here can ever produce this evidence."""
    return Observable(id=id, collectable=False, availability=Availability.UNKNOWN)


def _by_id(observables: Iterable[Observable]) -> dict[str, Observable]:
    """Index observables by their coordinate id, rejecting duplicates. A set of observables carries
    at most one entry per id — two entries for the same id (identical or, worse, contradictory like
    a=PRESENT and a=ABSENT) would corrupt evidence and signatures, so it is an error, not silently
    summed."""
    by_id: dict[str, Observable] = {}
    for o in observables:
        if o.id in by_id:
            raise ValueError(f"duplicate observable id {o.id!r}: a coordinate must appear at most once")
        by_id[o.id] = o
    return by_id


def usable_observables(observables: Iterable[Observable]) -> list[Observable]:
    """The observables that carry incident evidence: collectable AND OBSERVED. Excludes both
    UNKNOWN (Invariant 1) and uncollectable (Invariant 2). Rejects duplicate ids."""
    return [o for o in _by_id(observables).values() if o.is_usable]


def usable_ids(observables: Iterable[Observable]) -> tuple[str, ...]:
    """``F_usable`` — the sorted ids of usable observables. This is the domain a hypothesis
    signature is projected over; uncollectable and UNKNOWN ids never appear here (Invariant 2)."""
    return tuple(sorted(o.id for o in usable_observables(observables)))


def hypothesis_signature(
    prediction: Mapping[str, str], observables: Iterable[Observable]
) -> tuple[tuple[str, str], ...]:
    """``S_O(C) = {(f, prediction(C, f)) : f in F_usable}`` — the hypothesis's *predicted* state at
    each usable observable id, sorted by id. The state in the signature is the **prediction**, not
    the incident observation; ids the hypothesis does not predict are omitted. Uncollectable and
    UNKNOWN ids are excluded because they are not in ``F_usable`` (Invariant 2), so a signature can
    never depend on evidence this deployment cannot produce or did not measure. (This is the Phase D
    contract; Phase A provides the primitive — real predictions come from Phases B/C.)"""
    ids = usable_ids(observables)
    return tuple((f, prediction[f]) for f in ids if f in prediction)


def evidence_score(expectations: Mapping[str, str], observables: Iterable[Observable]) -> float:
    """A minimal, monotone evidence score for a hypothesis given its expected states per observable
    id: the confidence-weighted count of **usable** observables whose measured state matches the
    expectation. UNKNOWN and uncollectable observables contribute nothing (Invariants 1 and 2), and
    ``measurement_confidence`` is validated to ``[0, 1]`` at construction, so the score is monotone
    and bounded. (Phase A only: not wired into the ranker; no accuracy claim.)"""
    total = 0.0
    for o in usable_observables(observables):
        expected = expectations.get(o.id)
        if expected is not None and o.state == expected:
            total += o.measurement_confidence
    return total


def integration_gaps(observables: Iterable[Observable]) -> list[str]:
    """Ids of uncollectable observables — never incident evidence, but the basis for an
    *integration-gap* recommendation ("wire up this signal to disambiguate future incidents")."""
    return sorted(o.id for o in _by_id(observables).values() if not o.collectable)


@dataclass(frozen=True)
class ObservableSet:
    """A set of observables keyed by id (at most one entry per coordinate). It **owns** its
    representation: the input is defensively copied into an immutable tuple and the dataclass is
    frozen, so the one-coordinate-per-id invariant checked at construction holds for the object's
    whole lifetime — a caller mutating the list it passed in, appending to ``.observables``, or
    reassigning the field cannot make a constructed set contradict itself. The methods delegate to
    the invariant-preserving free functions above."""

    observables: tuple[Observable, ...] = ()

    def __post_init__(self) -> None:
        # Defensive copy into an immutable tuple: severs any alias to the caller's mutable input.
        object.__setattr__(self, "observables", tuple(self.observables))
        _by_id(self.observables)  # reject duplicate coordinate ids up front

    def usable(self) -> list[Observable]:
        return usable_observables(self.observables)

    def usable_ids(self) -> tuple[str, ...]:
        return usable_ids(self.observables)

    def signature(self, prediction: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
        return hypothesis_signature(prediction, self.observables)

    def score(self, expectations: Mapping[str, str]) -> float:
        return evidence_score(expectations, self.observables)

    def integration_gaps(self) -> list[str]:
        return integration_gaps(self.observables)
