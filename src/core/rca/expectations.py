"""Observation expectations — the two-layer evidence model (#180, Phase C of the #177 epic).

A hypothesis relates to the telemetry in **two logically separate ways**, and conflating them is the
failure mode this phase removes:

- **Hard incompatibility** — an observation that *logically contradicts* the hypothesis, which
  eliminates it. Example: the same process instance is continuously confirmed healthy and serving
  throughout the incident, which cannot coexist with "that process is dead". The **absence** of an
  expected event (no OOM, no restart) is deliberately **not** a contradiction — you cannot refute a
  cause by not having collected its symptom.
- **Soft evidence** — an :class:`ExpectedObservation` with a categorical ``expected_state`` and a
  soft ``strength`` (USUALLY / OFTEN / MAYBE / NOT_REQUIRED). It only moves *support*; it never
  eliminates.

Two invariants fall out and are enforced by unit tests:

- **Invariant 3 (hard/soft separation).** A hard contradiction eliminates; a soft mismatch only
  changes the score.
- **Invariant 4 (strength ⟂ identity).** ``strength`` is *support weight only*: the structural
  signature is the categorical ``expected_state`` alone, so ``USUALLY ABSENT`` and ``MAYBE ABSENT``
  both project to ``ABSENT`` and land in the same equivalence class (:func:`ObservationModel.predictions`).

The hand-authored templates at the bottom cover the currently-evaluated failure families as a small
starter set; they are **parameterised by service/edge name** (never hard-coded), so the core stays
source-agnostic (#81).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from src.core.rca.observable import Observable, usable_observables


class Strength(str, Enum):
    """Soft support weight of an expected observation. **Never** carries elimination power — that is
    the exclusive job of a hard contradiction — and **never** enters the structural signature."""

    USUALLY = "usually"          # strong soft evidence
    OFTEN = "often"              # moderate
    MAYBE = "maybe"              # weak
    NOT_REQUIRED = "not_required"  # informational; contributes no weight


# Soft weights. NOT_REQUIRED is exactly 0.0 so an unmet "not required" expectation is inert.
_WEIGHT: Mapping[Strength, float] = {
    Strength.USUALLY: 1.0,
    Strength.OFTEN: 0.6,
    Strength.MAYBE: 0.3,
    Strength.NOT_REQUIRED: 0.0,
}


@dataclass(frozen=True)
class ExpectedObservation:
    """One soft expectation: a categorical ``expected_state`` for ``observable_id`` at a soft
    ``strength``. Strength is support weight only (Invariant 4)."""

    observable_id: str
    expected_state: str
    strength: Strength = Strength.MAYBE

    def __post_init__(self) -> None:
        if not isinstance(self.observable_id, str) or not self.observable_id:
            raise ValueError(f"expected observation: observable_id must be a non-empty string, got "
                             f"{self.observable_id!r}")
        if not isinstance(self.expected_state, str) or not self.expected_state:
            raise ValueError(f"expected observation {self.observable_id!r}: expected_state must be a "
                             f"non-empty string, got {self.expected_state!r}")
        if not isinstance(self.strength, Strength):
            try:
                object.__setattr__(self, "strength", Strength(self.strength))
            except ValueError:
                raise ValueError(
                    f"expected observation {self.observable_id!r}: strength must be one of "
                    f"{[s.value for s in Strength]}, got {self.strength!r}"
                ) from None

    @property
    def weight(self) -> float:
        return _WEIGHT[self.strength]


@dataclass(frozen=True)
class Contradiction:
    """A hard, purely-logical contradiction rule: if ``observable_id`` is **observed** in one of
    ``states``, the hypothesis is impossible. Use only for genuine logical contradictions (a process
    confirmed alive vs. "process dead"), never for a missing expected symptom."""

    observable_id: str
    states: frozenset[str]

    def __post_init__(self) -> None:
        if not isinstance(self.observable_id, str) or not self.observable_id:
            raise ValueError(f"contradiction: observable_id must be a non-empty string, got "
                             f"{self.observable_id!r}")
        states = frozenset(self.states)
        if not states or not all(isinstance(s, str) and s for s in states):
            raise ValueError(f"contradiction {self.observable_id!r}: states must be a non-empty set "
                             f"of non-empty strings, got {self.states!r}")
        object.__setattr__(self, "states", states)


def _observed_states(observations: Iterable[Observable]) -> dict[str, str]:
    """Map observable id -> measured state for the **usable** (collectable ∧ OBSERVED) observations.
    Only measured facts can contradict or support; UNKNOWN / uncollectable contribute nothing."""
    return {o.id: o.state for o in usable_observables(list(observations)) if o.state is not None}


@dataclass(frozen=True)
class ObservationModel:
    """A hypothesis's categorical observation model: soft ``expected`` observations plus hard
    ``contradictions``. Immutable (frozen, tuple/frozenset fields). At most one expectation and one
    contradiction per observable id, so the categorical signature is well-defined."""

    expected: tuple[ExpectedObservation, ...] = ()
    contradictions: tuple[Contradiction, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "expected", tuple(self.expected))
        object.__setattr__(self, "contradictions", tuple(self.contradictions))
        seen_e: set[str] = set()
        for e in self.expected:
            if e.observable_id in seen_e:
                raise ValueError(f"observation model: duplicate expectation for {e.observable_id!r}")
            seen_e.add(e.observable_id)
        seen_c: set[str] = set()
        for c in self.contradictions:
            if c.observable_id in seen_c:
                raise ValueError(f"observation model: duplicate contradiction for {c.observable_id!r}")
            seen_c.add(c.observable_id)

    def predictions(self) -> dict[str, str]:
        """The categorical signature seam: ``{observable_id: expected_state}`` — **strength-free**
        (Invariant 4), so it feeds :func:`~src.core.rca.observable.hypothesis_signature` unchanged."""
        return {e.observable_id: e.expected_state for e in self.expected}

    def hard_incompatibility(self, observations: Iterable[Observable]) -> bool:
        """True iff a usable observation logically contradicts this hypothesis (hard elimination).
        A missing/UNKNOWN observation is never a contradiction."""
        observed = _observed_states(observations)
        return any(
            observed.get(c.observable_id) in c.states for c in self.contradictions
        )

    def soft_support(self, observations: Iterable[Observable]) -> float:
        """Confidence-free soft support: usable observations that match a soft expectation add its
        weight; those that mismatch subtract it; unobserved expectations are neutral. This only moves
        support and can never eliminate (that is :meth:`hard_incompatibility`'s job alone)."""
        observed = _observed_states(observations)
        total = 0.0
        for e in self.expected:
            state = observed.get(e.observable_id)
            if state is None:
                continue  # not measured this incident → neutral (never a penalty, never elimination)
            total += e.weight if state == e.expected_state else -e.weight
        return total


# --- Hand-authored starter templates for the currently-evaluated failure families ----------------
# Parameterised by service / edge name so core stays source-agnostic (#81). Not full causal coverage.

def process_dead_model(service: str) -> ObservationModel:
    """A ``process``-death hypothesis for ``service``. The death is **hard-contradicted** only by the
    process being positively confirmed up (healthy/serving/alive); the OOM and restart symptoms are
    *soft* — their absence weakens but never eliminates (the observation-model regression, Invariant 3)."""
    return ObservationModel(
        expected=(
            ExpectedObservation(f"{service}.error_log", "present", Strength.USUALLY),
            ExpectedObservation(f"{service}.restart", "present", Strength.OFTEN),
            ExpectedObservation(f"{service}.oom", "present", Strength.MAYBE),
        ),
        contradictions=(
            Contradiction(f"{service}.health", frozenset({"healthy", "serving", "alive"})),
        ),
    )


def edge_network_failure_model(caller: str, callee: str) -> ObservationModel:
    """A ``edge`` network-failure hypothesis for the ``caller``→``callee`` dependency. Hard-contradicted
    only by the edge being confirmed connected/healthy; connection errors and timeouts are soft."""
    edge = f"{caller}->{callee}"
    return ObservationModel(
        expected=(
            ExpectedObservation(f"{edge}.conn_error", "present", Strength.USUALLY),
            ExpectedObservation(f"{edge}.timeout", "present", Strength.OFTEN),
            ExpectedObservation(f"{callee}.error_log", "absent", Strength.MAYBE),
        ),
        contradictions=(
            Contradiction(f"{edge}.connectivity", frozenset({"healthy", "connected"})),
        ),
    )
