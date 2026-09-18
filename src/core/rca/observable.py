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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Availability(str, Enum):
    """Was a trustworthy measurement of this observable available for this incident?"""

    UNKNOWN = "unknown"   # not measured / not trustworthy — carries no incident evidence
    OBSERVED = "observed"  # measured — its state (including ABSENT) is evidence


class State(str, Enum):
    """The measured (or expected) categorical state. Open-ended by design; the common values are
    named, and callers may add deployment-specific ones — equality is what the model relies on."""

    PRESENT = "present"
    ABSENT = "absent"
    HIGH = "high"
    LOW = "low"
    NORMAL = "normal"


@dataclass(frozen=True)
class Observable:
    """One availability-aware observable coordinate.

    The two axes are independent and must stay independent: ``collectable`` is a *deployment*
    property (can this integration ever emit it), ``availability`` is a *per-incident* property
    (did we get a trustworthy measurement this time). ``state`` is only meaningful when
    ``availability == OBSERVED``; for an UNKNOWN observable it is ``None``.
    """

    id: str
    collectable: bool = True
    availability: Availability = Availability.UNKNOWN
    state: Optional[State] = None
    value: Optional[float] = None
    baseline: Optional[State] = None
    measurement_confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.availability == Availability.OBSERVED and self.state is None:
            raise ValueError(f"observable {self.id!r}: OBSERVED requires a concrete state")
        if self.availability == Availability.UNKNOWN and self.state is not None:
            raise ValueError(
                f"observable {self.id!r}: UNKNOWN must not carry a state "
                "(that would collapse 'not measured' into an observed value)"
            )

    @property
    def is_usable(self) -> bool:
        """Usable = collectable here AND actually measured this incident. Only usable observables
        carry incident evidence, enter usable signatures, or act as distinguishers. UNKNOWN and
        uncollectable observables are *not* usable (Invariants 1 and 2)."""
        return self.collectable and self.availability == Availability.OBSERVED

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collectable": self.collectable,
            "availability": self.availability.value,
            "state": self.state.value if self.state is not None else None,
            "value": self.value,
            "baseline": self.baseline.value if self.baseline is not None else None,
            "measurement_confidence": self.measurement_confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Observable":
        return cls(
            id=d["id"],
            collectable=d.get("collectable", True),
            availability=Availability(d.get("availability", "unknown")),
            state=State(d["state"]) if d.get("state") is not None else None,
            value=d.get("value"),
            baseline=State(d["baseline"]) if d.get("baseline") is not None else None,
            measurement_confidence=d.get("measurement_confidence", 1.0),
        )


# Convenience constructors that make the three non-collapsing states impossible to mix up.
def unknown(id: str, *, collectable: bool = True, measurement_confidence: float = 1.0) -> Observable:
    """collectable-but-not-measured (or not trustworthy) this incident."""
    return Observable(id=id, collectable=collectable, availability=Availability.UNKNOWN,
                      measurement_confidence=measurement_confidence)


def observed(id: str, state: State, *, value: Optional[float] = None,
             baseline: Optional[State] = None, measurement_confidence: float = 1.0) -> Observable:
    """measured this incident; ``state`` (including ABSENT) is evidence."""
    return Observable(id=id, collectable=True, availability=Availability.OBSERVED, state=state,
                      value=value, baseline=baseline, measurement_confidence=measurement_confidence)


def uncollectable(id: str) -> Observable:
    """no integration here can ever produce this evidence."""
    return Observable(id=id, collectable=False, availability=Availability.UNKNOWN)


def usable_observables(observables: list[Observable]) -> list[Observable]:
    """The observables that carry incident evidence: collectable AND OBSERVED. Excludes both
    UNKNOWN (Invariant 1) and uncollectable (Invariant 2)."""
    return [o for o in observables if o.is_usable]


def usable_signature(observables: list[Observable]) -> tuple[tuple[str, str], ...]:
    """The (id, state) signature over usable observables, sorted by id. Uncollectable and UNKNOWN
    observables never appear here (Invariant 2), so a signature can never depend on evidence this
    deployment cannot produce or did not measure."""
    return tuple(sorted((o.id, o.state.value) for o in usable_observables(observables)))


def evidence_score(expectations: dict[str, State], observables: list[Observable]) -> float:
    """A minimal, monotone evidence score for a hypothesis given its expected states per observable
    id: the confidence-weighted count of **usable** observables whose measured state matches the
    expectation. UNKNOWN and uncollectable observables contribute nothing, which is exactly what
    makes Invariants 1 and 2 hold — this primitive is what later phases' scoring must preserve.
    (Phase A only: not wired into the ranker; no accuracy claim.)"""
    total = 0.0
    for o in usable_observables(observables):
        expected = expectations.get(o.id)
        if expected is not None and o.state == expected:
            total += o.measurement_confidence
    return total


def integration_gaps(observables: list[Observable]) -> list[str]:
    """Ids of uncollectable observables — never incident evidence, but the basis for an
    *integration-gap* recommendation ("wire up this signal to disambiguate future incidents")."""
    return sorted(o.id for o in observables if not o.collectable)


@dataclass
class ObservableSet:
    """A small container so callers can pass observables around as one object; the invariants are
    on the free functions above (and re-exposed here for convenience)."""

    observables: list[Observable] = field(default_factory=list)

    def usable(self) -> list[Observable]:
        return usable_observables(self.observables)

    def signature(self) -> tuple[tuple[str, str], ...]:
        return usable_signature(self.observables)

    def score(self, expectations: dict[str, State]) -> float:
        return evidence_score(expectations, self.observables)

    def integration_gaps(self) -> list[str]:
        return integration_gaps(self.observables)
