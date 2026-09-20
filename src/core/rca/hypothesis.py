"""Causal hypothesis ontology (#179, Phase B of the #177 causal-inference epic).

Today a "root cause" is a bare ``root_cause_service`` string. That is too thin for the epic: a fault
can be a *process* failing, an *edge* (call/dependency) degrading, a *resource* saturating, an
*infra event*, a *change* (deploy/config), or an *external* dependency — and the structural inference
in later phases reasons over that richer object, not over a service name.

This phase introduces the abstraction and nothing more. A :class:`Hypothesis` carries its ``kind``
and internal structure distinctly from ``localization`` — the user-facing name the CLI/API renders —
so output stays honest (a service name) without dumbing the model down (the ``kind`` and structure are
retained internally). ``predictions`` and :meth:`Hypothesis.hard_incompatibility` are the seams Phase C
fills; here they are deliberate stubs.

Existing RCA service candidates (:class:`~src.core.rca.candidates.RootCauseCandidate`) map to
``process`` hypotheses via :func:`hypotheses_from_candidates`, preserving order and provenance so the
current ranker output is unchanged — this is plumbing, not a ranking change.
"""
from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Optional

from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.observable import Observable

_EMPTY_PREDICTIONS: Mapping[str, str] = MappingProxyType({})


class Kind(str, Enum):
    """The causal-object ontology. A **closed** set — adding a kind is a deliberate ontology change,
    not deployment-configurable — with the six kinds #179 requires as the minimum initial set."""

    PROCESS = "process"          # a service/process itself failing
    EDGE = "edge"                # a call / dependency edge degrading
    RESOURCE = "resource"        # a resource (CPU, memory, connection pool, disk) saturating
    INFRA_EVENT = "infra_event"  # node/pod/network infrastructure event
    CHANGE = "change"            # a deploy / config / flag change
    EXTERNAL = "external"        # an external dependency outside the deployment


@dataclass(frozen=True, eq=False)
class Hypothesis:
    """One causal hypothesis.

    ``kind`` and ``localization`` are kept distinct on purpose: ``localization`` is the user-facing
    projection (the service/edge/resource name shown in CLI/API output), while ``kind`` and any
    internal structure are retained even when only ``localization`` is rendered.

    ``predictions`` (observable id -> predicted categorical state) and :meth:`hard_incompatibility`
    are **stubs in Phase B**, filled by Phase C. ``predictions`` feeds
    :func:`~src.core.rca.observable.hypothesis_signature` unchanged once populated.

    **Identity is the causal object, never the ranking.** Equality and hash are defined over the
    causal fields (``id``, ``kind``, ``localization``, ``predictions``) and deliberately **exclude**
    ``source``. #177/#182 require structural partitioning to be invariant under arbitrary ranking
    scores, so a hypothesis's identity cannot depend on the score the ranker happened to assign.

    ``source`` is optional ranking provenance — the :class:`RootCauseCandidate` a ``process``
    hypothesis was wrapped from, kept so score/features/evidence stay reachable. It is **owned, not
    borrowed**: the candidate is defensively deep-copied at construction, so mutating the caller's
    candidate afterwards cannot rewrite this frozen hypothesis's provenance or serialization.
    """

    id: str
    kind: Kind
    localization: str
    predictions: Mapping[str, str] = field(default_factory=lambda: _EMPTY_PREDICTIONS)
    source: Optional[RootCauseCandidate] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Validate the runtime representation (frozen dataclass, no static type gate here).
        if not isinstance(self.id, str) or not self.id:
            raise ValueError(f"hypothesis id must be a non-empty string, got {self.id!r}")
        if not isinstance(self.kind, Kind):
            try:
                object.__setattr__(self, "kind", Kind(self.kind))
            except ValueError:
                raise ValueError(
                    f"hypothesis {self.id!r}: kind must be one of {[k.value for k in Kind]}, "
                    f"got {self.kind!r}"
                ) from None
        if not isinstance(self.localization, str) or not self.localization:
            raise ValueError(
                f"hypothesis {self.id!r}: localization must be a non-empty string, got "
                f"{self.localization!r}"
            )
        for k, v in dict(self.predictions).items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError(
                    f"hypothesis {self.id!r}: predictions must map observable-id strings to state "
                    f"strings, got {k!r}: {v!r}"
                )
        # Own the mapping: a read-only view over a defensive copy, so the object cannot be mutated
        # behind a caller's back (matches the Phase A representation-ownership discipline).
        object.__setattr__(self, "predictions", MappingProxyType(dict(self.predictions)))
        # Own the provenance too: a defensive deep copy severs the alias to the caller's mutable
        # candidate, so its score/features/service can never change this hypothesis after the fact.
        if self.source is not None:
            object.__setattr__(self, "source", copy.deepcopy(self.source))

    def _identity(self) -> tuple:
        """The causal identity — everything that defines the hypothesis *except* ranking provenance."""
        return (self.id, self.kind, self.localization, tuple(sorted(self.predictions.items())))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Hypothesis):
            return NotImplemented
        return self._identity() == other._identity()

    def __hash__(self) -> int:
        return hash(self._identity())

    def hard_incompatibility(self, observables: Iterable[Observable]) -> bool:
        """Is this hypothesis hard-incompatible with the observed evidence? **Stub for Phase B** —
        prediction strengths (PRESENT_HARD / ABSENT_HARD) arrive in Phase C, so nothing is yet known
        to be hard-incompatible and this is always ``False``. The signature is the Phase C seam."""
        return False

    def render(self) -> str:
        """The user-facing projection — just the localization. The ``kind``/structure stay internal."""
        return self.localization

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind.value,
            "localization": self.localization,
            "predictions": dict(self.predictions),
        }
        if self.source is not None:
            d["source"] = self.source.to_dict()
        return d


def process_hypothesis_from_candidate(candidate: RootCauseCandidate) -> Hypothesis:
    """Wrap an existing service candidate as a ``process`` hypothesis, localized to its service and
    keeping an owned (deep-copied) snapshot of the candidate as provenance, so score/features/evidence
    stay reachable without the ranking leaking into causal identity. No score or ordering is changed —
    this is the compatibility bridge that keeps current RCA output intact."""
    return Hypothesis(
        id=f"process:{candidate.service}",
        kind=Kind.PROCESS,
        localization=candidate.service,
        source=candidate,
    )


def hypotheses_from_candidates(
    candidates: Iterable[RootCauseCandidate],
) -> list[Hypothesis]:
    """Map ranked service candidates to ``process`` hypotheses **in the same order**, so wrapping is
    output-neutral: ``[h.localization for h in result] == [c.service for c in candidates]``."""
    return [process_hypothesis_from_candidate(c) for c in candidates]
