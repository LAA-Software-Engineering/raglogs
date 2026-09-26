"""Integration: the #209 M3 evaluator's frozen arm IS the product's structural view. On the same persisted
rows, ``evaluate_case``'s M2a+M2b arm must return exactly ``build_structural_view``'s outcome and
localization — the evaluator measures the shipped model, not a copy. Skipped without Postgres."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("DB_URL") and not os.getenv("INTEGRATION_TESTS"),
    reason="Integration tests require DB_URL environment variable",
)

W = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
BASE = W - timedelta(seconds=300)
END = W + timedelta(seconds=300)
SCOPE = "eval:m3-parity"


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
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with get_db() as db:
        yield db


def _spans():
    """frontend -> payment (Charge) and frontend -> ad (GetAds); in the incident, Charge fails with no
    payment span (unreachable callee)."""
    from src.core.ingestion.telemetry import ParsedSpan

    out, n = [], 0
    for i in range(1, 11):
        for ts, incident in ((W - timedelta(seconds=i), False), (W + timedelta(seconds=i), True)):
            for op, callee in (("oteldemo.PaymentService/Charge", "payment"), ("oteldemo.AdService/GetAds", "ad")):
                n += 1
                failing = incident and callee == "payment"
                out.append(ParsedSpan(trace_id=f"t{n}", span_id=f"p{n}", service="frontend", operation=op,
                                      start_time=ts, duration_ms=10.0, status_code="2" if failing else "1"))
                if not failing:
                    out.append(ParsedSpan(trace_id=f"t{n}", span_id=f"c{n}", parent_span_id=f"p{n}",
                                          service=callee, operation="handle",
                                          start_time=ts + timedelta(milliseconds=1), duration_ms=9.0,
                                          status_code="1"))
    return out


def _cpu():
    from src.core.ingestion.telemetry import ParsedMetricSample

    inst = {"service.instance.id": "ad-1"}
    return [ParsedMetricSample(service="ad", metric="jvm.cpu.recent_utilization", value=v,
                               ts=W + timedelta(seconds=sign * 30 * i), metric_type="gauge", attributes=inst)
            for i in range(1, 6) for sign, v in ((-1, 0.01), (1, 0.9))]


def test_full_arm_matches_the_product_structural_view(db_session):
    from src.core.ingestion.telemetry import persist_metric_samples, persist_spans
    from src.core.rca.structural import build_structural_view, load_structural_rows
    from src.eval.structural_m3 import FULL_ARM, evaluate_case

    persist_spans(db_session, _spans(), scope=SCOPE)
    persist_metric_samples(db_session, _cpu(), scope=SCOPE)
    db_session.flush()

    built = build_structural_view(db_session, SCOPE, W, END, BASE)
    assert built is not None
    product, _ranking = built

    metric_rows, span_rows = load_structural_rows(db_session, SCOPE, END, BASE)
    c = evaluate_case("otel_parity", "payment", True, span_rows, metric_rows, W)
    assert c.arms[FULL_ARM].outcome == product.outcome.value
    assert c.arms[FULL_ARM].localization == tuple(product.localization)
    # and the evaluator attributes the families it saw: the edge into payment, util on ad
    assert "edge" in c.retained_via
    assert c.util_present == ("util:ad:cpu",)
