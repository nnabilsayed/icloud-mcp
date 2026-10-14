"""iCloud Calendar MCP server (CalDAV). Credentials come from env vars:
ICLOUD_USERNAME  - Apple ID email
ICLOUD_APP_PASSWORD - app-specific password (appleid.apple.com)
"""
import os
import uuid
from datetime import datetime, timedelta

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


def _summarize(ev) -> dict:
    comp = ev.icalendar_component
    return {
        "uid": str(comp.get("uid")),
        "summary": str(comp.get("summary", "")),
        "start": comp.get("dtstart").dt.isoformat() if comp.get("dtstart") else None,
        "end": comp.get("dtend").dt.isoformat() if comp.get("dtend") else None,
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
    """List events in a calendar between ISO datetimes start and end."""
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
    """Create an event. start/end are ISO datetimes; end defaults to start + 1 hour."""
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end) if end else s + timedelta(hours=1)
    cal = Calendar()
    cal.add("prodid", "-//icloud-mcp//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", str(uuid.uuid4()))
    ev.add("summary", summary)
    ev.add("dtstart", s)
    ev.add("dtend", e)
    ev.add("dtstamp", datetime.utcnow())
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
    """Update fields of an existing event by uid. Only provided fields change."""
    ev = _find(_calendar(calendar), uid)
    comp = ev.icalendar_component
    for key, val in (("summary", summary), ("location", location), ("description", description)):
        if val is not None:
            comp.pop(key, None)
            comp.add(key, val)
    for key, val in (("dtstart", start), ("dtend", end)):
        if val is not None:
            comp.pop(key, None)
            comp.add(key, datetime.fromisoformat(val))
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


