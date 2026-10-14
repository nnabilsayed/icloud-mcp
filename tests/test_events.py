from datetime import date, datetime, timedelta, timezone

import pytest

import server as S


def make(cal, summary="x", start="2026-11-05", **kw):
    return S.create_event("Home", summary, start, **kw)


def raw(cal, uid):
    return next(e.comp for e in cal.items if str(e.comp["uid"]) == uid)


# ---- all-day -------------------------------------------------------------
def test_parse_when_date_vs_datetime():
    assert S._parse_when("2026-11-05") == date(2026, 11, 5)
    assert S._parse_when("2026-11-05T10:00:00") == datetime(2026, 11, 5, 10)


def test_all_day_one_day_stores_exclusive_end(cal):
    ev = make(cal)
    comp = raw(cal, ev["uid"])
    assert comp["dtstart"].dt == date(2026, 11, 5)
    assert comp["dtend"].dt == date(2026, 11, 6)  # iCalendar DTEND is exclusive
    assert (ev["start"], ev["end"], ev["all_day"]) == ("2026-11-05", "2026-11-05", True)


def test_all_day_multi_day_end_is_inclusive_in_and_out(cal):
    ev = make(cal, start="2026-11-06", end="2026-11-08")
    assert raw(cal, ev["uid"])["dtend"].dt == date(2026, 11, 9)
    assert ev["end"] == "2026-11-08"


def test_all_day_rejects_bad_ends(cal):
    with pytest.raises(ValueError, match="before start"):
        make(cal, start="2026-11-05", end="2026-11-04")
    with pytest.raises(ValueError, match="all-day"):
        make(cal, start="2026-11-05", end="2026-11-06T10:00:00")


def test_timed_defaults_to_one_hour_and_rejects_date_end(cal):
    ev = make(cal, start="2026-11-05T10:00:00")
    assert (ev["start"], ev["end"], ev["all_day"]) == ("2026-11-05T10:00:00", "2026-11-05T11:00:00", False)
    with pytest.raises(ValueError, match="timed"):
        make(cal, start="2026-11-05T10:00:00", end="2026-11-06")


def test_update_converts_between_timed_and_all_day_and_extends(cal):
    ev = make(cal, start="2026-11-05T10:00:00")
    up = S.update_event("Home", ev["uid"], start="2026-11-05")
    assert (up["start"], up["end"], up["all_day"]) == ("2026-11-05", "2026-11-05", True)
    up = S.update_event("Home", ev["uid"], end="2026-11-07")
    assert up["end"] == "2026-11-07"
    up = S.update_event("Home", ev["uid"], start="2026-11-05T09:00:00")
    assert (up["start"], up["end"], up["all_day"]) == ("2026-11-05T09:00:00", "2026-11-05T10:00:00", False)


def test_update_rejects_mixed_kinds(cal):
    ev = make(cal)
    with pytest.raises(ValueError):
        S.update_event("Home", ev["uid"], end="2026-11-06T10:00:00")


# ---- repeat --------------------------------------------------------------
def test_rrule_weekly_days_and_count(cal):
    ev = make(cal, start="2026-11-02T09:00:00", repeat="weekly", repeat_days=["mo", "WE"], repeat_count=4)
    assert ev["repeat"] == {"frequency": "weekly", "interval": 1, "days": ["MO", "WE"], "count": 4}
    assert ev["recurring"] is True


@pytest.mark.parametrize("start,kind", [
    (date(2026, 11, 10), date),
    (datetime(2026, 11, 10, 9), datetime),
    (datetime(2026, 11, 10, 9, tzinfo=timezone(timedelta(hours=3))), datetime),
])
def test_rrule_until_matches_start_kind(start, kind):
    rule = S._build_rrule(start, "monthly", 1, 0, "2027-02-28", None)
    assert isinstance(rule["until"], kind)
    if kind is datetime and start.tzinfo:
        assert rule["until"].tzinfo == timezone.utc  # zone-aware starts need a UTC UNTIL


@pytest.mark.parametrize("kwargs,msg", [
    (dict(repeat="hourly"), "repeat must be"),
    (dict(repeat="daily", repeat_count=2, repeat_until="2026-12-01"), "not both"),
    (dict(repeat="daily", repeat_days=["MO"]), "weekly"),
    (dict(repeat="daily", repeat_interval=0), "at least 1"),
])
def test_rrule_rejects_bad_input(cal, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        make(cal, start="2026-11-03T10:00:00", **kwargs)


def test_update_repeat_replace_and_remove(cal):
    ev = make(cal, start="2026-11-03T18:00:00", repeat="daily", repeat_interval=2)
    up = S.update_event("Home", ev["uid"], repeat="weekly", repeat_count=3)
    assert up["repeat"] == {"frequency": "weekly", "interval": 1, "count": 3}
    assert S.update_event("Home", ev["uid"], summary="renamed")["repeat"] is not None  # omitted = untouched
    up = S.update_event("Home", ev["uid"], repeat="none")
    assert up["repeat"] is None and up["recurring"] is False


# ---- alerts --------------------------------------------------------------
def test_alerts_sorted_deduped_and_persisted(cal):
    ev = make(cal, start="2026-11-12T15:00:00", alerts=[30, 1440, 30])
    assert ev["alerts"] == [1440, 30]
    assert S._summarize(S._find(cal, ev["uid"]))["alerts"] == [1440, 30]


def test_alerts_update_replace_keep_clear(cal):
    ev = make(cal, start="2026-11-12T15:00:00", alerts=[30])
    assert S.update_event("Home", ev["uid"], alerts=[5])["alerts"] == [5]
    assert S.update_event("Home", ev["uid"], summary="renamed")["alerts"] == [5]
    assert S.update_event("Home", ev["uid"], alerts=[])["alerts"] == []


def test_alerts_reject_negative_and_default_to_none(cal):
    with pytest.raises(ValueError, match="BEFORE"):
        make(cal, start="2026-11-12T15:00:00", alerts=[-5])
    assert make(cal, start="2026-11-12T15:00:00")["alerts"] == []


def test_all_day_alert_counts_from_midnight(cal):
    ev = make(cal, start="2026-11-13", alerts=[900])
    assert ev["alerts"] == [900]


# ---- range matching ------------------------------------------------------
def test_search_range_keeps_late_floating_event_in_its_local_day(cal):
    make(cal, "late", start="2026-10-14T23:30:00")
    day = (datetime(2026, 10, 14), datetime(2026, 10, 15))
    assert [S._summarize(e)["summary"] for e in S._search_range(cal, *day)] == ["late"]


def test_search_range_excludes_outside_and_includes_overlapping_multi_day(cal):
    make(cal, "outside", start="2026-10-20T10:00:00")
    make(cal, "span", start="2026-10-12", end="2026-10-15")
    got = [S._summarize(e)["summary"] for e in S._search_range(cal, datetime(2026, 10, 14), datetime(2026, 10, 15))]
    assert got == ["span"]


# ---- retry ---------------------------------------------------------------
def test_retry_recovers_then_gives_up(cal):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise S.niquests.exceptions.ConnectionError("drop")
        return "ok"

    assert S._with_retry(flaky) == "ok" and calls["n"] == 3
    with pytest.raises(S.niquests.exceptions.ConnectionError):
        S._with_retry(lambda: (_ for _ in ()).throw(S.niquests.exceptions.ConnectionError("down")))


def test_retry_does_not_swallow_real_errors(cal):
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ValueError("not a network problem")

    with pytest.raises(ValueError):
        S._with_retry(bad)
    assert calls["n"] == 1


def test_create_does_not_duplicate_when_reply_is_lost(cal):
    cal.fail_after_save = 1  # event is stored, then the connection "drops"
    ev = make(cal, "once")
    assert ev["summary"] == "once"
    assert len(cal.items) == 1


# ---- agenda --------------------------------------------------------------
def test_agenda_groups_events_due_and_overdue(cal, monkeypatch):
    import reminders
    make(cal, "today ev", start="2026-10-14T09:00:00")
    monkeypatch.setattr(reminders, "open_reminders", lambda: [
        {"title": "due", "due": "2026-10-14", "completed": False},
        {"title": "late", "due": "2026-10-10", "completed": False},
        {"title": "undated", "due": None, "completed": False},
        {"title": "later", "due": "2026-10-20", "completed": False},
    ])
    a = S.agenda(start="2026-10-14")
    assert [e["summary"] for e in a["events"]] == ["today ev"]
    assert [r["title"] for r in a["reminders"]] == ["due"]
    assert [r["title"] for r in a["overdue"]] == ["late"]
    assert S.agenda(start="2026-10-14", include_overdue=False)["overdue"] == []


def test_agenda_rejects_bad_days(cal):
    for days in (0, 400):
        with pytest.raises(ValueError):
            S.agenda(days=days)


def test_search_range_boundaries_are_half_open(cal):
    """An event ending exactly when the window opens, or starting exactly when it closes, is outside it."""
    make(cal, "ends at open", start="2026-10-14T09:00:00", end="2026-10-14T10:00:00")
    make(cal, "starts at close", start="2026-10-14T12:00:00", end="2026-10-14T13:00:00")
    make(cal, "inside", start="2026-10-14T10:00:00", end="2026-10-14T12:00:00")
    got = [S._summarize(e)["summary"] for e in S._search_range(cal, datetime(2026, 10, 14, 10), datetime(2026, 10, 14, 12))]
    assert got == ["inside"]


def test_moving_an_event_keeps_its_length(cal):
    ev = make(cal, start="2026-11-05T10:00:00", end="2026-11-05T11:30:00")
    up = S.update_event("Home", ev["uid"], start="2026-11-09T14:00:00")  # a later day: the end must follow
    assert (up["start"], up["end"]) == ("2026-11-09T14:00:00", "2026-11-09T15:30:00")
    day = make(cal, "trip", start="2026-11-10", end="2026-11-12")  # three days
    up = S.update_event("Home", day["uid"], start="2026-12-01")
    assert (up["start"], up["end"], up["all_day"]) == ("2026-12-01", "2026-12-03", True)
    up = S.update_event("Home", ev["uid"], start="2026-11-09T16:00:00", end="2026-11-09T16:45:00")  # explicit end still wins
    assert up["end"] == "2026-11-09T16:45:00"
