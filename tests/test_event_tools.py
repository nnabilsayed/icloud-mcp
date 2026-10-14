"""Event URL/categories/status/priority, move and duplicate, calendar colour and details."""
import pytest

import activity
import server as S
from test_calendars import FakeAccount


@pytest.fixture
def acct(monkeypatch):
    fake = FakeAccount("Home", "Work")
    monkeypatch.setattr(S, "_principal", lambda: fake)
    monkeypatch.setattr(S, "_event_calendars", None)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    return fake


def book(calendar, summary, start="2030-03-01T10:00:00", end=None, **kw):
    return S.create_event(calendar, summary, start, end, warn_conflicts=False, **kw)


def items(acct, name):
    return next(c for c in acct.cals if c.name == name).items


# ---- extra fields --------------------------------------------------------
def test_create_with_url_categories_status_and_priority(acct):
    ev = book("Home", "Launch", url="https://example.com/launch", categories=["work", " travel "], status="confirmed", priority=2)
    assert (ev["url"], ev["categories"], ev["status"], ev["priority"]) == ("https://example.com/launch", ["work", "travel"], "CONFIRMED", 2)
    plain = book("Home", "Plain")
    assert (plain["url"], plain["categories"], plain["status"], plain["priority"]) == ("", [], "", 0)


def test_update_sets_clears_and_leaves_alone(acct):
    ev = book("Home", "x", url="https://a.example", categories=["a"], status="TENTATIVE", priority=5)
    up = S.update_event("Home", ev["uid"], summary="renamed")  # nothing about the extras: untouched
    assert (up["url"], up["categories"], up["status"], up["priority"]) == ("https://a.example", ["a"], "TENTATIVE", 5)
    up = S.update_event("Home", ev["uid"], url="https://b.example", categories=["b", "c"], status="CANCELLED", priority=1)
    assert (up["url"], up["categories"], up["status"], up["priority"]) == ("https://b.example", ["b", "c"], "CANCELLED", 1)
    up = S.update_event("Home", ev["uid"], url="", categories=[], status="", priority=0)
    assert (up["url"], up["categories"], up["status"], up["priority"]) == ("", [], "", 0)


@pytest.mark.parametrize("kwargs,msg", [
    (dict(url="not a link"), "doesn't look like a link"),
    (dict(status="maybe"), "status must be"),
    (dict(priority=10), "priority must be"),
    (dict(priority=-1), "priority must be"),
])
def test_bad_extras_are_rejected(acct, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        book("Home", "x", **kwargs)
    ev = book("Home", "ok")
    with pytest.raises(ValueError, match=msg):
        S.update_event("Home", ev["uid"], **kwargs)


def test_extras_are_searchable_and_undoable(acct):
    ev = book("Home", "Offsite", categories=["Travel"])
    assert [e["summary"] for e in S.search_events("travel")["events"]] == ["Offsite"]
    up = S.update_event("Home", ev["uid"], url="https://x.example", categories=[], priority=3)
    S.undo(up["log_id"])
    back = S._summarize(items(acct, "Home")[0])
    assert (back["url"], back["categories"], back["priority"]) == ("", ["Travel"], 0)


# ---- move ----------------------------------------------------------------
def test_move_event_between_calendars_and_back(acct):
    ev = book("Home", "Dentist", location="Cairo", alerts=[30])
    out = S.move_event(ev["uid"], "Work")
    assert (out["from"], out["to"]) == ("Home", "Work") and items(acct, "Home") == []
    moved = S._summarize(items(acct, "Work")[0])
    assert moved["uid"] == ev["uid"] and moved["location"] == "Cairo" and moved["alerts"] == [30]  # same event, same details
    assert activity.get(out["log_id"])["undo"] == {"op": "move_event", "uid": ev["uid"], "from": "Work", "to": "Home"}
    S.undo(out["log_id"])
    assert len(items(acct, "Home")) == 1 and items(acct, "Work") == []


def test_move_refuses_the_same_calendar_and_unknown_ones(acct):
    ev = book("Home", "x")
    with pytest.raises(ValueError, match="already in 'Home'"):
        S.move_event(ev["uid"], "Home")
    with pytest.raises(ValueError, match="not found"):
        S.move_event(ev["uid"], "Nope")
    with pytest.raises(ValueError, match="not found"):
        S.move_event("no-such-uid", "Work")
    assert len(items(acct, "Home")) == 1


def test_move_uses_one_atomic_move_request_never_copy_then_delete(acct):
    ev = book("Home", "x")
    S.move_event(ev["uid"], "Work")
    (method, url, headers), = acct.client.requests
    assert method == "MOVE" and url.startswith("http://fake/home/") and headers["Destination"].startswith("http://fake/work/")
    assert headers["Overwrite"] == "F"  # never overwrite something already there


def test_a_retried_move_after_a_lost_reply_is_a_no_op(acct):
    """The first attempt moved the event but its reply was lost; the retry finds it already in Work."""
    ev = book("Home", "x")
    S._move_event_impl(ev["uid"], "Home", "Work")
    assert S._move_event_impl(ev["uid"], "Home", "Work") == "x"
    assert len(items(acct, "Work")) == 1 and items(acct, "Home") == []
    with pytest.raises(ValueError, match="not found in 'Home'"):
        S._move_event_impl("never-existed", "Home", "Work")


def test_a_refused_move_leaves_the_event_where_it_was(acct):
    ev = book("Home", "x")
    acct.cals[1].save_event(items(acct, "Home")[0].data)  # same uid already in Work: iCloud answers 412
    with pytest.raises(RuntimeError, match="HTTP 412"):
        S._move_event_impl(ev["uid"], "Home", "Work")
    assert len(items(acct, "Home")) == 1


def test_a_repeating_event_moves_as_a_series(acct):
    ev = book("Home", "Standup", repeat="weekly", repeat_count=4)
    S.move_event(ev["uid"], "Work")
    assert S._summarize(items(acct, "Work")[0])["repeat"]["count"] == 4


# ---- duplicate -----------------------------------------------------------
def test_duplicate_gets_a_new_uid_and_leaves_the_original_alone(acct):
    ev = book("Home", "Standup", "2030-03-01T09:00:00", "2030-03-01T09:30:00", location="Room 1", alerts=[10],
              url="https://x.example", categories=["work"], priority=4)
    dup = S.duplicate_event(ev["uid"])
    assert dup["uid"] != ev["uid"] and dup["calendar"] == "Home" and len(items(acct, "Home")) == 2
    assert (dup["summary"], dup["start"], dup["end"], dup["location"], dup["alerts"], dup["url"], dup["categories"], dup["priority"]) == \
           ("Standup", "2030-03-01T09:00:00", "2030-03-01T09:30:00", "Room 1", [10], "https://x.example", ["work"], 4)
    assert S._summarize(items(acct, "Home")[0])["uid"] == ev["uid"]


def test_duplicate_to_another_calendar_at_a_new_time_keeps_the_length(acct):
    ev = book("Home", "Meeting", "2030-03-01T09:00:00", "2030-03-01T10:30:00")
    dup = S.duplicate_event(ev["uid"], to_calendar="Work", start="2030-03-08T14:00:00", title="Meeting (next week)")
    assert (dup["calendar"], dup["summary"], dup["start"], dup["end"]) == ("Work", "Meeting (next week)", "2030-03-08T14:00:00", "2030-03-08T15:30:00")
    assert len(items(acct, "Home")) == 1 and len(items(acct, "Work")) == 1


def test_duplicate_all_day_and_repeating_events(acct):
    trip = book("Home", "Trip", "2030-05-01", "2030-05-03")
    dup = S.duplicate_event(trip["uid"], start="2030-06-10")
    assert (dup["start"], dup["end"], dup["all_day"]) == ("2030-06-10", "2030-06-12", True)
    series = book("Home", "Gym", repeat="weekly", repeat_count=3, alerts=[15])
    copy = S.duplicate_event(series["uid"], to_calendar="Work")
    assert copy["repeat"]["count"] == 3 and copy["alerts"] == [15] and copy["recurring"] is True


def test_duplicate_is_logged_and_undoable(acct):
    ev = book("Home", "x")
    dup = S.duplicate_event(ev["uid"], to_calendar="Work")
    assert activity.get(dup["log_id"])["undo"] == {"op": "delete_event", "calendar": "Work", "uid": dup["uid"]}
    S.undo(dup["log_id"])
    assert items(acct, "Work") == [] and len(items(acct, "Home")) == 1


def test_duplicate_errors(acct):
    with pytest.raises(ValueError, match="not found"):
        S.duplicate_event("nope")
    ev = book("Home", "x")
    with pytest.raises(ValueError, match="not found"):
        S.duplicate_event(ev["uid"], to_calendar="Nope")
    with pytest.raises(ValueError, match="all-day"):
        S.duplicate_event(book("Home", "day", "2030-05-01")["uid"], start="2030-06-01", end="2030-06-02T10:00:00")
    assert len(items(acct, "Home")) == 2  # a rejected duplicate created nothing


# ---- calendar details ----------------------------------------------------
HOME_XML = """<multistatus xmlns="DAV:"><response><href>/c/home/</href><propstat><prop>
<displayname>Home</displayname>
<resourcetype><collection/><calendar xmlns="urn:ietf:params:xml:ns:caldav"/></resourcetype>
<calendar-color xmlns="http://apple.com/ns/ical/">#34AADCFF</calendar-color>
<supported-calendar-component-set xmlns="urn:ietf:params:xml:ns:caldav"><comp name="VEVENT"/></supported-calendar-component-set>
<current-user-privilege-set><privilege><read/></privilege><privilege><write/></privilege></current-user-privilege-set>
</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"""

SUBSCRIBED_XML = """<multistatus xmlns="DAV:"><response><href>/c/hol/</href><propstat><prop>
<displayname>Holidays</displayname>
<resourcetype><collection/><calendar xmlns="urn:ietf:params:xml:ns:caldav"/><subscribed xmlns="http://calendarserver.org/ns/"/></resourcetype>
<source xmlns="http://calendarserver.org/ns/"><href xmlns="DAV:">webcal://example.com/holidays.ics</href></source>
<supported-calendar-component-set xmlns="urn:ietf:params:xml:ns:caldav"><comp name="VEVENT"/></supported-calendar-component-set>
<current-user-privilege-set><privilege><read/></privilege></current-user-privilege-set>
</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"""

LEGACY_XML = HOME_XML.replace("Home", "Reminders").replace('name="VEVENT"', 'name="VTODO"')


def test_parsing_a_normal_calendar():
    d = S._describe_calendar("Home", S._parse_calendar_props(HOME_XML))
    assert d == {"name": "Home", "color": "#34AADCFF", "kind": "events", "writable": True, "subscribed": False}


def test_a_subscribed_read_only_calendar_is_flagged():
    d = S._describe_calendar("Holidays", S._parse_calendar_props(SUBSCRIBED_XML))
    assert d["subscribed"] is True and d["writable"] is False and d["source"] == "webcal://example.com/holidays.ics"


def test_the_legacy_reminders_list_is_labelled():
    d = S._describe_calendar("Reminders", S._parse_calendar_props(LEGACY_XML))
    assert d["kind"].startswith("reminders") and d["writable"] is True


def test_list_calendar_details_lists_everything_and_survives_a_failing_calendar(acct, monkeypatch):
    docs = {"Home": HOME_XML, "Work": None}
    monkeypatch.setattr(S, "_propfind_raw", lambda cal: docs[cal.name] if docs[cal.name] else (_ for _ in ()).throw(RuntimeError("boom")))
    rows = S.list_calendar_details()
    assert [r["name"] for r in rows] == ["Home", "Work"] and rows[0]["color"] == "#34AADCFF"
    assert rows[1]["color"] is None and rows[1]["kind"] == "unknown"  # one bad calendar doesn't hide the rest


# ---- colour --------------------------------------------------------------
def test_set_calendar_color_logs_the_previous_colour_and_undoes(acct, monkeypatch):
    cal = acct.cals[0]
    cal.color = "#34AADCFF"
    monkeypatch.setattr(S, "_calendar_color", lambda c: c.color)
    out = S.set_calendar_color("Home", "#ff3b30")
    assert (out["color"], out["previous"]) == ("#FF3B30FF", "#34AADCFF") and cal.color == "#FF3B30FF"
    assert activity.get(out["log_id"])["undo"] == {"op": "set_calendar_color", "name": "Home", "color": "#34AADCFF"}
    S.undo(out["log_id"])
    assert cal.color == "#34AADCFF"
    with pytest.raises(ValueError, match="color must look like"):
        S.set_calendar_color("Home", "red")


def test_a_colour_change_without_a_known_previous_colour_is_not_undoable(acct, monkeypatch):
    monkeypatch.setattr(S, "_calendar_color", lambda c: None)
    out = S.set_calendar_color("Work", "#34C759")
    assert activity.get(out["log_id"])["undo"] is None
