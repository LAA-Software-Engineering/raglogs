"""CLI --from/--to must accept Z-suffixed ISO 8601 timestamps.

Python 3.10 (a supported version) rejects trailing Z in
``datetime.fromisoformat``. Commands advertise that form in help/examples
and must parse it the same way as the API helper.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

from typer.testing import CliRunner

from src.cli.main import app
from src.core.compare.differ import CompareResult
from src.core.explain.summarizer import ExplainResult

runner = CliRunner()

Z_FROM = "2026-03-12T22:00:00Z"
Z_TO = "2026-03-12T22:30:00Z"
EXPECTED_START = datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)
EXPECTED_END = datetime(2026, 3, 12, 22, 30, 0, tzinfo=timezone.utc)


def _ctx_db() -> MagicMock:
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    return mock_db


def _explain_result(window_start, window_end) -> ExplainResult:
    return ExplainResult(
        window_start=window_start,
        window_end=window_end,
        summary_text="ok",
        confidence="high",
        evidence_items=[],
        services_affected=[],
    )


class TestCliIsoZTimestamps:
    def test_explain_from_to_accepts_z_suffix(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
                 return_value=None,
             ), \
             patch(
                 "src.core.explain.summarizer.explain_window",
                 side_effect=lambda **kw: _explain_result(
                     kw["window_start"], kw["window_end"]
                 ),
             ) as mock_explain:
            result = runner.invoke(
                app,
                ["explain", "--from", Z_FROM, "--to", Z_TO, "--format", "json"],
            )

        assert result.exit_code == 0, result.output
        assert "Invalid isoformat string" not in result.output
        assert mock_explain.call_args.kwargs["window_start"] == EXPECTED_START
        assert mock_explain.call_args.kwargs["window_end"] == EXPECTED_END

    def test_timeline_from_to_accepts_z_suffix(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
                 return_value=None,
             ), \
             patch(
                 "src.core.clustering.clusterer.run_clustering",
                 return_value=([], []),
             ), \
             patch(
                 "src.core.explain.evidence.assemble_evidence",
                 return_value=MagicMock(),
             ), \
             patch(
                 "src.core.timeline.builder.build_timeline",
                 return_value=[],
             ) as mock_build:
            result = runner.invoke(
                app,
                ["timeline", "--from", Z_FROM, "--to", Z_TO, "--format", "json"],
            )

        assert result.exit_code == 0, result.output
        assert "Invalid isoformat string" not in result.output
        mock_build.assert_called_once()

    def test_clusters_from_to_accepts_z_suffix(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
                 return_value=None,
             ), \
             patch(
                 "src.core.clustering.clusterer.run_clustering",
                 return_value=([], []),
             ) as mock_cluster:
            result = runner.invoke(
                app,
                ["clusters", "--from", Z_FROM, "--to", Z_TO, "--format", "json"],
            )

        assert result.exit_code == 0, result.output
        assert "Invalid isoformat string" not in result.output
        assert mock_cluster.call_args.kwargs["window_start"] == EXPECTED_START
        assert mock_cluster.call_args.kwargs["window_end"] == EXPECTED_END

    def test_compare_window_flags_accept_z_suffix(self):
        mock_db = _ctx_db()
        b_from = "2026-03-11T22:00:00Z"
        b_to = "2026-03-11T22:30:00Z"
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
                 return_value=None,
             ), \
             patch(
                 "src.core.clustering.clusterer.run_clustering",
                 return_value=([], []),
             ) as mock_cluster, \
             patch(
                 "src.core.explain.evidence.assemble_evidence",
                 return_value=MagicMock(trigger_candidates=[]),
             ), \
             patch(
                 "src.core.compare.differ.compare_windows",
                 return_value=CompareResult(
                     window_a_start=EXPECTED_START,
                     window_a_end=EXPECTED_END,
                     window_b_start=datetime(2026, 3, 11, 22, 0, 0, tzinfo=timezone.utc),
                     window_b_end=datetime(2026, 3, 11, 22, 30, 0, tzinfo=timezone.utc),
                 ),
             ):
            result = runner.invoke(
                app,
                [
                    "compare",
                    "--window-a-from", Z_FROM,
                    "--window-a-to", Z_TO,
                    "--window-b-from", b_from,
                    "--window-b-to", b_to,
                    "--format", "json",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Invalid isoformat string" not in result.output
        first = mock_cluster.call_args_list[0].kwargs
        assert first["window_start"] == EXPECTED_START
        assert first["window_end"] == EXPECTED_END

    def test_compare_preserves_non_utc_offset(self):
        """``.replace(tzinfo=utc)`` would keep wall time and drop +05:00."""
        mock_db = _ctx_db()
        offset = timezone(timedelta(hours=5))
        a_from = "2026-03-12T22:00:00+05:00"
        a_to = "2026-03-12T22:30:00+05:00"
        b_from = "2026-03-11T22:00:00+05:00"
        b_to = "2026-03-11T22:30:00+05:00"
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
                 return_value=None,
             ), \
             patch(
                 "src.core.clustering.clusterer.run_clustering",
                 return_value=([], []),
             ) as mock_cluster, \
             patch(
                 "src.core.explain.evidence.assemble_evidence",
                 return_value=MagicMock(trigger_candidates=[]),
             ), \
             patch(
                 "src.core.compare.differ.compare_windows",
                 return_value=CompareResult(
                     window_a_start=datetime(2026, 3, 12, 22, 0, 0, tzinfo=offset),
                     window_a_end=datetime(2026, 3, 12, 22, 30, 0, tzinfo=offset),
                     window_b_start=datetime(2026, 3, 11, 22, 0, 0, tzinfo=offset),
                     window_b_end=datetime(2026, 3, 11, 22, 30, 0, tzinfo=offset),
                 ),
             ):
            result = runner.invoke(
                app,
                [
                    "compare",
                    "--window-a-from", a_from,
                    "--window-a-to", a_to,
                    "--window-b-from", b_from,
                    "--window-b-to", b_to,
                    "--format", "json",
                ],
            )

        assert result.exit_code == 0, result.output
        first = mock_cluster.call_args_list[0].kwargs
        assert first["window_start"] == datetime(2026, 3, 12, 22, 0, 0, tzinfo=offset)
        assert first["window_end"] == datetime(2026, 3, 12, 22, 30, 0, tzinfo=offset)
        assert first["window_start"].utcoffset() == timedelta(hours=5)

    def test_ingest_from_to_accepts_z_suffix(self):
        mock_db = _ctx_db()
        job = MagicMock()
        job.id = uuid4()
        stats = MagicMock(
            files_processed=0,
            lines_read=0,
            parsed_count=1,
            error_count=0,
            services_detected=["api"],
            duration_seconds=0.1,
        )
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.ingestion.service.ingest_from_source",
                 return_value=(job, stats),
             ) as mock_ingest:
            result = runner.invoke(
                app,
                [
                    "ingest",
                    "--adapter", "cloudwatch",
                    "--from", Z_FROM,
                    "--to", Z_TO,
                ],
            )

        assert result.exit_code == 0, result.output
        assert "Invalid isoformat string" not in result.output
        window = mock_ingest.call_args.kwargs["window"]
        assert window.start == EXPECTED_START
        assert window.end == EXPECTED_END
