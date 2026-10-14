"""Links (URL attachments), time alarms and flag/priority views."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import reminders as R


# ---- links ---------------------------------------------------------------
@pytest.mark.parametrize("url", ["https://example.com/a", "http://x.y", "mailto:a@b.co", "tel:+123"])
def test_check_url_accepts_links(url):
    assert R._check_url(f"  {url} ") == url


@pytest.mark.parametrize("url", ["not a url", "example.com", ""])
def test_check_url_rejects_non_links(url):
    with pytest.raises(ValueError, match="doesn't look like a link"):
        R._check_url(url)


class FakeLinkSvc:
    def __init__(self, urls):
        self.atts = [SimpleNamespace(id=f"A/{i}", url=u) for i, u in enumerate(urls)]
        self.calls = []

    def attachments_for(self, r):
        return list(self.atts)

    def delete_attachment(self, r, att):
        self.calls.append(("del", att.url))
        self.atts.remove(att)

    def create_url_attachment(self, r, url):
        self.calls.append(("add", url))
        self.atts.append(SimpleNamespace(id=f"A/new{len(self.atts)}", url=url))

    def get(self, rid):
        return SimpleNamespace(id=rid, attachment_ids=[a.id for a in self.atts])


def _rem(**kw):
    base = dict(id="R/1", attachment_ids=["A/0"], alarm_ids=[], due_date=None, all_day=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_apply_links_replaces_with_the_desired_set():
    svc = FakeLinkSvc(["https://a.example", "https://b.example"])
    R._apply_links(svc, _rem(), ["https://b.example", "https://c.example", "https://c.example"])
    assert svc.calls == [("del", "https://a.example"), ("add", "https://c.example")]


def test_apply_links_none_leaves_alone_and_empty_clears():
    svc = FakeLinkSvc(["https://a.example"])
    R._apply_links(svc, _rem(), None)
    assert svc.calls == []
    R._apply_links(svc, _rem(), [])
    assert svc.calls == [("del", "https://a.example")]


def test_apply_links_validates_before_writing():
    svc = FakeLinkSvc(["https://a.example"])
    with pytest.raises(ValueError):
        R._apply_links(svc, _rem(), ["https://ok.example", "nope"])
    assert svc.calls == []


# ---- alarm encoding ------------------------------------------------------
APP_JSON = '{"hour":13,"timeZone":{"identifier":"Africa\\/Cairo"},"year":2026,"era":1,"day":31,"month":10,"minute":0,"second":0}'


def test_encoded_trigger_matches_what_the_app_writes_byte_for_byte():
    import base64
    enc = R._encode_components(datetime(2026, 10, 31, 13, 0), "Africa/Cairo")
    assert base64.b64decode(enc).decode() == APP_JSON  # copied from an app-made reminder


def test_decode_roundtrip_and_zone_conversion(monkeypatch):
    monkeypatch.setattr(R, "get_localzone", lambda: timezone.utc)
    comps = R._decode_components(R._encode_components(datetime(2026, 10, 31, 13, 0), "Africa/Cairo"))
    assert comps["hour"] == 13 and comps["timeZone"]["identifier"] == "Africa/Cairo"
    assert R._components_to_local(comps) == datetime(2026, 10, 31, 11, 0)  # Cairo is UTC+2 that day


def test_decode_accepts_bytes_and_str():
    enc = R._encode_components(datetime(2026, 1, 2, 3, 4), "UTC")
    assert R._decode_components(enc) == R._decode_components(enc.encode())


def test_due_nonce_follows_the_due_date_token():
    tm = {"map": {"dueDate": {"counter": 7, "modificationTime": 813564037.0258}}}
    assert R._due_nonce(tm) == pytest.approx(700813564037.0258)  # same formula as real app-made alarms
    assert R._due_nonce(None) > 1e11  # fallback when the reminder has no due-date token


def test_merge_token_map_keeps_other_entries_and_bumps_counters():
    old = {"map": {"dueDate": {"counter": 3, "modificationTime": 1.0, "replicaID": "x"},
                   "alarmIDs": {"counter": 2, "modificationTime": 1.0, "replicaID": "x"}}}
    new = json.loads(R._merge_token_map(old, ["alarmIDs", "lastModifiedDate"]))["map"]
    assert new["dueDate"] == old["map"]["dueDate"]  # untouched: the alarm stays tied to it
    assert new["alarmIDs"]["counter"] == 3
    assert "lastModifiedDate" in new


# ---- alarm specs ---------------------------------------------------------
def test_parse_alarm_time():
    r = _rem(due_date=datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc))
    assert R._parse_alarm_time("2026-10-19T18:00:30", r) == datetime(2026, 10, 19, 18, 0)
    with pytest.raises(ValueError, match="needs a time"):
        R._parse_alarm_time("2026-10-19", r)
    with pytest.raises(ValueError, match="'due' needs"):
        R._parse_alarm_time("due", _rem())
    with pytest.raises(ValueError, match="'due' needs"):
        R._parse_alarm_time("due", _rem(due_date=datetime(2026, 10, 20, tzinfo=timezone.utc), all_day=True))


def test_validate_extras_rejects_everything_bad_up_front():
    R._validate_extras(["https://x.example"], ["due", "2026-10-19T18:00"], True)
    with pytest.raises(ValueError):
        R._validate_extras(["bad"], None, True)
    with pytest.raises(ValueError):
        R._validate_extras(None, ["due"], False)
    with pytest.raises(ValueError):
        R._validate_extras(None, ["2026-10-19"], True)
    with pytest.raises(ValueError):
        R._validate_extras(None, ["junk"], True)


def test_apply_alarms_diffs_against_existing(monkeypatch):
    t1, t2 = datetime(2026, 10, 19, 18, 0), datetime(2026, 10, 20, 9, 0)
    have = [{"kind": "date", "at": t1, "name": "A1"}, {"kind": "location", "title": "home"}]
    added, removed = [], []
    monkeypatch.setattr(R, "_read_alarms", lambda svc, r: have)
    monkeypatch.setattr(R, "_add_date_alarm", lambda svc, r, when: added.append(when))
    monkeypatch.setattr(R, "_remove_alarm", lambda svc, r, a: removed.append(a["name"]))
    R._apply_alarms(None, _rem(), None)
    assert (added, removed) == ([], [])
    R._apply_alarms(None, _rem(), ["2026-10-20T09:00"])  # replaces the 18:00 alarm
    assert added == [t2] and removed == ["A1"]
    added.clear(), removed.clear()
    R._apply_alarms(None, _rem(), [])  # clears time alarms, never location ones
    assert added == [] and removed == ["A1"]


# ---- smart views ---------------------------------------------------------
ITEMS = [
    {"title": "a", "flagged": False, "priority": 0, "due": "2026-10-20"},
    {"title": "b", "flagged": True, "priority": 5, "due": "2026-10-18"},
    {"title": "c", "flagged": True, "priority": 1, "due": "2026-10-25"},
    {"title": "d", "flagged": False, "priority": 1, "due": None},
    {"title": "e", "flagged": True, "priority": 0, "due": "2026-10-10"},
]


def titles(items):
    return [r["title"] for r in items]


def test_no_filter_keeps_the_original_order():
    assert titles(R._filter_reminders(ITEMS, None, "")) == ["a", "b", "c", "d", "e"]


def test_flagged_view_sorted_by_priority_then_due():
    assert titles(R._filter_reminders(ITEMS, True, "")) == ["c", "b", "e"]
    assert titles(R._filter_reminders(ITEMS, False, "")) == ["d", "a"]


@pytest.mark.parametrize("priority,expected", [
    ("high", ["c", "d"]), ("medium", ["b"]), ("low", []), ("none", ["e", "a"]), ("any", ["c", "d", "b"]),
    ("HIGH", ["c", "d"]),
])
def test_priority_views(priority, expected):
    assert titles(R._filter_reminders(ITEMS, None, priority)) == expected


def test_combined_and_invalid():
    assert titles(R._filter_reminders(ITEMS, True, "high")) == ["c"]
    with pytest.raises(ValueError, match="priority must be"):
        R._filter_reminders(ITEMS, None, "urgent")
