"""Shared fakes so the tests never touch iCloud."""
import os
import sys
from pathlib import Path

import pytest
from icalendar import Calendar

os.environ.setdefault("ICLOUD_USERNAME", "test@example.com")
os.environ.setdefault("ICLOUD_APP_PASSWORD", "not-a-real-password")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402


class FakeEvent:
    def __init__(self, cal, comp):
        self.cal, self.comp = cal, comp

    @property
    def icalendar_component(self):
        return self.comp

    @property
    def url(self) -> str:
        return f"{self.cal.url}{self.comp['uid']}.ics"

    @property
    def client(self):
        return self.cal.client

    @property
    def data(self) -> str:
        wrapper = Calendar()
        wrapper.add("prodid", "-//test//EN")
        wrapper.add("version", "2.0")
        wrapper.add_component(self.comp)
        return wrapper.to_ical().decode()

    @data.setter
    def data(self, ical: str) -> None:
        self.comp = next(c for c in Calendar.from_ical(ical).walk("VEVENT"))

    def save(self):
        pass

    def delete(self):
        self.cal.items.remove(self)


class FakeCalendar:
    """Just enough of caldav.Calendar for the tools: save_event, events, search."""

    def __init__(self, name="Home"):
        self.name = name
        self.url = f"http://fake/{name.lower()}/"
        self.client = None
        self.items: list[FakeEvent] = []
        self.fail_after_save = 0  # raise a network error this many times after saving

    def save_event(self, ical: str):
        comp = next(c for c in Calendar.from_ical(ical).walk("VEVENT"))
        ev = FakeEvent(self, comp)
        self.items.append(ev)
        if self.fail_after_save:
            self.fail_after_save -= 1
            raise server.niquests.exceptions.ConnectionError("reply lost")
        return ev

    def events(self):
        return list(self.items)

    def search(self, start=None, end=None, event=True, expand=True):
        return list(self.items)  # the real server filtering is what _search_range corrects for


@pytest.fixture(autouse=True)
def _private_activity_log(tmp_path, monkeypatch):
    """Never touch the real activity log from tests."""
    monkeypatch.setenv("ICLOUD_MCP_DATA_DIR", str(tmp_path / "activity"))
    monkeypatch.setenv("ICLOUD_MCP_AGENT", "pytest")


@pytest.fixture
def cal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(server, "_calendar", lambda name: fake)
    monkeypatch.setattr(server, "_event_calendar_list", lambda: [fake])
    monkeypatch.setattr(server, "_event_calendars", None)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    return fake
