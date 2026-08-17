"""
Tests for src.core.explain.markdown_report

Snapshot-style checks for the paste-ready incident report shape.
No database required.
"""
from datetime import datetime, timezone

from src.core.explain.markdown_report import (
    build_explain_reproduce_cmd,
    render_incident_report,
)
from src.core.explain.summarizer import ExplainResult

WINDOW_START = datetime(2026, 3, 12, 22, 33, 30, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 3, 12, 23, 33, 30, tzinfo=timezone.utc)

SUMMARY_TEXT = (
    "Incident summary\n"
    "\n"
    "Window: 2026-03-12 22:33:30 UTC → 2026-03-12 23:33:30 UTC\n"
    "Services affected: billing-worker, api\n"
    "Primary issue: Stripe signature verification failed for endpoint /webhooks/stripe\n"
    "Secondary effects: None identified\n"
    "Likely trigger: Deploy completed for billing-worker version v2.4.1\n"
    "\n"
    "Evidence:\n"
    "- 184 similar failures in billing-worker\n"
    "\n"
    "Confidence: high"
)

REPRODUCE_CMD = "raglogs explain --since 1h --service billing-worker --format markdown"

FULL_REPORT_SNAPSHOT = """# Incident report

Stripe signature verification failed for endpoint /webhooks/stripe

- **Window:** 2026-03-12T22:33:30+00:00 → 2026-03-12T23:33:30+00:00 (1h)
- **Services:** billing-worker, api
- **Environment:** production
- **Total logs:** 461
- **Confidence:** high
- **Mode:** rules

## Summary

Incident summary

Window: 2026-03-12 22:33:30 UTC → 2026-03-12 23:33:30 UTC
Services affected: billing-worker, api
Primary issue: Stripe signature verification failed for endpoint /webhooks/stripe
Secondary effects: None identified
Likely trigger: Deploy completed for billing-worker version v2.4.1

Evidence:
- 184 similar failures in billing-worker

Confidence: high

## Evidence

- 184 similar failures in billing-worker
- Not observed in prior 24h baseline

## Primary cluster

- **Fingerprint:** `fp-stripe-sig`
- **Count:** 184
- **Importance:** 8.42
- **Message:** `Stripe signature verification failed for endpoint /webhooks/stripe`
- **Services:** billing-worker
- **First seen:** 2026-03-12T22:40:31+00:00
- **Last seen:** 2026-03-12T23:30:29+00:00
- **Baseline count:** 0
- **Change ratio:** 185.0

## Secondary clusters

- `POST /api/checkout 500 Internal Server Error` — 39 events (`fp-checkout-500`; api)
- `Webhook retries` — 1 event (`fp-retry`; billing-worker)

## Trigger candidates

- 2026-03-12T22:38:29+00:00 · deployment-controller — `Deploy completed for billing-worker version v2.4.1`

## Reproduce

Operators can redirect this command to a file for tickets and postmortems:

```bash
raglogs explain --since 1h --service billing-worker --format markdown
```

`raglogs explain --since 1h --service billing-worker --format markdown > postmortem.md`
"""


def _result(**overrides) -> ExplainResult:
    data = {
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "summary_text": SUMMARY_TEXT,
        "confidence": "high",
        "evidence_items": [
            "184 similar failures in billing-worker",
            "Not observed in prior 24h baseline",
        ],
        "services_affected": ["billing-worker", "api"],
        "primary_cluster": {
            "message": "Stripe signature verification failed for endpoint /webhooks/stripe",
            "count": 184,
            "services": ["billing-worker"],
            "fingerprint": "fp-stripe-sig",
            "importance_score": 8.42,
            "first_seen": "2026-03-12T22:40:31+00:00",
            "last_seen": "2026-03-12T23:30:29+00:00",
            "baseline_count": 0,
            "change_ratio": 185.0,
        },
        "secondary_clusters": [
            {
                "message": "POST /api/checkout 500 Internal Server Error",
                "count": 39,
                "services": ["api"],
                "fingerprint": "fp-checkout-500",
                "importance_score": 4.1,
            },
            {
                "message": "Webhook retries",
                "count": 1,
                "services": ["billing-worker"],
                "fingerprint": "fp-retry",
                "importance_score": 1.2,
            },
        ],
        "trigger_candidates": [
            {
                "message": "Deploy completed for billing-worker version v2.4.1",
                "timestamp": "2026-03-12T22:38:29+00:00",
                "service": "deployment-controller",
            }
        ],
        "total_logs": 461,
        "mode": "rules",
    }
    data.update(overrides)
    return ExplainResult(**data)


class TestRequiredHeadings:
    def test_populated_report_has_required_headings(self):
        md = render_incident_report(_result(), reproduce_cmd=REPRODUCE_CMD)
        assert md.startswith("# Incident report\n")
        assert "\n## Summary\n" in md
        assert "\n## Evidence\n" in md
        assert "\n## Reproduce\n" in md

    def test_evidence_items_are_markdown_list_items(self):
        md = render_incident_report(_result())
        evidence_block = md.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
        assert "- 184 similar failures in billing-worker" in evidence_block
        assert "- Not observed in prior 24h baseline" in evidence_block


class TestOptionalSections:
    def test_omits_primary_cluster_when_absent(self):
        md = render_incident_report(_result(primary_cluster=None))
        assert "## Primary cluster" not in md
        assert "## Summary" in md
        assert "## Evidence" in md

    def test_omits_secondary_clusters_when_empty(self):
        md = render_incident_report(_result(secondary_clusters=[]))
        assert "## Secondary clusters" not in md

    def test_omits_triggers_when_empty(self):
        md = render_incident_report(_result(trigger_candidates=[]))
        after_evidence = md.split("## Evidence", 1)[1]
        assert "## Trigger candidates" not in after_evidence

    def test_empty_evidence_keeps_heading_without_none_wall(self):
        md = render_incident_report(_result(evidence_items=[]))
        assert "## Evidence" in md
        evidence_block = md.split("## Evidence\n", 1)[1].split("\n## ", 1)[0]
        assert "- None" not in evidence_block
        assert "None identified" not in evidence_block

    def test_reproduce_only_when_cmd_provided(self):
        with_cmd = render_incident_report(_result(), reproduce_cmd=REPRODUCE_CMD)
        without_cmd = render_incident_report(_result())
        assert "## Reproduce" in with_cmd
        assert REPRODUCE_CMD in with_cmd
        assert "```bash" in with_cmd
        assert "## Reproduce" not in without_cmd
        assert "postmortem.md" not in without_cmd

    def test_omits_environment_when_unknown(self):
        md = render_incident_report(_result())
        assert "**Environment:**" not in md

    def test_includes_environment_when_known(self):
        md = render_incident_report(_result(), environment="production")
        assert "- **Environment:** production" in md


class TestSpecialCharacters:
    def test_message_special_chars_are_wrapped_in_code(self):
        result = _result(
            primary_cluster={
                "message": "error *critical* _warn_ | pipe",
                "count": 3,
                "fingerprint": "fp-special",
                "importance_score": 1.0,
            },
            secondary_clusters=[],
            trigger_candidates=[],
        )
        md = render_incident_report(result)
        assert "`error *critical* _warn_ | pipe`" in md
        # Structure stays intact — headings are still headings.
        assert md.startswith("# Incident report\n")
        assert "\n## Primary cluster\n" in md

    def test_evidence_special_chars_are_escaped(self):
        result = _result(
            evidence_items=["saw *bold* and _italic_ and | pipes"],
            secondary_clusters=[],
            trigger_candidates=[],
        )
        md = render_incident_report(result)
        assert "- saw \\*bold\\* and \\_italic\\_ and \\| pipes" in md

    def test_backticks_in_message_use_extra_ticks(self):
        result = _result(
            primary_cluster={
                "message": "failed with `secret` token",
                "count": 2,
            },
            secondary_clusters=[],
            trigger_candidates=[],
        )
        md = render_incident_report(result)
        assert "``failed with `secret` token``" in md


class TestTitle:
    def test_title_from_primary_cluster_message(self):
        md = render_incident_report(_result())
        lines = md.splitlines()
        assert lines[0] == "# Incident report"
        assert lines[2] == "Stripe signature verification failed for endpoint /webhooks/stripe"

    def test_title_falls_back_to_summary_when_no_primary(self):
        md = render_incident_report(
            _result(
                primary_cluster=None,
                summary_text="Incident summary\n\nWindow: x\n\nInsufficient evidence to identify a likely issue.",
            )
        )
        assert "Insufficient evidence to identify a likely issue." in md.splitlines()[2]


class TestSnapshot:
    def test_full_populated_report_matches_snapshot(self):
        md = render_incident_report(
            _result(),
            reproduce_cmd=REPRODUCE_CMD,
            environment="production",
        )
        assert md == FULL_REPORT_SNAPSHOT


class TestReproduceCmd:
    def test_always_includes_format_markdown(self):
        cmd = build_explain_reproduce_cmd(since="1h")
        assert cmd == "raglogs explain --since 1h --format markdown"

    def test_includes_flags_that_were_passed(self):
        cmd = build_explain_reproduce_cmd(
            since="1h",
            service="billing-worker",
            env="production",
            no_llm=True,
            baseline_window="7d",
            ingestion_job="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            all_ingestions=True,
            max_clusters=5,
        )
        assert cmd == (
            "raglogs explain --since 1h --service billing-worker --env production "
            "--no-llm --baseline-window 7d "
            "--ingestion-job aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee "
            "--all-ingestions --max-clusters 5 --format markdown"
        )

    def test_omits_default_max_clusters(self):
        cmd = build_explain_reproduce_cmd(since="1h", max_clusters=10)
        assert "--max-clusters" not in cmd

    def test_from_to_window(self):
        cmd = build_explain_reproduce_cmd(
            from_time="2026-03-12T22:00:00Z",
            to_time="2026-03-12T22:30:00Z",
        )
        assert cmd == (
            "raglogs explain --from 2026-03-12T22:00:00Z "
            "--to 2026-03-12T22:30:00Z --format markdown"
        )

    def test_quotes_values_with_spaces(self):
        cmd = build_explain_reproduce_cmd(since="1h", service="billing worker")
        assert cmd == "raglogs explain --since 1h --service 'billing worker' --format markdown"
