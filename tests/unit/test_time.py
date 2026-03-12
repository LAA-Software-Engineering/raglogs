import pytest
from datetime import timedelta

from src.utils.time import parse_duration, resolve_window, resolve_baseline_window, format_window


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
