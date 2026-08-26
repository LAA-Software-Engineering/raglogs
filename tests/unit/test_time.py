import pytest
from datetime import datetime, timedelta, timezone

from src.utils.time import (
    format_window,
    parse_duration,
    parse_iso,
    resolve_baseline_window,
    resolve_window,
    rewrite_iso_z,
)


class TestParseDuration:
    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)
        assert parse_duration("5min") == timedelta(minutes=5)

    def test_hours(self):
        assert parse_duration("1h") == timedelta(hours=1)
        assert parse_duration("24h") == timedelta(hours=24)

    def test_days(self):
        assert parse_duration("7d") == timedelta(days=7)
        assert parse_duration("1day") == timedelta(days=1)

    def test_seconds(self):
        assert parse_duration("60s") == timedelta(seconds=60)

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_duration("not_a_duration")

    def test_invalid_unit(self):
        with pytest.raises(ValueError):
            parse_duration("5x")


class TestResolveWindow:
    def test_since(self):
        start, end = resolve_window(since="30m")
        delta = end - start
        assert abs(delta.total_seconds() - 1800) < 2

    def test_since_1h(self):
        start, end = resolve_window(since="1h")
        delta = end - start
        assert abs(delta.total_seconds() - 3600) < 2

    def test_no_args_raises(self):
        with pytest.raises(ValueError):
            resolve_window()

    def test_from_and_to(self):
        from datetime import datetime, timezone
        from_time = datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)
        to_time = datetime(2026, 3, 12, 22, 30, 0, tzinfo=timezone.utc)
        start, end = resolve_window(from_time=from_time, to_time=to_time)
        assert start == from_time
        assert end == to_time


class TestBaselineWindow:
    def test_baseline_precedes_window(self):
        from datetime import datetime, timezone
        window_start = datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)
        window_end = datetime(2026, 3, 12, 22, 30, 0, tzinfo=timezone.utc)
        bl_start, bl_end = resolve_baseline_window(window_start, window_end, "24h")
        assert bl_end == window_start
        assert (bl_end - bl_start).total_seconds() == 86400

    def test_format_window(self):
        from datetime import datetime, timezone
        start = datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)
        end = datetime(2026, 3, 12, 22, 30, 0, tzinfo=timezone.utc)
        result = format_window(start, end)
        assert "22:00:00" in result
        assert "22:30:00" in result
        assert "→" in result


def _simulate_py310_fromisoformat(monkeypatch):
    """Python 3.10 rejects a trailing Z; datetime.fromisoformat cannot be patched."""
    import src.utils.time as time_mod

    real_fromisoformat = time_mod.datetime.fromisoformat

    class Py310DateTime(time_mod.datetime):
        @classmethod
        def fromisoformat(cls, date_string):
            if isinstance(date_string, str) and date_string.endswith("Z"):
                raise ValueError(f"Invalid isoformat string: '{date_string}'")
            return real_fromisoformat(date_string)

    monkeypatch.setattr(time_mod, "datetime", Py310DateTime)


class TestRewriteIsoZ:
    """Shared trailing-Z rewrite used by CLI parse_iso and API _parse_iso."""

    def test_trailing_z_to_offset(self):
        assert rewrite_iso_z("2026-03-12T22:00:00Z") == "2026-03-12T22:00:00+00:00"

    def test_non_z_unchanged(self):
        assert rewrite_iso_z("2026-03-12T22:00:00+05:00") == "2026-03-12T22:00:00+05:00"
        assert rewrite_iso_z("2026-03-12T22:00:00") == "2026-03-12T22:00:00"

    def test_only_terminal_z(self):
        # Global replace would mangle this; trailing-only rewrite must not.
        assert rewrite_iso_z("Z2026-03-12T22:00:00") == "Z2026-03-12T22:00:00"


class TestParseIso:
    """CLI ISO parsing must work on Python 3.10, which rejects trailing Z."""

    def test_accepts_trailing_z(self):
        dt = parse_iso("2026-03-12T22:00:00Z")
        assert dt == datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)

    def test_accepts_z_when_fromisoformat_rejects_it(self, monkeypatch):
        _simulate_py310_fromisoformat(monkeypatch)
        dt = parse_iso("2026-03-12T22:00:00Z")
        assert dt == datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)

    def test_preserves_numeric_offset(self):
        dt = parse_iso("2026-03-12T22:00:00+05:00")
        assert dt.utcoffset() == timedelta(hours=5)
        assert dt.replace(tzinfo=None) == datetime(2026, 3, 12, 22, 0, 0)

    def test_naive_assumed_utc(self):
        dt = parse_iso("2026-03-12T22:00:00")
        assert dt == datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)

    def test_plus_00_00(self):
        dt = parse_iso("2026-03-12T22:00:00+00:00")
        assert dt == datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_iso("not-a-timestamp")


class TestApiParseIsoWrapper:
    """API _parse_iso wraps rewrite_iso_z; keeps Optional/swallow/leave-naive."""

    def test_accepts_trailing_z(self):
        from src.api.schemas.v1 import _parse_iso

        dt = _parse_iso("2026-03-12T22:00:00Z")
        assert dt == datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)

    def test_accepts_z_when_fromisoformat_rejects_it(self, monkeypatch):
        import src.api.schemas.v1 as v1_mod
        import src.utils.time as time_mod

        real_fromisoformat = time_mod.datetime.fromisoformat

        class Py310DateTime(time_mod.datetime):
            @classmethod
            def fromisoformat(cls, date_string):
                if isinstance(date_string, str) and date_string.endswith("Z"):
                    raise ValueError(f"Invalid isoformat string: '{date_string}'")
                return real_fromisoformat(date_string)

        monkeypatch.setattr(v1_mod, "datetime", Py310DateTime)
        monkeypatch.setattr(time_mod, "datetime", Py310DateTime)

        from src.api.schemas.v1 import _parse_iso

        dt = _parse_iso("2026-03-12T22:00:00Z")
        assert dt == datetime(2026, 3, 12, 22, 0, 0, tzinfo=timezone.utc)

    def test_invalid_returns_none(self):
        from src.api.schemas.v1 import _parse_iso

        assert _parse_iso("not-a-timestamp") is None

    def test_empty_and_none_return_none(self):
        from src.api.schemas.v1 import _parse_iso

        assert _parse_iso(None) is None
        assert _parse_iso("") is None

    def test_naive_left_naive(self):
        from src.api.schemas.v1 import _parse_iso

        dt = _parse_iso("2026-03-12T22:00:00")
        assert dt == datetime(2026, 3, 12, 22, 0, 0)
        assert dt.tzinfo is None

