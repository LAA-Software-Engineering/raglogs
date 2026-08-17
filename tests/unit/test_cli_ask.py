"""CLI wiring for `raglogs ask` ingestion scoping.

The core retrieval path already accepts `ingestion_job_id`; these tests
cover Typer flag wiring only (no database).
"""
import uuid
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from src.cli.main import app
from src.core.retrieval.question_router import AskResult

runner = CliRunner()


def _mock_result() -> AskResult:
    return AskResult(
        question="why did login fail?",
        answer_text="Auth token invalid in api.",
        evidence_items=["12 events: Auth token invalid"],
        clusters_used=[],
        total_matches=12,
        retrieval_mode="keyword",
    )


def _ctx_db() -> MagicMock:
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    return mock_db


class TestAskCliIngestionFlags:
    def test_help_lists_ingestion_flags(self):
        result = runner.invoke(app, ["ask", "--help"])
        assert result.exit_code == 0
        assert "--ingestion-job" in result.output
        assert "--all-ingestions" in result.output

    def test_defaults_to_latest_completed_ingestion(self):
        job_id = uuid.uuid4()
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
                 return_value=job_id,
             ) as mock_latest, \
             patch(
                 "src.core.retrieval.question_router.answer_question",
                 return_value=_mock_result(),
             ) as mock_answer:
            result = runner.invoke(app, ["ask", "why did login fail?"])

        assert result.exit_code == 0, result.output
        mock_latest.assert_called_once_with(mock_db, scope="default")
        assert mock_answer.call_args.kwargs["ingestion_job_id"] == job_id

    def test_ingestion_job_flag_passed_through(self):
        job_id = str(uuid.uuid4())
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
             ) as mock_latest, \
             patch(
                 "src.core.retrieval.question_router.answer_question",
                 return_value=_mock_result(),
             ) as mock_answer:
            result = runner.invoke(app, [
                "ask", "why did login fail?", "--ingestion-job", job_id,
            ])

        assert result.exit_code == 0, result.output
        mock_latest.assert_not_called()
        assert str(mock_answer.call_args.kwargs["ingestion_job_id"]) == job_id

    def test_all_ingestions_skips_scoping(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.explain.summarizer.get_latest_ingestion_job_id",
             ) as mock_latest, \
             patch(
                 "src.core.retrieval.question_router.answer_question",
                 return_value=_mock_result(),
             ) as mock_answer:
            result = runner.invoke(app, [
                "ask", "why did login fail?", "--all-ingestions",
            ])

        assert result.exit_code == 0, result.output
        mock_latest.assert_not_called()
        assert mock_answer.call_args.kwargs["ingestion_job_id"] is None

    def test_invalid_ingestion_job_exits_1(self):
        mock_db = _ctx_db()
        with patch("src.db.session.get_db", side_effect=lambda: mock_db), \
             patch(
                 "src.core.retrieval.question_router.answer_question",
             ) as mock_answer:
            result = runner.invoke(app, [
                "ask", "why did login fail?", "--ingestion-job", "not-a-uuid",
            ])

        assert result.exit_code == 1, result.output
        mock_answer.assert_not_called()
        assert "Error" in result.output
