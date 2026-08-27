"""parse_timestamp_field must treat numeric strings the same as numbers,
without misreading a bare-digit calendar date as an epoch.

Millisecond-epoch timestamps encoded as strings (as many JSON log sources
emit them) previously fell through dateutil, then the Unix-timestamp regex
in extract_timestamp - which requires an exact 10-digit match - and came
out as None. Log lines with a None timestamp are silently dropped from
every windowed query (clustering, explain, timeline, compare).

The numeric-string branch only runs after dateutil itself rejects the
value: routing every bare-digit string through it regardless previously
regressed calendar strings dateutil already parsed correctly ("20260312",
a bare year, ...) into wrong-but-plausible-looking datetimes - a worse
failure than the None being fixed, since a wrong non-null timestamp is
silently included under the wrong time window instead of being filtered.
"""

from datetime import datetime, timezone

from src.core.parsing.timestamp import parse_timestamp_field

MS_EPOCH = 1710280870123
SEC_EPOCH = 1710280870
EXPECTED_MS = datetime(2024, 3, 12, 22, 1, 10, 123000, tzinfo=timezone.utc)
EXPECTED_SEC = datetime(2024, 3, 12, 22, 1, 10, tzinfo=timezone.utc)


def test_string_millisecond_epoch_matches_numeric_millisecond_epoch():
    assert (
        parse_timestamp_field(str(MS_EPOCH))
        == parse_timestamp_field(MS_EPOCH)
        == EXPECTED_MS
    )


def test_string_second_epoch_matches_numeric_second_epoch():
    assert (
        parse_timestamp_field(str(SEC_EPOCH))
        == parse_timestamp_field(SEC_EPOCH)
        == EXPECTED_SEC
    )


def test_string_epoch_with_surrounding_whitespace_still_parses():
    assert parse_timestamp_field(f" {MS_EPOCH} ") == EXPECTED_MS


def test_string_fractional_epoch_parses():
    assert parse_timestamp_field(f"{SEC_EPOCH}.5") == datetime(
        2024, 3, 12, 22, 1, 10, 500000, tzinfo=timezone.utc
    )


def test_iso_string_still_parses_via_dateutil():
    assert parse_timestamp_field("2026-03-12T22:01:10Z") == datetime(
        2026, 3, 12, 22, 1, 10, tzinfo=timezone.utc
    )


def test_unparseable_string_returns_none():
    assert parse_timestamp_field("not a timestamp") is None


def test_bare_digit_calendar_date_is_not_misread_as_an_epoch():
    # "20260312" is a plausible epoch under a magnitude-only numeric check
    # (all digits), but dateutil already parses it correctly as a calendar
    # date. The numeric-string branch must only run when dateutil itself
    # rejects the value, not preempt it for every digit string.
    assert parse_timestamp_field("20260312") == datetime(
        2026, 3, 12, tzinfo=timezone.utc
    )


def test_bare_digit_calendar_date_before_unix_epoch_range():
    # A date dateutil parses fine but that a naive int(...) > 1e12 check
    # would not distinguish from a millisecond epoch by digit count alone.
    assert parse_timestamp_field("19991231") == datetime(
        1999, 12, 31, tzinfo=timezone.utc
    )
