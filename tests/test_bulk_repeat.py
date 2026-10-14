"""Repeat rules (in-place edit), moving, subtasks, alarm re-tying and bulk planning."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import reminders as R


# ---- repeat rules --------------------------------------------------------
class FakeWrites:
    def __init__(self):
        self.created, self.updates = [], []

    def _reminder_record_name(self, rid):
        return rid

    def _create_linked_child(self, **kw):
        self.created.append(kw)

    def _submit_single_record_update(self, **kw):
        self.updates.append(kw)


class FakeSvc:
    def __init__(self):
        self._writes = FakeWrites()
        self.deleted = []

    def delete_recurrence_rule(self, r, stub):
        self.deleted.append(stub.id)


def rule(name="RecurrenceRule/1", wire=1, interval=1, count=0):
    return {"name": name, "tag": "t", "wire": wire, "interval": interval, "count": count}


def reminder():
    return SimpleNamespace(id="R/1", recurrence_rule_ids=[])


def fields_of(call):
    return {k: v["value"] for k, v in call["fields"].items()}


@pytest.mark.parametrize("repeat,wire", [("daily", 0), ("weekly", 1), ("monthly", 2), ("yearly", 3)])
def test_new_rule_is_created_with_the_exact_wire_value(monkeypatch, repeat, wire):
    svc = FakeSvc()
    monkeypatch.setattr(R, "_read_rules", lambda s, r: [])
    R._apply_repeat(svc, reminder(), repeat, 2, 5)
    (call,) = svc._writes.created
    f = {k: v["value"] for k, v in call["child_fields"].items()}
    assert (f["Frequency"], f["Interval"], f["OccurrenceCount"]) == (wire, 2, 5)  # daily is 0 too: no patch step
    assert svc._writes.updates == [] and svc.deleted == []


def test_existing_rule_is_edited_in_place_changing_only_what_differs(monkeypatch):
    svc = FakeSvc()
    monkeypatch.setattr(R, "_read_rules", lambda s, r: [rule(wire=1, interval=1, count=0)])
    R._apply_repeat(svc, reminder(), "monthly", 1, 0)
    (call,) = svc._writes.updates
    assert fields_of(call) == {"Frequency": 2} and call["record_name"] == "RecurrenceRule/1"
    assert svc._writes.created == [] and svc.deleted == []
    svc._writes.updates.clear()
    R._apply_repeat(svc, reminder(), "weekly", 3, 4)
    assert fields_of(svc._writes.updates[0]) == {"Interval": 3, "OccurrenceCount": 4}


def test_identical_rule_writes_nothing_and_none_count_equals_zero(monkeypatch):
    svc = FakeSvc()
    monkeypatch.setattr(R, "_read_rules", lambda s, r: [rule(wire=2, interval=1, count=None)])
    R._apply_repeat(svc, reminder(), "monthly", 1, 0)
    assert svc._writes.updates == [] and svc._writes.created == [] and svc.deleted == []


def test_stray_extra_rules_are_removed_and_none_removes_all(monkeypatch):
    svc = FakeSvc()
    monkeypatch.setattr(R, "_read_rules", lambda s, r: [rule("RecurrenceRule/1"), rule("RecurrenceRule/2")])
    R._apply_repeat(svc, reminder(), "weekly", 1, 0)
    assert svc.deleted == ["RecurrenceRule/2"] and svc._writes.updates == []
    svc.deleted.clear()
    R._apply_repeat(svc, reminder(), "none", 1, 0)
    assert svc.deleted == ["RecurrenceRule/1", "RecurrenceRule/2"]


def test_repeat_none_and_invalid_input(monkeypatch):
    svc = FakeSvc()
    monkeypatch.setattr(R, "_read_rules", lambda s, r: [rule()])
    R._apply_repeat(svc, reminder(), None, 1, 0)  # untouched
    assert svc.deleted == [] and svc._writes.updates == []
    for kwargs, msg in ((("hourly", 1, 0), "repeat must be"), (("daily", 0, 0), "at least 1"), (("daily", 1, -1), "negative")):
        with pytest.raises(ValueError, match=msg):
            R._apply_repeat(svc, reminder(), *kwargs)


# ---- keep alarms tied to the due date ------------------------------------
def test_sync_alarm_nonces_updates_only_stale_alarms(monkeypatch):
    tm = {"map": {"dueDate": {"counter": 1, "modificationTime": 800.0}}}
    want = 1e11 + 800.0
    records = {"Alarm/stale": want - 5, "Alarm/fresh": want}
    svc = FakeSvc()
    monkeypatch.setattr(R, "_reminder_state", lambda s, r: ([], tm, "tag"))
    monkeypatch.setattr(R, "_read_alarms", lambda s, r: [{"name": n, "tag": "t"} for n in records])
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: {"fields": {"DueDateResolutionTokenAsNonce": {"value": records[n]}}})
    R._sync_alarm_nonces(svc, SimpleNamespace(id="R/1", alarm_ids=["a", "b"]))
    assert [c["record_name"] for c in svc._writes.updates] == ["Alarm/stale"]
    assert fields_of(svc._writes.updates[0]) == {"DueDateResolutionTokenAsNonce": want}
    svc._writes.updates.clear()
    R._sync_alarm_nonces(svc, SimpleNamespace(id="R/1", alarm_ids=[]))  # no alarms: no lookups at all
    assert svc._writes.updates == []


# ---- move ----------------------------------------------------------------
def test_move_writes_the_list_reference_and_keeps_the_due_token(monkeypatch):
    tm = {"map": {"dueDate": {"counter": 3, "modificationTime": 1.0, "replicaID": "x"}}}
    svc = FakeSvc()
    monkeypatch.setattr(R, "_reminder_state", lambda s, r: ([], tm, "tag7"))
    R._move_reminder(svc, SimpleNamespace(id="Reminder/1"), SimpleNamespace(id="ABC", title="Work"))
    (call,) = svc._writes.updates
    assert call["record_change_tag"] == "tag7"
    assert call["fields"]["List"]["value"]["recordName"] == "List/ABC"
    new_map = json.loads(call["fields"]["ResolutionTokenMap"]["value"])["map"]
    assert new_map["dueDate"] == tm["map"]["dueDate"] and "list" in new_map


# ---- subtasks ------------------------------------------------------------
def test_subtasks_are_the_live_children_of_a_reminder():
    kids = [SimpleNamespace(id="R/2", parent_reminder_id="R/1", deleted=False),
            SimpleNamespace(id="R/3", parent_reminder_id="R/1", deleted=True),
            SimpleNamespace(id="R/4", parent_reminder_id="R/9", deleted=False),
            SimpleNamespace(id="R/1", parent_reminder_id=None, deleted=False)]
    svc = SimpleNamespace(list_reminders=lambda lid, include_completed: SimpleNamespace(reminders=kids))
    assert [k.id for k in R._subtasks_of(svc, SimpleNamespace(id="R/1", list_id="L"))] == ["R/2"]


# ---- bulk selection and planning -----------------------------------------
def item(title, due=None, flagged=False, priority=0, completed=False, list_="Groceries", **kw):
    return {"id": f"R/{title}", "title": title, "list": list_, "due": due, "flagged": flagged,
            "priority": priority, "completed": completed, **kw}


ITEMS = [
    item("late milk", due="2026-10-10"),
    item("today eggs", due="2026-10-14", flagged=True, priority=1),
    item("next week", due="2026-10-21T09:00:00+03:00"),
    item("undated"),
    item("done thing", due="2026-10-01", completed=True),
]


def names(items):
    return [i["title"] for i in items]


def test_filters_select_the_right_reminders():
    f = lambda **kw: names(R._filter_bulk(ITEMS, kw.get("flagged"), kw.get("priority", ""), kw.get("overdue", False),
                                         kw.get("due_from", ""), kw.get("due_to", ""), kw.get("title", ""),
                                         kw.get("include_completed", False), "2026-10-14"))
    assert f(overdue=True) == ["late milk"]  # completed is skipped, today isn't overdue
    assert f(overdue=True, include_completed=True) == ["late milk"]  # overdue means not done
    assert f(include_completed=True, title="done") == ["done thing"]
    assert f(due_from="2026-10-14", due_to="2026-10-14") == ["today eggs"]
    assert f(due_to="2026-10-14") == ["late milk", "today eggs"]  # undated never matches a date filter
    assert f(flagged=True) == ["today eggs"]
    assert f(title="MILK") == ["late milk"]


def test_shift_due_keeps_the_wall_clock_time(monkeypatch):
    monkeypatch.setattr(R, "get_localzone", lambda: timezone(timedelta(hours=3)))
    assert R._shift_due("2026-10-31", 2) == "2026-11-02"
    assert R._shift_due("2026-10-14", -14) == "2026-09-30"
    assert R._shift_due("2026-10-21T09:00:00+03:00", 1) == "2026-10-22T09:00:00"
    assert R._shift_due("2026-10-21T22:00:00+00:00", 1) == "2026-10-23T01:00:00"  # 22:00 UTC is 01:00 local, +1 day


def test_plan_skips_reminders_that_would_not_change():
    target = SimpleNamespace(title="Work")
    plan, skipped = R._plan_bulk("move", [item("a", list_="Work"), item("b")], target, 0, "", "")
    assert names(plan) == ["b"] and plan[0]["change"] == "move to Work" and skipped[0]["why"] == "already in that list"
    plan, skipped = R._plan_bulk("complete", [item("a", completed=True), item("b")], None, 0, "", "")
    assert names(plan) == ["b"] and names(skipped) == ["a"]
    plan, skipped = R._plan_bulk("flag", [item("a", flagged=True), item("b")], None, 0, "", "")
    assert names(plan) == ["b"]
    plan, skipped = R._plan_bulk("set_priority", [item("a", priority=1), item("b")], None, 0, "", "high")
    assert names(plan) == ["b"]


def test_plan_reschedule_shifts_or_sets_and_skips_undated(monkeypatch):
    monkeypatch.setattr(R, "get_localzone", lambda: timezone.utc)
    plan, skipped = R._plan_bulk("reschedule", [item("a", due="2026-10-14"), item("b")], None, 3, "", "")
    assert plan[0]["new_due"] == "2026-10-17" and names(skipped) == ["b"] and skipped[0]["why"] == "no due date to shift"
    plan, skipped = R._plan_bulk("reschedule", [item("a"), item("b", due="2026-10-14")], None, 0, "2027-01-01", "")
    assert [p["new_due"] for p in plan] == ["2027-01-01", "2027-01-01"] and skipped == []


def test_delete_plan_is_labelled_clearly():
    plan, _ = R._plan_bulk("delete", [item("a")], None, 0, "", "")
    assert plan[0]["change"] == "DELETE"
