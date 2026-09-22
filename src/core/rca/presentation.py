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


def _ordered_classes(result: StructuralResult, ranking: tuple[ScoredClass, ...]):
    """The result's classes in Phase G presentation order when a ranking is supplied, else the packet's
    own deterministic order. Ranking only *reorders* — it never adds or drops a class (a ranking whose
    id-set disagrees with the packet is ignored, so presentation can't silently diverge from inference)."""
    if not ranking:
        return list(result.classes)
    by_ids = {frozenset(c.hypothesis_ids): c for c in result.classes}
    ordered = [by_ids[frozenset(r.hypothesis_ids)] for r in ranking
               if frozenset(r.hypothesis_ids) in by_ids]
    if len(ordered) != len(result.classes):  # ranking disagrees with the packet -> trust the packet
        return list(result.classes)
    return ordered


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
    classes = _ordered_classes(result, ranking)
    score_by_ids = {frozenset(r.hypothesis_ids): r for r in ranking}
    class_packets = []
    for c in classes:
        scored = score_by_ids.get(frozenset(c.hypothesis_ids))
        class_packets.append({
            "localizations": list(c.localizations),
            "hypothesis_ids": list(c.hypothesis_ids),
            "signature": [list(pair) for pair in c.signature],
            "evidence": [
                {"observable": e.observable_id, "observed": e.observed_state,
                 "expected": e.expected_state, "relation": e.relation}
                for e in c.evidence
            ],
            "d_missing": sorted(c.d_missing),
            "score": scored.score if scored else None,
            "ranker_signal": scored.ranker_signal if scored else None,
        })
    return {
        "outcome": result.outcome.value,
        "localization": list(result.localization),
        "classes": class_packets,
        # Distinguishers not collected (would separate the surviving classes): why-not-separable.
        "d_missing": sorted(result.d_missing),
        # What to collect next: the prediction distinguishers plus the wire-this-up integration gaps
        # (uncollectable coordinates) — recommendations, never incident evidence (Invariant 2).
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
    """Actionable next observations: the missing prediction distinguishers (``D_missing``), the hard-rule
    elimination checks (collect X to rule a hypothesis out), and the integration gaps (uncollectable
    telemetry to wire up). All recommendations — deduplicated, stable-ordered — never incident facts."""
    obs: list[str] = list(sorted(result.d_missing))
    for chk in result.potential_elimination_checks:
        obs.append(f"{chk.observable_id} (would rule out {chk.hypothesis_id})")
    for gap in result.integration_gaps:
        obs.append(f"{gap} (not collected — integration gap)")
    return list(dict.fromkeys(obs))  # stable de-dupe


def render_lines(result: StructuralResult, ranking: tuple[ScoredClass, ...] = ()) -> tuple[str, ...]:
    """The user-facing text layout for a structural result (the #187 template), classes in Phase G
    order when ``ranking`` is supplied."""
    classes = _ordered_classes(result, ranking)
    lines: list[str] = [f"Outcome:   {_OUTCOME_HEADLINE[result.outcome]}"]

    if result.outcome is Outcome.NO_COMPATIBLE_HYPOTHESIS:
        lines.append("All candidate hypotheses were ruled out by observed evidence:")
        for e in result.eliminated:
            why = ", ".join(f"{ev.observable_id}={ev.observed_state}" for ev in e.evidence)
            lines.append(f"  - {e.member.localization}" + (f"  (ruled out by {why})" if why else ""))
        return tuple(lines)

    if result.localization:
        lines.append(f"Localization: {', '.join(result.localization)}")

    # Hypotheses (single class -> "Hypothesis"; multiple classes -> competing groups in rank order).
    if len(classes) == 1:
        cls = classes[0]
        label = "Hypothesis" if cls.is_singleton else "Remaining hypotheses"
        lines.append(f"{label}:")
        for loc in cls.localizations:
            lines.append(f"  - {loc}")
    else:
        lines.append("Competing hypotheses (most-supported first):")
        for c in classes:
            lines.append(f"  - {', '.join(c.localizations)}")

    # Observed evidence — the packet's own measured, supporting facts (never integration gaps).
    evidence = _supporting_evidence(classes)
    if evidence:
        lines.append("Observed evidence:")
        for e in evidence:
            lines.append(f"  ✓ {e.observable_id} = {e.observed_state}")

    # Why they cannot be separated — D_missing (collected-but-not, or model has no distinguisher).
    if result.outcome is Outcome.NON_IDENTIFIABLE:
        lines.append("Why they cannot be separated:")
        for d in sorted(result.d_missing):
            lines.append(f"  ? {d} (not collected)")
    elif result.outcome is Outcome.IRREDUCIBLE:
        lines.append("Why they cannot be separated:")
        lines.append("  ? no modeled observation distinguishes them (extend the model)")

    nxt = _next_observations(result)
    if nxt:
        lines.append("Useful next observations:")
        for o in nxt:
            lines.append(f"  - {o}")
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
