"""iCloud Calendar MCP server (CalDAV). Credentials come from env vars:
ICLOUD_USERNAME  - Apple ID email
ICLOUD_APP_PASSWORD - app-specific password (appleid.apple.com)
"""
import functools
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import caldav
import niquests
from icalendar import Alarm, Calendar, Event
from tzlocal import get_localzone
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("icloud-calendar")

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
        "recurring": bool(comp.get("rrule") or comp.get("recurrence-id")),
        "location": str(comp.get("location", "")),
        "description": str(comp.get("description", "")),
    }


def _find(cal, uid: str):
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
) -> dict:
    """Create an event. start/end are ISO datetimes; end defaults to start + 1 hour.
    All-day: pass start as 'YYYY-MM-DD'. end is then the last day (inclusive, also
    'YYYY-MM-DD') and defaults to the same day, e.g. start='2026-10-20' is a one-day event.
    Repeat: repeat='daily'|'weekly'|'monthly'|'yearly' (omit for none), every
    repeat_interval units. It runs forever unless you give repeat_count (total
    occurrences) or repeat_until ('YYYY-MM-DD', inclusive), not both. For weekly,
    repeat_days like ['MO','WE'] picks weekdays (default: the start's weekday).
    alerts: notify N minutes before the start, e.g. [30, 1440] = 30 minutes and 1 day
    before. For all-day events the start is midnight, so 900 = 9:00 the day before."""
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
    cal.add_component(ev)
    target = _calendar(calendar)
    try:
        created = target.save_event(cal.to_ical().decode())
    except _NETWORK_ERRORS:
        # The request may have succeeded before the connection dropped. If our uid is
        # already there, return it; otherwise re-raise so _retry sends the create again.
        _reset_client()
        try:
            return _summarize(_find(_calendar(calendar), uid))
        except ValueError:
            pass
        raise
    return _summarize(created)


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
) -> dict:
    """Update fields of an existing event by uid. Only provided fields change.
    Use 'YYYY-MM-DD' for start/end to make it all-day (end = last day, inclusive).
    repeat: daily|weekly|monthly|yearly replaces the repeat rule (see create_event
    for the other repeat_* options); 'none' removes it; omit to leave it alone.
    alerts: the complete list of alerts in minutes before start (replaces existing);
    [] removes all; omit to leave them alone.
    On a repeating event this changes the whole series."""
    ev = _find(_calendar(calendar), uid)
    comp = ev.icalendar_component
    for key, val in (("summary", summary), ("location", location), ("description", description)):
        if val is not None:
            comp.pop(key, None)
            comp.add(key, val)
    if start is not None or end is not None:
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
            new_end = cur_end  # same kind as before: keep the existing end
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
    if repeat is not None:
        comp.pop("rrule", None)
        if repeat.strip().lower() not in ("", "none"):
            comp.add("rrule", _build_rrule(comp["dtstart"].dt, repeat, repeat_interval, repeat_count, repeat_until, repeat_days))
    if alerts is not None:
        _set_alerts(comp, alerts)
    ev.save()
    return _summarize(ev)


@mcp.tool()
@_retry
def delete_event(calendar: str, uid: str) -> str:
    """Delete an event by uid. For a repeating event this deletes the whole series."""
    _find(_calendar(calendar), uid).delete()
    return f"Deleted {uid}"


import reminders  # noqa: E402  (CloudKit-backed Reminders tools)

reminders.register(mcp)


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


