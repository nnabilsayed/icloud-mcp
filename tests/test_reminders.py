from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import reminders as R


def test_repeat_wire_values_match_the_app():
    # Verified against reminders created in the Reminders app itself.
    assert R._FREQ == {"daily": 0, "weekly": 1, "monthly": 2, "yearly": 3}


def test_parse_due_all_day_is_midnight_utc():
    dt, all_day = R._parse_due("2026-10-14")
    assert all_day and dt == datetime(2026, 10, 14, tzinfo=timezone.utc)


def test_parse_due_timed_and_empty():
    dt, all_day = R._parse_due("2026-10-14T09:00:00")
    assert not all_day and dt.tzinfo is not None and dt.hour == 9
    assert R._parse_due("") == (None, False)


def _reminder(**kw):
    base = dict(id="R/1", list_id="L", title="t", desc="", completed=False, flagged=False, priority=0,
                parent_reminder_id=None)
    return SimpleNamespace(**base, **kw)


@pytest.mark.parametrize("offset", [-10, -5, 0, 3, 12])
def test_all_day_date_is_the_same_in_every_time_zone(monkeypatch, offset):
    monkeypatch.setattr(R, "get_localzone", lambda: timezone(timedelta(hours=offset)))
    r = _reminder(all_day=True, due_date=datetime(2026, 10, 14, tzinfo=timezone.utc))
    assert R._out(r)["due"] == "2026-10-14"


def test_timed_due_is_shown_in_local_time(monkeypatch):
    monkeypatch.setattr(R, "get_localzone", lambda: timezone(timedelta(hours=3)))
    r = _reminder(all_day=False, due_date=datetime(2026, 10, 14, 20, tzinfo=timezone.utc))
    assert R._out(r)["due"].startswith("2026-10-14T23:00")
