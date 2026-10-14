"""iCloud Calendar MCP server (CalDAV). Credentials come from env vars:
ICLOUD_USERNAME  - Apple ID email
ICLOUD_APP_PASSWORD - app-specific password (appleid.apple.com)
"""
import os
import uuid
from datetime import date, datetime, timedelta, timezone

import caldav
from icalendar import Calendar, Event
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("icloud-calendar")

_client = None


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
    for cal in _principal().calendars():
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
        "location": str(comp.get("location", "")),
        "description": str(comp.get("description", "")),
    }


def _find(cal, uid: str):
    for ev in cal.events():
        if str(ev.icalendar_component.get("uid")) == uid:
            return ev
    raise ValueError(f"Event {uid} not found")


@mcp.tool()
def list_calendars() -> list[str]:
    """List the names of all iCloud calendars."""
    return [c.name for c in _principal().calendars()]


@mcp.tool()
def list_events(calendar: str, start: str, end: str) -> list[dict]:
    """List events in a calendar between ISO datetimes (or dates) start and end.
    All-day events are included and flagged with all_day=true."""
    cal = _calendar(calendar)
    found = cal.search(
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        event=True,
        expand=True,
    )
    return [_summarize(e) for e in found]


@mcp.tool()
def create_event(
    calendar: str,
    summary: str,
    start: str,
    end: str | None = None,
    location: str = "",
    description: str = "",
) -> dict:
    """Create an event. start/end are ISO datetimes; end defaults to start + 1 hour.
    All-day: pass start as 'YYYY-MM-DD'. end is then the last day (inclusive, also
    'YYYY-MM-DD') and defaults to the same day, e.g. start='2026-10-20' is a one-day event."""
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
    ev.add("uid", str(uuid.uuid4()))
    ev.add("summary", summary)
    ev.add("dtstart", s)
    ev.add("dtend", e)
    ev.add("dtstamp", datetime.now(timezone.utc))
    if location:
        ev.add("location", location)
    if description:
        ev.add("description", description)
    cal.add_component(ev)
    created = _calendar(calendar).save_event(cal.to_ical().decode())
    return _summarize(created)


@mcp.tool()
def update_event(
    calendar: str,
    uid: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    location: str | None = None,
    description: str | None = None,
) -> dict:
    """Update fields of an existing event by uid. Only provided fields change.
    Use 'YYYY-MM-DD' for start/end to make it all-day (end = last day, inclusive)."""
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
    ev.save()
    return _summarize(ev)


@mcp.tool()
def delete_event(calendar: str, uid: str) -> str:
    """Delete an event by uid."""
    _find(_calendar(calendar), uid).delete()
    return f"Deleted {uid}"


import reminders  # noqa: E402  (CloudKit-backed Reminders tools)

reminders.register(mcp)


if __name__ == "__main__":
    mcp.run()


