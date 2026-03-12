"""
Tests for src.core.explain.templates

Verifies the rendered text output shape — section order, secondary loop
completeness, trigger hierarchy, truncation, and edge cases.
"""
import pytest
from datetime import datetime, timezone, timedelta

from src.core.clustering.clusterer import ClusterData
from src.core.explain.evidence import EvidencePacket, TriggerCandidate
from src.core.explain.templates import render_text_summary, render_insufficient_evidence, _trunc


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dt(offset_minutes: int = 0) -> datetime:
    return datetime(2026, 3, 12, 14, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=offset_minutes)


def _cluster(
    message: str,
    count: int = 50,
    services: dict | None = None,
    levels: dict | None = None,
    first_seen: datetime | None = None,
) -> ClusterData:
    return ClusterData(
        fingerprint="abcd1234",
        representative_message=message,
        count=count,
        services=services or {"api": count},
        levels=levels or {"error": count},
        first_seen=first_seen or _dt(),
        last_seen=_dt(60),
        baseline_count=0,
        change_ratio=51.0,
        importance_score=8.0,
    )


def _trigger(message: str, offset: int = -5, service: str = "deploy") -> TriggerCandidate:
    return TriggerCandidate(message=message, timestamp=_dt(offset), service=service)


def _packet(
    primary=None,
    secondary=None,
    triggers=None,
    evidence=None,
    services=None,
) -> EvidencePacket:
    return EvidencePacket(
        window_start=_dt(-60),
        window_end=_dt(0),
        total_logs=404,
        primary_cluster=primary,
        secondary_clusters=secondary or [],
        trigger_candidates=triggers or [],
        evidence_items=evidence or ["184 similar failures in billing-worker"],
        services_affected=services or ["api", "billing-worker"],
    )


# ── Section structure ─────────────────────────────────────────────────────────

class TestSectionOrder:
    def test_starts_with_incident_summary(self):
        p = _packet(primary=_cluster("DB timeout"))
        text = render_text_summary(p, "medium")
        assert text.startswith("Incident summary")

    def test_window_precedes_services(self):
        p = _packet(primary=_cluster("DB timeout"))
        text = render_text_summary(p, "medium")
        assert text.index("Window:") < text.index("Services affected:")

    def test_services_precede_primary_issue(self):
        p = _packet(primary=_cluster("DB timeout"))
        text = render_text_summary(p, "medium")
        assert text.index("Services affected:") < text.index("Primary issue:")

    def test_primary_precedes_secondary(self):
        p = _packet(primary=_cluster("DB timeout"), secondary=[_cluster("cache miss", count=5)])
        text = render_text_summary(p, "medium")
        assert text.index("Primary issue:") < text.index("Secondary effects:")

    def test_secondary_precedes_trigger(self):
        p = _packet(
            primary=_cluster("DB timeout"),
            secondary=[_cluster("cache miss", count=5)],
            triggers=[_trigger("Deploy completed")],
        )
        text = render_text_summary(p, "medium")
        assert text.index("Secondary effects:") < text.index("Likely trigger:")

    def test_trigger_precedes_evidence(self):
        p = _packet(
            primary=_cluster("DB timeout"),
            triggers=[_trigger("Deploy completed")],
        )
        text = render_text_summary(p, "medium")
        assert text.index("Likely trigger:") < text.index("Evidence:")

    def test_evidence_precedes_confidence(self):
        p = _packet(primary=_cluster("DB timeout"))
        text = render_text_summary(p, "high")
        assert text.index("Evidence:") < text.index("Confidence:")

    def test_confidence_is_last_line(self):
        p = _packet(primary=_cluster("DB timeout"))
        text = render_text_summary(p, "medium-high")
        assert text.strip().endswith("Confidence: medium-high")


# ── Secondary effects ─────────────────────────────────────────────────────────

class TestSecondarySection:
    def test_all_secondaries_rendered(self):
        """Regression: the loop indentation bug caused only the last to appear."""
        s1 = _cluster("POST /checkout 500", count=39)
        s2 = _cluster("POST /checkout 200 latency=5120ms", count=25)
        s3 = _cluster("queue growing 364 events pending", count=2)
        p = _packet(primary=_cluster("Stripe error"), secondary=[s1, s2, s3])
        text = render_text_summary(p, "high")
        # All three should be bullet points under "Secondary effects:"
        secondary_block = text.split("Secondary effects:")[1].split("Likely trigger:")[0]
        bullets = [l for l in secondary_block.splitlines() if l.strip().startswith("-")]
        assert len(bullets) == 3

    def test_no_secondary_emits_none_identified(self):
        p = _packet(primary=_cluster("DB timeout"), secondary=[])
        text = render_text_summary(p, "medium")
        assert "Secondary effects: None identified" in text

    def test_secondary_count_in_bullets(self):
        s = _cluster("cache miss", count=1)
        p = _packet(primary=_cluster("DB timeout"), secondary=[s])
        text = render_text_summary(p, "medium")
        assert "(1 event)" in text

    def test_secondary_plural_count(self):
        s = _cluster("cache miss", count=12)
        p = _packet(primary=_cluster("DB timeout"), secondary=[s])
        text = render_text_summary(p, "medium")
        assert "(12 events)" in text

    def test_max_three_secondaries_shown(self):
        secondaries = [_cluster(f"error type {i}", count=10-i) for i in range(6)]
        p = _packet(primary=_cluster("DB timeout"), secondary=secondaries)
        text = render_text_summary(p, "medium")
        secondary_block = text.split("Secondary effects:")[1].split("Likely trigger:")[0]
        bullets = [l for l in secondary_block.splitlines() if l.strip().startswith("-")]
        assert len(bullets) == 3


# ── Trigger section ───────────────────────────────────────────────────────────

class TestTriggerSection:
    def test_single_trigger_uses_likely_trigger_label(self):
        t = _trigger("Deploy completed for billing-worker v2.4.1", offset=-5)
        p = _packet(primary=_cluster("Stripe error"), triggers=[t])
        text = render_text_summary(p, "high")
        assert "Likely trigger: Deploy completed" in text

    def test_trigger_includes_service_and_time(self):
        t = _trigger("Deploy completed", offset=-5, service="deployment-controller")
        p = _packet(primary=_cluster("Stripe error"), triggers=[t])
        text = render_text_summary(p, "high")
        assert "deployment-controller" in text
        assert "13:55:00 UTC" in text  # _dt(-5) from 14:00

    def test_supporting_trigger_evidence_emitted(self):
        t1 = _trigger("Deploy completed for billing-worker v2.4.1", offset=-5)
        t2 = _trigger("Application started billing-worker v2.4.1", offset=-4, service="billing-worker")
        p = _packet(primary=_cluster("Stripe error"), triggers=[t1, t2])
        text = render_text_summary(p, "high")
        assert "Supporting trigger evidence:" in text
        assert "Application started" in text

    def test_no_trigger_emits_none_identified(self):
        p = _packet(primary=_cluster("DB timeout"), triggers=[])
        text = render_text_summary(p, "medium")
        assert "none identified" in text

    def test_no_supporting_section_when_single_trigger(self):
        t = _trigger("Deploy completed")
        p = _packet(primary=_cluster("Stripe error"), triggers=[t])
        text = render_text_summary(p, "high")
        assert "Supporting trigger evidence:" not in text


# ── Evidence section ──────────────────────────────────────────────────────────

class TestEvidenceSection:
    def test_each_item_prefixed_with_dash(self):
        items = ["184 similar failures in billing-worker", "Not observed in prior 24h baseline"]
        p = _packet(primary=_cluster("error"), evidence=items)
        text = render_text_summary(p, "medium")
        assert "- 184 similar failures in billing-worker" in text
        assert "- Not observed in prior 24h baseline" in text

    def test_empty_evidence_emits_insufficient_evidence_bullet(self):
        # render_text_summary renders evidence_items as-is; when the list is empty
        # it shows the fallback bullet "- Insufficient evidence collected"
        p = _packet(primary=_cluster("error"), evidence=[])
        # Override the helper default so evidence_items is truly empty
        p.evidence_items = []
        text = render_text_summary(p, "low")
        assert "Insufficient evidence collected" in text


# ── Primary issue ─────────────────────────────────────────────────────────────

class TestPrimaryIssue:
    def test_message_in_primary_issue(self):
        p = _packet(primary=_cluster("Stripe signature verification failed"))
        text = render_text_summary(p, "high")
        assert "Stripe signature verification failed" in text

    def test_no_primary_cluster_fallback(self):
        p = _packet(primary=None)
        text = render_text_summary(p, "low")
        assert "No significant error cluster identified" in text

    def test_long_message_truncated(self):
        long_msg = "A" * 200
        p = _packet(primary=_cluster(long_msg))
        text = render_text_summary(p, "medium")
        primary_line = next(l for l in text.splitlines() if l.startswith("Primary issue:"))
        assert len(primary_line) < 200


# ── Insufficient evidence ─────────────────────────────────────────────────────

class TestRenderInsufficientEvidence:
    def test_structure(self):
        text = render_insufficient_evidence(_dt(-60), _dt(0), total_logs=12)
        assert "Incident summary" in text
        assert "Insufficient evidence" in text
        assert "12 total logs" in text
        assert "Confidence: low" in text

    def test_zero_logs(self):
        text = render_insufficient_evidence(_dt(-60), _dt(0), total_logs=0)
        assert "0 total logs" in text


# ── _trunc ────────────────────────────────────────────────────────────────────

class TestTrunc:
    def test_short_string_unchanged(self):
        assert _trunc("hello world", 50) == "hello world"

    def test_truncates_at_word_boundary(self):
        result = _trunc("hello world foo bar", 12)
        assert result.endswith("…")
        assert "hello world" in result or "hello" in result

    def test_no_space_fallback(self):
        result = _trunc("abcdefghij", 5)
        assert result.endswith("…")
        assert len(result) <= 6  # 5 chars + ellipsis
