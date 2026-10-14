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
    def list_reminders(list: str = "", include_completed: bool = False) -> list[dict]:
        """List reminders in one list (name or id), or across all lists if empty."""
        svc = _service()
        lists = [_list(list)] if list else [l for l in svc.lists() if not l.deleted]
        out = []
        for l in lists:
            res = svc.list_reminders(l.id, include_completed=include_completed)
            out += [_out(r, l.title) for r in res.reminders if not r.deleted]
        return out

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
    ) -> dict:
        """Create a real Apple Reminder in a list (name or id).
        urgent: make it an Urgent alarm (needs a due date and time; turn-on only).
        early_days / early_hours: also notify N days or hours before the due date
        (needs a due date; the app holds one early reminder, so use one of them).
        due: '' for none, 'YYYY-MM-DD' for all-day, or an ISO datetime (local time).
        priority: 0 none, 1 high, 5 medium, 9 low.
        repeat: '' none, or daily|weekly|monthly|yearly; repeat_interval=every N
        units; repeat_count=0 means forever. tags: names without '#'.
        Repeat needs a due date to be meaningful."""
        l = _list(list)
        dt, all_day = _parse_due(due)
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
    ) -> dict:
        """Update a reminder by id. Only provided fields change. due='' clears it.
        early_days / early_hours: notify N days or hours before due; 0 clears it.
        repeat: daily|weekly|monthly|yearly, or 'none' to remove.
        tags: the complete desired tag set (replaces existing); [] removes all."""
        svc = _service()
        r = svc.get(id)
        if title is not None:
            r.title = title
        if notes is not None:
            r.desc = notes
        if priority is not None:
            r.priority = priority
        if flagged is not None:
            r.flagged = flagged
        if due is not None:
            r.due_date, r.all_day = _parse_due(due)
            r.time_zone = None if r.all_day else get_localzone_name()
        svc.update(r)
        _apply_repeat(svc, svc.get(id), repeat, repeat_interval, repeat_count)
        _apply_tags(svc, svc.get(id), tags)
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

