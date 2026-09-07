"""Canary: run the eval harness end-to-end against the committed sample case.

Exercises the DB-backed runner (ingest -> explain_window -> baseline -> score)
against real Postgres, and turns #77's "seeded, expect ~100% on the canary"
acceptance criterion into an actual assertion. `001-sample-incident` is the
fixture the product was fitted to, so raglogs should nail it.
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

CASES_DIR = Path(__file__).resolve().parents[1] / "eval" / "cases"


@pytest.fixture
def db_session():
    from sqlalchemy import text

    from src.db.models import Base
    from src.db.session import check_connection, get_db, get_engine

    if not check_connection():
        pytest.skip("Cannot connect to database")

    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    # Clean slate so tests stay isolated when they share one database
    # (e.g. a single Postgres service across the whole CI run).
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with get_db() as db:
        yield db


def test_sample_incident_canary(db_session):
    from src.eval.case import load_case
    from src.eval.metrics import root_cause_hit
    from src.eval.runner import run_case

    case = load_case(CASES_DIR / "001-sample-incident")
    result = run_case(db_session, case)

    # raglogs arm nails the fitted fixture: produces an explanation whose primary
    # cluster names the labeled root-cause service, and returns some trigger.
    assert result.raglogs.produced_explanation is True
    assert root_cause_hit(case, result.raglogs) is True
    assert result.raglogs.returned_any_trigger is True

    # The trivial baseline also finds an error cluster here (it just can't
    # explain the trigger); this keeps the baseline query exercised too.
    assert result.baseline.produced_explanation is True
