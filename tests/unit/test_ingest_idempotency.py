"""Unit tests for Idempotency-Key on POST /v1/ingestions."""

from __future__ import annotations

import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from src.api.app import app
from src.core.ingestion.idempotency import (
    InvalidIdempotencyKey,
    is_active,
    key_log_prefix,
    parse_idempotency_key,
    store_idempotency_key,
)
from src.db.models import IngestIdempotencyKey, WorkerJob

client = TestClient(app, raise_server_exceptions=False)


def _ctx_db() -> MagicMock:
    mock_db = MagicMock()
    mock_db.__enter__ = MagicMock(return_value=mock_db)
    mock_db.__exit__ = MagicMock(return_value=False)
    mock_db.query.return_value.filter.return_value.first.return_value = None
    mock_db.execute.return_value.scalar_one.return_value = 0
    mock_db.begin_nested.return_value = nullcontext()
    return mock_db


class TestParseIdempotencyKey:
    def test_none_is_omitted(self) -> None:
        assert parse_idempotency_key(None) is None

    def test_strips_and_keeps_value(self) -> None:
        assert parse_idempotency_key("  abc-123  ") == "abc-123"

    def test_empty_raises(self) -> None:
        with pytest.raises(InvalidIdempotencyKey, match="non-empty"):
            parse_idempotency_key("")
        with pytest.raises(InvalidIdempotencyKey, match="non-empty"):
            parse_idempotency_key("   ")

    def test_too_long_raises(self) -> None:
        with pytest.raises(InvalidIdempotencyKey, match="256"):
            parse_idempotency_key("k" * 257)

    def test_log_prefix_is_short(self) -> None:
        prefix = key_log_prefix("super-secret-idempotency-key")
        assert prefix == "super-se"
        assert "secret-idempotency" not in prefix


class TestIsActive:
    def test_future_expiry_is_active(self) -> None:
        row = MagicMock()
        row.expires_at = datetime(2026, 8, 18, tzinfo=timezone.utc)
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        assert is_active(row, now) is True

    def test_past_expiry_is_inactive(self) -> None:
        row = MagicMock()
        row.expires_at = datetime(2026, 8, 16, tzinfo=timezone.utc)
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        assert is_active(row, now) is False


class TestStoreIdempotencyKey:
    def test_unique_conflict_returns_existing(self) -> None:
        winner_id = uuid.uuid4()
        winner = IngestIdempotencyKey(
            key="same",
            worker_job_id=winner_id,
            ingestion_job_id=None,
            mode="batch",
            created_at=datetime.now(tz=timezone.utc),
            expires_at=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, winner]
        db.begin_nested.return_value = nullcontext()
        db.flush.side_effect = IntegrityError("INSERT", {}, Exception("unique"))

        row, replay = store_idempotency_key(
            db,
            "same",
            worker_job_id=uuid.uuid4(),
            ingestion_job_id=None,
            mode="batch",
            now=datetime.now(tz=timezone.utc),
            ttl_seconds=86400,
        )
        assert replay is True
        assert row.worker_job_id == winner_id


class TestCreateIngestionIdempotency:
    def test_same_key_returns_original_worker_job_id(self) -> None:
        stored: dict[str, IngestIdempotencyKey | None] = {"row": None}
        mock_db = _ctx_db()

        def query_side_effect(model: object) -> MagicMock:
            q = MagicMock()
            name = getattr(model, "__name__", "")
            if name == "IngestIdempotencyKey":
                q.filter.return_value.first.return_value = stored["row"]
            else:
                q.filter.return_value.first.return_value = None
            return q

        def capture_add(obj: object) -> None:
            if isinstance(obj, WorkerJob):
                obj.id = uuid.uuid4()
            if isinstance(obj, IngestIdempotencyKey):
                stored["row"] = obj
                if getattr(obj, "expires_at", None) is None:
                    obj.expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=1)

        mock_db.query.side_effect = query_side_effect
        mock_db.add.side_effect = capture_add

        with (
            patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            first = client.post(
                "/v1/ingestions",
                json={"paths": ["/logs"]},
                headers={"Idempotency-Key": "req-1"},
            )
            second = client.post(
                "/v1/ingestions",
                json={"paths": ["/logs"]},
                headers={"idempotency-key": "req-1"},
            )

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["worker_job_id"] == second.json()["worker_job_id"]

    def test_same_key_on_unversioned_alias(self) -> None:
        stored: dict[str, IngestIdempotencyKey | None] = {"row": None}
        mock_db = _ctx_db()

        def query_side_effect(model: object) -> MagicMock:
            q = MagicMock()
            name = getattr(model, "__name__", "")
            if name == "IngestIdempotencyKey":
                q.filter.return_value.first.return_value = stored["row"]
            else:
                q.filter.return_value.first.return_value = None
            return q

        def capture_add(obj: object) -> None:
            if isinstance(obj, WorkerJob):
                obj.id = uuid.uuid4()
            if isinstance(obj, IngestIdempotencyKey):
                stored["row"] = obj
                if getattr(obj, "expires_at", None) is None:
                    obj.expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=1)

        mock_db.query.side_effect = query_side_effect
        mock_db.add.side_effect = capture_add

        with (
            patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            first = client.post(
                "/ingestions",
                json={"paths": ["/logs"]},
                headers={"Idempotency-Key": "alias-1"},
            )
            second = client.post(
                "/ingestions",
                json={"paths": ["/logs"]},
                headers={"Idempotency-Key": "alias-1"},
            )

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["worker_job_id"] == second.json()["worker_job_id"]

    def test_different_keys_create_two_jobs(self) -> None:
        ids: list[uuid.UUID] = []
        mock_db = _ctx_db()

        def capture_add(obj: object) -> None:
            if isinstance(obj, WorkerJob):
                obj.id = uuid.uuid4()
                ids.append(obj.id)

        mock_db.add.side_effect = capture_add

        with (
            patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            first = client.post(
                "/v1/ingestions",
                json={"paths": ["/logs"]},
                headers={"Idempotency-Key": "key-a"},
            )
            second = client.post(
                "/v1/ingestions",
                json={"paths": ["/logs"]},
                headers={"Idempotency-Key": "key-b"},
            )

        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["worker_job_id"] != second.json()["worker_job_id"]
        assert len(ids) == 2

    def test_expired_key_starts_a_new_job(self) -> None:
        old_id = uuid.uuid4()
        new_id = uuid.uuid4()
        expired = MagicMock()
        expired.key = "stale"
        expired.worker_job_id = old_id
        expired.ingestion_job_id = None
        expired.mode = "batch"
        expired.expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)

        mock_db = _ctx_db()
        mock_db.query.return_value.filter.return_value.first.return_value = expired

        def capture_add(obj: object) -> None:
            if isinstance(obj, WorkerJob):
                obj.id = new_id

        mock_db.add.side_effect = capture_add

        with (
            patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            resp = client.post(
                "/v1/ingestions",
                json={"paths": ["/logs"]},
                headers={"Idempotency-Key": "stale"},
            )

        assert resp.status_code == 202
        assert resp.json()["worker_job_id"] == str(new_id)
        assert resp.json()["worker_job_id"] != str(old_id)
        mock_db.delete.assert_called()

    def test_empty_key_is_400(self) -> None:
        with patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]):
            resp = client.post(
                "/v1/ingestions",
                json={"paths": ["/logs"]},
                headers={"Idempotency-Key": "   "},
            )
        assert resp.status_code == 400
        assert "non-empty" in resp.json()["detail"]

    def test_oversized_key_is_400(self) -> None:
        resp = client.post(
            "/v1/ingestions",
            json={"paths": ["/logs"]},
            headers={"Idempotency-Key": "k" * 257},
        )
        assert resp.status_code == 400
        assert "256" in resp.json()["detail"]

    def test_omitted_key_keeps_current_behavior(self) -> None:
        job_id = uuid.uuid4()
        mock_db = _ctx_db()

        def capture_add(obj: object) -> None:
            if isinstance(obj, WorkerJob):
                obj.id = job_id

        mock_db.add.side_effect = capture_add

        with (
            patch("src.adapters.file.adapter.discover_files", return_value=["f.log"]),
            patch("src.db.session.get_db", side_effect=lambda: mock_db),
        ):
            resp = client.post("/v1/ingestions", json={"paths": ["/logs"]})

        assert resp.status_code == 202
        assert resp.json()["worker_job_id"] == str(job_id)
        added_keys = [
            obj
            for call in mock_db.add.call_args_list
            for obj in call.args
            if isinstance(obj, IngestIdempotencyKey)
        ]
        assert added_keys == []
