"""get_event, search_events and the direct uid lookup."""
import pytest

import server as S
from caldav.lib.error import NotFoundError
from test_calendars import AcctCalendar, FakeAccount  # noqa: F401  (fixtures below reuse the fake account)


def book(calendar, summary, start, end=None, **kw):
    return S.create_event(calendar, summary, start, end, warn_conflicts=False, **kw)


# ---- _find ---------------------------------------------------------------
def test_find_fetches_directly_and_does_not_scan(cal, monkeypatch):
    ev = book("Home", "x", "2030-01-05T10:00:00")
    target, scans = cal.items[0], {"n": 0}
    monkeypatch.setattr(S, "_load_direct", lambda c, uid: target)
    cal.events = lambda: scans.__setitem__("n", scans["n"] + 1) or []
    assert S._find(cal, ev["uid"]) is target and scans["n"] == 0


@pytest.mark.parametrize("direct", [lambda c, u: None, lambda c, u: (_ for _ in ()).throw(RuntimeError("412 Precondition Failed"))])
def test_find_falls_back_to_a_scan_when_the_direct_fetch_finds_nothing_or_fails(cal, monkeypatch, direct):
    ev = book("Home", "x", "2030-01-05T10:00:00")
    monkeypatch.setattr(S, "_load_direct", direct)
    assert str(S._find(cal, ev["uid"]).comp["uid"]) == ev["uid"]  # found by scanning
    with pytest.raises(ValueError, match="not found"):
        S._find(cal, "no-such-uid")


def test_find_works_on_a_calendar_that_cannot_fetch_directly(cal):
    ev = book("Home", "x", "2030-01-05T10:00:00")  # the fake has no client or url, like a failing server
    assert str(S._find(cal, ev["uid"]).comp["uid"]) == ev["uid"]


def test_direct_fetch_checks_the_uid_matches(monkeypatch):
    from types import SimpleNamespace

    class Ev:
        icalendar_component = {"uid": "other"}

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def load(self):
            pass

    monkeypatch.setattr(S.caldav, "Event", Ev)
    cal = SimpleNamespace(client=object(), url=SimpleNamespace(join=lambda part: "http://x/" + part))
    assert S._load_direct(cal, "abc") is None  # the file at that address holds a different event
    Ev.icalendar_component = {"uid": "abc"}
    found = S._load_direct(cal, "abc")
    assert found.kwargs["url"] == "http://x/abc.ics"  # the event's own address: <uid>.ics


# ---- get_event -----------------------------------------------------------
@pytest.fixture
def acct(monkeypatch):
    fake = FakeAccount("Home", "Work")
    monkeypatch.setattr(S, "_principal", lambda: fake)
    monkeypatch.setattr(S, "_event_calendars", None)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    return fake


def test_get_event_by_uid_in_a_named_calendar_or_any(acct):
    ev = book("Work", "Quarterly review", "2030-03-01T09:00:00", location="Room 4", alerts=[30], repeat="weekly", repeat_count=3)
    got = S.get_event(ev["uid"], calendar="Work")
    assert got["calendar"] == "Work" and got["summary"] == "Quarterly review" and got["location"] == "Room 4"
    assert got["alerts"] == [30] and got["repeat"]["count"] == 3
    assert S.get_event(ev["uid"])["calendar"] == "Work"  # no calendar given: searched everywhere
    with pytest.raises(ValueError, match="not found in 'Home'"):
        S.get_event(ev["uid"], calendar="Home")
    with pytest.raises(ValueError, match="in any calendar"):
        S.get_event("nope")


# ---- search_events -------------------------------------------------------
def test_search_matches_title_notes_and_location_ignoring_case(acct):
    book("Home", "Dentist appointment", "2099-01-05T10:00:00")
    book("Work", "Team lunch", "2099-01-06T12:00:00", description="Book a table near the DENTIST's office")
    book("Home", "Gym", "2099-01-07T18:00:00", location="Dentist Plaza")
    book("Home", "Unrelated", "2099-01-08T18:00:00")
    out = S.search_events("dEnTiSt")
    assert out["count"] == 3 and [e["summary"] for e in out["events"]] == ["Dentist appointment", "Team lunch", "Gym"]
    assert {e["calendar"] for e in out["events"]} == {"Home", "Work"}
    assert [e["summary"] for e in S.search_events("dentist", calendar="Work")["events"]] == ["Team lunch"]


def test_past_events_are_hidden_unless_asked_for(acct):
    book("Home", "old dentist visit", "2020-01-05T10:00:00")
    book("Home", "next dentist visit", "2099-01-05T10:00:00")
    book("Home", "yearly dentist check", "2020-06-01T10:00:00", repeat="yearly")  # a repeating event isn't over
    assert sorted(e["summary"] for e in S.search_events("dentist")["events"]) == ["next dentist visit", "yearly dentist check"]
    assert S.search_events("dentist", include_past=True)["count"] == 3


def test_limit_and_count(acct):
    for i in range(5):
        book("Home", f"standup {i}", f"2099-02-0{i + 1}T09:00:00")
    out = S.search_events("standup", limit=2)
    assert out["count"] == 5 and [e["summary"] for e in out["events"]] == ["standup 0", "standup 1"]


def test_a_date_range_limits_the_search(acct):
    book("Home", "trip a", "2099-03-05T09:00:00")
    book("Home", "trip b", "2099-04-05T09:00:00")
    out = S.search_events("trip", start="2099-03-01", end="2099-03-31")
    assert [e["summary"] for e in out["events"]] == ["trip a"]
    old = book("Home", "trip old", "2001-03-05T09:00:00")
    assert [e["summary"] for e in S.search_events("trip old", start="2001-03-01", end="2001-03-31")["events"]] == ["trip old"]  # past is fine with a range


def test_search_rejects_bad_input(acct):
    for kwargs, msg in ((dict(text="  "), "must not be empty"), (dict(text="x", limit=0), "limit"),
                        (dict(text="x", start="2099-03-05", end="2099-03-01"), "after start"),
                        (dict(text="x", calendar="Nope"), "not found")):
        with pytest.raises(ValueError, match=msg):
            S.search_events(**kwargs)
