"""Real Apple Reminders tools via iCloud CloudKit (pyicloud).

Unofficial: uses the private web API behind icloud.com. Needs a cached,
trusted session created by running login.py once in a terminal (password +
2FA). Uses ICLOUD_USERNAME from the environment.
"""
import base64
import functools
import json
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import activity
from pyicloud import PyiCloudService
from tzlocal import get_localzone, get_localzone_name

_api = None


def _service():
    global _api
    if _api is None:
        api = PyiCloudService(os.environ["ICLOUD_USERNAME"])
        if api.requires_2fa or not api.is_trusted_session:
            raise RuntimeError(
                "iCloud session expired. Run `uv run python login.py` in the "
                "icloud-mcp folder to sign in again."
            )
        _api = api
    return _api.reminders


def _reset_on_error(fn):
    """Drop the cached session if a call fails so the next call re-authenticates."""
    @functools.wraps(fn)  # keeps the real signature so MCP builds the right schema
    def wrapper(*a, **kw):
        global _api
        try:
            return fn(*a, **kw)
        except Exception as exc:
            _api = None
            # iCloud drops idle connections now and then; retry once for those.
            # Writes can't be double-applied: the failure is raised before the
            # request is accepted, and creates have no client-side idempotency
            # risk beyond a duplicate, so only retry clear connection errors.
            if type(exc).__name__ == "PyiCloudConnectionException":
                return fn(*a, **kw)
            raise
    return wrapper


def _list(ident: str):
    """Resolve a list by id or title; error on unknown or ambiguous names."""
    lists = [l for l in _service().lists() if not l.deleted]
    exact = [l for l in lists if l.id == ident]
    if exact:
        return exact[0]
    named = [l for l in lists if l.title.strip().lower() == ident.strip().lower()]
    if len(named) == 1:
        return named[0]
    if not named:
        raise ValueError(f"No reminder list '{ident}'. Have: {[l.title for l in lists]}")
    raise ValueError(
        f"'{ident}' is ambiguous; use one of these ids: "
        f"{[{'id': l.id, 'count': l.count} for l in named]}"
    )


def _parse_due(due: str):
    """Return (datetime|None, all_day). Plain dates become all-day reminders."""
    if not due:
        return None, False
    if len(due) == 10:
        # The app stores an all-day date as midnight UTC of that date, no time zone.
        # Local midnight would land on the previous day for zones east of UTC.
        d = date.fromisoformat(due)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), True
    dt = datetime.fromisoformat(due)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_localzone())
    return dt, False


def _out(r, list_title: str | None = None) -> dict:
    # All-day dates are midnight UTC, so read the date in UTC; timed ones in local time.
    due = r.due_date.astimezone(timezone.utc if r.all_day else get_localzone()) if r.due_date else None
    return {
        "id": r.id,
        "list": list_title,
        "list_id": r.list_id,
        "parent": r.parent_reminder_id,
        "title": r.title,
        "notes": r.desc,
        "completed": r.completed,
        "due": (due.date().isoformat() if r.all_day else due.isoformat()) if due else None,
        "all_day": r.all_day,
        "flagged": r.flagged,
        "priority": r.priority,
    }


# --- Early reminder ("Early Reminder" in the app) -------------------------
# Not an alarm record: Apple keeps it in the reminder's DueDateDeltaAlertsData
# field as base64 JSON. Only unit 2 (= days) has been verified against the app.
_ACCOUNT_ID_FILE = Path(__file__).with_name(".account_id")
_UNIT_MINUTES = 0  # verified by decoding a "15 minutes before" alert made in the app
_UNIT_HOURS = 1  # verified by decoding a "3 hours before" alert made in the app
_UNIT_DAYS = 2


def _early_choice(days, hours, minutes):
    """The one early reminder asked for as (count, unit), or None when none was given.
    The app holds a single early reminder, so asking for two is an error, not a silent pick."""
    given = [(c, u) for c, u in ((minutes, _UNIT_MINUTES), (hours, _UNIT_HOURS), (days, _UNIT_DAYS))
             if c is not None]
    asked = [(c, u) for c, u in given if c]
    if len(asked) > 1:
        raise ValueError("Give only one of early_minutes, early_hours or early_days "
                         "(the app holds one early reminder).")
    if asked:
        return asked[0]
    return (0, _UNIT_DAYS) if given else None  # an explicit 0 clears it


def _raw_lookup(svc, rid: str) -> dict:
    from pyicloud.services.reminders._constants import _REMINDERS_ZONE_REQ

    resp = svc._raw.lookup([rid], zone_id=_REMINDERS_ZONE_REQ)
    return resp.records[0].model_dump()["fields"]


def _read_alerts_blob(svc, rid: str) -> dict | None:
    f = _raw_lookup(svc, rid).get("DueDateDeltaAlertsData")
    if not f or not f.get("value"):
        return None
    v = f["value"]
    return json.loads(base64.b64decode(v if isinstance(v, bytes) else str(v)))


def capture_account_id(svc, rid: str) -> str:
    """Remember Apple's account identifier from a reminder that already has an
    early reminder set in the app (it isn't stored anywhere else we can read)."""
    blob = _read_alerts_blob(svc, rid)
    if not blob or not blob.get("accountIdentifier"):
        raise ValueError("That reminder has no early reminder to learn from.")
    _ACCOUNT_ID_FILE.write_text(blob["accountIdentifier"])
    return blob["accountIdentifier"]


def _account_id() -> str:
    try:
        return _ACCOUNT_ID_FILE.read_text().strip()
    except FileNotFoundError:
        raise RuntimeError(
            "Early reminders need a one-time setup: set an early reminder on any "
            "reminder in the Reminders app, then ask me to capture the account id."
        )


def _set_early(svc, rid: str, count: int | None, unit: int = _UNIT_DAYS) -> None:
    """count=N -> N units before due (unit 1=hours, 2=days); 0/None -> clear."""
    from pyicloud.services.reminders._protocol import _generate_resolution_token_map

    r = svc.get(rid)
    if count and r.due_date is None:
        raise ValueError("An early reminder needs a due date.")
    alerts = []
    if count:
        alerts.append({
            "dueDateDeltaCount": -abs(int(count)),
            "creationDate": time.time() - 978307200.0,  # Apple reference epoch
            "identifier": str(uuid.uuid4()).upper(),
            "minimumSupportedAppVersion": 0,
            "dueDateDeltaUnit": unit,
        })
    blob = {
        "reminderIdentifier": rid.split("/", 1)[-1],
        "dueDateDeltaAlerts": alerts,
        "accountIdentifier": _account_id(),
        "minimumSupportedVersion": 20230430,
    }
    value = base64.b64encode(json.dumps(blob, separators=(",", ":")).encode()).decode()
    w = svc._writes
    w._submit_single_record_update(
        operation_name="Set early reminder",
        record_name=w._reminder_record_name(r.id),
        record_type="Reminder",
        record_change_tag=r.record_change_tag,
        fields={
            "DueDateDeltaAlertsData": {"type": "ENCRYPTED_BYTES", "value": value},
            "ResolutionTokenMap": {
                "type": "STRING",
                "value": _generate_resolution_token_map(
                    ["dueDateDeltaAlertsData", "lastModifiedDate"]
                ),
            },
            "LastModifiedDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)},
        },
        model_obj=r,
    )


# --- Urgent (iOS 26.2 "Urgent" alarm) -------------------------------------
# Stored as a small JSON asset plus an encrypted checksum string on the reminder.
# Confirmed working. Apple's checksum algorithm is unknown; SHA-512 of the file is accepted.
_PERSON_ID_FILE = Path(__file__).with_name(".urgent_person_id")


def capture_urgent_person_id(svc, rid: str) -> None:
    """Learn this account's personID from a reminder where Urgent was set in the app."""
    f = _raw_lookup(svc, rid).get("UrgentPresentationAlarmsAsData")
    if not f or not f.get("value", {}).get("downloadURL"):
        raise ValueError("That reminder has no Urgent setting to learn from.")
    data = json.loads(svc._raw._client.download_asset_bytes(f["value"]["downloadURL"]))
    _PERSON_ID_FILE.write_text(data["account"][0]["personID"])


def _set_urgent(svc, rid: str) -> None:
    import hashlib

    from pyicloud.services.reminders._constants import _REMINDERS_ZONE
    from pyicloud.services.reminders._protocol import _generate_resolution_token_map

    r = svc.get(rid)
    if r.due_date is None:
        raise ValueError("Urgent needs a due date and time.")
    try:
        person = _PERSON_ID_FILE.read_text().strip()
    except FileNotFoundError:
        raise RuntimeError(
            "Urgent needs a one-time setup: turn Urgent on for any reminder in the "
            "Reminders app, then ask me to capture the Urgent person id."
        )
    body = json.dumps(
        {
            "minimumSupportedVersion": 20251103,
            "account": [{
                "isEnabled": True,
                "personID": person,
                "modifiedOn": time.time() - 978307200.0,
            }],
        },
        separators=(",", ":"),
    ).encode()
    http = svc._raw._client._http
    zone = (
        _REMINDERS_ZONE.model_dump(exclude_none=True)
        if hasattr(_REMINDERS_ZONE, "model_dump")
        else _REMINDERS_ZONE
    )
    tok = http.post("/assets/upload", {
        "tokens": [{
            "recordType": "Reminder",
            "fieldName": "UrgentPresentationAlarmsAsData",
            "recordName": r.id,
        }],
        "zoneID": zone,
    })["tokens"][0]
    up = http._session.post(
        tok["url"], data=body,
        headers={"Content-Type": "application/octet-stream"}, timeout=60,
    )
    up.raise_for_status()
    w = svc._writes
    w._submit_single_record_update(
        operation_name="Set urgent",
        record_name=w._reminder_record_name(r.id),
        record_type="Reminder",
        record_change_tag=r.record_change_tag,
        fields={
            "UrgentPresentationAlarmsAsData": {"type": "ASSETID", "value": up.json()["singleFile"]},
            "UrgentPresentationAlarmsChecksum": {
                "type": "STRING",
                "value": hashlib.sha512(body).hexdigest(),
                "isEncrypted": True,
            },
            "ResolutionTokenMap": {
                "type": "STRING",
                "value": _generate_resolution_token_map(
                    ["urgentPresentationAlarmsChecksum", "lastModifiedDate"]
                ),
            },
            "LastModifiedDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)},
        },
        model_obj=r,
    )


# CloudKit's wire values are zero-based, verified against rules made by the app
# itself (a monthly reminder is stored as 2). pyicloud's RecurrenceFrequency enum
# is one-based, so its names are off by one and must not be trusted for read or write.
_FREQ = {"daily": 0, "weekly": 1, "monthly": 2, "yearly": 3}


def _read_rules(svc, r) -> list[dict]:
    """Live recurrence rules for a reminder, read raw (wire values, not the enum)."""
    from pyicloud.services.reminders._constants import _REMINDERS_ZONE_REQ

    out = []
    for rule_id in r.recurrence_rule_ids:
        name = rule_id if rule_id.startswith("RecurrenceRule/") else f"RecurrenceRule/{rule_id}"
        resp = svc._raw.lookup([name], zone_id=_REMINDERS_ZONE_REQ)
        rec = resp.records[0].model_dump()
        f = rec.get("fields", {})
        if f.get("Deleted", {}).get("value"):
            continue
        out.append({
            "name": name,
            "tag": rec.get("recordChangeTag"),
            "wire": f["Frequency"]["value"],
            "interval": f["Interval"]["value"],
            "count": f["OccurrenceCount"]["value"],
        })
    return out


# --- Keep alarms tied to the current due date -----------------------------------
def _sync_alarm_nonces(svc, r) -> None:
    """svc.update() rewrites the whole ResolutionTokenMap, which leaves existing alarms pointing at
    an older version of the due date. Re-tie them, as the app's own data always is."""
    if not r.alarm_ids:
        return
    _ids, tm, _tag = _reminder_state(svc, r)
    want = _due_nonce(tm)
    for a in _read_alarms(svc, r):
        rec = _lookup_record(svc, a["name"])
        cur = rec["fields"].get("DueDateResolutionTokenAsNonce", {}).get("value") if rec else None
        if cur is None or abs(cur - want) < 0.01:
            continue
        stub = SimpleNamespace(record_change_tag=None)
        svc._writes._submit_single_record_update(
            operation_name="Re-tie alarm to due date", record_name=a["name"], record_type="Alarm",
            record_change_tag=a["tag"],
            fields={"DueDateResolutionTokenAsNonce": {"type": "DOUBLE", "value": want}}, model_obj=stub)


# --- Repeat rules: update in place, create with the wire value directly ----------
def _delete_rule(svc, r, old: dict) -> None:
    from pyicloud.services.reminders.models import RecurrenceFrequency, RecurrenceRule

    stub = RecurrenceRule(  # frequency is irrelevant for deletion; avoids enum issues
        id=old["name"], reminder_id=r.id, frequency=RecurrenceFrequency.DAILY, record_change_tag=old["tag"])
    svc.delete_recurrence_rule(r, stub)


def _create_rule(svc, r, wire: int, interval: int, count: int) -> None:
    """Create a rule with the exact wire frequency (the library's enum can't express daily = 0)."""
    w = svc._writes
    w._create_linked_child(
        reminder=r, reminder_ids_attr="recurrence_rule_ids", prefix="RecurrenceRule",
        record_type="RecurrenceRule", field_name="RecurrenceRuleIDs", token_field_name="recurrenceRuleIDs",
        child_fields={
            "Reminder": {"type": "REFERENCE", "value": {"recordName": w._reminder_record_name(r.id), "action": "VALIDATE"}},
            "Frequency": {"type": "INT64", "value": wire},
            "Interval": {"type": "INT64", "value": interval},
            "OccurrenceCount": {"type": "INT64", "value": count},
            "FirstDayOfTheWeek": {"type": "INT64", "value": 0},
            "Imported": {"type": "INT64", "value": 0},
            "Deleted": {"type": "INT64", "value": 0},
        },
        operation_name="Create recurrence rule")


def _update_rule(svc, old: dict, wire: int, interval: int, count: int) -> bool:
    """Change only what differs, on the existing rule record. Returns whether anything changed."""
    fields = {}
    if old["wire"] != wire:
        fields["Frequency"] = {"type": "INT64", "value": wire}
    if old["interval"] != interval:
        fields["Interval"] = {"type": "INT64", "value": interval}
    if (old["count"] or 0) != count:
        fields["OccurrenceCount"] = {"type": "INT64", "value": count}
    if not fields:
        return False
    svc._writes._submit_single_record_update(
        operation_name="Update recurrence rule", record_name=old["name"], record_type="RecurrenceRule",
        record_change_tag=old["tag"], fields=fields, model_obj=SimpleNamespace(record_change_tag=None))
    return True


def _apply_repeat(svc, r, repeat: str | None, interval: int, count: int) -> None:
    """repeat: None = leave alone, ''/'none' = remove, else daily|weekly|monthly|yearly.
    An existing rule is edited in place; a missing one is created with the wire value directly."""
    if repeat is None:
        return
    key = repeat.strip().lower()
    if key not in ("", "none") and key not in _FREQ:
        raise ValueError(f"repeat must be one of {list(_FREQ)} or 'none', got '{repeat}'")
    if key not in ("", "none"):
        if interval < 1:
            raise ValueError("repeat_interval must be at least 1")
        if count < 0:
            raise ValueError("repeat_count can't be negative")
    old = _read_rules(svc, r)
    if key in ("", "none"):
        for o in old:
            _delete_rule(svc, r, o)
        return
    wire = _FREQ[key]
    for extra in old[1:]:  # a reminder holds one rule; drop strays
        _delete_rule(svc, r, extra)
    if old:
        _update_rule(svc, old[0], wire, interval, count)
    else:
        _create_rule(svc, r, wire, interval, count)


# --- Moving between lists and subtasks --------------------------------------------
def _move_reminder(svc, r, target) -> None:
    """Point a reminder at another list (the library's update() doesn't write the list)."""
    w = svc._writes
    _ids, tm, tag = _reminder_state(svc, r)
    list_id = target.id if target.id.startswith("List/") else f"List/{target.id}"
    w._submit_single_record_update(
        operation_name="Move reminder", record_name=w._reminder_record_name(r.id), record_type="Reminder",
        record_change_tag=tag, model_obj=r, fields={
            "List": {"type": "REFERENCE", "value": {"recordName": list_id, "action": "VALIDATE"}},
            "ResolutionTokenMap": {"type": "STRING", "value": _merge_token_map(tm, ["list", "lastModifiedDate"])},
            "LastModifiedDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)},
        })


def _subtasks_of(svc, r) -> list:
    res = svc.list_reminders(r.list_id, include_completed=True)
    return [x for x in res.reminders if not x.deleted and x.parent_reminder_id == r.id]


def _apply_tags(svc, r, tags: list[str] | None) -> None:
    """tags: None = leave alone, else the complete desired set (without '#')."""
    if tags is None:
        return
    want = {t.strip().lstrip("#") for t in tags if t.strip()}
    have = {h.name: h for h in svc.tags_for(r)}
    for name, h in have.items():
        if name not in want:
            svc.delete_hashtag(r, h)
    for name in want - set(have):
        svc.create_hashtag(r, name)


# --- Links (URL attachments) -----------------------------------------------
def _read_links(svc, r) -> list[dict]:
    """URL attachments on a reminder (images are listed by file name only)."""
    out = []
    if not r.attachment_ids:
        return out
    try:
        atts = svc.attachments_for(r)
    except Exception:  # a dangling attachment id shouldn't break the whole reminder
        return out
    for a in atts:
        if hasattr(a, "url"):
            out.append({"id": a.id, "url": a.url})
        elif getattr(a, "filename", ""):
            out.append({"id": a.id, "image": a.filename})
    return out


def _check_url(url: str) -> str:
    u = url.strip()
    if "://" not in u and not u.lower().startswith(("mailto:", "tel:")):
        raise ValueError(f"'{url}' doesn't look like a link (include http:// or https://)")
    return u


def _apply_links(svc, r, links: list[str] | None) -> None:
    """links: None = leave alone, else the complete desired set of URLs ([] removes all)."""
    if links is None:
        return
    want = list(dict.fromkeys(_check_url(u) for u in links))  # validate first, keep order, dedupe
    have = {a.url: a for a in svc.attachments_for(r) if hasattr(a, "url")} if r.attachment_ids else {}
    for url, att in have.items():
        if url not in want:
            svc.delete_attachment(r, att)
            r = svc.get(r.id)  # the reminder's change tag moves on every write
    for url in want:
        if url not in have:
            svc.create_url_attachment(r, url)
            r = svc.get(r.id)


# --- Alarms ------------------------------------------------------------------
# An app-made timed reminder carries an Alarm record plus an AlarmTrigger whose
# DateComponentsData is base64 JSON (wall-clock parts + time zone). The alarm is
# tied to one version of the due date through DueDateResolutionTokenAsNonce, which
# equals counter * 1e11 + modificationTime of the reminder's "dueDate" token.
def _zone_req():
    from pyicloud.services.reminders._constants import _REMINDERS_ZONE_REQ

    return _REMINDERS_ZONE_REQ


def _lookup_record(svc, name: str) -> dict | None:
    resp = svc._raw.lookup([name], zone_id=_zone_req())
    rec = resp.records[0].model_dump()
    return rec if rec.get("fields") else None  # None = record is gone (NOT_FOUND)


def _decode_components(value) -> dict:
    raw = bytes(value) if isinstance(value, (bytes, bytearray)) else str(value).encode()
    return json.loads(base64.b64decode(raw).decode("utf-8"))


def _encode_components(local: datetime, tz_name: str) -> str:
    comps = {
        "hour": local.hour, "timeZone": {"identifier": tz_name}, "year": local.year, "era": 1,
        "day": local.day, "month": local.month, "minute": local.minute, "second": 0,
    }
    text = json.dumps(comps, separators=(",", ":")).replace("/", "\\/")  # as the app writes it
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _components_to_local(comps: dict) -> datetime:
    """Wall-clock parts in their own zone, converted to naive local time."""
    naive = datetime(comps["year"], comps["month"], comps["day"], comps.get("hour", 0), comps.get("minute", 0))
    tz = (comps.get("timeZone") or {}).get("identifier")
    if not tz:
        return naive
    from zoneinfo import ZoneInfo

    return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(get_localzone()).replace(tzinfo=None)


def _due_nonce(token_map: dict | None) -> float:
    dd = (token_map or {}).get("map", {}).get("dueDate")
    if dd:
        return dd["counter"] * 1e11 + dd["modificationTime"]
    return 1e11 + (time.time() - 978307200.0)  # same fallback pyicloud uses


def _merge_token_map(old: dict | None, fields: list[str]) -> str:
    """Bump the given fields in the reminder's ResolutionTokenMap, keeping every other entry."""
    from pyicloud.services.reminders._protocol import _generate_resolution_token_map

    fresh = json.loads(_generate_resolution_token_map(fields))["map"]
    merged = dict((old or {}).get("map", {}))
    for key, entry in fresh.items():
        if key in merged:
            entry = {**entry, "counter": merged[key].get("counter", 0) + 1}
        merged[key] = entry
    return json.dumps({"map": merged}, separators=(",", ":"))


def _read_alarms(svc, r) -> list[dict]:
    """Alarms on a reminder, read raw. Missing records are skipped (Apple leaves dangling ids)."""
    out = []
    for aid in r.alarm_ids:
        name = aid if aid.startswith("Alarm/") else f"Alarm/{aid}"
        try:
            a = _lookup_record(svc, name)
            if not a or a["fields"].get("Deleted", {}).get("value"):
                continue
            tid = a["fields"]["TriggerID"]["value"]
            t = _lookup_record(svc, f"AlarmTrigger/{tid}")
            if not t or t["fields"].get("Deleted", {}).get("value"):
                continue
            kind = t["fields"].get("Type", {}).get("value", "")
            item = {"name": name, "tag": a.get("recordChangeTag"), "trigger": f"AlarmTrigger/{tid}",
                    "trigger_tag": t.get("recordChangeTag"), "kind": kind.lower()}
            if kind == "Date":
                item["at"] = _components_to_local(_decode_components(t["fields"]["DateComponentsData"]["value"]))
            else:
                item["title"] = str(t["fields"].get("Title", {}).get("value", ""))
            out.append(item)
        except Exception:
            continue
    return out


def _modify(svc, ops, name: str) -> None:
    from pyicloud.services.reminders._support import _assert_modify_success

    resp = svc._writes._get_raw().modify(operations=ops, zone_id=_zone_req(), atomic=True)
    _assert_modify_success(resp, name)


def _reminder_state(svc, r) -> tuple[list[str], dict | None, str | None]:
    rec = _lookup_record(svc, svc._writes._reminder_record_name(r.id))
    f = rec["fields"]
    ids = list(f.get("AlarmIDs", {}).get("value") or [])
    tm = json.loads(f["ResolutionTokenMap"]["value"]) if f.get("ResolutionTokenMap", {}).get("value") else None
    return ids, tm, rec.get("recordChangeTag")


def _add_date_alarm(svc, r, local: datetime) -> None:
    from pyicloud.common.cloudkit import CKModifyOperation

    w = svc._writes
    ids, tm, tag = _reminder_state(svc, r)
    alarm_uuid, trigger_uuid = str(uuid.uuid4()).upper(), str(uuid.uuid4()).upper()
    alarm_name, trigger_name = f"Alarm/{alarm_uuid}", f"AlarmTrigger/{trigger_uuid}"
    rname = w._reminder_record_name(r.id)
    ref = lambda n: {"type": "REFERENCE", "value": {"recordName": n, "action": "VALIDATE"}}
    ops = [
        CKModifyOperation(operationType="update", record=w._write_record(
            record_name=rname, record_type="Reminder", record_change_tag=tag, fields={
                "AlarmIDs": {"type": "STRING_LIST", "value": ids + [alarm_uuid]},
                "ResolutionTokenMap": {"type": "STRING", "value": _merge_token_map(tm, ["alarmIDs", "lastModifiedDate"])},
                "LastModifiedDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)},
            })),
        CKModifyOperation(operationType="create", record=w._write_record(
            record_name=alarm_name, record_type="Alarm", parent_record_name=rname, fields={
                "AlarmUID": {"type": "STRING", "value": alarm_uuid},
                "Deleted": {"type": "INT64", "value": 0}, "Imported": {"type": "INT64", "value": 0},
                "Reminder": ref(rname), "TriggerID": {"type": "STRING", "value": trigger_uuid},
                "DueDateResolutionTokenAsNonce": {"type": "DOUBLE", "value": _due_nonce(tm)},
            })),
        CKModifyOperation(operationType="create", record=w._write_record(
            record_name=trigger_name, record_type="AlarmTrigger", parent_record_name=alarm_name, fields={
                "DateComponentsData": {"type": "BYTES", "value": _encode_components(local, get_localzone_name())},
                "Type": {"type": "STRING", "value": "Date"},
                "Alarm": ref(alarm_name),
                "Imported": {"type": "INT64", "value": 0}, "Deleted": {"type": "INT64", "value": 0},
            })),
    ]
    _modify(svc, ops, "Add date alarm")


def _remove_alarm(svc, r, alarm: dict) -> None:
    from pyicloud.common.cloudkit import CKModifyOperation

    w = svc._writes
    ids, tm, tag = _reminder_state(svc, r)
    raw = alarm["name"].split("/", 1)[1]
    soft_delete = lambda name, rtype, ctag: CKModifyOperation(operationType="update", record=w._write_record(
        record_name=name, record_type=rtype, record_change_tag=ctag, fields={"Deleted": {"type": "INT64", "value": 1}}))
    ops = [
        CKModifyOperation(operationType="update", record=w._write_record(
            record_name=w._reminder_record_name(r.id), record_type="Reminder", record_change_tag=tag, fields={
                "AlarmIDs": {"type": "STRING_LIST", "value": [i for i in ids if i != raw]},
                "ResolutionTokenMap": {"type": "STRING", "value": _merge_token_map(tm, ["alarmIDs", "lastModifiedDate"])},
                "LastModifiedDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)},
            })),
        soft_delete(alarm["name"], "Alarm", alarm["tag"]),
        soft_delete(alarm["trigger"], "AlarmTrigger", alarm["trigger_tag"]),
    ]
    _modify(svc, ops, "Remove alarm")


def _due_local(r) -> datetime | None:
    """A timed reminder's due time as naive local time (None for all-day or no due date)."""
    if not r.due_date or r.all_day:
        return None
    return r.due_date.astimezone(get_localzone()).replace(tzinfo=None, second=0, microsecond=0)


def _parse_alarm_time(value: str, r) -> datetime:
    v = value.strip()
    if v.lower() == "due":
        due = _due_local(r)
        if due is None:
            raise ValueError("'due' needs a reminder with a due date and time")
        return due
    if len(v) == 10:
        raise ValueError(f"alarm '{value}' needs a time: 'YYYY-MM-DDTHH:MM' (or 'due')")
    return datetime.fromisoformat(v).replace(tzinfo=None, second=0, microsecond=0)


def _validate_extras(links: list[str] | None, alarms: list[str] | None, has_timed_due: bool) -> None:
    """Reject bad links/alarms before anything is written (so a bad call leaves no half-made reminder)."""
    for u in links or []:
        _check_url(u)
    for a in alarms or []:
        v = a.strip()
        if v.lower() == "due":
            if not has_timed_due:
                raise ValueError("'due' needs a reminder with a due date and time")
        elif len(v) == 10:
            raise ValueError(f"alarm '{a}' needs a time: 'YYYY-MM-DDTHH:MM' (or 'due')")
        else:
            datetime.fromisoformat(v)


def _apply_alarms(svc, r, alarms: list[str] | None) -> None:
    """alarms: None = leave alone, else the complete set of time alarms ('YYYY-MM-DDTHH:MM'
    local, or 'due' = at the due time). [] removes time alarms; location alarms are untouched."""
    if alarms is None:
        return
    want = list(dict.fromkeys(_parse_alarm_time(a, r) for a in alarms))  # validate before writing
    have = [a for a in _read_alarms(svc, r) if a["kind"] == "date"]
    for a in have:
        if a["at"].replace(second=0, microsecond=0) not in want:
            _remove_alarm(svc, r, a)
    existing = {a["at"].replace(second=0, microsecond=0) for a in have}
    for when in want:
        if when not in existing:
            _add_date_alarm(svc, r, when)


def _default_alarms(alarms: list[str] | None, notify: bool, timed_due: bool) -> list[str] | None:
    """A reminder with a due TIME only notifies if it has an alarm (verified on a phone: a due
    time alone stays silent). So a new timed reminder gets an alarm at its due time unless the
    caller said otherwise."""
    if alarms is not None:
        return alarms
    return ["due"] if notify and timed_due else None


def _alarms_after_due_change(date_alarms: list[datetime], old_due: datetime | None,
                             new_timed: bool, notify: bool) -> list[str] | None:
    """Time alarms a reminder should have after its due time changed (None = nothing to do).
    Alarms other than the one at the old due time are kept; the at-due alarm follows the new time."""
    keep = [a.replace(second=0, microsecond=0) for a in date_alarms]
    had_at_due = old_due is not None and old_due in keep
    keep = [a for a in keep if a != old_due]
    if new_timed and notify:
        return [a.isoformat(timespec="minutes") for a in keep] + ["due"]
    if had_at_due:
        return [a.isoformat(timespec="minutes") for a in keep]
    return None


def _alarms_out(svc, r) -> list[dict]:
    due = _due_local(r)
    out = []
    for a in _read_alarms(svc, r):
        if a["kind"] == "date":
            at = a["at"].replace(second=0, microsecond=0)
            out.append({"kind": "date", "at": at.isoformat(timespec="minutes"), "at_due": at == due})
        else:
            out.append({"kind": a["kind"] or "other", "title": a.get("title", "")})
    return sorted(out, key=lambda x: x.get("at", "~"))


# --- Smart views ---------------------------------------------------------------
_PRIORITY = {"high": 1, "medium": 5, "low": 9, "none": 0}


def _filter_reminders(items: list[dict], flagged: bool | None, priority: str) -> list[dict]:
    """Flag/priority views. Sorted most urgent first (high priority, then soonest due)."""
    p = priority.strip().lower()
    if p and p not in ("any", *_PRIORITY):
        raise ValueError(f"priority must be one of high|medium|low|none|any, got '{priority}'")
    out = items
    if flagged is not None:
        out = [r for r in out if bool(r["flagged"]) == flagged]
    if p == "any":
        out = [r for r in out if r["priority"] != 0]
    elif p:
        out = [r for r in out if r["priority"] == _PRIORITY[p]]
    if flagged is not None or p:
        out = sorted(out, key=lambda r: (r["priority"] or 99, r["due"] or "~", r["title"]))
    return out


# --- Bulk selection and planning (pure, testable) ----------------------------------
def _titles_by_list(svc) -> dict:
    return {l.id: l.title for l in svc.lists() if not l.deleted}


def _select_reminders(svc, list_ident, include_completed, flagged, priority, overdue,
                      due_from, due_to, title_contains) -> list[dict]:
    lists = [_list(list_ident)] if list_ident else [l for l in svc.lists() if not l.deleted]
    items = []
    for l in lists:
        res = svc.list_reminders(l.id, include_completed=include_completed)
        items += [_out(r, l.title) for r in res.reminders if not r.deleted]
    return _filter_bulk(items, flagged, priority, overdue, due_from, due_to, title_contains,
                        include_completed, datetime.now().date().isoformat())


def _filter_bulk(items, flagged, priority, overdue, due_from, due_to, title_contains,
                 include_completed, today: str) -> list[dict]:
    out = _filter_reminders(items, flagged, priority)
    if not include_completed:
        out = [r for r in out if not r["completed"]]
    day = lambda r: r["due"][:10] if r["due"] else None
    if overdue:
        out = [r for r in out if day(r) and day(r) < today and not r["completed"]]
    if due_from:
        out = [r for r in out if day(r) and day(r) >= due_from.strip()]
    if due_to:
        out = [r for r in out if day(r) and day(r) <= due_to.strip()]
    if title_contains:
        needle = title_contains.strip().lower()
        out = [r for r in out if needle in r["title"].lower()]
    return out


def _shift_due(due: str, days: int) -> str:
    """Shift a due value as list_reminders reports it ('YYYY-MM-DD' or ISO with offset) by N days,
    keeping the local wall-clock time; returns a value update_reminder accepts."""
    if len(due) == 10:
        return (date.fromisoformat(due) + timedelta(days=days)).isoformat()
    dt = datetime.fromisoformat(due)
    local = dt.astimezone(get_localzone()).replace(tzinfo=None) if dt.tzinfo else dt
    return (local + timedelta(days=days)).isoformat(timespec="seconds")


def _plan_bulk(act, chosen, target, shift_days, new_due, set_priority):
    """Turn the selection into (plan, skipped): what would change, and what is left alone and why."""
    plan, skipped = [], []
    for r in chosen:
        item = {"id": r["id"], "title": r["title"], "list": r["list"], "due": r["due"]}
        why = None
        if act == "complete":
            why = "already completed" if r["completed"] else None
            item["change"] = "mark complete"
        elif act == "uncomplete":
            why = None if r["completed"] else "not completed"
            item["change"] = "mark incomplete"
        elif act == "flag":
            why = "already flagged" if r["flagged"] else None
            item["change"] = "flag"
        elif act == "unflag":
            why = None if r["flagged"] else "not flagged"
            item["change"] = "remove flag"
        elif act == "move":
            why = "already in that list" if r["list"] == target.title else None
            item["change"] = f"move to {target.title}"
        elif act == "set_priority":
            value = _PRIORITY[set_priority.strip().lower()]
            why = "already that priority" if r["priority"] == value else None
            item["change"] = f"priority -> {set_priority.strip().lower()}"
        elif act == "reschedule":
            if new_due:
                item["new_due"] = new_due
            elif r["due"]:
                item["new_due"] = _shift_due(r["due"], shift_days)
            else:
                why = "no due date to shift"
            item["change"] = f"due -> {item.get('new_due')}"
        elif act == "delete":
            item["change"] = "DELETE"
        if why:
            skipped.append({"id": r["id"], "title": r["title"], "why": why})
        else:
            plan.append(item)
    return plan, skipped


def _is_conflict(exc: Exception) -> bool:
    return "CONFLICT" in str(exc)


def _update(svc, r) -> None:
    """svc.update(r), retried when iCloud answers CONFLICT. That means the change tag we hold is
    already out of date, which happens when a reminder is read straight after a write. The rejected
    write changed nothing, so retrying with a fresh tag is safe (the last writer wins, as ever)."""
    for attempt in range(3):
        try:
            svc.update(r)
            return
        except Exception as exc:
            if not _is_conflict(exc) or attempt == 2:
                raise
            time.sleep(1.5 * (attempt + 1))
            rec = _lookup_record(svc, svc._writes._reminder_record_name(r.id))
            if rec:
                r.record_change_tag = rec.get("recordChangeTag")


# --- Lists: create and rename ----------------------------------------------------
# Matched against lists the Reminders app made and renamed (read raw): a list is one `List`
# record. The app's rename changes only the encrypted Name and bumps the "name" entry of the
# token map (counter + 1, new time, same replica). A new list needs no change to the account's
# list-order record: it shows up at the bottom of the sidebar (verified on a phone).
_LIST_TOKEN_KEYS = ["minimumSupportedVersion", "name", "parentAccount", "reminderIDsMergeableOrdering",
                    "shouldAutoCategorizeItems", "shouldCategorizeGroceryItems", "sortingStyle"]


def _check_list_name(name: str, lists, ignore_id: str | None = None) -> str:
    n = name.strip()
    if not n:
        raise ValueError("A list needs a name")
    if len(n) > 200:
        raise ValueError("That list name is too long (200 characters at most)")
    for l in lists:
        if not l.deleted and l.id != ignore_id and l.title.strip().lower() == n.lower():
            raise ValueError(f"A list named '{l.title}' already exists; names must be unique so tools can tell them apart")
    return n


def _list_fields(name: str, token_map_json: str) -> dict:
    return {
        "Name": {"type": "STRING", "value": name, "isEncrypted": True},
        "SortingStyle": {"type": "STRING", "value": "manual"},
        "IsLinkedToAccount": {"type": "INT64", "value": 1},
        "IsGroup": {"type": "INT64", "value": 0},
        "Imported": {"type": "INT64", "value": 0},
        "Deleted": {"type": "INT64", "value": 0},
        "ReminderIDs": {"type": "STRING", "value": "[]"},
        "ShouldCategorizeGroceryItems": {"type": "INT64", "value": 0, "isEncrypted": True},
        "ShouldAutoCategorizeItems": {"type": "INT64", "value": 0, "isEncrypted": True},
        "ResolutionTokenMap": {"type": "STRING", "value": token_map_json},
    }


def _bump_token(old_map: dict | None, key: str, now_apple: float | None = None) -> str:
    """Bump one entry of a token map the way the app does: counter + 1, new time, same replica."""
    m = json.loads(json.dumps((old_map or {}).get("map", {})))
    entry = m.get(key)
    when = (time.time() - 978307200.0) if now_apple is None else now_apple
    if entry:
        entry["counter"] = entry.get("counter", 0) + 1
        entry["modificationTime"] = when
    else:
        from pyicloud.services.reminders._protocol import _generate_resolution_token_map

        m[key] = json.loads(_generate_resolution_token_map([key]))["map"][key]
    return json.dumps({"map": m}, separators=(",", ":"))


def _create_list(svc, name: str) -> str:
    from pyicloud.common.cloudkit import CKModifyOperation
    from pyicloud.services.reminders._protocol import _generate_resolution_token_map

    record_name = f"List/{str(uuid.uuid4()).upper()}"
    op = CKModifyOperation(operationType="create", record=svc._writes._write_record(
        record_name=record_name, record_type="List",
        fields=_list_fields(name, _generate_resolution_token_map(_LIST_TOKEN_KEYS))))
    _modify(svc, [op], "Create list")
    return record_name


def _rename_list(svc, lst, new_name: str) -> None:
    list_name = lst.id if lst.id.startswith("List/") else f"List/{lst.id}"
    rec = _lookup_record(svc, list_name)
    if rec is None:
        raise ValueError("That list no longer exists")
    tm = json.loads(rec["fields"]["ResolutionTokenMap"]["value"]) if rec["fields"].get("ResolutionTokenMap", {}).get("value") else None
    svc._writes._submit_single_record_update(
        operation_name="Rename list", record_name=list_name, record_type="List",
        record_change_tag=rec.get("recordChangeTag"), model_obj=SimpleNamespace(record_change_tag=None), fields={
            "Name": {"type": "STRING", "value": new_name, "isEncrypted": True},
            "ResolutionTokenMap": {"type": "STRING", "value": _bump_token(tm, "name")},
        })


def _set_list_deleted(svc, list_id: str, deleted: bool) -> None:
    """What the app writes when you delete a list: Deleted=1, IsLinkedToAccount=0 and a bump of
    the list's "parentAccount" token (read back from a list deleted in the app). The app also edits
    the account's list-order record; that is left alone, as it already keeps entries for
    deleted lists (it holds far more ids than there are lists)."""
    list_name = list_id if list_id.startswith("List/") else f"List/{list_id}"
    rec = _lookup_record(svc, list_name)
    if rec is None:
        raise ValueError("That list no longer exists")
    tm = json.loads(rec["fields"]["ResolutionTokenMap"]["value"]) if rec["fields"].get("ResolutionTokenMap", {}).get("value") else None
    svc._writes._submit_single_record_update(
        operation_name="Delete list" if deleted else "Restore list", record_name=list_name, record_type="List",
        record_change_tag=rec.get("recordChangeTag"), model_obj=SimpleNamespace(record_change_tag=None), fields={
            "Deleted": {"type": "INT64", "value": 1 if deleted else 0},
            "IsLinkedToAccount": {"type": "INT64", "value": 0 if deleted else 1},  # the app unlinks a deleted list
            "ResolutionTokenMap": {"type": "STRING", "value": _bump_token(tm, "parentAccount")},
        })


# --- Undo support ----------------------------------------------------------------
UNDO_OPS = ("delete_reminder", "undelete_reminder", "restore_reminder", "delete_list", "undelete_list", "rename_list")


def _snapshot(svc, r, groups: set) -> dict:
    """What _restore needs to put a reminder back. The basics are free; `groups` limits the extra
    lookups (repeat, tags, links, alarms) to the parts that are about to change."""
    snap = {"id": r.id, "title": r.title, "notes": r.desc, "priority": r.priority, "flagged": r.flagged,
            "completed": r.completed, "due": _out(r, None)["due"], "parent": r.parent_reminder_id,
            "list_id": r.list_id}
    if "repeat" in groups:
        rules = _read_rules(svc, r)
        snap["repeat"] = ({"wire": rules[0]["wire"], "interval": rules[0]["interval"], "count": rules[0]["count"] or 0}
                          if rules else None)
    if "tags" in groups:
        snap["tags"] = [h.name for h in svc.tags_for(r)]
    if "links" in groups:
        snap["links"] = [x["url"] for x in _read_links(svc, r) if "url" in x]
    if "alarms" in groups:
        snap["alarms"] = [a["at"].replace(second=0, microsecond=0).isoformat(timespec="minutes")
                          for a in _read_alarms(svc, r) if a["kind"] == "date"]
    return snap


def _norm(value):
    """Order-insensitive form for comparing snapshot values."""
    return sorted(value) if isinstance(value, list) else value


def _restore(svc, snap: dict, attempts: int = 3) -> None:
    """Restore, then re-read and check. iCloud sometimes merges quick successive writes to one
    reminder (a title once came back as a mix of the old and new text), so a restore that doesn't
    match is repeated, and reported honestly if it still doesn't."""
    groups = {g for g in ("repeat", "tags", "links", "alarms") if g in snap}
    differs = []
    for attempt in range(attempts):
        _restore_once(svc, snap)
        now = _snapshot(svc, svc.get(snap["id"]), groups)
        differs = [k for k in snap if k != "id" and _norm(now.get(k)) != _norm(snap[k])]
        if not differs:
            return
        time.sleep(2)
    raise ValueError(f"Restored most of it, but these still differ from before: {', '.join(differs)}. "
                     "The log entry stays undoable; try the undo again in a minute.")


def _restore_once(svc, snap: dict) -> None:
    """Put back the parts present in the snapshot (a snapshot may hold only some of them)."""
    r = svc.get(snap["id"])
    if r.deleted:
        raise ValueError("That reminder has been deleted since; undo the deletion first")
    if "title" in snap:
        r.title = snap["title"]
    if "notes" in snap:
        r.desc = snap["notes"]
    for key in ("priority", "flagged", "completed"):
        if key in snap:
            setattr(r, key, snap[key])
    if "due" in snap:
        r.due_date, r.all_day = _parse_due(snap["due"] or "")
        r.time_zone = None if (r.all_day or not r.due_date) else get_localzone_name()
    if "parent" in snap:
        r.parent_reminder_id = snap["parent"]
    _update(svc, r)
    names = {v: k for k, v in _FREQ.items()}
    if "repeat" in snap:
        rule = snap["repeat"]
        _apply_repeat(svc, svc.get(snap["id"]), names[rule["wire"]] if rule else "none",
                      rule["interval"] if rule else 1, rule["count"] if rule else 0)
    if "tags" in snap:
        _apply_tags(svc, svc.get(snap["id"]), snap["tags"])
    if "links" in snap:
        _apply_links(svc, svc.get(snap["id"]), snap["links"])
    if "alarms" in snap:
        _apply_alarms(svc, svc.get(snap["id"]), snap["alarms"])
    if "list_id" in snap and svc.get(snap["id"]).list_id != snap["list_id"]:
        target = next((l for l in svc.lists() if l.id == snap["list_id"] and not l.deleted), None)
        if target is None:
            raise ValueError("The list this reminder was in no longer exists")
        kids = _subtasks_of(svc, svc.get(snap["id"]))  # subtasks travel with their parent
        _move_reminder(svc, svc.get(snap["id"]), target)
        for kid in kids:
            _move_reminder(svc, svc.get(kid.id), target)
    _sync_alarm_nonces(svc, svc.get(snap["id"]))


def _delete_with_subtasks(svc, rid: str) -> list[str]:
    """Delete a reminder and its subtasks (as the app does); returns every id that was deleted."""
    r = svc.get(rid)
    gone = []
    for kid in _subtasks_of(svc, r):
        svc.delete(svc.get(kid.id))
        gone.append(kid.id)
    svc.delete(svc.get(rid))
    return [rid] + gone


def _undelete(svc, rid: str) -> None:
    """Flip a soft-deleted reminder back (the record is kept with Deleted=1)."""
    w = svc._writes
    rec = _lookup_record(svc, w._reminder_record_name(rid))
    if rec is None:
        raise ValueError("That reminder no longer exists on iCloud, so it can't be restored")
    tm = json.loads(rec["fields"]["ResolutionTokenMap"]["value"]) if rec["fields"].get("ResolutionTokenMap", {}).get("value") else None
    w._submit_single_record_update(
        operation_name="Restore reminder", record_name=w._reminder_record_name(rid), record_type="Reminder",
        record_change_tag=rec.get("recordChangeTag"), model_obj=SimpleNamespace(record_change_tag=None), fields={
            "Deleted": {"type": "INT64", "value": 0},
            "ResolutionTokenMap": {"type": "STRING", "value": _merge_token_map(tm, ["deleted", "lastModifiedDate"])},
            "LastModifiedDate": {"type": "TIMESTAMP", "value": int(time.time() * 1000)},
        })


@_reset_on_error
def undo_op(op: dict) -> str:
    svc = _service()
    kind = op["op"]
    if kind == "delete_reminder":
        _delete_with_subtasks(svc, op["id"])
        return "removed the reminder that was created"
    if kind == "undelete_reminder":
        for rid in op["ids"]:
            _undelete(svc, rid)
        return f"restored {len(op['ids'])} reminder(s)"
    if kind == "restore_reminder":
        _restore(svc, op["snapshot"])
        return "restored the reminder to how it was"
    if kind == "delete_list":
        _set_list_deleted(svc, op["id"], True)
        return "removed the list that was created"
    if kind == "undelete_list":
        _set_list_deleted(svc, op["id"], False)
        for rid in op.get("reminder_ids", []):
            _undelete(svc, rid)
        return f"restored the list and {len(op.get('reminder_ids', []))} reminder(s)"
    if kind == "rename_list":
        lst = next((l for l in svc.lists() if l.id == op["id"] and not l.deleted), None)
        if lst is None:
            raise ValueError("That list no longer exists")
        _rename_list(svc, lst, op["name"])
        return f"renamed the list back to '{op['name']}'"
    raise ValueError(f"Unknown undo operation '{kind}'")


def _detail(svc, rid: str, list_title: str | None = None) -> dict:
    """Fresh read of one reminder including repeat rule and tags."""
    r = svc.get(rid)
    d = _out(r, list_title)
    names = {v: k for k, v in _FREQ.items()}
    d["repeat"] = [
        {
            "frequency": names.get(x["wire"], f"unknown({x['wire']})"),
            "interval": x["interval"],
            "count": x["count"] or None,
        }
        for x in _read_rules(svc, r)
    ]
    d["tags"] = [h.name for h in svc.tags_for(r)]
    d["links"] = _read_links(svc, r)
    d["subtasks"] = [{"id": x.id, "title": x.title, "completed": x.completed} for x in _subtasks_of(svc, r)]
    d["alarms"] = _alarms_out(svc, r)
    alerts = (_read_alerts_blob(svc, rid) or {}).get("dueDateDeltaAlerts", [])
    for key, unit in (("early_minutes", _UNIT_MINUTES), ("early_hours", _UNIT_HOURS), ("early_days", _UNIT_DAYS)):
        d[key] = next(
            (abs(a["dueDateDeltaCount"]) for a in alerts if a.get("dueDateDeltaUnit") == unit),
            None,
        )
    d["early_other"] = [
        a for a in alerts if a.get("dueDateDeltaUnit") not in (_UNIT_MINUTES, _UNIT_HOURS, _UNIT_DAYS)
    ]  # units we haven't mapped yet; shown raw
    d["urgent"] = "UrgentPresentationAlarmsAsData" in _raw_lookup(svc, rid)
    return d


def open_reminders() -> list[dict]:
    """All incomplete reminders across every list (used by the agenda tool)."""
    svc = _service()
    out = []
    for l in svc.lists():
        if l.deleted:
            continue
        res = svc.list_reminders(l.id, include_completed=False)
        out += [_out(r, l.title) for r in res.reminders if not r.deleted and not r.completed]
    return out


def register(mcp):
    @mcp.tool()
    @_reset_on_error
    def list_reminder_lists() -> list[dict]:
        """List Apple Reminders lists (id, title, item count)."""
        return [
            {"id": l.id, "title": l.title, "count": l.count}
            for l in _service().lists()
            if not l.deleted
        ]

    @mcp.tool()
    @_reset_on_error
    def create_list(name: str) -> dict:
        """Create a new Reminders list (shown at the bottom of your lists in the app).
        Names must be unique. Returns the new list's id and title; use either as the `list`
        argument of the other tools. (No colour or icon yet: the app picks its default.)"""
        svc = _service()
        n = _check_list_name(name, svc.lists())
        record_name = _create_list(svc, n)
        out = {"id": record_name, "title": n}
        out["log_id"] = activity.record("create_list", f"Created list '{n}'",
                                        undo={"op": "delete_list", "id": record_name})
        return out

    @mcp.tool()
    @_reset_on_error
    def rename_list(list: str, new_name: str) -> dict:
        """Rename a list (name or id). Names must be unique."""
        svc = _service()
        lst = _list(list)
        n = _check_list_name(new_name, svc.lists(), ignore_id=lst.id)
        old = lst.title
        if n == old:
            return {"id": lst.id, "title": n, "changed": False}
        _rename_list(svc, lst, n)
        out = {"id": lst.id, "title": n, "changed": True}
        out["log_id"] = activity.record("rename_list", f"Renamed list '{old}' to '{n}'",
                                        undo={"op": "rename_list", "id": lst.id, "name": old})
        return out

    @mcp.tool()
    @_reset_on_error
    def delete_list(list: str, delete_reminders: bool = False) -> dict:
        """Delete a list (name or id). A list that still holds reminders is refused unless
        delete_reminders=True, which deletes those reminders (and their subtasks) too. Both can be
        undone with the undo tool."""
        svc = _service()
        lst = _list(list)
        live = [r for r in svc.list_reminders(lst.id, include_completed=True).reminders if not r.deleted]
        if live and not delete_reminders:
            raise ValueError(f"'{lst.title}' still holds {len(live)} reminder(s). Move them first, or pass "
                             "delete_reminders=True to delete them with the list.")
        gone = []
        for r in live:
            if not svc.get(r.id).deleted:
                svc.delete(svc.get(r.id))
            gone.append(r.id)
        _set_list_deleted(svc, lst.id, True)
        out = {"id": lst.id, "title": lst.title, "deleted_reminders": len(gone)}
        out["log_id"] = activity.record(
            "delete_list", f"Deleted list '{lst.title}'" + (f" and its {len(gone)} reminder(s)" if gone else ""),
            undo={"op": "undelete_list", "id": lst.id, "reminder_ids": gone})
        return out

    @mcp.tool()
    @_reset_on_error
    def list_reminders(
        list: str = "",
        include_completed: bool = False,
        flagged: bool | None = None,
        priority: str = "",
    ) -> list[dict]:
        """List reminders in one list (name or id), or across all lists if empty.
        Smart views: flagged=True for only flagged (False = only unflagged);
        priority='high'|'medium'|'low'|'none', or 'any' for every prioritized reminder.
        When either is set the result is sorted most urgent first (priority, then due)."""
        svc = _service()
        lists = [_list(list)] if list else [l for l in svc.lists() if not l.deleted]
        out = []
        for l in lists:
            res = svc.list_reminders(l.id, include_completed=include_completed)
            out += [_out(r, l.title) for r in res.reminders if not r.deleted]
        return _filter_reminders(out, flagged, priority)

    @mcp.tool()
    @_reset_on_error
    def create_reminder(
        title: str,
        list: str,
        due: str = "",
        notes: str = "",
        priority: int = 0,
        flagged: bool = False,
        repeat: str = "",
        repeat_interval: int = 1,
        repeat_count: int = 0,
        tags: list[str] | None = None,
        early_days: int = 0,
        early_hours: int = 0,
        early_minutes: int = 0,
        urgent: bool = False,
        links: list[str] | None = None,
        alarms: list[str] | None = None,
        notify: bool = True,
        parent: str = "",
    ) -> dict:
        """Create a real Apple Reminder in a list (name or id).
        parent: id of a reminder to nest this under as a subtask (same list, one level deep);
        prefer the add_subtask tool.
        urgent: make it an Urgent alarm (needs a due date and time; turn-on only).
        links: URLs to attach, e.g. ['https://example.com/page'].
        alarms: extra time alarms, each 'YYYY-MM-DDTHH:MM' (local) or 'due' (at the due
        time, which needs a timed due date), e.g. ['due', '2026-10-20T08:00'].
        notify: a reminder with a due TIME gets an alarm at that time, like the app does,
        because without one it never notifies. Set notify=False for a silent due time.
        Passing alarms yourself overrides this default.
        early_days / early_hours / early_minutes: also notify N days, hours or minutes before
        the due date (needs a due date; the app holds one early reminder, so use one of them).
        The due time stays the real time; this is the app's "Early Reminder" setting, so for
        "9 PM with a 15 minute heads-up" pass due 21:00 and early_minutes=15, not an extra alarm.
        due: '' for none, 'YYYY-MM-DD' for all-day, or an ISO datetime (local time).
        priority: 0 none, 1 high, 5 medium, 9 low.
        repeat: '' none, or daily|weekly|monthly|yearly; repeat_interval=every N
        units; repeat_count=0 means forever. tags: names without '#'.
        Repeat needs a due date to be meaningful."""
        early = _early_choice(early_days or None, early_hours or None, early_minutes or None)
        l = _list(list)
        dt, all_day = _parse_due(due)
        timed = dt is not None and not all_day
        alarms = _default_alarms(alarms, notify, timed)
        _validate_extras(links, alarms, timed)
        parent_id = None
        if parent:
            pr = _service().get(parent)
            if pr.list_id != l.id:
                raise ValueError("A subtask must be in the same list as its parent")
            if pr.parent_reminder_id:
                raise ValueError("Subtasks can't have subtasks (the app nests one level)")
            parent_id = pr.id
        r = _service().create(
            list_id=l.id,
            parent_reminder_id=parent_id,
            title=title,
            desc=notes,
            due_date=dt,
            all_day=all_day,
            priority=priority,
            flagged=flagged,
            time_zone=None if all_day else get_localzone_name(),
        )
        svc = _service()
        _apply_repeat(svc, r, repeat or None, repeat_interval, repeat_count)
        _apply_tags(svc, r, tags)
        _apply_links(svc, svc.get(r.id), links)
        _apply_alarms(svc, svc.get(r.id), alarms)
        if early:
            _set_early(svc, r.id, *early)
        if urgent:
            _set_urgent(svc, r.id)
        out = _detail(svc, r.id, l.title)
        out["log_id"] = activity.record(
            "create_reminder", f"Created {'subtask' if parent_id else 'reminder'} '{title}' in {l.title}",
            undo={"op": "delete_reminder", "id": r.id})
        return out

    @mcp.tool()
    @_reset_on_error
    def update_reminder(
        id: str,
        title: str | None = None,
        due: str | None = None,
        notes: str | None = None,
        priority: int | None = None,
        flagged: bool | None = None,
        repeat: str | None = None,
        repeat_interval: int = 1,
        repeat_count: int = 0,
        tags: list[str] | None = None,
        early_days: int | None = None,
        early_hours: int | None = None,
        early_minutes: int | None = None,
        links: list[str] | None = None,
        alarms: list[str] | None = None,
        notify: bool = True,
        move_to: str | None = None,
        parent: str | None = None,
    ) -> dict:
        """Update a reminder by id. Only provided fields change. due='' clears it.
        move_to: a list (name or id) to move it to, with its subtasks.
        parent: id of a reminder to nest it under as a subtask (same list, one level deep);
        '' makes it a top-level reminder again.
        early_days / early_hours / early_minutes: notify N days, hours or minutes before due
        (the app's Early Reminder; one only); 0 clears it.
        repeat: daily|weekly|monthly|yearly, or 'none' to remove.
        tags: the complete desired tag set (replaces existing); [] removes all.
        links: the complete set of attached URLs (replaces existing); [] removes all.
        alarms: the complete set of time alarms ('YYYY-MM-DDTHH:MM' local, or 'due');
        replaces existing time alarms, [] removes them, omit to keep. When you change the
        due to a time, the alarm at the due time follows it (and is added if missing, so the
        reminder notifies; notify=False keeps it silent).
        Location alarms are never touched."""
        early = _early_choice(early_days, early_hours, early_minutes)
        svc = _service()
        r = svc.get(id)
        if due is None:
            timed_due = _due_local(r) is not None
        else:
            timed_due = bool(due) and len(due.strip()) != 10
        _validate_extras(links, alarms, timed_due)
        groups = {g for g, v in (("repeat", repeat), ("tags", tags), ("links", links)) if v is not None}
        if alarms is not None or due is not None:
            groups.add("alarms")
        before = _snapshot(svc, r, groups)
        if title is not None:
            r.title = title
        if notes is not None:
            r.desc = notes
        if priority is not None:
            r.priority = priority
        if flagged is not None:
            r.flagged = flagged
        if parent is not None:
            if parent:
                pr = svc.get(parent)
                if pr.id == r.id or pr.list_id != r.list_id:
                    raise ValueError("The parent must be a different reminder in the same list")
                if pr.parent_reminder_id:
                    raise ValueError("Subtasks can't have subtasks (the app nests one level)")
                if _subtasks_of(svc, r):
                    raise ValueError("This reminder has subtasks, so it can't become a subtask itself")
            r.parent_reminder_id = svc.get(parent).id if parent else None
        move_target = _list(move_to) if move_to else None
        moved_alarms = None
        if due is not None:
            old_due = _due_local(r)
            old_date_alarms = [a["at"] for a in _read_alarms(svc, r) if a["kind"] == "date"]
            r.due_date, r.all_day = _parse_due(due)
            r.time_zone = None if r.all_day else get_localzone_name()
            if alarms is None:
                moved_alarms = _alarms_after_due_change(old_date_alarms, old_due, bool(r.due_date) and not r.all_day, notify)
        _update(svc, r)
        _apply_repeat(svc, svc.get(id), repeat, repeat_interval, repeat_count)
        _apply_tags(svc, svc.get(id), tags)
        _apply_links(svc, svc.get(id), links)
        if alarms is not None:
            _apply_alarms(svc, svc.get(id), alarms)
        elif moved_alarms is not None:
            _apply_alarms(svc, svc.get(id), moved_alarms)
        if early is not None:
            _set_early(svc, id, *early)
        if move_target is not None and svc.get(id).list_id != move_target.id:
            kids = _subtasks_of(svc, svc.get(id))
            _move_reminder(svc, svc.get(id), move_target)
            for kid in kids:
                _move_reminder(svc, svc.get(kid.id), move_target)
        _sync_alarm_nonces(svc, svc.get(id))
        out = _detail(svc, id)
        changed = [n for n, v in (("title", title), ("due", due), ("notes", notes), ("priority", priority),
                                  ("flag", flagged), ("repeat", repeat), ("tags", tags), ("links", links),
                                  ("alarms", alarms), ("list", move_to), ("parent", parent),
                                  ("early reminder", early)) if v is not None]
        out["log_id"] = activity.record(
            "update_reminder", f"Updated reminder '{out['title']}': {', '.join(changed) or 'no fields'}",
            undo={"op": "restore_reminder", "snapshot": before})
        return out

    @mcp.tool()
    @_reset_on_error
    def complete_reminder(id: str, completed: bool = True) -> dict:
        """Mark a reminder complete (or incomplete with completed=False)."""
        svc = _service()
        r = svc.get(id)
        was = r.completed
        r.completed = completed
        _update(svc, r)
        _sync_alarm_nonces(svc, svc.get(id))
        out = _out(svc.get(id))
        out["log_id"] = activity.record(
            "complete_reminder", f"Marked reminder '{out['title']}' {'complete' if completed else 'incomplete'}",
            undo={"op": "restore_reminder", "snapshot": {"id": id, "completed": was}})
        return out

    @mcp.tool()
    @_reset_on_error
    def delete_reminder(id: str) -> str:
        """Delete a reminder by id."""
        svc = _service()
        title = svc.get(id).title
        gone = _delete_with_subtasks(svc, id)  # the app removes a reminder's subtasks with it
        log_id = activity.record(
            "delete_reminder", f"Deleted reminder '{title}'" + (f" and {len(gone) - 1} subtask(s)" if len(gone) > 1 else ""),
            undo={"op": "undelete_reminder", "ids": gone})
        return f"Deleted {id} (log id {log_id})"

    _BULK_ACTIONS = ("complete", "uncomplete", "flag", "unflag", "move", "reschedule", "set_priority", "delete")

    @mcp.tool()
    @_reset_on_error
    def add_subtask(
        parent_id: str,
        title: str,
        due: str = "",
        notes: str = "",
        priority: int = 0,
        flagged: bool = False,
        notify: bool = True,
    ) -> dict:
        """Add a subtask under a reminder. It lives in the parent's list; the app nests one
        level, so a subtask can't have subtasks. Takes the same due/notes/priority/flagged/
        notify options as create_reminder. Returns the new subtask (its 'parent' is set)."""
        svc = _service()
        p = svc.get(parent_id)
        return create_reminder(title=title, list=p.list_id, due=due, notes=notes, priority=priority,
                               flagged=flagged, notify=notify, parent=p.id)

    @mcp.tool()
    @_reset_on_error
    def bulk_reminders(
        action: str,
        ids: list[str] | None = None,
        list: str = "",
        flagged: bool | None = None,
        priority: str = "",
        overdue: bool = False,
        due_from: str = "",
        due_to: str = "",
        title_contains: str = "",
        include_completed: bool = False,
        to_list: str = "",
        shift_days: int = 0,
        new_due: str = "",
        set_priority: str = "",
        apply: bool = False,
    ) -> dict:
        """Change many reminders at once. SAFE BY DEFAULT: with apply=False (the default) it only
        previews what would change; call again with apply=True to do it.
        action: complete | uncomplete | flag | unflag | move | reschedule | set_priority | delete.
        Choose reminders with ids=[...] (exact), or with filters: list (name or id), flagged,
        priority ('high'|'medium'|'low'|'none'|'any'), overdue=True, due_from / due_to
        ('YYYY-MM-DD', inclusive), title_contains. At least one is required, and at most 50
        reminders per call. Completed ones are skipped unless include_completed=True.
        move needs to_list. reschedule needs exactly one of shift_days (+/- days, keeps the time)
        or new_due ('YYYY-MM-DD' or ISO datetime). set_priority needs set_priority."""
        act = action.strip().lower()
        if act not in _BULK_ACTIONS:
            raise ValueError(f"action must be one of {_BULK_ACTIONS}, got '{action}'")
        if not (ids or list or flagged is not None or priority or overdue or due_from or due_to or title_contains):
            raise ValueError("Say which reminders: ids, or a filter (list, flagged, priority, overdue, "
                             "due_from/due_to, title_contains). Refusing to act on everything.")
        if act == "move" and not to_list:
            raise ValueError("move needs to_list")
        if act == "reschedule" and bool(shift_days) == bool(new_due):
            raise ValueError("reschedule needs exactly one of shift_days or new_due")
        if act == "set_priority" and set_priority.strip().lower() not in _PRIORITY:
            raise ValueError("set_priority must be high|medium|low|none")
        svc = _service()
        target = _list(to_list) if act == "move" else None
        if ids:
            names = {l.id: l.title for l in svc.lists()}
            chosen = [_out(svc.get(i), None) for i in ids]
            for c in chosen:
                c["list"] = names.get(c["list_id"])
        else:
            chosen = _select_reminders(svc, list, include_completed, flagged, priority, overdue,
                                       due_from, due_to, title_contains)
        if len(chosen) > 50:
            raise ValueError(f"{len(chosen)} reminders match; narrow it to 50 or fewer per call")
        plan, skipped = _plan_bulk(act, chosen, target, shift_days, new_due, set_priority)
        errors = []
        bid = None
        if apply:
            with activity.batch() as bid:
                for item in plan:
                    try:
                        rid = item["id"]
                        if act == "complete":
                            complete_reminder(rid, True)
                        elif act == "uncomplete":
                            complete_reminder(rid, False)
                        elif act == "flag":
                            update_reminder(id=rid, flagged=True)
                        elif act == "unflag":
                            update_reminder(id=rid, flagged=False)
                        elif act == "move":
                            update_reminder(id=rid, move_to=to_list)
                        elif act == "reschedule":
                            update_reminder(id=rid, due=item["new_due"])
                        elif act == "set_priority":
                            update_reminder(id=rid, priority=_PRIORITY[set_priority.strip().lower()])
                        elif act == "delete":
                            delete_reminder(rid)
                    except Exception as exc:
                        errors.append({"id": item["id"], "title": item["title"], "error": f"{type(exc).__name__}: {exc}"[:200]})
        out = {"action": act, "applied": bool(apply), "count": len(plan), "items": plan, "skipped": skipped}
        if errors:
            out["errors"] = errors
        if apply:
            out["batch"] = bid
            activity.record("bulk_reminders", f"Bulk {act}: {len(plan) - len(errors)} of {len(plan)} reminder(s) "
                            f"(batch {bid}; undo with undo(batch='{bid}'))")
        else:
            out["note"] = "Preview only. Call again with the same arguments plus apply=True to do it."
        return out
