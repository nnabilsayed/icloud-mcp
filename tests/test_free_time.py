"""find_free_time, check_conflicts and the conflict warning on create_event."""
from datetime import date, datetime, time, timedelta

import pytest

import server as S

NINE, SIX = time(9), time(18)
D = date(2026, 11, 2)  # a Monday


def busy(*spans):
    return [(datetime.fromisoformat(a), datetime.fromisoformat(b), {}) for a, b in spans]


def free(spans, duration=60, buffer=0, now=None, days=(D,), ws=NINE, we=SIX):
    return [(x["start"][11:], x["end"][11:], x["minutes"])
            for x in S._free_slots(busy(*spans), list(days), ws, we, duration, buffer, now)]


# ---- the gap arithmetic --------------------------------------------------
def test_an_empty_day_is_one_big_slot():
    assert free([]) == [("09:00", "18:00", 540)]


def test_gaps_around_events():
    assert free([("2026-11-02T10:00", "2026-11-02T11:00"), ("2026-11-02T13:00", "2026-11-02T14:30")]) == [
        ("09:00", "10:00", 60), ("11:00", "13:00", 120), ("14:30", "18:00", 210)]


def test_short_gaps_are_dropped_by_duration():
    spans = [("2026-11-02T10:00", "2026-11-02T11:00"), ("2026-11-02T11:30", "2026-11-02T12:00")]
    assert [g[0] for g in free(spans, duration=60)] == ["09:00", "12:00"]  # the 30-minute gap is too small
    assert [g[0] for g in free(spans, duration=30)] == ["09:00", "11:00", "12:00"]


def test_buffer_widens_every_event_on_both_sides():
    assert free([("2026-11-02T12:00", "2026-11-02T13:00")], buffer=15) == [("09:00", "11:45", 165), ("13:15", "18:00", 285)]


def test_overlapping_and_adjacent_events_merge():
    spans = [("2026-11-02T10:00", "2026-11-02T12:00"), ("2026-11-02T11:00", "2026-11-02T13:00"), ("2026-11-02T13:00", "2026-11-02T14:00")]
    assert free(spans) == [("09:00", "10:00", 60), ("14:00", "18:00", 240)]


def test_events_outside_working_hours_are_clipped():
    spans = [("2026-11-02T07:00", "2026-11-02T09:30"), ("2026-11-02T17:00", "2026-11-02T20:00")]
    assert free(spans) == [("09:30", "17:00", 450)]
    assert free([("2026-11-01T22:00", "2026-11-03T06:00")]) == []  # busy all of the working day


def test_a_fully_booked_day_has_no_slots():
    assert free([("2026-11-02T08:00", "2026-11-02T19:00")]) == []


def test_today_starts_from_the_next_quarter_hour_and_never_in_the_past():
    assert free([], now=datetime(2026, 11, 2, 10, 7)) == [("10:15", "18:00", 465)]
    assert free([], now=datetime(2026, 11, 2, 7, 0)) == [("09:00", "18:00", 540)]  # before work: normal day
    assert free([], now=datetime(2026, 11, 2, 18, 0)) == []  # work is over
    assert free([], now=datetime(2026, 11, 2, 17, 50)) == []  # rounds up past the end


def test_several_days_each_get_their_own_slots():
    out = S._free_slots(busy(("2026-11-02T09:00", "2026-11-02T17:00")), [D, D + timedelta(days=1)], NINE, SIX, 60, 0)
    assert [(x["start"][:10], x["start"][11:]) for x in out] == [("2026-11-02", "17:00"), ("2026-11-03", "09:00")]


# ---- the tools, against a fake calendar ----------------------------------
def book(cal, summary, start, end=None, **kw):
    return S.create_event("Home", summary, start, end, warn_conflicts=False, **kw)


def test_find_free_time_end_to_end(cal):
    book(cal, "standup", "2026-11-02T10:00:00", "2026-11-02T10:30:00")
    book(cal, "lunch", "2026-11-02T12:00:00", "2026-11-02T13:00:00")
    out = S.find_free_time(duration_minutes=90, start="2026-11-02", days=1)
    assert [(x["start"][11:], x["end"][11:]) for x in out["slots"]] == [("10:30", "12:00"), ("13:00", "18:00")]
    assert out["total_slots"] == 2 and out["from"] == out["to"] == "2026-11-02"


def test_all_day_events_only_block_when_asked(cal):
    book(cal, "birthday", "2026-11-02")
    assert S.find_free_time(start="2026-11-02", days=1)["total_slots"] == 1
    assert S.find_free_time(start="2026-11-02", days=1, all_day_blocks=True)["total_slots"] == 0


def test_weekday_filter_and_limit(cal):
    out = S.find_free_time(start="2026-11-02", days=7, weekdays=["MO", "WE"], limit=1)
    assert out["total_slots"] == 2 and len(out["slots"]) == 1 and out["slots"][0]["start"].startswith("2026-11-02")


@pytest.mark.parametrize("kwargs,msg", [
    (dict(duration_minutes=0), "duration_minutes"),
    (dict(days=0), "days"),
    (dict(days=61), "days"),
    (dict(work_start="18:00", work_end="09:00"), "after"),
    (dict(work_start="nine"), "HH:MM"),
    (dict(weekdays=["XX"]), "weekdays"),
    (dict(buffer_minutes=-1), "buffer"),
    (dict(calendars=["Nope"]), "event calendars"),
])
def test_find_free_time_rejects_bad_input(cal, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        S.find_free_time(start="2026-11-02", **kwargs)


def test_check_conflicts(cal):
    book(cal, "meeting", "2026-11-02T10:00:00", "2026-11-02T11:00:00")
    book(cal, "birthday", "2026-11-02")
    hit = S.check_conflicts("2026-11-02T10:30:00", "2026-11-02T11:30:00")
    assert not hit["free"] and {c["summary"] for c in hit["conflicts"]} == {"meeting", "birthday"}
    assert [c["all_day"] for c in hit["conflicts"] if c["summary"] == "birthday"] == [True]
    assert S.check_conflicts("2026-11-02T11:00:00", "2026-11-02T12:00:00")["conflicts"][0]["summary"] == "birthday"  # back-to-back isn't a clash
    assert S.check_conflicts("2026-11-03T10:00:00")["free"]
    assert len(S.check_conflicts("2026-11-02")["conflicts"]) == 2  # a whole day
    with pytest.raises(ValueError):
        S.check_conflicts("2026-11-02T10:00:00", "2026-11-02T09:00:00")


def test_create_event_warns_but_still_creates(cal):
    book(cal, "meeting", "2026-11-02T10:00:00", "2026-11-02T11:00:00")
    ev = S.create_event("Home", "clash", "2026-11-02T10:30:00", "2026-11-02T11:30:00")
    assert [c["summary"] for c in ev["conflicts"]] == ["meeting"] and len(cal.items) == 2  # created anyway
    ok = S.create_event("Home", "fine", "2026-11-02T12:00:00", "2026-11-02T13:00:00")
    assert ok["conflicts"] == []
    quiet = S.create_event("Home", "ignored", "2026-11-02T10:30:00", warn_conflicts=False)
    assert quiet["conflicts"] is None
    allday = S.create_event("Home", "all day", "2026-11-02")
    assert allday["conflicts"] is None  # not checked for all-day events


def test_a_failing_conflict_check_never_blocks_the_create(cal, monkeypatch):
    monkeypatch.setattr(S, "_busy_intervals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ev = S.create_event("Home", "still created", "2026-11-02T10:00:00")
    assert ev["conflicts"] is None and len(cal.items) == 1
