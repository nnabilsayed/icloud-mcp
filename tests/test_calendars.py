"""Creating, renaming and deleting calendars."""
import pytest
from caldav.elements import dav, ical as ical_elements

import activity
import server as S
from conftest import FakeCalendar


class AcctCalendar(FakeCalendar):
    def __init__(self, acct, name):
        super().__init__(name)
        self.acct = acct
        self.color = None

    def set_properties(self, props):
        for p in props:
            if isinstance(p, dav.DisplayName):
                self.name = p.value
            elif isinstance(p, ical_elements.CalendarColor):
                self.color = p.value

    def delete(self):
        self.acct.cals.remove(self)

    def get_supported_components(self):
        return ["VEVENT"]


class FakeClient:
    """Just the WebDAV MOVE iCloud supports; a uid that already exists in the target is a 412."""

    def __init__(self, acct):
        self.acct = acct
        self.requests = []

    def request(self, url, method, body="", headers=None):
        from types import SimpleNamespace

        self.requests.append((method, url, headers))
        assert method == "MOVE"
        src_cal = next(c for c in self.acct.cals for e in c.items if e.url == url)
        ev = next(e for e in src_cal.items if e.url == url)
        dest = (headers or {})["Destination"]
        dst_cal = next(c for c in self.acct.cals if dest.startswith(c.url))
        if any(str(e.comp["uid"]) == str(ev.comp["uid"]) for e in dst_cal.items):
            return SimpleNamespace(status=412)
        src_cal.items.remove(ev)
        ev.cal = dst_cal
        dst_cal.items.append(ev)
        return SimpleNamespace(status=201)


class FakeAccount:
    def __init__(self, *names):
        self.cals = [AcctCalendar(self, n) for n in names]
        self.client = FakeClient(self)
        for c in self.cals:
            c.client = self.client
        self.lose_reply = 0

    def calendars(self):
        return list(self.cals)

    def make_calendar(self, name=None, supported_calendar_component_set=None):
        cal = AcctCalendar(self, name)
        cal.client = self.client
        self.cals.append(cal)
        if self.lose_reply:
            self.lose_reply -= 1
            raise S.niquests.exceptions.ConnectionError("reply lost")
        return cal


@pytest.fixture
def acct(monkeypatch):
    fake = FakeAccount("Home", "Work")
    monkeypatch.setattr(S, "_principal", lambda: fake)
    monkeypatch.setattr(S, "_event_calendars", None)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    return fake


def names(acct):
    return [c.name for c in acct.cals]


# ---- names and colours ---------------------------------------------------
def test_calendar_names_are_trimmed_unique_and_not_empty(acct):
    cals = acct.cals
    assert S._check_calendar_name("  Trips ", cals) == "Trips"
    with pytest.raises(ValueError, match="needs a name"):
        S._check_calendar_name(" ", cals)
    with pytest.raises(ValueError, match="too long"):
        S._check_calendar_name("x" * 201, cals)
    with pytest.raises(ValueError, match="already exists"):
        S._check_calendar_name("home", cals)
    assert S._check_calendar_name("Home", cals, ignore=cals[0]) == "Home"  # renaming to itself is fine


@pytest.mark.parametrize("given,stored", [("#ff3b30", "#FF3B30FF"), ("#FF3B3080", "#FF3B3080")])
def test_colors_are_normalised(given, stored):
    assert S._normalize_color(given) == stored


@pytest.mark.parametrize("bad", ["red", "#FFF", "#GG0000", "FF3B30", "#FF3B30F"])
def test_bad_colors_are_rejected(bad):
    with pytest.raises(ValueError, match="color must look like"):
        S._normalize_color(bad)


# ---- create --------------------------------------------------------------
def test_create_calendar_with_colour_and_log(acct):
    out = S.create_calendar("Trips", color="#34C759")
    assert out["name"] == "Trips" and out["color"] == "#34C759FF" and "Trips" in names(acct)
    assert next(c for c in acct.cals if c.name == "Trips").color == "#34C759FF"
    assert "Trips" in S.list_calendars()  # the cached calendar list was refreshed
    assert activity.get(out["log_id"])["undo"] == {"op": "delete_calendar", "name": "Trips"}
    with pytest.raises(ValueError, match="already exists"):
        S.create_calendar("work")
    with pytest.raises(ValueError, match="color"):
        S.create_calendar("Other", color="blue")
    assert names(acct) == ["Home", "Work", "Trips"]  # the rejected calls created nothing


def test_a_lost_reply_does_not_create_a_second_calendar(acct):
    acct.lose_reply = 1  # the calendar is created, then the connection drops
    out = S.create_calendar("Trips")
    assert out["name"] == "Trips" and names(acct).count("Trips") == 1


# ---- rename --------------------------------------------------------------
def test_rename_calendar_and_undo(acct):
    out = S.rename_calendar("Work", "Office")
    assert names(acct) == ["Home", "Office"] and out["changed"] is True
    assert activity.get(out["log_id"])["undo"] == {"op": "rename_calendar", "name": "Office", "old": "Work"}
    assert S.rename_calendar("Office", "office")["changed"] in (True, False)  # same name apart from case
    S.undo(out["log_id"])
    assert names(acct) == ["Home", "Work"]


def test_rename_refuses_duplicates_and_unknown_calendars(acct):
    with pytest.raises(ValueError, match="already exists"):
        S.rename_calendar("Work", "Home")
    with pytest.raises(ValueError, match="not found"):
        S.rename_calendar("Nope", "x")
    assert names(acct) == ["Home", "Work"]


# ---- delete --------------------------------------------------------------
def book(acct, calendar, summary, start="2026-11-05T10:00:00"):
    cal = next(c for c in acct.cals if c.name == calendar)
    S.create_event(calendar, summary, start, warn_conflicts=False)
    return cal


def test_delete_refuses_a_calendar_with_events(acct):
    book(acct, "Work", "standup")
    with pytest.raises(ValueError, match="still holds 1 event"):
        S.delete_calendar("Work")
    assert names(acct) == ["Home", "Work"]


def test_delete_empty_calendar_and_undo(acct):
    out = S.delete_calendar("Work")
    assert names(acct) == ["Home"] and out["deleted_events"] == 0
    S.undo(out["log_id"])
    assert names(acct) == ["Home", "Work"]


def test_delete_with_events_and_undo_restores_them(acct):
    book(acct, "Work", "standup")
    book(acct, "Work", "review", "2026-11-06T14:00:00")
    uids = sorted(str(e.comp["uid"]) for e in acct.cals[1].items)
    out = S.delete_calendar("Work", delete_events=True)
    assert names(acct) == ["Home"] and out["deleted_events"] == 2
    row = activity.get(out["log_id"])
    assert row["undo"]["op"] == "recreate_calendar" and len(row["undo"]["events"]) == 2 and "2 event" in row["summary"]
    S.undo(out["log_id"])
    back = next(c for c in acct.cals if c.name == "Work")
    assert sorted(str(e.comp["uid"]) for e in back.items) == uids  # same events, same uids


def test_too_many_events_to_keep_for_undo(acct, monkeypatch):
    book(acct, "Work", "one")
    monkeypatch.setattr(S, "MAX_UNDOABLE_EVENTS", 0)
    with pytest.raises(ValueError, match="kept for undo"):
        S.delete_calendar("Work", delete_events=True)
    assert names(acct) == ["Home", "Work"]


def test_undo_of_a_create_only_removes_an_empty_calendar(acct):
    out = S.create_calendar("Trips")
    book(acct, "Trips", "ski")
    result = S.undo(out["log_id"])
    assert "now holds events" in result["failed"]["error"] and "Trips" in names(acct)
    acct.cals[-1].items.clear()
    S.undo(out["log_id"])
    assert "Trips" not in names(acct)


def test_the_legacy_reminders_calendar_is_never_touched(acct, monkeypatch):
    legacy = AcctCalendar(acct, "Reminders ⚠️")
    legacy.get_supported_components = lambda: ["VTODO"]
    acct.cals.append(legacy)
    for call in (lambda: S.rename_calendar("Reminders ⚠️", "x"), lambda: S.delete_calendar("Reminders ⚠️")):
        with pytest.raises(ValueError, match="not found"):
            call()
