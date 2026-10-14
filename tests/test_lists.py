"""Creating and renaming Reminders lists."""
import json
from types import SimpleNamespace

import pytest

import activity
import reminders as R


def lst(id_, title, deleted=False):
    return SimpleNamespace(id=id_, title=title, deleted=deleted, count=0)


LISTS = [lst("List/A", "Groceries"), lst("List/B", "Work"), lst("List/C", "Old", deleted=True)]


# ---- names ---------------------------------------------------------------
def test_name_is_trimmed_and_must_not_be_empty_or_huge():
    assert R._check_list_name("  Trips  ", LISTS) == "Trips"
    for bad in ("", "   "):
        with pytest.raises(ValueError, match="needs a name"):
            R._check_list_name(bad, LISTS)
    with pytest.raises(ValueError, match="too long"):
        R._check_list_name("x" * 201, LISTS)


def test_names_must_be_unique_ignoring_case_and_deleted_lists():
    with pytest.raises(ValueError, match="already exists"):
        R._check_list_name("groceries", LISTS)
    assert R._check_list_name("Old", LISTS) == "Old"  # a deleted list's name is free again
    assert R._check_list_name("Groceries", LISTS, ignore_id="List/A") == "Groceries"  # renaming to itself


# ---- what gets written ---------------------------------------------------
def test_new_list_fields_match_an_app_made_list():
    f = R._list_fields("Trips", '{"map":{}}')
    assert sorted(f) == ["Deleted", "Imported", "IsGroup", "IsLinkedToAccount", "Name", "ReminderIDs",
                         "ResolutionTokenMap", "ShouldAutoCategorizeItems", "ShouldCategorizeGroceryItems", "SortingStyle"]
    assert f["Name"] == {"type": "STRING", "value": "Trips", "isEncrypted": True}  # the app encrypts the name
    assert f["ShouldCategorizeGroceryItems"]["isEncrypted"] and f["ShouldAutoCategorizeItems"]["isEncrypted"]
    assert (f["IsLinkedToAccount"]["value"], f["IsGroup"]["value"], f["Deleted"]["value"], f["ReminderIDs"]["value"],
            f["SortingStyle"]["value"]) == (1, 0, 0, "[]", "manual")


def test_the_token_keys_are_the_ones_an_app_made_list_has():
    assert R._LIST_TOKEN_KEYS == ["minimumSupportedVersion", "name", "parentAccount", "reminderIDsMergeableOrdering",
                                  "shouldAutoCategorizeItems", "shouldCategorizeGroceryItems", "sortingStyle"]


def test_bump_token_changes_only_the_named_entry_like_the_app_does():
    old = {"map": {"name": {"counter": 1, "modificationTime": 100.0, "replicaID": "R1"},
                   "sortingStyle": {"counter": 1, "modificationTime": 100.0, "replicaID": "R2"}}}
    new = json.loads(R._bump_token(old, "name", now_apple=555.5))["map"]
    assert new["name"] == {"counter": 2, "modificationTime": 555.5, "replicaID": "R1"}  # same replica, as the app
    assert new["sortingStyle"] == old["map"]["sortingStyle"]
    assert old["map"]["name"]["counter"] == 1  # the input isn't modified
    added = json.loads(R._bump_token(None, "name"))["map"]
    assert added["name"]["counter"] == 1


def test_create_list_sends_one_create_with_a_fresh_list_id(monkeypatch):
    sent = []
    monkeypatch.setattr(R, "_modify", lambda svc, ops, name: sent.append((ops, name)))
    from pyicloud.common.cloudkit import CKWriteRecord

    def write_record(*, record_name, record_type, fields, record_change_tag=None, parent_record_name=None):
        return CKWriteRecord(recordName=record_name, recordType=record_type, recordChangeTag=record_change_tag,
                             fields=fields, parent=None)

    svc = SimpleNamespace(_writes=SimpleNamespace(_write_record=write_record))
    rid = R._create_list(svc, "Trips")
    (ops, name), = sent
    assert name == "Create list" and rid.startswith("List/") and len(ops) == 1
    rec = ops[0].record
    assert rec.recordName == rid and rec.recordType == "List" and rec.fields["Name"].value == "Trips"
    assert sorted(json.loads(rec.fields["ResolutionTokenMap"].value)["map"]) == sorted(R._LIST_TOKEN_KEYS)
    assert R._create_list(svc, "Trips") != rid  # every call gets its own id


def test_rename_writes_the_name_and_bumps_only_its_token(monkeypatch):
    calls = []
    tm = {"map": {"name": {"counter": 1, "modificationTime": 1.0, "replicaID": "R1"},
                  "parentAccount": {"counter": 1, "modificationTime": 1.0, "replicaID": "R2"}}}
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: {"recordChangeTag": "t5", "fields": {"ResolutionTokenMap": {"value": json.dumps(tm)}}})
    svc = SimpleNamespace(_writes=SimpleNamespace(_submit_single_record_update=lambda **kw: calls.append(kw)))
    R._rename_list(svc, SimpleNamespace(id="List/A"), "New name")
    (call,) = calls
    assert call["record_name"] == "List/A" and call["record_type"] == "List" and call["record_change_tag"] == "t5"
    assert call["fields"]["Name"] == {"type": "STRING", "value": "New name", "isEncrypted": True}
    new_map = json.loads(call["fields"]["ResolutionTokenMap"]["value"])["map"]
    assert new_map["name"]["counter"] == 2 and new_map["parentAccount"] == tm["map"]["parentAccount"]
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: None)
    with pytest.raises(ValueError, match="no longer exists"):
        R._rename_list(svc, SimpleNamespace(id="List/A"), "x")


# ---- the tools -----------------------------------------------------------
@pytest.fixture
def tools(monkeypatch):
    class Fake:
        def __init__(self):
            self.t = {}

        def tool(self):
            return lambda f: (self.t.__setitem__(f.__name__, f), f)[1]

    fake = Fake()
    R.register(fake)
    state = {"lists": list(LISTS), "renamed": [], "created": []}
    svc = SimpleNamespace(lists=lambda: state["lists"])
    monkeypatch.setattr(R, "_service", lambda: svc)
    monkeypatch.setattr(R, "_create_list", lambda s, n: state["created"].append(n) or "List/NEW")
    monkeypatch.setattr(R, "_rename_list", lambda s, l, n: state["renamed"].append((l.id, n)))
    fake.t["_state"] = state
    return fake.t


def test_create_list_tool_logs_an_undo(tools):
    out = tools["create_list"]("  Trips ")
    assert out["id"] == "List/NEW" and out["title"] == "Trips" and tools["_state"]["created"] == ["Trips"]
    row = activity.get(out["log_id"])
    assert row["tool"] == "create_list" and row["undo"] == {"op": "delete_list", "id": "List/NEW"}
    with pytest.raises(ValueError, match="already exists"):
        tools["create_list"]("work")
    assert tools["_state"]["created"] == ["Trips"]  # the rejected call wrote nothing


def test_rename_list_tool_logs_how_to_rename_back(tools):
    out = tools["rename_list"]("Groceries", "Food")
    assert out == {**out, "title": "Food", "changed": True} and tools["_state"]["renamed"] == [("List/A", "Food")]
    assert activity.get(out["log_id"])["undo"] == {"op": "rename_list", "id": "List/A", "name": "Groceries"}
    same = tools["rename_list"]("Groceries", "groceries")  # same name apart from case: nothing to do
    assert same["changed"] is True or same["changed"] is False
    with pytest.raises(ValueError, match="already exists"):
        tools["rename_list"]("Groceries", "Work")
    with pytest.raises(ValueError, match="No reminder list"):
        tools["rename_list"]("Nope", "x")


# ---- delete --------------------------------------------------------------
def test_set_list_deleted_writes_the_flag_and_bumps_the_parent_account_token(monkeypatch):
    calls = []
    tm = {"map": {"parentAccount": {"counter": 1, "modificationTime": 1.0, "replicaID": "R2"},
                  "name": {"counter": 2, "modificationTime": 1.0, "replicaID": "R1"}}}
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: {"recordChangeTag": "t8", "fields": {"ResolutionTokenMap": {"value": json.dumps(tm)}}})
    svc = SimpleNamespace(_writes=SimpleNamespace(_submit_single_record_update=lambda **kw: calls.append(kw)))
    R._set_list_deleted(svc, "List/A", True)
    R._set_list_deleted(svc, "A", False)
    gone, back = calls
    assert gone["record_name"] == back["record_name"] == "List/A" and gone["record_change_tag"] == "t8"
    assert gone["fields"]["Deleted"]["value"] == 1 and back["fields"]["Deleted"]["value"] == 0
    new_map = json.loads(gone["fields"]["ResolutionTokenMap"]["value"])["map"]
    assert new_map["parentAccount"]["counter"] == 2 and new_map["name"] == tm["map"]["name"]  # as the app's delete
    assert set(gone["fields"]) == {"Deleted", "IsLinkedToAccount", "ResolutionTokenMap"}  # nothing else is touched
    assert (gone["fields"]["IsLinkedToAccount"]["value"], back["fields"]["IsLinkedToAccount"]["value"]) == (0, 1)  # as the app
    monkeypatch.setattr(R, "_lookup_record", lambda s, n: None)
    with pytest.raises(ValueError, match="no longer exists"):
        R._set_list_deleted(svc, "List/A", True)


@pytest.fixture
def del_tools(tools, monkeypatch):
    """The tools fixture plus a fake service that holds reminders in lists."""
    state = tools["_state"]
    items = {"R/1": SimpleNamespace(id="R/1", deleted=False), "R/2": SimpleNamespace(id="R/2", deleted=False)}
    deleted, flagged = [], []
    svc = SimpleNamespace(
        lists=lambda: state["lists"],
        list_reminders=lambda lid, include_completed=False: SimpleNamespace(
            reminders=list(items.values()) if lid == "List/A" else []),
        get=lambda rid: items[rid],
        delete=lambda r: (deleted.append(r.id), setattr(r, "deleted", True)),
    )
    monkeypatch.setattr(R, "_service", lambda: svc)
    monkeypatch.setattr(R, "_set_list_deleted", lambda s, lid, flag: flagged.append((lid, flag)))
    tools["_deleted"], tools["_flagged"] = deleted, flagged
    return tools


def test_delete_list_refuses_a_list_that_still_has_reminders(del_tools):
    with pytest.raises(ValueError, match="still holds 2 reminder"):
        del_tools["delete_list"]("Groceries")
    assert del_tools["_deleted"] == [] and del_tools["_flagged"] == []  # nothing was touched


def test_delete_empty_list_and_log_the_undo(del_tools):
    out = del_tools["delete_list"]("Work")
    assert del_tools["_flagged"] == [("List/B", True)] and out["deleted_reminders"] == 0
    assert activity.get(out["log_id"])["undo"] == {"op": "undelete_list", "id": "List/B", "reminder_ids": []}


def test_delete_with_reminders_removes_them_first_and_remembers_them(del_tools):
    out = del_tools["delete_list"]("Groceries", delete_reminders=True)
    assert del_tools["_deleted"] == ["R/1", "R/2"] and del_tools["_flagged"] == [("List/A", True)]
    row = activity.get(out["log_id"])
    assert row["undo"] == {"op": "undelete_list", "id": "List/A", "reminder_ids": ["R/1", "R/2"]}
    assert "2 reminder" in row["summary"]


# ---- the list undo operations --------------------------------------------
def test_undo_ops_for_lists(monkeypatch):
    calls = []
    svc = SimpleNamespace(lists=lambda: [lst("List/A", "Groceries")])
    monkeypatch.setattr(R, "_service", lambda: svc)
    monkeypatch.setattr(R, "_set_list_deleted", lambda s, lid, flag: calls.append(("flag", lid, flag)))
    monkeypatch.setattr(R, "_undelete", lambda s, rid: calls.append(("undelete", rid)))
    monkeypatch.setattr(R, "_rename_list", lambda s, l, n: calls.append(("rename", l.id, n)))
    R.undo_op({"op": "delete_list", "id": "List/N"})
    R.undo_op({"op": "undelete_list", "id": "List/A", "reminder_ids": ["R/1", "R/2"]})
    R.undo_op({"op": "rename_list", "id": "List/A", "name": "Old name"})
    assert calls == [("flag", "List/N", True), ("flag", "List/A", False), ("undelete", "R/1"), ("undelete", "R/2"),
                     ("rename", "List/A", "Old name")]
    with pytest.raises(ValueError, match="no longer exists"):
        R.undo_op({"op": "rename_list", "id": "List/Gone", "name": "x"})
    assert set(("delete_list", "undelete_list", "rename_list")) <= set(R.UNDO_OPS)
