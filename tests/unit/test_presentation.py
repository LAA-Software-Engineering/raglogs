"""Phase I (#187) — presentation of the deterministic structural packet; LLM as renderer. Pure, no DB.

Covers the five outcomes' text layout, Phase G ordering of competing classes, Invariant 2 at the
surface (uncollectable telemetry appears ONLY as a next-observation recommendation, never as observed
evidence or a distinguisher), and the LLM-as-renderer contract: the structured packet is authoritative
and computed only from the deterministic result — a model can neither invent nor remove a hypothesis,
and the noop provider yields exactly the deterministic narrative.
"""

from src.core.rca.candidates import RootCauseCandidate
from src.core.rca.expectations import (
    Contradiction,
    ExpectedObservation,
    ObservationModel,
    Strength,
)
from src.core.rca.features import ServiceFeatures
from src.core.rca.hypothesis import Kind, from_observation_model
from src.core.rca.observable import observed, uncollectable
from src.core.rca.outcome import Outcome, resolve
from src.core.rca.partition import partition
from src.core.rca.presentation import (
    render_lines,
    render_structural,
    structural_packet,
)
from src.core.rca.scoring import rank_classes


def _h(hid, model, *, kind=Kind.PROCESS, localization=None, score=0.0):
    cand = RootCauseCandidate(service=hid, score=score, features=ServiceFeatures(service=hid))
    return from_observation_model(hid, kind, localization or hid, model, source=cand)


def _model(*expected, contradictions=()):
    return ObservationModel(expected=expected, contradictions=contradictions)


class _FakeProvider:
    """A provider that returns whatever prose it is told — including a lie — to prove the LLM cannot
    change the structured packet."""

    def __init__(self, prose):
        self.prose = prose
        self.seen = None

    def generate_summary(self, packet):
        self.seen = packet
        return self.prose


def _identified():
    h = _h("payment", _model(ExpectedObservation("payment.err", "present", Strength.USUALLY)))
    return resolve(partition([h], [observed("payment.err", "present")]))


def _non_identifiable():
    h1 = _h("proc", _model(ExpectedObservation("sig", "present", Strength.USUALLY),
                           ExpectedObservation("hidden", "present", Strength.MAYBE)),
            localization="payment process")
    h2 = _h("edge", _model(ExpectedObservation("sig", "present", Strength.USUALLY),
                           ExpectedObservation("hidden", "absent", Strength.MAYBE)),
            kind=Kind.EDGE, localization="checkout->payment")
    return resolve(partition([h1, h2], [observed("sig", "present")]))


class TestOutcomeLayouts:
    def test_identified(self):
        lines = render_lines(_identified())
        assert lines[0] == "Outcome:   IDENTIFIED"
        assert "Localization: payment" in lines
        assert "Hypothesis:" in lines
        assert any("payment.err = present" in ln for ln in lines)

    def test_non_identifiable_shows_remaining_and_why(self):
        r = _non_identifiable()
        assert r.outcome is Outcome.NON_IDENTIFIABLE
        lines = render_lines(r)
        assert lines[0] == "Outcome:   NON_IDENTIFIABLE"
        assert "Remaining hypotheses:" in lines
        assert any("payment process" in ln for ln in lines) and any("checkout->payment" in ln for ln in lines)
        assert "Why they cannot be separated:" in lines
        assert any("hidden" in ln and "not collected" in ln for ln in lines)

    def test_irreducible_says_extend_the_model(self):
        # identical predictions everywhere -> one multi-member class, empty d_missing
        h1 = _h("a", _model(ExpectedObservation("s", "present", Strength.OFTEN)))
        h2 = _h("b", _model(ExpectedObservation("s", "present", Strength.OFTEN)), kind=Kind.EDGE)
        r = resolve(partition([h1, h2], [observed("s", "present")]))
        assert r.outcome is Outcome.IRREDUCIBLE
        lines = render_lines(r)
        assert "Why they cannot be separated:" in lines
        assert any("extend the model" in ln for ln in lines)

    def test_uncertain_lists_competing_classes_in_rank_order(self):
        strong = _h("strong", _model(ExpectedObservation("a", "present", Strength.USUALLY)),
                    localization="A")
        weak = _h("weak", _model(ExpectedObservation("b", "present", Strength.MAYBE)),
                  localization="B")
        p = partition([weak, strong], [observed("a", "present"), observed("b", "present")])
        r = resolve(p)
        assert r.outcome is Outcome.UNCERTAIN
        lines = render_lines(r, rank_classes(p))
        assert "Competing hypotheses (most-supported first):" in lines
        # 'A' (score 3) must be listed before 'B' (score 1)
        body = [ln for ln in lines if ln.startswith("  - ")]
        assert body.index("  - A") < body.index("  - B")

    def test_no_compatible_hypothesis_lists_ruled_out_and_no_localization(self):
        m = _model(ExpectedObservation("a", "present"),
                   contradictions=(Contradiction("g", frozenset({"true"})),))
        h = _h("dead", m, localization="payment")
        r = resolve(partition([h], [observed("g", "true")]))
        assert r.outcome is Outcome.NO_COMPATIBLE_HYPOTHESIS
        lines = render_lines(r)
        assert "All candidate hypotheses were ruled out by observed evidence:" in lines
        assert any("payment" in ln and "g=true" in ln for ln in lines)
        assert not any(ln.startswith("Localization:") for ln in lines)


def _non_identifiable_via_uncollectable():
    # Two hypotheses agree on the usable `sig` and disagree only on `payment.health`, which is
    # UNCOLLECTABLE. So payment.health lands in BOTH d_missing (predicted differently, not usable) and
    # integration_gaps (uncollectable) — the exact overlap Invariant 2 must resolve in favor of "gap".
    h1 = _h("proc", _model(ExpectedObservation("sig", "present", Strength.USUALLY),
                           ExpectedObservation("payment.health", "present", Strength.MAYBE)),
            localization="payment process")
    h2 = _h("edge", _model(ExpectedObservation("sig", "present", Strength.USUALLY),
                           ExpectedObservation("payment.health", "absent", Strength.MAYBE)),
            kind=Kind.EDGE, localization="checkout->payment")
    obs = [observed("sig", "present"), uncollectable("payment.health")]
    return resolve(partition([h1, h2], obs), observations=obs)


class TestInvariant2AtSurface:
    def test_uncollectable_distinguisher_is_never_shown_as_a_reason(self):
        r = _non_identifiable_via_uncollectable()
        assert r.outcome is Outcome.NON_IDENTIFIABLE
        assert "payment.health" in r.integration_gaps
        assert "payment.health" in r.d_missing  # structurally it IS a distinguisher...

        packet = structural_packet(r)
        # ...but the presented packet never surfaces it as one (Invariant 2).
        assert "payment.health" not in packet["d_missing"]
        assert all("payment.health" not in c["d_missing"] for c in packet["classes"])
        assert "payment.health" in packet["integration_gaps"]
        # In next_observations it appears ONLY with the integration-gap label, never as a bare id.
        health = [o for o in packet["next_observations"] if "payment.health" in o]
        assert health == ["payment.health (not collected — integration gap)"]

        lines = render_lines(r)
        assert "Why they cannot be separated:" in lines
        assert not any("? payment.health" in ln for ln in lines)  # not a distinguisher line
        health_lines = [ln for ln in lines if "payment.health" in ln]
        assert health_lines and all("integration gap" in ln for ln in health_lines)

    def test_no_compatible_still_surfaces_integration_gaps(self):
        m = _model(ExpectedObservation("a", "present"),
                   contradictions=(Contradiction("g", frozenset({"true"})),))
        h = _h("dead", m, localization="payment")
        obs = [observed("g", "true"), uncollectable("payment.health")]
        r = resolve(partition([h], obs), observations=obs)
        assert r.outcome is Outcome.NO_COMPATIBLE_HYPOTHESIS
        lines = render_lines(r)
        assert "Useful next observations:" in lines  # early return no longer drops this
        assert any("payment.health" in ln and "integration gap" in ln for ln in lines)

    def test_uncollectable_is_only_a_next_observation(self):
        # payment.err observed (supports); payment.health is uncollectable -> integration gap.
        h = _h("payment", _model(ExpectedObservation("payment.err", "present", Strength.USUALLY)))
        p = partition([h], [observed("payment.err", "present"), uncollectable("payment.health")])
        r = resolve(p, observations=[observed("payment.err", "present"), uncollectable("payment.health")])
        assert "payment.health" in r.integration_gaps

        packet = structural_packet(r)
        # It is a recommendation, never observed evidence.
        assert "payment.health" in packet["integration_gaps"]
        assert any("payment.health" in o for o in packet["next_observations"])
        for cls in packet["classes"]:
            assert all("payment.health" != e["observable"] for e in cls["evidence"])

        lines = render_lines(r)
        gap_line = [ln for ln in lines if "payment.health" in ln]
        assert gap_line and all("integration gap" in ln for ln in gap_line)
        assert not any(ln.startswith("  ✓") and "payment.health" in ln for ln in lines)


class TestRankingContract:
    def _uncertain(self):
        strong = _h("strong", _model(ExpectedObservation("a", "present", Strength.USUALLY)),
                    localization="A")
        weak = _h("weak", _model(ExpectedObservation("b", "present", Strength.MAYBE)),
                  localization="B")
        p = partition([weak, strong], [observed("a", "present"), observed("b", "present")])
        return p, resolve(p)

    def test_default_makes_no_support_order_claim(self):
        _p, r = self._uncertain()
        lines = render_lines(r)  # no ranking
        assert "Competing hypotheses:" in lines
        assert not any("most-supported first" in ln for ln in lines)

    def test_applied_ranking_claims_and_delivers_support_order(self):
        p, r = self._uncertain()
        lines = render_lines(r, rank_classes(p))
        assert "Competing hypotheses (most-supported first):" in lines
        body = [ln for ln in lines if ln.startswith("  - ")]
        assert body.index("  - A") < body.index("  - B")

    def test_duplicate_idset_ranking_is_rejected_wholesale(self):
        p, r = self._uncertain()
        good = rank_classes(p)
        bad = (good[0], good[0])  # same length as the packet, but repeats a class and drops the other
        packet = structural_packet(r, bad)
        # rejected -> packet order, and NO scores attached from the rejected ranking
        assert all(c["score"] is None and c["ranker_signal"] is None for c in packet["classes"])
        assert {tuple(c["hypothesis_ids"]) for c in packet["classes"]} == {("strong",), ("weak",)}
        lines = render_lines(r, bad)
        assert "Competing hypotheses:" in lines  # no support-order claim on a rejected ranking

    def test_partial_ranking_attaches_no_scores(self):
        p, r = self._uncertain()
        partial = (rank_classes(p)[0],)  # only one of the two classes
        packet = structural_packet(r, partial)
        assert all(c["score"] is None for c in packet["classes"])

    def test_valid_ranking_attaches_scores_and_orders(self):
        p, r = self._uncertain()
        packet = structural_packet(r, rank_classes(p))
        assert [c["localizations"][0] for c in packet["classes"]] == ["A", "B"]
        assert [c["score"] for c in packet["classes"]] == [3, 1]


class TestLLMAsRenderer:
    def test_noop_narrative_is_the_deterministic_text(self):
        r = _identified()
        pres = render_structural(r)  # no provider == noop
        assert pres.narrative == "\n".join(render_lines(r))
        assert pres.outcome == Outcome.IDENTIFIED.value

    def test_llm_prose_cannot_change_the_structured_packet(self):
        r = _non_identifiable()
        liar = _FakeProvider("The root cause is definitely the database. Case closed.")
        pres = render_structural(r, provider=liar)
        # narrative is the LLM prose...
        assert pres.narrative == "The root cause is definitely the database. Case closed."
        # ...but the structured packet is the deterministic one — the lie changed nothing.
        assert pres.packet["outcome"] == Outcome.NON_IDENTIFIABLE.value
        assert "database" not in [loc for c in pres.packet["classes"] for loc in c["localizations"]]
        assert liar.seen == pres.packet  # the model was handed the authoritative packet to phrase

    def test_failing_provider_falls_back_to_deterministic(self):
        class _Boom:
            def generate_summary(self, packet):
                raise RuntimeError("provider down")

        r = _identified()
        pres = render_structural(r, provider=_Boom())
        assert pres.narrative == "\n".join(render_lines(r))

    def test_empty_prose_falls_back_to_deterministic(self):
        r = _identified()
        pres = render_structural(r, provider=_FakeProvider("   "))
        assert pres.narrative == "\n".join(render_lines(r))
