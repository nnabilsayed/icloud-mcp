"""The activity log and undo."""
import json
from types import SimpleNamespace

import pytest

import activity
import reminders as R
import server as S


# ---- the log itself ------------------------------------------------------
def test_record_and_read_back_newest_first():
    a = activity.record("create_event", "first", undo={"op": "x"})
    b = activity.record("update_event", "second")
    rows = activity.entries()
    assert [r["id"] for r in rows] == [b, a]
    assert rows[1]["agent"] == "pytest" and rows[1]["undo"] == {"op": "x"} and rows[1]["undone"] is False


def test_filters_and_limit():
    activity.record("create_event", "one", undo={"op": "x"})
    activity.record("delete_event", "two")
    activity.record("create_event", "three", undo={"op": "x"})
    assert [r["summary"] for r in activity.entries(tool="create_event")] == ["three", "one"]
    assert [r["summary"] for r in activity.entries(only_undoable=True)] == ["three", "one"]
    assert [r["summary"] for r in activity.entries(limit=1)] == ["three"]
    assert activity.entries(agent="someone else") == []
    assert len(activity.entries(agent="PYTEST")) == 3  # names compare case-insensitively


def test_undone_is_its_own_line_and_hides_from_undoable():
    i = activity.record("create_event", "x", undo={"op": "x"})
    activity.mark_undone(i, by="codex")
    row = activity.get(i)
    assert row["undone"] is True and row["undone_by"] == "codex"
    assert activity.entries(only_undoable=True) == []
    assert len(activity.log_path().read_text(encoding="utf-8").splitlines()) == 2  # nothing was rewritten


def test_batches_group_and_come_back_newest_first():
    with activity.batch() as bid:
        a = activity.record("update_reminder", "a", undo={"op": "x"})
        b = activity.record("update_reminder", "b", undo={"op": "x"})
    outside = activity.record("update_reminder", "c", undo={"op": "x"})
    assert [r["id"] for r in activity.batch_entries(bid)] == [b, a]
    assert activity.get(outside)["batch"] is None and activity.get(a)["batch"] == bid
    activity.mark_undone(b)
    assert [r["id"] for r in activity.batch_entries(bid)] == [a]


def test_torn_lines_are_skipped():
    activity.record("create_event", "ok", undo={"op": "x"})
    with open(activity.log_path(), "a", encoding="utf-8") as f:
        f.write('{"event": "action", "id": "broken\n')
    assert [r["summary"] for r in activity.entries()] == ["ok"]


def test_the_file_is_trimmed_but_keeps_the_newest(monkeypatch):
    monkeypatch.setattr(activity, "MAX_ENTRIES", 5)
    for i in range(40):
        activity.record("create_event", f"entry {i}" + "x" * 3000, undo={"op": "x"})
    rows = activity.entries(limit=100)
    assert 5 <= len(rows) <= 40 and rows[0]["summary"].startswith("entry 39")


def test_agent_name_resolution(monkeypatch):
    monkeypatch.setenv("ICLOUD_MCP_AGENT", "fallback")
    activity.set_agent_resolver(lambda: "claude-code")
    assert activity.agent_name() == "claude-code"
    activity.set_agent_resolver(lambda: (_ for _ in ()).throw(RuntimeError("no request")))
    assert activity.agent_name() == "fallback"  # outside a request
    activity.set_agent_resolver(lambda: "")
    assert activity.agent_name() == "fallback"
    activity.set_agent_resolver(None)
    monkeypatch.delenv("ICLOUD_MCP_AGENT")
    assert activity.agent_name() == "unknown"


# ---- calendar changes are logged and reversible --------------------------
def snapshot_of(cal):
    return sorted((str(e.comp["uid"]), str(e.comp.get("summary")), str(e.comp["dtstart"].dt)) for e in cal.items)


def test_create_is_logged_and_undone(cal):
    ev = S.create_event("Home", "Dentist", "2026-11-05T10:00:00")
    assert ev["log_id"] and len(cal.items) == 1
    row = activity.get(ev["log_id"])
    assert row["tool"] == "create_event" and "Dentist" in row["summary"] and row["undo"]["op"] == "delete_event"
    out = S.undo(ev["log_id"])
    assert cal.items == [] and out["undone"][0]["id"] == ev["log_id"]
    assert activity.get(ev["log_id"])["undone"] is True


def test_update_is_undone_exactly(cal):
    ev = S.create_event("Home", "Dentist", "2026-11-05T10:00:00", location="Cairo", alerts=[30])
    before = snapshot_of(cal)
    upd = S.update_event("Home", ev["uid"], summary="Dentist (moved)", start="2026-11-06T11:00:00", location="Giza", alerts=[])
    assert snapshot_of(cal) != before and "title" in activity.get(upd["log_id"])["summary"]
    S.undo(upd["log_id"])
    assert snapshot_of(cal) == before
    restored = S._summarize(cal.items[0])
    assert restored["location"] == "Cairo" and restored["alerts"] == [30]


def test_delete_is_undone_with_the_same_uid_and_series(cal):
    ev = S.create_event("Home", "Standup", "2026-11-02T09:00:00", repeat="weekly", repeat_count=4)
    msg = S.delete_event("Home", ev["uid"])
    assert cal.items == [] and "log id" in msg
    log_id = msg.split("log id ")[1].rstrip(")")
    S.undo(log_id)
    back = S._summarize(cal.items[0])
    assert back["uid"] == ev["uid"] and back["repeat"]["count"] == 4


def test_undo_with_no_id_takes_the_latest_undoable(cal):
    first = S.create_event("Home", "one", "2026-11-05T10:00:00")
    second = S.create_event("Home", "two", "2026-11-06T10:00:00")
    out = S.undo()
    assert out["undone"][0]["id"] == second["log_id"] and len(cal.items) == 1
    assert activity.get(first["log_id"])["undone"] is False


def test_undo_refuses_bad_requests(cal):
    ev = S.create_event("Home", "x", "2026-11-05T10:00:00")
    S.undo(ev["log_id"])
    for kwargs, msg in ((dict(entry_id=ev["log_id"]), "already undone"), (dict(entry_id="nope"), "No log entry"),
                        (dict(entry_id="a", batch="b"), "not both"), (dict(batch="zzzz"), "No undoable")):
        with pytest.raises(ValueError, match=msg):
            S.undo(**kwargs)
    with pytest.raises(ValueError, match="Nothing to undo"):
        S.undo()
    undo_entry = activity.entries(tool="undo")[0]
    with pytest.raises(ValueError, match="can't be undone"):
        S.undo(undo_entry["id"])  # an undo isn't itself undoable


def test_a_failed_undo_reports_and_stays_undoable(cal):
    ev = S.create_event("Home", "x", "2026-11-05T10:00:00")
    cal.items.clear()  # someone deleted it in the app meanwhile
    out = S.undo(ev["log_id"])
    assert out["undone"] == [] and "not found" in out["failed"]["error"]
    assert activity.get(ev["log_id"])["undone"] is False


def test_activity_log_tool_shape(cal):
    ev = S.create_event("Home", "x", "2026-11-05T10:00:00")
    (row,) = S.activity_log()
    assert row["id"] == ev["log_id"] and row["undoable"] is True and row["agent"] == "pytest" and row["tool"] == "create_event"
    S.undo(ev["log_id"])
    rows = S.activity_log()
    assert [r["tool"] for r in rows] == ["undo", "create_event"]
    assert S.activity_log(only_undoable=True) == []


# ---- reminders: snapshot, restore, undelete ------------------------------
class FakeSvc:
    def __init__(self, reminder, lists=(), kids=()):
        self.r = reminder
        self.kids = list(kids)
        self.updated = []
        self._lists = list(lists)
        self._writes = SimpleNamespace(_reminder_record_name=lambda rid: rid, _submit_single_record_update=self._submit)
        self.submitted = []

    def get(self, rid):
        return next(x for x in [self.r, *self.kids] if x.id == rid)

    def update(self, r):
        self.updated.append((r.title, r.desc, r.priority, r.flagged, r.completed, r.due_date, r.all_day, r.parent_reminder_id))

    def lists(self):
        return self._lists

    def list_reminders(self, list_id, include_completed=False):
        return SimpleNamespace(reminders=[self.r, *self.kids])

    def _submit(self, **kw):
        self.submitted.append(kw)


def rem(**kw):
    base = dict(id="R/1", list_id="List/A", title="t", desc="n", completed=False, flagged=False, priority=0,
                parent_reminder_id=None, due_date=None, all_day=False, time_zone=None, deleted=False,
                alarm_ids=[], attachment_ids=[], recurrence_rule_ids=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_snapshot_only_reads_the_groups_that_will_change(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "_read_rules", lambda s, r: calls.append("rules") or [{"wire": 1, "interval": 2, "count": None}])
    monkeypatch.setattr(R, "_read_alarms", lambda s, r: calls.append("alarms") or [])
    svc = SimpleNamespace(tags_for=lambda r: calls.append("tags") or [])
    snap = R._snapshot(svc, rem(), set())
    assert calls == [] and snap["title"] == "t" and "repeat" not in snap and snap["due"] is None
    snap = R._snapshot(svc, rem(), {"repeat"})
    assert snap["repeat"] == {"wire": 1, "interval": 2, "count": 0} and calls == ["rules"]


def test_restore_puts_back_only_what_the_snapshot_holds(monkeypatch):
    r = rem(title="changed", completed=True)
    svc = FakeSvc(r)
    monkeypatch.setattr(R, "_sync_alarm_nonces", lambda s, x: None)
    R._restore(svc, {"id": "R/1", "completed": False})  # the light snapshot complete_reminder takes
    assert r.title == "changed" and r.completed is False  # title untouched


def test_restore_full_snapshot_including_repeat_alarms_and_list(monkeypatch):
    r = rem(title="new", list_id="List/B", priority=5, flagged=True)
    kid = rem(id="R/2", title="kid", parent_reminder_id="R/1", list_id="List/B")
    svc = FakeSvc(r, lists=[SimpleNamespace(id="List/A", title="Groceries", deleted=False)], kids=[kid])
    seen = {}
    monkeypatch.setattr(R, "_apply_repeat", lambda s, x, name, i, c: seen.__setitem__("repeat", (name, i, c)))
    monkeypatch.setattr(R, "_apply_tags", lambda s, x, t: seen.__setitem__("tags", t))
    monkeypatch.setattr(R, "_apply_links", lambda s, x, l: seen.__setitem__("links", l))
    monkeypatch.setattr(R, "_apply_alarms", lambda s, x, a: seen.__setitem__("alarms", a))
    monkeypatch.setattr(R, "_move_reminder", lambda s, x, target: seen.setdefault("moved", []).append((x.id, target.title)))
    monkeypatch.setattr(R, "_sync_alarm_nonces", lambda s, x: None)
    snap = {"id": "R/1", "title": "old", "notes": "", "priority": 0, "flagged": False, "completed": False,
            "due": "2026-10-20", "parent": None, "list_id": "List/A",
            "repeat": {"wire": 2, "interval": 3, "count": 6}, "tags": ["a"], "links": ["https://x.example"],
            "alarms": ["2026-10-19T18:00"]}
    monkeypatch.setattr(R, "_snapshot", lambda s, x, g: dict(snap))  # the re-read matches: no retry
    R._restore(svc, snap)
    assert (r.title, r.priority, r.flagged, r.all_day) == ("old", 0, False, True)
    assert seen == {"repeat": ("monthly", 3, 6), "tags": ["a"], "links": ["https://x.example"],
                    "alarms": ["2026-10-19T18:00"], "moved": [("R/1", "Groceries"), ("R/2", "Groceries")]}


def test_restore_repeat_none_and_missing_list(monkeypatch):
    r = rem(list_id="List/B")
    svc = FakeSvc(r, lists=[])
    seen = {}
    monkeypatch.setattr(R, "_apply_repeat", lambda s, x, name, i, c: seen.__setitem__("repeat", name))
    monkeypatch.setattr(R, "_sync_alarm_nonces", lambda s, x: None)
    with pytest.raises(ValueError, match="no longer exists"):
        R._restore(svc, {"id": "R/1", "repeat": None, "list_id": "List/A"})
    assert seen["repeat"] == "none"


def test_restore_refuses_a_deleted_reminder():
    with pytest.raises(ValueError, match="deleted"):
        R._restore(FakeSvc(rem(deleted=True)), {"id": "R/1", "title": "x"})


def test_undelete_flips_the_flag_and_keeps_the_token_map(monkeypatch):
    svc = FakeSvc(rem())
    tm = {"map": {"dueDate": {"counter": 2, "modificationTime": 1.0, "replicaID": "x"}}}
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: {"recordChangeTag": "t9", "fields": {"ResolutionTokenMap": {"value": json.dumps(tm)}}})
    R._undelete(svc, "R/1")
    (call,) = svc.submitted
    assert call["fields"]["Deleted"]["value"] == 0 and call["record_change_tag"] == "t9"
    new_map = json.loads(call["fields"]["ResolutionTokenMap"]["value"])["map"]
    assert new_map["dueDate"] == tm["map"]["dueDate"] and "deleted" in new_map
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: None)
    with pytest.raises(ValueError, match="no longer exists"):
        R._undelete(svc, "R/1")


def test_undo_op_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(R, "_service", lambda: "svc")
    monkeypatch.setattr(R, "_delete_with_subtasks", lambda s, rid: calls.append(("delete", rid)) or [rid])
    monkeypatch.setattr(R, "_undelete", lambda s, rid: calls.append(("undelete", rid)))
    monkeypatch.setattr(R, "_restore", lambda s, snap: calls.append(("restore", snap["id"])))
    R.undo_op({"op": "delete_reminder", "id": "R/1"})
    R.undo_op({"op": "undelete_reminder", "ids": ["R/1", "R/2"]})
    R.undo_op({"op": "restore_reminder", "snapshot": {"id": "R/3"}})
    assert calls == [("delete", "R/1"), ("undelete", "R/1"), ("undelete", "R/2"), ("restore", "R/3")]
    with pytest.raises(ValueError, match="Unknown"):
        R.undo_op({"op": "nope"})


def test_undo_routes_reminder_ops_to_the_reminders_module(cal, monkeypatch):
    seen = []
    monkeypatch.setattr(R, "undo_op", lambda op: seen.append(op["op"]) or "done")
    rid = activity.record("delete_reminder", "Deleted reminder 'x'", undo={"op": "undelete_reminder", "ids": ["R/1"]})
    out = S.undo(rid)
    assert seen == ["undelete_reminder"] and out["undone"][0]["result"] == "done"


def test_batch_undo_runs_newest_first_and_stops_at_a_failure(cal, monkeypatch):
    order = []
    def fake(op):
        order.append(op["n"])
        if op["n"] == 2:
            raise RuntimeError("changed since")
        return "ok"
    monkeypatch.setattr(R, "undo_op", fake)
    with activity.batch() as bid:
        for n in (1, 2, 3):
            activity.record("update_reminder", f"step {n}", undo={"op": "restore_reminder", "n": n})
    out = S.undo(batch=bid)
    assert order == [3, 2] and [d["summary"] for d in out["undone"]] == ["step 3"] and out["failed"]["summary"] == "step 2"
    assert [r["summary"] for r in activity.batch_entries(bid)] == ["step 2", "step 1"]  # still undoable


# ---- the reminder tools themselves log what they do ----------------------
class FakeReminders:
    """A tiny in-memory reminders service for exercising the tool bodies."""

    def __init__(self, *items):
        self.items = {r.id: r for r in items}
        self.deleted = []

    def get(self, rid):
        return self.items[rid]

    def update(self, r):
        pass

    def delete(self, r):
        r.deleted = True
        self.deleted.append(r.id)

    def list_reminders(self, list_id, include_completed=False):
        return SimpleNamespace(reminders=list(self.items.values()))


@pytest.fixture
def tools(monkeypatch):
    class Fake:
        def __init__(self):
            self.t = {}

        def tool(self):
            return lambda f: (self.t.__setitem__(f.__name__, f), f)[1]

    fake = Fake()
    R.register(fake)
    monkeypatch.setattr(R, "_sync_alarm_nonces", lambda s, r: None)
    return fake.t


def test_delete_reminder_logs_an_undelete_covering_its_subtasks(tools, monkeypatch):
    svc = FakeReminders(rem(id="R/1", title="parent"), rem(id="R/2", title="kid", parent_reminder_id="R/1"))
    monkeypatch.setattr(R, "_service", lambda: svc)
    msg = tools["delete_reminder"]("R/1")
    assert svc.deleted == ["R/2", "R/1"] and "log id" in msg
    (row,) = activity.entries(tool="delete_reminder")
    assert row["undo"] == {"op": "undelete_reminder", "ids": ["R/1", "R/2"]} and "1 subtask" in row["summary"]


def test_complete_reminder_logs_the_previous_state(tools, monkeypatch):
    svc = FakeReminders(rem(id="R/1", title="milk", completed=False))
    monkeypatch.setattr(R, "_service", lambda: svc)
    out = tools["complete_reminder"]("R/1", True)
    assert out["log_id"] and svc.items["R/1"].completed is True
    row = activity.get(out["log_id"])
    assert row["undo"] == {"op": "restore_reminder", "snapshot": {"id": "R/1", "completed": False}}
    R._restore(svc, row["undo"]["snapshot"])
    assert svc.items["R/1"].completed is False  # and the undo really reverses it


# ---- restore verifies itself ---------------------------------------------
def test_restore_retries_when_the_reread_differs_then_succeeds(monkeypatch):
    svc = FakeSvc(rem())
    monkeypatch.setattr(R, "_sync_alarm_nonces", lambda s, x: None)
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    reads = iter([{"title": "mixed up", "tags": ["a", "b"]}, {"title": "old", "tags": ["b", "a"]}])
    monkeypatch.setattr(R, "_snapshot", lambda s, x, g: next(reads))
    monkeypatch.setattr(R, "_apply_tags", lambda s, x, t: None)
    R._restore(svc, {"id": "R/1", "title": "old", "tags": ["a", "b"]})
    assert len(svc.updated) == 2  # written twice; list order doesn't count as a difference


def test_restore_reports_what_still_differs_after_the_last_attempt(monkeypatch):
    svc = FakeSvc(rem())
    monkeypatch.setattr(R, "_sync_alarm_nonces", lambda s, x: None)
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    monkeypatch.setattr(R, "_snapshot", lambda s, x, g: {"title": "origED"})
    with pytest.raises(ValueError, match="still differ from before: title"):
        R._restore(svc, {"id": "R/1", "title": "orig"})
    assert len(svc.updated) == 3


# ---- logging never breaks a tool -----------------------------------------
def test_a_locked_log_file_never_breaks_trimming(monkeypatch):
    monkeypatch.setattr(activity, "MAX_ENTRIES", 5)
    monkeypatch.setattr(activity.time, "sleep", lambda s: None)
    monkeypatch.setattr(activity.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("in use")))
    for i in range(30):
        assert activity.record("create_event", f"entry {i}" + "x" * 3000, undo={"op": "x"}) != "unlogged"
    assert activity.entries(limit=1)[0]["summary"].startswith("entry 29")


def test_record_survives_an_unwritable_log(monkeypatch, capsys):
    monkeypatch.setattr(activity, "_append", lambda obj: (_ for _ in ()).throw(OSError("disk full")))
    assert activity.record("create_event", "x") == "unlogged"
    assert "could not record" in capsys.readouterr().err
    activity.mark_undone("abc")  # must not raise either


# ---- saves survive a stale change tag ------------------------------------
class ConflictSvc:
    def __init__(self, fail_times, message="CloudKit rejected 1 record(s): CONFLICT (client oplock error updating record)"):
        self.fail_times, self.message, self.calls, self.tags = fail_times, message, 0, []
        self._writes = SimpleNamespace(_reminder_record_name=lambda rid: rid)

    def update(self, r):
        self.calls += 1
        self.tags.append(r.record_change_tag)
        if self.calls <= self.fail_times:
            raise RuntimeError(self.message)


def test_update_retries_a_conflict_with_a_fresh_change_tag(monkeypatch):
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: {"recordChangeTag": "fresh"})
    svc, r = ConflictSvc(fail_times=2), rem(record_change_tag="stale")
    R._update(svc, r)
    assert svc.calls == 3 and svc.tags == ["stale", "fresh", "fresh"]


def test_update_gives_up_after_three_conflicts_and_never_retries_other_errors(monkeypatch):
    monkeypatch.setattr(R.time, "sleep", lambda s: None)
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: {"recordChangeTag": "fresh"})
    svc = ConflictSvc(fail_times=9)
    with pytest.raises(RuntimeError, match="CONFLICT"):
        R._update(svc, rem(record_change_tag="t"))
    assert svc.calls == 3
    other = ConflictSvc(fail_times=9, message="CloudKit rejected: ACCESS_DENIED")
    with pytest.raises(RuntimeError, match="ACCESS_DENIED"):
        R._update(other, rem(record_change_tag="t"))
    assert other.calls == 1
