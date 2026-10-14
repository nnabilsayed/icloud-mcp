"""iCloud Calendar MCP server (CalDAV). Credentials come from env vars:
ICLOUD_USERNAME  - Apple ID email
ICLOUD_APP_PASSWORD - app-specific password (appleid.apple.com)
"""
import functools
import xml.etree.ElementTree as ET
import os
import time
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import time as time_of_day

import activity
import errors
import caldav
from caldav.elements import dav, ical as ical_elements
import niquests
from icalendar import Alarm, Calendar, Event
from tzlocal import get_localzone
from mcp.server.fastmcp import FastMCP

mcp = errors.install(FastMCP("icloud-calendar"))
activity.set_agent_resolver(lambda: mcp.get_context().session.client_params.clientInfo.name)

_client = None


_NETWORK_ERRORS = (niquests.exceptions.ConnectionError, niquests.exceptions.Timeout)


def _reset_client() -> None:
    global _client, _event_calendars
    _client = None
    _event_calendars = None


def _with_retry(fn, attempts: int = 3):
    """Call fn(); on a dropped connection or DNS blip, reset the client and retry."""
    for i in range(attempts):
        try:
            return fn()
        except _NETWORK_ERRORS:
            if i == attempts - 1:
                raise
            _reset_client()
            time.sleep(1 + i)


def _retry(fn):
    """Retry a tool on transient network errors (keeps the signature for MCP's schema)."""
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return _with_retry(lambda: fn(*a, **kw))
    return wrapper


def _principal():
    global _client
    if _client is None:
        _client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=os.environ["ICLOUD_USERNAME"],
            password=os.environ["ICLOUD_APP_PASSWORD"],
        )
    return _client.principal()


def _calendar(name: str):
    for cal in _event_calendar_list():
        if cal.name == name:
            return cal
    raise ValueError(f"Calendar '{name}' not found")


def _is_all_day(value) -> bool:
    return isinstance(value, date) and not isinstance(value, datetime)


def _parse_when(value: str):
    """'YYYY-MM-DD' is a date (all-day); anything else is an ISO datetime."""
    value = value.strip()
    if len(value) == 10:
        return date.fromisoformat(value)
    return datetime.fromisoformat(value)


_FREQS = {"daily": "DAILY", "weekly": "WEEKLY", "monthly": "MONTHLY", "yearly": "YEARLY"}
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _build_rrule(start, repeat: str, interval: int, count: int, until: str, days: list[str] | None) -> dict:
    """Build an RRULE dict. repeat: daily|weekly|monthly|yearly. Stop with count OR until."""
    key = repeat.strip().lower()
    if key not in _FREQS:
        raise ValueError(f"repeat must be one of {list(_FREQS)} or 'none', got '{repeat}'")
    if interval < 1:
        raise ValueError("repeat_interval must be at least 1")
    if count and until:
        raise ValueError("Use repeat_count or repeat_until, not both")
    rule = {"freq": _FREQS[key], "interval": interval}
    if days:
        picked = [d.strip().upper()[:2] for d in days]
        if key != "weekly" or any(d not in _WEEKDAYS for d in picked):
            raise ValueError("repeat_days (MO,TU,WE,TH,FR,SA,SU) only works with repeat='weekly'")
        rule["byday"] = picked
    if count:
        rule["count"] = count
    elif until:
        end_day = date.fromisoformat(until.strip())
        if _is_all_day(start):
            rule["until"] = end_day
        elif start.tzinfo is not None:  # timed + zone-aware: UNTIL must be UTC
            rule["until"] = datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59, tzinfo=start.tzinfo).astimezone(timezone.utc)
        else:  # floating time: UNTIL must be floating too
            rule["until"] = datetime(end_day.year, end_day.month, end_day.day, 23, 59, 59)
    return rule


def _describe_rrule(comp):
    rule = comp.get("rrule")
    if not rule:
        return None

    def vals(key):  # parsed rules hold lists, freshly built ones hold scalars
        v = rule.get(key)
        return [] if v is None else (v if isinstance(v, list) else [v])

    out = {"frequency": str(vals("FREQ")[0]).lower(), "interval": int(vals("INTERVAL")[0]) if vals("INTERVAL") else 1}
    if vals("BYDAY"):
        out["days"] = [str(d) for d in vals("BYDAY")]
    if vals("COUNT"):
        out["count"] = int(vals("COUNT")[0])
    if vals("UNTIL"):
        u = vals("UNTIL")[0]
        out["until"] = (u.date() if isinstance(u, datetime) else u).isoformat()
    return out


def _set_alerts(comp, minutes: list[int]) -> None:
    """Replace all alerts on an event with one display alert per entry (minutes before start)."""
    if any(m < 0 for m in minutes):
        raise ValueError("alerts are minutes BEFORE the start, so they must be 0 or more")
    comp.subcomponents = [c for c in comp.subcomponents if c.name != "VALARM"]
    for m in sorted(set(minutes), reverse=True):
        alarm = Alarm()
        alarm.add("action", "DISPLAY")
        alarm.add("description", str(comp.get("summary", "Reminder")))
        alarm.add("trigger", timedelta(minutes=-m))
        comp.add_component(alarm)


def _alert_minutes(comp) -> list[int]:
    out = []
    for sub in comp.subcomponents:
        if sub.name != "VALARM" or not sub.get("trigger"):
            continue
        trig = sub["trigger"].dt
        if isinstance(trig, timedelta):  # relative alarms only; absolute ones are skipped
            out.append(int(round(-trig.total_seconds() / 60)))
    return sorted(out, reverse=True)


def _local_naive(v) -> datetime:
    """A date/datetime as a naive local datetime, for comparing against a local window."""
    if isinstance(v, datetime):
        return v.astimezone(get_localzone()).replace(tzinfo=None) if v.tzinfo else v
    return datetime(v.year, v.month, v.day)


def _search_range(cal, a: datetime, b: datetime) -> list:
    """Events overlapping the local window [a, b). The server matches floating (zone-less)
    times in its own zone, so query one day wider each side and filter here."""
    pad = timedelta(days=1)
    found = []
    for e in cal.search(start=a - pad, end=b + pad, event=True, expand=True):
        comp = e.icalendar_component
        st = comp.get("dtstart")
        if st is None:
            continue
        start = _local_naive(st.dt)
        end = _local_naive(comp.get("dtend").dt) if comp.get("dtend") else start
        if start < b and (end > a or start >= a):
            found.append(e)
    return found


_STATUSES = ("TENTATIVE", "CONFIRMED", "CANCELLED")


def _categories_of(comp) -> list[str]:
    cats = comp.get("categories")
    if cats is None:
        return []
    out = []
    for c in (cats if isinstance(cats, list) else [cats]):
        out += [str(x) for x in getattr(c, "cats", [c])]
    return out


def _set_extras(comp, url, categories, status, priority) -> None:
    """URL, categories, status and priority. None = leave alone; '' / [] / 0 clears. Everything is
    checked before it is written."""
    if url is not None and url.strip() and "://" not in url and not url.strip().lower().startswith(("mailto:", "tel:")):
        raise ValueError(f"url '{url}' doesn't look like a link (include http:// or https://)")
    if status is not None and status.strip() and status.strip().upper() not in _STATUSES:
        raise ValueError(f"status must be one of {list(_STATUSES)} (or '' to clear), got '{status}'")
    if priority is not None and not 0 <= priority <= 9:
        raise ValueError("priority must be 0 (none) to 9; 1 is the highest")
    if url is not None:
        comp.pop("url", None)
        if url.strip():
            comp.add("url", url.strip())
    if categories is not None:
        comp.pop("categories", None)
        cleaned = [c.strip() for c in categories if c.strip()]
        if cleaned:
            comp.add("categories", cleaned)
    if status is not None:
        comp.pop("status", None)
        if status.strip():
            comp.add("status", status.strip().upper())
    if priority is not None:
        comp.pop("priority", None)
        if priority:
            comp.add("priority", priority)


def _apply_times(comp, start: str | None, end: str | None) -> None:
    """Change an event's start and/or end. Moving the start alone keeps the event's length."""
    if start is None and end is None:
        return
    cur_start = comp.get("dtstart").dt
    cur_end = comp.get("dtend").dt if comp.get("dtend") else None
    new_start = _parse_when(start) if start is not None else cur_start
    if end is not None:
        new_end = _parse_when(end)
        if _is_all_day(new_start):
            if not _is_all_day(new_end):
                raise ValueError("An all-day event needs end as 'YYYY-MM-DD'")
            new_end = new_end + timedelta(days=1)  # inclusive last day -> exclusive DTEND
    elif _is_all_day(new_start) == _is_all_day(cur_start) and cur_end is not None:
        try:
            new_end = new_start + (cur_end - cur_start)  # moving an event keeps its length
        except TypeError:  # one side is zone-aware and the other isn't
            new_end = cur_end
    elif _is_all_day(new_start):
        new_end = new_start + timedelta(days=1)  # became all-day: one day
    else:
        new_end = new_start + timedelta(hours=1)  # became timed: one hour
    if _is_all_day(new_end) != _is_all_day(new_start):
        raise ValueError("start and end must both be dates or both be datetimes")
    comp.pop("dtstart", None)
    comp.add("dtstart", new_start)
    comp.pop("dtend", None)
    comp.add("dtend", new_end)


def _summarize(ev) -> dict:
    comp = ev.icalendar_component
    start = comp.get("dtstart").dt if comp.get("dtstart") else None
    end = comp.get("dtend").dt if comp.get("dtend") else None
    all_day = _is_all_day(start)
    if all_day and end is not None:
        end = end - timedelta(days=1)  # iCalendar's DTEND is exclusive; report the last day
    return {
        "uid": str(comp.get("uid")),
        "summary": str(comp.get("summary", "")),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else (start.isoformat() if all_day and start else None),
        "all_day": all_day,
        "repeat": _describe_rrule(comp),
        "alerts": _alert_minutes(comp),
        "url": str(comp.get("url", "")),
        "categories": _categories_of(comp),
        "status": str(comp.get("status", "")),
        "priority": int(comp.get("priority", 0) or 0),
        "recurring": bool(comp.get("rrule") or comp.get("recurrence-id")),
        "location": str(comp.get("location", "")),
        "description": str(comp.get("description", "")),
    }


def _load_direct(cal, uid: str):
    """Fetch an event straight from its own address (<calendar>/<uid>.ics), one request instead of a
    scan. iCloud does not support searching by uid (it answers 412), but events made by this server
    and most Apple-made ones live at that address. Returns None if it isn't there."""
    ev = caldav.Event(client=cal.client, url=cal.url.join(f"{uid}.ics"), parent=cal)
    ev.load()
    return ev if str(ev.icalendar_component.get("uid")) == uid else None


def _find(cal, uid: str):
    """The event with this uid: tries the direct fetch first, and scans the calendar when that finds
    nothing or can't be used, so a miss is still a true miss."""
    try:
        ev = _load_direct(cal, uid)
        if ev is not None:
            return ev
    except Exception:  # not at that address, a fake calendar, or a server that refuses: scan instead
        pass
    for ev in cal.events():
        if str(ev.icalendar_component.get("uid")) == uid:
            return ev
    raise ValueError(f"Event {uid} not found")


@mcp.tool()
@_retry
def list_calendars() -> list[str]:
    """List the names of iCloud calendars that can hold events."""
    return [c.name for c in _event_calendar_list()]


@mcp.tool()
@_retry
def list_events(calendar: str, start: str, end: str) -> list[dict]:
    """List events in a calendar between ISO datetimes (or dates) start and end.
    All-day events are included and flagged with all_day=true."""
    found = _search_range(_calendar(calendar), datetime.fromisoformat(start), datetime.fromisoformat(end))
    return [_summarize(e) for e in found]


def _log_created(calendar: str, summary: str, out: dict) -> dict:
    out["log_id"] = activity.record(
        "create_event", f"Created event '{summary}' ({out['start']}) in {calendar}",
        undo={"op": "delete_event", "calendar": calendar, "uid": out["uid"]})
    return out


@mcp.tool()
@_retry
def create_event(
    calendar: str,
    summary: str,
    start: str,
    end: str | None = None,
    location: str = "",
    description: str = "",
    repeat: str = "",
    repeat_interval: int = 1,
    repeat_count: int = 0,
    repeat_until: str = "",
    repeat_days: list[str] | None = None,
    alerts: list[int] | None = None,
    warn_conflicts: bool = True,
    url: str = "",
    categories: list[str] | None = None,
    status: str = "",
    priority: int = 0,
) -> dict:
    """Create an event. start/end are ISO datetimes; end defaults to start + 1 hour.
    All-day: pass start as 'YYYY-MM-DD'. end is then the last day (inclusive, also
    'YYYY-MM-DD') and defaults to the same day, e.g. start='2026-10-20' is a one-day event.
    Repeat: repeat='daily'|'weekly'|'monthly'|'yearly' (omit for none), every
    repeat_interval units. It runs forever unless you give repeat_count (total
    occurrences) or repeat_until ('YYYY-MM-DD', inclusive), not both. For weekly,
    repeat_days like ['MO','WE'] picks weekdays (default: the start's weekday).
    alerts: notify N minutes before the start, e.g. [30, 1440] = 30 minutes and 1 day
    before. For all-day events the start is midnight, so 900 = 9:00 the day before.
    warn_conflicts: a timed event is still created, but the result lists any events it
    overlaps under 'conflicts' (None if the check could not run).
    url: a link for the event. categories: tags like ['work', 'travel']. status: TENTATIVE,
    CONFIRMED or CANCELLED. priority: 1 (highest) to 9, or 0 for none."""
    s = _parse_when(start)
    if _is_all_day(s):
        last = _parse_when(end) if end else s
        if not _is_all_day(last):
            raise ValueError("An all-day event needs end as 'YYYY-MM-DD' (or omit it)")
        if last < s:
            raise ValueError("end is before start")
        e = last + timedelta(days=1)
    else:
        e = _parse_when(end) if end else s + timedelta(hours=1)
        if _is_all_day(e):
            raise ValueError("A timed event needs end as an ISO datetime, not a date")
    cal = Calendar()
    cal.add("prodid", "-//icloud-mcp//EN")
    cal.add("version", "2.0")
    ev = Event()
    uid = str(uuid.uuid4())
    ev.add("uid", uid)
    ev.add("summary", summary)
    ev.add("dtstart", s)
    ev.add("dtend", e)
    ev.add("dtstamp", datetime.now(timezone.utc))
    if repeat:
        ev.add("rrule", _build_rrule(s, repeat, repeat_interval, repeat_count, repeat_until, repeat_days))
    if alerts:
        _set_alerts(ev, alerts)
    if location:
        ev.add("location", location)
    if description:
        ev.add("description", description)
    _set_extras(ev, url or None, categories, status or None, priority or None)
    cal.add_component(ev)
    target = _calendar(calendar)
    conflicts = None
    if warn_conflicts and not _is_all_day(s):
        try:
            ls, le = _local_naive(s), _local_naive(e)
            conflicts = [info for bs, be, info in _busy_intervals(_event_calendar_list(), ls, le, False)
                         if bs < le and be > ls]
        except Exception:
            conflicts = None  # the warning must never stop the event being created
    try:
        created = target.save_event(cal.to_ical().decode())
    except _NETWORK_ERRORS:
        # The request may have succeeded before the connection dropped. If our uid is
        # already there, return it; otherwise re-raise so _retry sends the create again.
        _reset_client()
        try:
            return _log_created(calendar, summary, {**_summarize(_find(_calendar(calendar), uid)), "conflicts": conflicts})
        except ValueError:
            pass
        raise
    return _log_created(calendar, summary, {**_summarize(created), "conflicts": conflicts})


@mcp.tool()
@_retry
def update_event(
    calendar: str,
    uid: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
    description: str | None = None,
    repeat: str | None = None,
    repeat_interval: int = 1,
    repeat_count: int = 0,
    repeat_until: str = "",
    repeat_days: list[str] | None = None,
    alerts: list[int] | None = None,
    url: str | None = None,
    categories: list[str] | None = None,
    status: str | None = None,
    priority: int | None = None,
) -> dict:
    """Update fields of an existing event by uid. Only provided fields change.
    Use 'YYYY-MM-DD' for start/end to make it all-day (end = last day, inclusive).
    repeat: daily|weekly|monthly|yearly replaces the repeat rule (see create_event
    for the other repeat_* options); 'none' removes it; omit to leave it alone.
    alerts: the complete list of alerts in minutes before start (replaces existing);
    [] removes all; omit to leave them alone.
    url / categories / status / priority: '' / [] / 0 clears; omit to leave alone.
    On a repeating event this changes the whole series."""
    ev = _find(_calendar(calendar), uid)
    before = ev.data  # the whole event as it is now, so the change can be undone
    comp = ev.icalendar_component
    for key, val in (("summary", summary), ("location", location), ("description", description)):
        if val is not None:
            comp.pop(key, None)
            comp.add(key, val)
    _apply_times(comp, start, end)
    if repeat is not None:
        comp.pop("rrule", None)
        if repeat.strip().lower() not in ("", "none"):
            comp.add("rrule", _build_rrule(comp["dtstart"].dt, repeat, repeat_interval, repeat_count, repeat_until, repeat_days))
    if alerts is not None:
        _set_alerts(comp, alerts)
    _set_extras(comp, url, categories, status, priority)
    ev.save()
    out = _summarize(ev)
    changed = [n for n, v in (("title", summary), ("start", start), ("end", end), ("location", location),
                              ("description", description), ("repeat", repeat), ("alerts", alerts),
                              ("url", url), ("categories", categories), ("status", status),
                              ("priority", priority)) if v is not None]
    out["log_id"] = activity.record(
        "update_event", f"Updated event '{out['summary']}' in {calendar}: {', '.join(changed) or 'no fields'}",
        undo={"op": "restore_event", "calendar": calendar, "uid": uid, "ical": before})
    return out


@mcp.tool()
@_retry
def delete_event(calendar: str, uid: str) -> str:
    """Delete an event by uid. For a repeating event this deletes the whole series.
    The deletion is logged and can be undone with the undo tool."""
    ev = _find(_calendar(calendar), uid)
    before, title = ev.data, str(ev.icalendar_component.get("summary", ""))
    ev.delete()
    log_id = activity.record("delete_event", f"Deleted event '{title}' from {calendar}",
                             undo={"op": "recreate_event", "calendar": calendar, "ical": before})
    return f"Deleted {uid} (log id {log_id})"


import reminders  # noqa: E402  (CloudKit-backed Reminders tools)

reminders.register(mcp)

import notes  # noqa: E402  (CloudKit-backed Notes tools: create, move, delete)

notes.register(mcp)

import account_info  # noqa: E402  (read-only storage and plan summary)

account_info.register(mcp)


def _event_text(comp) -> str:
    return " ".join([str(comp.get(k, "")) for k in ("summary", "description", "location")] + _categories_of(comp)).lower()


def _locate_event(uid: str, calendar: str = ""):
    """(calendar, event) for a uid, in the named calendar or in any of them."""
    cals = [_calendar(calendar)] if calendar else _event_calendar_list()
    for cal in cals:
        try:
            return cal, _find(cal, uid)
        except ValueError:
            continue
    raise ValueError(f"Event {uid} not found" + (f" in '{calendar}'" if calendar else " in any calendar"))


@mcp.tool()
@_retry
def get_event(uid: str, calendar: str = "") -> dict:
    """One event's full details by its uid (the id every event tool returns). calendar is optional:
    leave it out to look in all calendars. Returns the same fields as list_events plus its
    calendar, repeat rule and alerts."""
    cal, ev = _locate_event(uid, calendar)
    return {"calendar": cal.name, **_summarize(ev)}


@mcp.tool()
@_retry
def search_events(
    text: str,
    calendar: str = "",
    start: str = "",
    end: str = "",
    include_past: bool = False,
    limit: int = 20,
) -> dict:
    """Find events by words in the title, notes or location (case-insensitive), without knowing
    the date, e.g. text='dentist'. calendar: one name (default all). start/end ('YYYY-MM-DD' or ISO
    datetime) limit the search to that range and list each occurrence of repeating events; without
    them it looks at every event, hides ones that are already over (include_past=True shows them)
    and lists a repeating event once. Returns the first `limit` matches by start time, and the
    total count."""
    needle = text.strip().lower()
    if not needle:
        raise ValueError("text must not be empty")
    if limit < 1:
        raise ValueError("limit must be at least 1")
    cals = [_calendar(calendar)] if calendar else _event_calendar_list()
    hits = []
    if start or end:
        a = datetime.fromisoformat(start) if start else datetime.combine(datetime.now().date(), time_of_day.min)
        b = datetime.fromisoformat(end) if end else a + timedelta(days=365)
        if b <= a:
            raise ValueError("end must be after start")
        for cal in cals:
            hits += [(cal, e) for e in _search_range(cal, a, b) if needle in _event_text(e.icalendar_component)]
    else:
        now = datetime.now()
        for cal in cals:
            for e in cal.events():
                comp = e.icalendar_component
                if needle not in _event_text(comp):
                    continue
                if not include_past and not comp.get("rrule") and comp.get("dtstart") is not None:
                    st = comp["dtstart"].dt
                    en = _local_naive(comp["dtend"].dt) if comp.get("dtend") else _local_naive(st)
                    if _is_all_day(st) and comp.get("dtend") is None:
                        en += timedelta(days=1)
                    if en < now:
                        continue
                hits.append((cal, e))
    rows = [{"calendar": cal.name, **_summarize(e)} for cal, e in hits]
    rows.sort(key=lambda r: (r["start"] or "", r["summary"]))
    return {"count": len(rows), "events": rows[:limit]}


def _move_event_impl(uid: str, src_name: str, dst_name: str) -> str:
    """Move an event with WebDAV MOVE, which is atomic and keeps its uid. (Copying it and deleting
    the original cannot work: iCloud refuses to hold one uid in two calendars, even briefly.)
    A retry after a lost reply is harmless: if the event is already in the target, it is done."""
    src, dst = _calendar(src_name), _calendar(dst_name)
    if src is dst:
        raise ValueError(f"The event is already in '{dst_name}'")
    try:
        ev = _find(src, uid)
    except ValueError:
        try:
            return str(_find(dst, uid).icalendar_component.get("summary", ""))  # an earlier attempt moved it
        except ValueError:
            raise ValueError(f"Event {uid} not found in '{src_name}'")
    title = str(ev.icalendar_component.get("summary", ""))
    src_url = str(ev.url)
    target_url = str(dst.url).rstrip("/") + "/" + src_url.rstrip("/").split("/")[-1]
    resp = ev.client.request(src_url, "MOVE", "", {"Destination": target_url, "Overwrite": "F"})
    if resp.status not in (200, 201, 204):
        raise RuntimeError(f"iCloud refused to move the event (HTTP {resp.status})")
    return title


@mcp.tool()
@_retry
def move_event(uid: str, to_calendar: str, calendar: str = "") -> dict:
    """Move an event to another calendar (same uid, same details). calendar is optional: leave it out
    to find the event in any calendar. A repeating event moves with its whole series. Can be undone."""
    src, ev = _locate_event(uid, calendar)
    dst = _calendar(to_calendar)
    src_name, dst_name = src.name, dst.name
    title = _move_event_impl(uid, src_name, dst_name)
    out = {"uid": uid, "title": title, "from": src_name, "to": dst_name}
    out["log_id"] = activity.record("move_event", f"Moved event '{title}' from {src_name} to {dst_name}",
                                    undo={"op": "move_event", "uid": uid, "from": dst_name, "to": src_name})
    return out


@mcp.tool()
@_retry
def duplicate_event(uid: str, calendar: str = "", to_calendar: str = "", start: str = "", end: str = "",
                    title: str = "") -> dict:
    """Copy an event under a new uid. calendar: where to find it (default: any). to_calendar: where
    the copy goes (default: the same calendar). start / end: when the copy happens (same formats as
    update_event; a new start alone keeps the event's length); default: the same time. title: a
    different title for the copy. A repeating event is copied as a whole series. Can be undone."""
    src, ev = _locate_event(uid, calendar)
    dst = _calendar(to_calendar) if to_calendar else src
    wrapper = Calendar.from_ical(ev.data)
    masters = [c for c in wrapper.walk("VEVENT") if "RECURRENCE-ID" not in c]
    if not masters:
        raise ValueError("That event has no main entry to copy")
    comp = masters[0]
    new_uid = str(uuid.uuid4())
    comp.pop("uid", None)
    comp.add("uid", new_uid)
    comp.pop("dtstamp", None)
    comp.add("dtstamp", datetime.now(timezone.utc))
    if title.strip():
        comp.pop("summary", None)
        comp.add("summary", title.strip())
    _apply_times(comp, start or None, end or None)
    out_cal = Calendar()
    out_cal.add("prodid", "-//icloud-mcp//EN")
    out_cal.add("version", "2.0")
    for extra in wrapper.subcomponents:  # time zone definitions the copy may refer to
        if extra.name != "VEVENT":
            out_cal.add_component(extra)
    out_cal.add_component(comp)
    try:
        created = dst.save_event(out_cal.to_ical().decode())
    except _NETWORK_ERRORS as exc:
        _reset_client()
        try:
            created = _find(_calendar(dst.name), new_uid)  # saved before the connection dropped
        except ValueError:
            created = None
        if created is None:
            raise exc  # not saved: let _retry send it again
    result = {"calendar": dst.name, **_summarize(created)}
    result["log_id"] = activity.record("duplicate_event", f"Duplicated event '{result['summary']}' into {dst.name}",
                                       undo={"op": "delete_event", "calendar": dst.name, "uid": new_uid})
    return result


# --- Calendar settings ------------------------------------------------------------------
_PROPFIND_BODY = ('''<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/" xmlns:cs="http://calendarserver.org/ns/">
 <d:prop><d:displayname/><d:resourcetype/><a:calendar-color/><c:supported-calendar-component-set/><d:current-user-privilege-set/><cs:source/></d:prop>
</d:propfind>''')


def _propfind_raw(cal) -> str:
    r = cal.client.propfind(str(cal.url), _PROPFIND_BODY, 0)
    raw = getattr(r, "raw", None) or getattr(r, "text", "")
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


def _parse_calendar_props(raw: str) -> dict:
    """Colour, what it holds, whether it is writable and whether it is a subscription."""
    local = lambda el: el.tag.split("}")[-1]
    out = {"color": None, "components": [], "privileges": [], "resource_types": [], "source": None}
    for el in ET.fromstring(raw).iter():
        name = local(el)
        if name == "calendar-color" and (el.text or "").strip():
            out["color"] = el.text.strip()
        elif name == "comp" and el.get("name"):
            out["components"].append(el.get("name"))
        elif name == "resourcetype":
            out["resource_types"] = [local(c) for c in el]
        elif name == "current-user-privilege-set":
            out["privileges"] = [local(c) for priv in el for c in priv]
        elif name == "source":
            href = next((c.text for c in el.iter() if local(c) == "href" and c.text), None)
            out["source"] = href or (el.text or "").strip() or None
    return out


def _describe_calendar(name: str, props: dict) -> dict:
    kinds = {"VEVENT": "events", "VTODO": "reminders"}
    kind = next((kinds[c] for c in props["components"] if c in kinds), "unknown")
    return {
        "name": name,
        "color": props["color"],
        "kind": kind if kind != "reminders" else "reminders (legacy list: not an event calendar)",
        "writable": any(p in ("write", "write-content", "all") for p in props["privileges"]),
        "subscribed": "subscribed" in props["resource_types"] or bool(props["source"]),
        **({"source": props["source"]} if props["source"] else {}),
    }


@mcp.tool()
@_retry
def list_calendar_details() -> list[dict]:
    """Every calendar with its colour, what it holds (events or the legacy reminders list), whether
    you can write to it, and whether it is a read-only subscription (holidays, sports...). Over
    CalDAV iCloud may not list subscriptions at all; if your account has none exposed, none appear."""
    out = []
    for cal in _principal().calendars():
        try:
            props = _parse_calendar_props(_propfind_raw(cal))
        except Exception:
            props = {"color": None, "components": [], "privileges": [], "resource_types": [], "source": None}
        out.append(_describe_calendar(cal.name, props))
    return out


def _calendar_color(cal) -> str | None:
    props = cal.get_properties([ical_elements.CalendarColor()])
    return next((v for k, v in props.items() if k.endswith("calendar-color")), None)


@mcp.tool()
@_retry
def set_calendar_color(calendar: str, color: str) -> dict:
    """Change an event calendar's colour ('#RRGGBB'). Can be undone."""
    cal = _calendar(calendar)
    new = _normalize_color(color)
    old = _calendar_color(cal)
    cal.set_properties([ical_elements.CalendarColor(new)])
    out = {"name": cal.name, "color": new, "previous": old}
    out["log_id"] = activity.record(
        "set_calendar_color", f"Changed the colour of '{cal.name}' from {old or 'none'} to {new}",
        undo={"op": "set_calendar_color", "name": cal.name, "color": old} if old else None)
    return out


# --- Calendars: create, rename, delete ------------------------------------------------
MAX_UNDOABLE_EVENTS = 500  # deleting a calendar stores its events in the log so it can be undone


def _check_calendar_name(name: str, cals, ignore=None) -> str:
    n = name.strip()
    if not n:
        raise ValueError("A calendar needs a name")
    if len(n) > 200:
        raise ValueError("That calendar name is too long (200 characters at most)")
    for c in cals:
        if c is not ignore and (c.name or "").strip().lower() == n.lower():
            raise ValueError(f"A calendar named '{c.name}' already exists; names must be unique so tools can tell them apart")
    return n


def _normalize_color(color: str) -> str:
    """'#RRGGBB' or '#RRGGBBAA' -> '#RRGGBBAA' (what iCloud stores)."""
    c = color.strip().upper()
    if not c.startswith("#") or len(c) not in (7, 9) or any(ch not in "0123456789ABCDEF" for ch in c[1:]):
        raise ValueError(f"color must look like '#RRGGBB', got '{color}'")
    return c if len(c) == 9 else c + "FF"


def _find_calendar(name: str):
    for c in _event_calendar_list():
        if (c.name or "").strip().lower() == name.strip().lower():
            return c
    return None


def _make_calendar(name: str, color: str = ""):
    """Create an event calendar; a lost reply is detected by looking the name up before retrying."""
    try:
        cal = _principal().make_calendar(name=name, supported_calendar_component_set=["VEVENT"])
    except _NETWORK_ERRORS:
        _reset_client()
        found = _find_calendar(name)
        if found is None:
            raise
        cal = found
    if color:
        cal.set_properties([ical_elements.CalendarColor(color)])
    _reset_client_cache()
    return cal


def _reset_client_cache() -> None:
    global _event_calendars
    _event_calendars = None


@mcp.tool()
@_retry
def create_calendar(name: str, color: str = "") -> dict:
    """Create a new event calendar. Names must be unique. color: optional '#RRGGBB'. Returns its name;
    use that as the `calendar` argument of the event tools. Can be undone."""
    n = _check_calendar_name(name, _event_calendar_list())
    c = _normalize_color(color) if color else ""
    cal = _make_calendar(n, c)
    out = {"name": n, "color": c or None}
    out["log_id"] = activity.record("create_calendar", f"Created calendar '{n}'",
                                    undo={"op": "delete_calendar", "name": n})
    return out


@mcp.tool()
@_retry
def rename_calendar(calendar: str, new_name: str) -> dict:
    """Rename an event calendar. Names must be unique. Can be undone."""
    cal = _calendar(calendar)
    old = cal.name
    n = _check_calendar_name(new_name, _event_calendar_list(), ignore=cal)
    if n == old:
        return {"name": n, "changed": False}
    cal.set_properties([dav.DisplayName(n)])
    _reset_client_cache()
    out = {"name": n, "changed": True}
    out["log_id"] = activity.record("rename_calendar", f"Renamed calendar '{old}' to '{n}'",
                                    undo={"op": "rename_calendar", "name": n, "old": old})
    return out


@mcp.tool()
@_retry
def delete_calendar(calendar: str, delete_events: bool = False) -> dict:
    """Delete an event calendar. One that still holds events is refused unless delete_events=True,
    which deletes the events with it (up to 500, so the deletion can be undone: undo recreates the
    calendar and its events)."""
    cal = _calendar(calendar)
    name = cal.name
    events = cal.events()
    if events and not delete_events:
        raise ValueError(f"'{name}' still holds {len(events)} event(s). Move or delete them first, or pass "
                         "delete_events=True to delete them with the calendar.")
    if len(events) > MAX_UNDOABLE_EVENTS:
        raise ValueError(f"'{name}' holds {len(events)} events, more than the {MAX_UNDOABLE_EVENTS} that can be "
                         "kept for undo. Delete it in the Calendar app if you really mean to.")
    saved = [e.data for e in events]
    cal.delete()
    _reset_client_cache()
    out = {"name": name, "deleted_events": len(saved)}
    out["log_id"] = activity.record(
        "delete_calendar", f"Deleted calendar '{name}'" + (f" and its {len(saved)} event(s)" if saved else ""),
        undo={"op": "recreate_calendar", "name": name, "events": saved})
    return out


def _undo_calendar_op(op: dict) -> str:
    kind = op["op"]
    if kind == "delete_event":
        _find(_calendar(op["calendar"]), op["uid"]).delete()
        return "removed the event that was created"
    if kind == "restore_event":
        ev = _find(_calendar(op["calendar"]), op["uid"])
        ev.data = op["ical"]
        ev.save()
        return "restored the event to how it was"
    if kind == "recreate_event":
        _calendar(op["calendar"]).save_event(op["ical"])
        return "recreated the deleted event"
    if kind == "move_event":
        _move_event_impl(op["uid"], op["from"], op["to"])
        return f"moved the event back to '{op['to']}'"
    if kind == "set_calendar_color":
        cal = _find_calendar(op["name"])
        if cal is None:
            raise ValueError(f"Calendar '{op['name']}' no longer exists")
        cal.set_properties([ical_elements.CalendarColor(op["color"])])
        return f"restored the colour {op['color']}"
    if kind == "delete_calendar":
        cal = _find_calendar(op["name"])
        if cal is None:
            raise ValueError(f"Calendar '{op['name']}' no longer exists")
        if cal.events():
            raise ValueError(f"Calendar '{op['name']}' now holds events, so it isn't removed automatically")
        cal.delete()
        _reset_client_cache()
        return "removed the calendar that was created"
    if kind == "rename_calendar":
        cal = _find_calendar(op["name"])
        if cal is None:
            raise ValueError(f"Calendar '{op['name']}' no longer exists")
        _check_calendar_name(op["old"], _event_calendar_list(), ignore=cal)
        cal.set_properties([dav.DisplayName(op["old"])])
        _reset_client_cache()
        return f"renamed the calendar back to '{op['old']}'"
    if kind == "recreate_calendar":
        _check_calendar_name(op["name"], _event_calendar_list())
        cal = _make_calendar(op["name"])
        for data in op["events"]:
            cal.save_event(data)
        return f"recreated the calendar with {len(op['events'])} event(s)"
    raise ValueError(f"Unknown undo operation '{kind}'")


@mcp.tool()
def activity_log(limit: int = 20, agent: str = "", tool: str = "", only_undoable: bool = False) -> list[dict]:
    """What has been changed through this server, newest first: when, which agent, which tool, a
    plain summary, whether it can still be undone (undoable) and whether it was undone. Use the
    id with the undo tool. Filter by agent name or tool; only_undoable hides the rest.
    Changes made directly in the Calendar/Reminders apps are not recorded here."""
    return [{"id": r["id"], "when": r["ts"], "agent": r["agent"], "tool": r["tool"], "summary": r["summary"],
             "undoable": bool(r["undo"]) and not r["undone"], "undone": r["undone"], "batch": r["batch"]}
            for r in activity.entries(max(1, min(limit, 200)), agent, tool, only_undoable)]


@mcp.tool()
@_retry
@reminders._reset_on_error
def undo(entry_id: str = "", batch: str = "") -> dict:
    """Undo a logged change. entry_id: an id from activity_log; batch: undo every change of one
    bulk operation (newest first); neither: undo the most recent undoable change by anyone.
    Calendar changes are restored exactly (create, edit and delete). Reminder edits restore the
    title, notes, due, priority, flag, completion, repeat, tags, links, alarms, list and parent;
    early-reminder and Urgent settings are not restored. If something changed since, the undo
    stops with a clear error and leaves the log entry undoable."""
    if entry_id and batch:
        raise ValueError("Give entry_id or batch, not both")
    if batch:
        targets = activity.batch_entries(batch)
        if not targets:
            raise ValueError(f"No undoable changes found for batch '{batch}'")
    elif entry_id:
        row = activity.get(entry_id)
        if row is None:
            raise ValueError(f"No log entry '{entry_id}'")
        if row["undone"]:
            raise ValueError(f"'{entry_id}' was already undone")
        if not row["undo"]:
            raise ValueError(f"'{entry_id}' ({row['tool']}) can't be undone")
        targets = [row]
    else:
        targets = activity.entries(limit=1, only_undoable=True)
        if not targets:
            raise ValueError("Nothing to undo")
    done = []
    for row in targets:
        try:
            op = row["undo"]
            if op["op"] in reminders.UNDO_OPS:
                note = reminders.undo_op(op)
            elif op["op"] in notes.UNDO_OPS:
                note = notes.undo_op(op)
            else:
                note = _undo_calendar_op(op)
        except Exception as exc:
            return {"undone": done, "failed": {"id": row["id"], "summary": row["summary"],
                                                "error": f"{type(exc).__name__}: {exc}"[:300]}}
        activity.mark_undone(row["id"])
        done.append({"id": row["id"], "summary": row["summary"], "result": note})
    activity.record("undo", f"Undid {len(done)} change(s): " + "; ".join(d["summary"] for d in done)[:300])
    return {"undone": done}


_event_calendars: list | None = None


def _event_calendar_list() -> list:
    """Calendars that hold events (skips the legacy Reminders list); cached per process."""
    global _event_calendars
    if _event_calendars is None:
        found = []
        for cal in _principal().calendars():
            try:
                comps = cal.get_supported_components()
            except Exception:
                comps = ["VEVENT"]
            if "VEVENT" in comps:
                found.append(cal)
        _event_calendars = found
    return _event_calendars


# --- Free time and conflicts ------------------------------------------------------
_DAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _parse_clock(value: str, label: str) -> time_of_day:
    try:
        h, m = value.strip().split(":")
        return time_of_day(int(h), int(m))
    except Exception:
        raise ValueError(f"{label} must look like 'HH:MM', got '{value}'")


def _busy_intervals(cals, a: datetime, b: datetime, all_day_blocks: bool) -> list[tuple]:
    """(start, end, info) for events overlapping the local window [a, b), as naive local times."""
    out = []
    for cal in cals:
        for e in _search_range(cal, a, b):
            comp = e.icalendar_component
            st = comp["dtstart"].dt
            if _is_all_day(st) and not all_day_blocks:
                continue
            start = _local_naive(st)
            end = _local_naive(comp["dtend"].dt) if comp.get("dtend") else start
            if end <= start:
                continue  # zero-length: nothing to be busy with
            out.append((start, end, {"calendar": cal.name, "summary": str(comp.get("summary", "")),
                                     "uid": str(comp.get("uid")), "all_day": _is_all_day(st)}))
    return sorted(out, key=lambda x: (x[0], x[1]))


def _free_slots(busy, days, work_start, work_end, duration: int, buffer: int, now: datetime | None = None) -> list[dict]:
    """Gaps of at least `duration` minutes inside working hours, around busy intervals.
    `buffer` widens each busy interval on both sides. Gaps are whole stretches, not chopped pieces."""
    pad, need = timedelta(minutes=buffer), timedelta(minutes=duration)
    slots = []
    for day in days:
        lo = datetime.combine(day, work_start)
        hi = datetime.combine(day, work_end)
        if now is not None:
            if now >= hi:
                continue
            if now > lo:  # today: nothing in the past; start on the next quarter hour
                lo = now.replace(second=0, microsecond=0)
                lo += timedelta(minutes=(-lo.minute) % 15)
                if lo >= hi:
                    continue
        cursor = lo
        for bs, be, _info in busy:
            bs, be = bs - pad, be + pad
            if be <= cursor or bs >= hi:
                continue
            if bs - cursor >= need:
                slots.append((cursor, bs))
            cursor = max(cursor, be)
            if cursor >= hi:
                break
        if hi - cursor >= need and cursor < hi:
            slots.append((cursor, hi))
    return [{"start": s.isoformat(timespec="minutes"), "end": e.isoformat(timespec="minutes"),
             "minutes": int((e - s).total_seconds() // 60)} for s, e in slots]


@mcp.tool()
@_retry
def find_free_time(
    duration_minutes: int = 60,
    start: str = "",
    days: int = 7,
    work_start: str = "09:00",
    work_end: str = "18:00",
    weekdays: list[str] | None = None,
    buffer_minutes: int = 0,
    all_day_blocks: bool = False,
    calendars: list[str] | None = None,
    limit: int = 10,
) -> dict:
    """Find open time across your calendars. Returns the free stretches (each at least
    duration_minutes long) within working hours, earliest first.
    start: 'YYYY-MM-DD' (default today); days: how many days from start (max 60).
    work_start / work_end: 'HH:MM' window per day. weekdays: e.g. ['MO','TU','WE','TH','FR']
    (default: every day). buffer_minutes: breathing room kept before and after each event.
    all_day_blocks: treat all-day events (like 'out of office') as busy; by default they
    don't block. calendars: limit to these names (default all). Today starts from now."""
    if duration_minutes < 1 or duration_minutes > 24 * 60:
        raise ValueError("duration_minutes must be between 1 and 1440")
    if days < 1 or days > 60:
        raise ValueError("days must be between 1 and 60")
    if buffer_minutes < 0 or limit < 1:
        raise ValueError("buffer_minutes can't be negative and limit must be at least 1")
    ws, we = _parse_clock(work_start, "work_start"), _parse_clock(work_end, "work_end")
    if we <= ws:
        raise ValueError("work_end must be after work_start")
    codes = [d.strip().upper()[:2] for d in (weekdays or _DAY_CODES)]
    if any(c not in _DAY_CODES for c in codes):
        raise ValueError(f"weekdays must be from {list(_DAY_CODES)}")
    first = date.fromisoformat(start.strip()) if start.strip() else datetime.now().date()
    day_list = [first + timedelta(days=i) for i in range(days)]
    day_list = [d for d in day_list if _DAY_CODES[d.weekday()] in codes]
    chosen = _event_calendar_list()
    if calendars:
        wanted = {c.strip() for c in calendars}
        chosen = [c for c in chosen if c.name in wanted]
        if not chosen:
            raise ValueError(f"None of {sorted(wanted)} are event calendars")
    window = (datetime.combine(first, time_of_day.min), datetime.combine(first + timedelta(days=days), time_of_day.min))
    busy = _busy_intervals(chosen, window[0], window[1], all_day_blocks)
    slots = _free_slots(busy, day_list, ws, we, duration_minutes, buffer_minutes, datetime.now())
    return {
        "duration_minutes": duration_minutes,
        "from": first.isoformat(),
        "to": (first + timedelta(days=days - 1)).isoformat(),
        "slots": slots[:limit],
        "total_slots": len(slots),
    }


@mcp.tool()
@_retry
def check_conflicts(start: str, end: str = "", calendars: list[str] | None = None) -> dict:
    """What overlaps a proposed time? start/end are ISO datetimes (end defaults to start + 1 hour),
    or start as 'YYYY-MM-DD' for a whole day. Looks across all calendars (or the named ones).
    Returns the overlapping events, each with its calendar; all-day ones are flagged all_day=true."""
    s = _parse_when(start)
    if _is_all_day(s):
        a, b = datetime.combine(s, time_of_day.min), datetime.combine(s + timedelta(days=1), time_of_day.min)
    else:
        a = s
        b = _parse_when(end) if end else s + timedelta(hours=1)
        if _is_all_day(b):
            raise ValueError("end must be an ISO datetime when start is one")
        if b <= a:
            raise ValueError("end must be after start")
    chosen = _event_calendar_list()
    if calendars:
        chosen = [c for c in chosen if c.name in {x.strip() for x in calendars}]
    hits = [info for bs, be, info in _busy_intervals(chosen, a, b, True) if bs < b and be > a]
    return {"start": a.isoformat(timespec="minutes"), "end": b.isoformat(timespec="minutes"),
            "free": not hits, "conflicts": hits}


_STOP = {"a", "an", "the", "and", "or", "of", "to", "for", "with", "at", "on", "in", "my", "your", "re", "fwd"}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[^\W_]+", text.lower()) if w not in _STOP}


def _similar(a: str, b: str) -> bool:
    """Same event, worded differently: most of the shorter title's words appear in the other."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return a.strip().lower() == b.strip().lower()
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.6


@mcp.tool()
@_retry
def propose_event(summary: str, start: str, end: str = "", calendar: str = "", location: str = "",
                  description: str = "", source: str = "", now: str = "") -> dict:
    """Check a calendar event BEFORE creating it, e.g. one an agent read out of an email
    ("interview confirmed for the 18th at 1pm"). Writes nothing. The agent extracts the title and
    the exact date/time (start/end as in create_event; resolve "the 18th" to a full date first);
    this returns the draft plus things to show the user: warnings (date in the past, far in the
    future, no end given), overlapping events and a possible duplicate (same day, similar title).
    source: where it came from (e.g. the email subject and sender), kept in the description.
    Show the draft, and only call create_event with 'create_event_args' once the user agrees.
    now: override the current time (ISO), for testing."""
    if not summary.strip():
        raise ValueError("summary must not be empty")
    s = _parse_when(start)
    all_day = _is_all_day(s)
    warnings = []
    if all_day:
        last = _parse_when(end) if end else s
        if not _is_all_day(last) or last < s:
            raise ValueError("An all-day event needs end as 'YYYY-MM-DD' on or after start")
        a, b = datetime.combine(s, time_of_day.min), datetime.combine(last + timedelta(days=1), time_of_day.min)
        end_out = last.isoformat() if end else ""
    else:
        if end:
            e = _parse_when(end)
            if _is_all_day(e):
                raise ValueError("A timed event needs end as an ISO datetime, not a date")
        else:
            e = s + timedelta(hours=1)
            warnings.append("No end time given; assuming one hour.")
        if e <= s:
            raise ValueError("end must be after start")
        a, b = _local_naive(s), _local_naive(e)
        end_out = e.isoformat(timespec="minutes")
    current = datetime.fromisoformat(now) if now else datetime.now()
    if a < current:
        warnings.append("This is in the past. Check the month and year the email meant.")
    elif a > current + timedelta(days=365):
        warnings.append("This is more than a year away. Check the year.")
    cals = _event_calendar_list()
    if calendar:
        _calendar(calendar)  # fails clearly if the name is unknown
    day_a = datetime.combine(a.date(), time_of_day.min)
    day_b = datetime.combine(b.date(), time_of_day.min) + timedelta(days=1)
    same_day = _busy_intervals(cals, day_a, day_b, True)
    duplicates = [info for _, _, info in same_day if _similar(info["summary"], summary)]
    if duplicates:
        warnings.append("An event with a similar title is already on that day; this may be a duplicate.")
    conflicts = [info for bs, be, info in same_day if bs < b and be > a and info not in duplicates]
    desc = description.strip()
    if source.strip():
        desc = (desc + "\n\n" if desc else "") + f"Source: {source.strip()}"
    args = {"calendar": calendar, "summary": summary.strip(), "start": start.strip()}
    for k, v in (("end", end_out), ("location", location), ("description", desc)):
        if v:
            args[k] = v
    return {"ok_to_create": not duplicates, "warnings": warnings, "conflicts": conflicts,
            "possible_duplicates": duplicates, "create_event_args": args}


@mcp.tool()
@_retry
@reminders._reset_on_error
def agenda(start: str = "", days: int = 1, include_overdue: bool = True) -> dict:
    """What's on: events from ALL calendars plus due reminders, in one call.
    start: 'YYYY-MM-DD' (default today); days: how many days from start (1 = just that
    day, 7 = a week). Returns events sorted by start (with their calendar), reminders
    due in the range, and overdue = incomplete reminders due before start (only when
    start is today or earlier; set include_overdue=False to leave them out).
    Reminders with no due date are not included."""
    if days < 1 or days > 366:
        raise ValueError("days must be between 1 and 366")
    first = date.fromisoformat(start.strip()) if start.strip() else datetime.now().date()
    last = first + timedelta(days=days - 1)
    window = (datetime(first.year, first.month, first.day), datetime(last.year, last.month, last.day) + timedelta(days=1))

    events, errors = [], []
    for cal in _event_calendars if _event_calendars is not None else _event_calendar_list():
        try:
            for e in _search_range(cal, window[0], window[1]):
                events.append({"calendar": cal.name, **_summarize(e)})
        except Exception as exc:  # one broken calendar shouldn't hide the rest
            errors.append(f"{cal.name}: {type(exc).__name__}")
    events.sort(key=lambda e: (e["start"] or "", e["summary"]))

    def due_day(r):  # local calendar day of a reminder's due value
        return date.fromisoformat(r["due"][:10]) if r["due"] else None

    due, overdue = [], []
    for r in reminders.open_reminders():
        d = due_day(r)
        if d is None or r["completed"]:
            continue
        if first <= d <= last:
            due.append(r)
        elif d < first and include_overdue and first <= datetime.now().date():
            overdue.append(r)
    key = lambda r: (r["due"] or "", r["title"])
    due.sort(key=key)
    overdue.sort(key=key)
    return {
        "from": first.isoformat(),
        "to": last.isoformat(),
        "events": events,
        "reminders": due,
        "overdue": overdue,
        **({"errors": errors} if errors else {}),
    }


if __name__ == "__main__":
    mcp.run()


