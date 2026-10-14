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
from datetime import date, datetime, timezone
from pathlib import Path

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
_UNIT_HOURS = 1  # verified by decoding a "3 hours before" alert made in the app
_UNIT_DAYS = 2


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


def _apply_repeat(svc, r, repeat: str | None, interval: int, count: int) -> None:
    """repeat: None = leave alone, ''/'none' = remove, else daily|weekly|monthly|yearly."""
    from pyicloud.services.reminders.models import RecurrenceFrequency, RecurrenceRule

    if repeat is None:
        return
    key = repeat.strip().lower()
    if key not in ("", "none") and key not in _FREQ:
        raise ValueError(f"repeat must be one of {list(_FREQ)} or 'none', got '{repeat}'")
    for old in _read_rules(svc, r):
        stub = RecurrenceRule(  # frequency is irrelevant for deletion; avoids enum issues
            id=old["name"],
            reminder_id=r.id,
            frequency=RecurrenceFrequency.DAILY,
            record_change_tag=old["tag"],
        )
        svc.delete_recurrence_rule(r, stub)
    if key in ("", "none"):
        return
    wire = _FREQ[key]
    # The library writes int(enum) as-is, so enum value N lands on the wire as N.
    # The enum has no 0, so daily is created as 1 and then patched to 0 below.
    rule = svc.create_recurrence_rule(
        r,
        frequency=RecurrenceFrequency(max(wire, 1)),
        interval=interval,
        occurrence_count=count,
    )
    if wire == 0:
        w = svc._writes
        w._submit_single_record_update(
            operation_name="Set daily recurrence",
            record_name=rule.id,
            record_type="RecurrenceRule",
            record_change_tag=rule.record_change_tag,
            fields={"Frequency": {"type": "INT64", "value": 0}},
            model_obj=rule,
        )


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
    d["alarms"] = _alarms_out(svc, r)
    alerts = (_read_alerts_blob(svc, rid) or {}).get("dueDateDeltaAlerts", [])
    for key, unit in (("early_hours", _UNIT_HOURS), ("early_days", _UNIT_DAYS)):
        d[key] = next(
            (abs(a["dueDateDeltaCount"]) for a in alerts if a.get("dueDateDeltaUnit") == unit),
            None,
        )
    d["early_other"] = [
        a for a in alerts if a.get("dueDateDeltaUnit") not in (_UNIT_HOURS, _UNIT_DAYS)
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
        urgent: bool = False,
        links: list[str] | None = None,
        alarms: list[str] | None = None,
        notify: bool = True,
    ) -> dict:
        """Create a real Apple Reminder in a list (name or id).
        urgent: make it an Urgent alarm (needs a due date and time; turn-on only).
        links: URLs to attach, e.g. ['https://example.com/page'].
        alarms: extra time alarms, each 'YYYY-MM-DDTHH:MM' (local) or 'due' (at the due
        time, which needs a timed due date), e.g. ['due', '2026-10-20T08:00'].
        notify: a reminder with a due TIME gets an alarm at that time, like the app does,
        because without one it never notifies. Set notify=False for a silent due time.
        Passing alarms yourself overrides this default.
        early_days / early_hours: also notify N days or hours before the due date
        (needs a due date; the app holds one early reminder, so use one of them).
        due: '' for none, 'YYYY-MM-DD' for all-day, or an ISO datetime (local time).
        priority: 0 none, 1 high, 5 medium, 9 low.
        repeat: '' none, or daily|weekly|monthly|yearly; repeat_interval=every N
        units; repeat_count=0 means forever. tags: names without '#'.
        Repeat needs a due date to be meaningful."""
        l = _list(list)
        dt, all_day = _parse_due(due)
        timed = dt is not None and not all_day
        alarms = _default_alarms(alarms, notify, timed)
        _validate_extras(links, alarms, timed)
        r = _service().create(
            list_id=l.id,
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
        if early_hours:
            _set_early(svc, r.id, early_hours, _UNIT_HOURS)
        elif early_days:
            _set_early(svc, r.id, early_days, _UNIT_DAYS)
        if urgent:
            _set_urgent(svc, r.id)
        return _detail(svc, r.id, l.title)

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
        links: list[str] | None = None,
        alarms: list[str] | None = None,
        notify: bool = True,
    ) -> dict:
        """Update a reminder by id. Only provided fields change. due='' clears it.
        early_days / early_hours: notify N days or hours before due; 0 clears it.
        repeat: daily|weekly|monthly|yearly, or 'none' to remove.
        tags: the complete desired tag set (replaces existing); [] removes all.
        links: the complete set of attached URLs (replaces existing); [] removes all.
        alarms: the complete set of time alarms ('YYYY-MM-DDTHH:MM' local, or 'due');
        replaces existing time alarms, [] removes them, omit to keep. When you change the
        due to a time, the alarm at the due time follows it (and is added if missing, so the
        reminder notifies; notify=False keeps it silent).
        Location alarms are never touched."""
        svc = _service()
        r = svc.get(id)
        if due is None:
            timed_due = _due_local(r) is not None
        else:
            timed_due = bool(due) and len(due.strip()) != 10
        _validate_extras(links, alarms, timed_due)
        if title is not None:
            r.title = title
        if notes is not None:
            r.desc = notes
        if priority is not None:
            r.priority = priority
        if flagged is not None:
            r.flagged = flagged
        moved_alarms = None
        if due is not None:
            old_due = _due_local(r)
            old_date_alarms = [a["at"] for a in _read_alarms(svc, r) if a["kind"] == "date"]
            r.due_date, r.all_day = _parse_due(due)
            r.time_zone = None if r.all_day else get_localzone_name()
            if alarms is None:
                moved_alarms = _alarms_after_due_change(old_date_alarms, old_due, bool(r.due_date) and not r.all_day, notify)
        svc.update(r)
        _apply_repeat(svc, svc.get(id), repeat, repeat_interval, repeat_count)
        _apply_tags(svc, svc.get(id), tags)
        _apply_links(svc, svc.get(id), links)
        if alarms is not None:
            _apply_alarms(svc, svc.get(id), alarms)
        elif moved_alarms is not None:
            _apply_alarms(svc, svc.get(id), moved_alarms)
        if early_hours is not None:
            _set_early(svc, id, early_hours, _UNIT_HOURS)
        elif early_days is not None:
            _set_early(svc, id, early_days, _UNIT_DAYS)
        return _detail(svc, id)

    @mcp.tool()
    @_reset_on_error
    def complete_reminder(id: str, completed: bool = True) -> dict:
        """Mark a reminder complete (or incomplete with completed=False)."""
        svc = _service()
        r = svc.get(id)
        r.completed = completed
        svc.update(r)
        return _out(svc.get(id))

    @mcp.tool()
    @_reset_on_error
    def delete_reminder(id: str) -> str:
        """Delete a reminder by id."""
        svc = _service()
        svc.delete(svc.get(id))
        return f"Deleted {id}"

