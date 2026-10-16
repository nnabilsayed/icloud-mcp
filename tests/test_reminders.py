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


@pytest.mark.parametrize("days,hours,minutes,expected", [
    (None, None, None, None),
    (0, 0, 0, (0, R._UNIT_DAYS)),  # explicit zeros mean "clear"
    (None, None, 15, (15, R._UNIT_MINUTES)),
    (None, 2, None, (2, R._UNIT_HOURS)),
    (3, None, None, (3, R._UNIT_DAYS)),
    (None, 0, None, (0, R._UNIT_DAYS)),
    (0, 0, 15, (15, R._UNIT_MINUTES)),  # a zero beside a real value is ignored
])
def test_early_choice(days, hours, minutes, expected):
    assert R._early_choice(days, hours, minutes) == expected


def test_weeks_and_months_choices():
    assert R._early_choice(None, None, None, 1, None) == (1, R._UNIT_WEEKS)
    assert R._early_choice(None, None, None, None, 1) == (1, R._UNIT_MONTHS)
    assert R._early_choice(None, None, None, None, 0) == (0, R._UNIT_DAYS)  # clears


def test_two_early_reminders_is_an_error():
    with pytest.raises(ValueError, match="Give only one"):
        R._early_choice(None, 1, 15)
    with pytest.raises(ValueError, match="Give only one"):
        R._early_choice(None, None, None, 1, 1)


def test_minutes_unit_matches_what_the_app_wrote():
    assert (R._UNIT_MINUTES, R._UNIT_HOURS, R._UNIT_DAYS) == (0, 1, 2)  # from decoded app records
    assert (R._UNIT_WEEKS, R._UNIT_MONTHS) == (3, 4)  # written from Python; the app showed them right


def _fake_svc(sent):
    reminder = SimpleNamespace(id="Reminder/ABC", due_date=datetime(2026, 10, 17, 18, 0, tzinfo=timezone.utc),
                               record_change_tag="tag")
    writes = SimpleNamespace(_reminder_record_name=lambda rid: rid,
                             _submit_single_record_update=lambda **kw: sent.update(kw))
    return SimpleNamespace(get=lambda rid: reminder, _writes=writes)


def _decoded_alerts(sent):
    import base64
    import json
    return json.loads(base64.b64decode(sent["fields"]["DueDateDeltaAlertsData"]["value"]))["dueDateDeltaAlerts"]


def test_set_early_writes_minutes_as_unit_zero(monkeypatch):
    monkeypatch.setattr(R, "_account_id", lambda: "acct")
    sent = {}
    R._set_early(_fake_svc(sent), "Reminder/ABC", 15, R._UNIT_MINUTES)
    alerts = _decoded_alerts(sent)
    assert [(a["dueDateDeltaCount"], a["dueDateDeltaUnit"]) for a in alerts] == [(-15, 0)]


def test_set_early_zero_clears(monkeypatch):
    monkeypatch.setattr(R, "_account_id", lambda: "acct")
    sent = {}
    R._set_early(_fake_svc(sent), "Reminder/ABC", 0, R._UNIT_DAYS)
    assert _decoded_alerts(sent) == []


def _item(title, notes=None, due=None):
    return {"title": title, "notes": notes, "due": due}


def test_search_matches_all_words_in_title_or_notes_and_ranks_titles_first():
    items = [_item("Call dentist", "about the bill", "2026-11-02"), _item("Pay bill", None, "2026-12-01"),
             _item("Bill the client", None, "2026-10-30"), _item("Buy milk")]
    assert [r["title"] for r in R.search_items(items, "BILL")] == ["Bill the client", "Pay bill", "Call dentist"]
    assert [r["title"] for r in R.search_items(items, "dentist bill")] == ["Call dentist"]
    assert R.search_items(items, "zebra") == []


def test_search_rejects_empty_text():
    with pytest.raises(ValueError, match="empty"):
        R.search_items([], "   ")
