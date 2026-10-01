"""The three Exotel traps, pinned by test so a refactor cannot reintroduce them."""
from __future__ import annotations

import datetime as dt

import pytest

from app.telephony.exotel import FROZEN_END_TIME, parse_exotel_time, trim_digits


@pytest.mark.parametrize("raw,expected", [
    ('"1"', "1"),          # the documented shape: quotes around the value
    ('"9"', "9"),
    ("1", "1"),            # already clean
    (' "2" ', "2"),        # whitespace either side
    ('""', None),          # empty after trimming
    (None, None),
    ("", None),
    ('"12"', "12"),        # multi-digit Gather input
])
def test_digits_are_unquoted(raw, expected):
    assert trim_digits(raw) == expected


def test_raw_quoted_digit_would_not_match_naively():
    """Guards the actual bug: comparing the raw value against '1' fails."""
    raw = '"1"'
    assert raw != "1"
    assert trim_digits(raw) == "1"


def test_frozen_end_time_is_rejected():
    assert parse_exotel_time(FROZEN_END_TIME) is None
    assert parse_exotel_time("1970-01-01 05:30:00") is None


def test_real_time_is_parsed_as_ist():
    got = parse_exotel_time("2026-09-17 14:44:22")
    assert got is not None
    assert got.utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert got.astimezone(dt.timezone.utc).hour == 9


def test_garbage_time_does_not_raise():
    assert parse_exotel_time("not a time") is None
    assert parse_exotel_time(None) is None
