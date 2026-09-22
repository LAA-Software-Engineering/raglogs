"""Presentation — render the deterministic structural packet; LLM as renderer only (#187, Phase I).

The structural pipeline (Phases A–E) computes a :class:`~src.core.rca.outcome.StructuralResult`: a
deterministic outcome + evidence packet. Phase G (:mod:`~src.core.rca.scoring`) orders the surviving
classes. This phase turns that into what a person (or an agent) reads, under one hard rule:

    **the deterministic packet is authoritative; the LLM only renders prose over it.**

So the structured output here is computed entirely from the ``StructuralResult`` (and the Phase G
ordering) — never from a model — and an LLM, when configured, is handed that packet to phrase, but its
text is *decorative*: it can never invent, merge, eliminate, or choose a hypothesis, because nothing
here parses model output back into structure. With the ``noop`` provider the narrative is exactly the
deterministic rendering, so the whole pipeline runs end-to-end with no model.

Two presentation-layer invariants:

- **Invariant 2 at the surface.** ``integration_gaps`` (uncollectable observables) appear **only** as
  *"useful next observations"* — wire-this-up recommendations — never as observed evidence and never as
  an incident-level distinguisher. Missing telemetry is a gap to close, not a fact about the incident.
- **Evidence is observed only.** The "observed evidence" lines are the packet's own
  :class:`~src.core.rca.outcome.EvidenceItem` facts (usable, measured); ``D_missing`` renders under
  *"why they cannot be separated"*, distinct from evidence.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

from src.core.rca.outcome import Outcome, Relation, StructuralResult
from src.core.rca.scoring import ScoredClass


def _apply_ranking(result: StructuralResult, ranking: tuple[ScoredClass, ...]):
    """Decide whether a Phase G ranking is used, once, for both order and scores. Returns
    ``(ordered_classes, score_by_ids_or_None, applied)``.

    A ranking is applied **only** when its class id-sets are *exactly* the packet's — same multiset,
    no duplicate, no missing, no extra — so presentation can neither drop, add, nor duplicate a class,
    and cannot claim a score order the classes were not given. Otherwise the packet's own order is used
    and **no** scores are attached (a partial/duplicated ranking is rejected wholesale, not applied to
    the overlap). ``rank_classes`` always produces a valid ranking; this only guards a hostile caller."""
    if not ranking:
        return list(result.classes), None, False
    packet_ids = sorted(tuple(sorted(frozenset(c.hypothesis_ids))) for c in result.classes)
    ranking_ids = sorted(tuple(sorted(frozenset(r.hypothesis_ids))) for r in ranking)
    if ranking_ids != packet_ids:  # any disagreement (incl. a repeated id-set) -> trust the packet
        return list(result.classes), None, False
    by_ids = {frozenset(c.hypothesis_ids): c for c in result.classes}
    ordered = [by_ids[frozenset(r.hypothesis_ids)] for r in ranking]
    return ordered, {frozenset(r.hypothesis_ids): r for r in ranking}, True


def _separable_missing(result: StructuralResult) -> frozenset[str]:
    """The prediction distinguishers that could actually be *collected* to separate the classes: the
    packet's ``D_missing`` minus the uncollectable ``integration_gaps``. An uncollectable id is never a
    distinguisher at the presentation layer (Invariant 2) — it is only a wire-this-up recommendation."""
    return frozenset(result.d_missing) - set(result.integration_gaps)


@dataclass(frozen=True)
class StructuralPresentation:
    """The rendered structural result: the serializable ``packet`` (for API/JSON), the ordered text
    ``lines`` (for CLI), and the ``narrative`` prose (deterministic text under ``noop``; LLM prose over
    the same packet otherwise — always decorative, never authoritative)."""

    outcome: str
    packet: dict[str, Any] = field(default_factory=dict)
    lines: tuple[str, ...] = ()
    narrative: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


_OUTCOME_HEADLINE = {
    Outcome.IDENTIFIED: "IDENTIFIED",
    Outcome.NON_IDENTIFIABLE: "NON_IDENTIFIABLE",
    Outcome.IRREDUCIBLE: "IRREDUCIBLE",
    Outcome.UNCERTAIN: "UNCERTAIN",
    Outcome.NO_COMPATIBLE_HYPOTHESIS: "NO_COMPATIBLE_HYPOTHESIS",
}


def structural_packet(
    result: StructuralResult, ranking: tuple[ScoredClass, ...] = ()
) -> dict[str, Any]:
    """The serializable deterministic packet (API/JSON). Classes are in Phase G order when ``ranking``
    is given; each carries its localizations, defining signature, observed supporting evidence, its
    ``d_missing`` (why-not-separable), and — when scored — its discrete ``score`` / ``ranker_signal``.
    ``integration_gaps`` and ``next_observations`` are recommendations, kept separate from evidence."""
    classes, score_by_ids, _applied = _apply_ranking(result, ranking)
    gaps = set(result.integration_gaps)
    class_packets = []
    for c in classes:
        scored = score_by_ids.get(frozenset(c.hypothesis_ids)) if score_by_ids else None
        class_packets.append({
            "localizations": list(c.localizations),
            "hypothesis_ids": list(c.hypothesis_ids),
            "signature": [list(pair) for pair in c.signature],
            "evidence": [
                {"observable": e.observable_id, "observed": e.observed_state,
                 "expected": e.expected_state, "relation": e.relation}
                for e in c.evidence
            ],
            # Uncollectable ids are never surfaced as distinguishers at the presentation layer (Inv 2).
            "d_missing": sorted(set(c.d_missing) - gaps),
            "score": scored.score if scored else None,
            "ranker_signal": scored.ranker_signal if scored else None,
        })
    return {
        "outcome": result.outcome.value,
        "localization": list(result.localization),
        "classes": class_packets,
        # Collectable distinguishers not yet collected (why-not-separable). Uncollectable coordinates
        # are excluded here and surface only under integration_gaps / next_observations (Invariant 2).
        "d_missing": sorted(_separable_missing(result)),
        # What to collect next: collectable distinguishers + hard-rule checks + the wire-this-up
        # integration gaps — recommendations, never incident evidence.
        "next_observations": _next_observations(result),
        "integration_gaps": list(result.integration_gaps),
        "eliminated": [
            {"localization": e.member.localization,
             "evidence": [{"observable": ev.observable_id, "observed": ev.observed_state}
                          for ev in e.evidence]}
            for e in result.eliminated
        ],
    }


def _next_observations(result: StructuralResult) -> list[str]:
    """Actionable next observations: the **collectable** missing distinguishers (bare ids), the hard-rule
    elimination checks (collect X to rule a hypothesis out), and the integration gaps (uncollectable
    telemetry, explicitly labelled). All recommendations — deduplicated, stable-ordered — never incident
    facts. An uncollectable id appears ONLY under its labelled integration-gap line (Invariant 2)."""
    obs: list[str] = list(sorted(_separable_missing(result)))
    for chk in result.potential_elimination_checks:
        obs.append(f"{chk.observable_id} (would rule out {chk.hypothesis_id})")
    for gap in result.integration_gaps:
        obs.append(f"{gap} (not collected — integration gap)")
    return list(dict.fromkeys(obs))  # stable de-dupe


def _append_next_observations(lines: list[str], result: StructuralResult) -> None:
    nxt = _next_observations(result)
    if nxt:
        lines.append("Useful next observations:")
        for o in nxt:
            lines.append(f"  - {o}")


def render_lines(result: StructuralResult, ranking: tuple[ScoredClass, ...] = ()) -> tuple[str, ...]:
    """The user-facing text layout for a structural result (the #187 template). Competing classes are in
    Phase G order **only when a valid ranking was supplied** — otherwise the packet's own order, and the
    header does not claim a support order the list does not have."""
    classes, _scores, applied = _apply_ranking(result, ranking)
    lines: list[str] = [f"Outcome:   {_OUTCOME_HEADLINE[result.outcome]}"]

    if result.outcome is Outcome.NO_COMPATIBLE_HYPOTHESIS:
        lines.append("All candidate hypotheses were ruled out by observed evidence:")
        for e in result.eliminated:
            why = ", ".join(f"{ev.observable_id}={ev.observed_state}" for ev in e.evidence)
            lines.append(f"  - {e.member.localization}" + (f"  (ruled out by {why})" if why else ""))
        _append_next_observations(lines, result)  # integration gaps still surface here (Inv 2)
        return tuple(lines)

    if result.localization:
        lines.append(f"Localization: {', '.join(result.localization)}")

    # Hypotheses (single class -> "Hypothesis"; multiple classes -> competing groups).
    if len(classes) == 1:
        cls = classes[0]
        label = "Hypothesis" if cls.is_singleton else "Remaining hypotheses"
        lines.append(f"{label}:")
        for loc in cls.localizations:
            lines.append(f"  - {loc}")
    else:
        # Claim a support order only when a ranking was actually applied.
        lines.append("Competing hypotheses (most-supported first):" if applied
                     else "Competing hypotheses:")
        for c in classes:
            lines.append(f"  - {', '.join(c.localizations)}")

    # Observed evidence — the packet's own measured, supporting facts (never integration gaps).
    evidence = _supporting_evidence(classes)
    if evidence:
        lines.append("Observed evidence:")
        for e in evidence:
            lines.append(f"  ✓ {e.observable_id} = {e.observed_state}")

    # Why they cannot be separated — collectable distinguishers only (uncollectable ones are an
    # integration gap, surfaced under "next observations", never presented as a distinguisher).
    if result.outcome is Outcome.NON_IDENTIFIABLE:
        lines.append("Why they cannot be separated:")
        separable = sorted(_separable_missing(result))
        if separable:
            for d in separable:
                lines.append(f"  ? {d} (not collected)")
        else:
            lines.append("  ? the distinguishing telemetry is not collected (see next observations)")
    elif result.outcome is Outcome.IRREDUCIBLE:
        lines.append("Why they cannot be separated:")
        lines.append("  ? no modeled observation distinguishes them (extend the model)")

    _append_next_observations(lines, result)
    return tuple(lines)


def _supporting_evidence(classes: Iterable) -> list:
    """Distinct observed SUPPORTS facts across the classes, in stable order — the honest "we saw this"
    lines. Soft mismatches and hard eliminations are not listed as supporting evidence."""
    seen: set[tuple[str, str]] = set()
    out = []
    for c in classes:
        for e in c.evidence:
            if e.relation == Relation.SUPPORTS and (e.observable_id, e.observed_state) not in seen:
                seen.add((e.observable_id, e.observed_state))
                out.append(e)
    return out


def render_structural(
    result: StructuralResult,
    ranking: tuple[ScoredClass, ...] = (),
    provider: Optional[Any] = None,
) -> StructuralPresentation:
    """Render ``result`` (ordered by the Phase G ``ranking`` when given). The structured ``packet`` and
    ``lines`` are computed **only** from the deterministic result. ``narrative`` is the deterministic
    text under the ``noop`` provider (or no provider); with a real provider it is LLM prose phrased over
    the same packet — decorative, never parsed back into structure, so the LLM can neither invent nor
    remove a hypothesis (the #187 LLM-as-renderer contract)."""
    packet = structural_packet(result, ranking)
    lines = render_lines(result, ranking)
    deterministic_text = "\n".join(lines)
    narrative = deterministic_text
    if provider is not None:
        prose = ""
        try:
            prose = provider.generate_summary(packet) or ""
        except Exception:
            prose = ""  # a failing renderer never breaks the deterministic result
        if prose.strip():
            narrative = prose.strip()
    return StructuralPresentation(
        outcome=result.outcome.value, packet=packet, lines=lines, narrative=narrative
    )
