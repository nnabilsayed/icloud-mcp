"""Apple Notes tools via iCloud CloudKit (pyicloud): create, move, and delete (to Recently Deleted).

Unofficial, like the Reminders tools: it writes the same private records the Notes app does.
Reading is limited on purpose: titles and folders can be listed, and one unlocked note can be
read by id. Nothing searches note bodies, and note text is never written to the activity log.

A note's text is stored as a gzipped protobuf holding the text, a small CRDT structure
(three "substring" records plus a version vector) and formatting runs. The library's schema
skips the CRDT parts, and a note without them is accepted and then trashed within a minute,
so they are built here from the text length. See FINDINGS.md.
"""
import base64
import gzip
import json
import time
import uuid
from pathlib import Path

import activity
import reminders

DEFAULT_FOLDER = "DefaultFolder-CloudKit"  # the "Notes" folder
TRASH_FOLDER = "TrashFolder-CloudKit"      # "Recently Deleted"
UNDO_OPS = {"trash_note", "restore_note"}

# Learned once from a note the Notes app wrote; per-account, so never committed.
_IDENTITY_FILE = Path(__file__).with_name(".notes_identity")

# ---- protobuf wire helpers ------------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(b: bytes, i: int):
    shift = value = 0
    while True:
        c = b[i]
        i += 1
        value |= (c & 0x7F) << shift
        if not c & 0x80:
            return value, i
        shift += 7


def _fields(b: bytes) -> list:
    """[(field_number, wire_type, value)] for one protobuf message level."""
    i, out = 0, []
    while i < len(b):
        key, i = _read_varint(b, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(b, i)
        elif wt == 2:
            n, i = _read_varint(b, i)
            v = bytes(b[i:i + n])
            i += n
        elif wt == 1:
            v = bytes(b[i:i + 8])
            i += 8
        elif wt == 5:
            v = bytes(b[i:i + 4])
            i += 4
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.append((fn, wt, v))
    return out


def _ld(fn: int, payload: bytes) -> bytes:  # length-delimited field
    return bytes([(fn << 3) | 2]) + _varint(len(payload)) + payload


def _vi(fn: int, n: int) -> bytes:          # varint field
    return bytes([fn << 3]) + _varint(n)


def utf16_len(text: str) -> int:
    """Notes counts characters in UTF-16 code units (an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


# ---- building a note body -------------------------------------------------------------


def _run(length: int, title: bool) -> bytes:
    """One formatting run, encoded by hand: the app writes an explicit style_type of 0 for a
    title paragraph and omits the field for body text, and the library's protobuf binding drops
    a zero, so it cannot express a title."""
    style = (_vi(1, 0) if title else b"") + _ld(9, uuid.uuid4().bytes)
    return _vi(1, length) + _ld(2, style) + _vi(13, int(time.time()))


def build_note(text: str, replica: bytes, runs=None) -> bytes:
    """The Note message: text, CRDT substrings, version vector, formatting runs.

    For a note written in one insertion, as here: a root substring, one substring holding the
    whole text (its length in UTF-16 units), an end sentinel, and a version vector with the
    author replica's clock set to the text length."""
    n = utf16_len(text)
    root = _ld(3, _ld(1, _vi(1, 0) + _vi(2, 0)) + _vi(2, 0) + _ld(3, _vi(1, 0) + _vi(2, 0)) + _vi(5, 1))
    body = _ld(3, _ld(1, _vi(1, 1) + _vi(2, 0)) + _vi(2, n) + _ld(3, _vi(1, 1) + _vi(2, 0)) + _vi(5, 2))
    end = _ld(3, _ld(1, _vi(1, 0) + _vi(2, 0xFFFFFFFF)) + _vi(2, 0) + _ld(3, _vi(1, 0) + _vi(2, 0xFFFFFFFF)))
    version = _ld(4, _ld(1, _ld(1, replica) + _ld(2, _vi(1, n)) + _ld(2, _vi(1, 1))))
    if runs is None:
        first, sep, rest = text.partition("\n")
        runs = [_run(utf16_len(first) + len(sep), True)]
        if rest:
            runs.append(_run(utf16_len(rest), False))
    return _ld(2, text.encode("utf-8")) + root + body + end + version + b"".join(_ld(5, r) for r in runs)


def wrap_note(note: bytes) -> str:
    """Document envelope, gzipped with the app's header bytes, base64 for the wire."""
    doc = _vi(1, 0) + _vi(2, 0) + _ld(3, note)
    gz = bytearray(gzip.compress(_vi(1, 0) + _ld(2, doc), mtime=0))
    gz[8], gz[9] = 0x00, 0x13
    return base64.b64encode(bytes(gz)).decode()


def note_from_text_data(value) -> bytes:
    raw = gzip.decompress(base64.b64decode(value))
    doc = next(v for fn, _, v in _fields(raw) if fn == 2)
    return next(v for fn, _, v in _fields(doc) if fn == 3)


def replica_of(note: bytes) -> bytes | None:
    """The author replica's UUID, from the version vector, or None if the note has no CRDT part."""
    for fn, _, v in _fields(note):
        if fn == 4:
            for f1, _, entry in _fields(v):
                if f1 == 1:
                    return next((x for n, _, x in _fields(entry) if n == 1), None)
    return None


# ---- service + identity ---------------------------------------------------------------


def _notes():
    reminders._service()  # builds and validates the shared iCloud session
    return reminders._api.notes


def _lookup(svc, record_name: str) -> dict:
    from pyicloud.services.notes._constants import NOTES_ZONE_REQ

    resp = svc.raw._client.lookup([record_name], zone_id=NOTES_ZONE_REQ)
    if not resp.records:
        raise ValueError(f"No note '{record_name}'")
    rec = resp.records[0].model_dump()
    if rec.get("serverErrorCode"):
        raise ValueError(f"No note '{record_name}' ({rec.get('serverErrorCode')})")
    return rec


def _identity(svc) -> dict:
    """The author replica plus the two blobs every note of this account carries. Learned from a
    note the app wrote, then cached."""
    try:
        return json.loads(_IDENTITY_FILE.read_text())
    except (FileNotFoundError, ValueError):
        pass
    for summary in svc.recents(limit=40):
        if summary.is_locked or summary.is_deleted:
            continue
        try:
            fields = _lookup(svc, summary.id)["fields"]
            replica = replica_of(note_from_text_data(fields["TextDataEncrypted"]["value"]))
            ident = {
                "replica": replica.hex() if replica else None,
                "version_blob": fields["ReplicaIDToNotesVersionDataEncrypted"]["value"],
                "user_blob": fields["ReplicaIDToUserIDEncrypted"]["value"],
            }
        except Exception:
            continue
        if ident["replica"]:
            for k in ("version_blob", "user_blob"):
                if isinstance(ident[k], bytes):
                    ident[k] = ident[k].decode()
            _IDENTITY_FILE.write_text(json.dumps(ident))
            return ident
    raise RuntimeError(
        "Creating notes needs a one-time setup: create any note in the Notes app on your phone "
        "or Mac (a few words is enough), wait a minute for it to sync, then try again."
    )


def _folder(svc, ident: str) -> tuple[str, str]:
    """(id, name) for a folder given by name or id. Empty means the default Notes folder."""
    folders = [f for f in svc.folders()]
    if not ident.strip():
        ident = DEFAULT_FOLDER
    exact = [f for f in folders if f.id == ident.strip()]
    named = [f for f in folders if f.name.strip().lower() == ident.strip().lower()]
    hit = exact or named
    if len(hit) == 1:
        return hit[0].id, hit[0].name
    if not hit:
        raise ValueError(f"No notes folder '{ident}'. Have: {[f.name for f in folders]}")
    raise ValueError(f"'{ident}' is ambiguous; use one of these ids: {[f.id for f in hit]}")


def _ref(folder_id: str) -> dict:
    return {"recordName": folder_id, "action": "VALIDATE"}


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode()


def _create(svc, text: str, folder_id: str) -> tuple[str, str, str]:
    from pyicloud.common.cloudkit import CKModifyOperation, CKWriteParent, CKWriteRecord
    from pyicloud.services.notes._constants import NOTES_ZONE_REQ

    ident = _identity(svc)
    title, _, rest = text.partition("\n")
    snippet = next((ln.strip() for ln in rest.split("\n") if ln.strip()), "")[:100]
    rid, now = str(uuid.uuid4()).upper(), int(time.time() * 1000)
    fields = {
        "TitleEncrypted": {"type": "ENCRYPTED_BYTES", "value": _b64(title.strip())},
        "SnippetEncrypted": {"type": "ENCRYPTED_BYTES", "value": _b64(snippet)},
        "TextDataEncrypted": {"type": "ENCRYPTED_BYTES", "value": wrap_note(build_note(text, bytes.fromhex(ident["replica"])))},
        "ReplicaIDToNotesVersionDataEncrypted": {"type": "ENCRYPTED_BYTES", "value": ident["version_blob"]},
        "ReplicaIDToUserIDEncrypted": {"type": "ENCRYPTED_BYTES", "value": ident["user_blob"]},
        "CreationDate": {"type": "TIMESTAMP", "value": now},
        "ModificationDate": {"type": "TIMESTAMP", "value": now},
        "FoldersModificationDate": {"type": "TIMESTAMP", "value": now},
        "Folder": {"type": "REFERENCE", "value": _ref(folder_id)},
        "Folders": {"type": "REFERENCE_LIST", "value": [_ref(folder_id)]},
        "AttachmentViewType": {"type": "INT64", "value": 0},
        "PaperStyleType": {"type": "INT64", "value": 1},
        "MinimumSupportedNotesVersion": {"type": "INT64", "value": 0},
        "Deleted": {"type": "INT64", "value": 0},
    }
    rec = CKWriteRecord(recordName=rid, recordType="Note", recordChangeTag=None, fields=fields,
                        parent=CKWriteParent(recordName=folder_id))
    resp = svc.raw._client.modify(operations=[CKModifyOperation(operationType="create", record=rec)],
                                  zone_id=NOTES_ZONE_REQ, atomic=True)
    errs = [getattr(r, "reason", None) or getattr(r, "serverErrorCode", None)
            for r in resp.records if getattr(r, "serverErrorCode", None)]
    if errs:
        raise RuntimeError(f"iCloud rejected the note: {errs[0]}")
    return rid, title.strip(), snippet


def _move(svc, note_id: str, folder_id: str) -> dict:
    """Move a note between folders; returns {'from': previous folder id}."""
    from pyicloud.common.cloudkit import CKModifyOperation, CKWriteParent, CKWriteRecord
    from pyicloud.services.notes._constants import NOTES_ZONE_REQ

    rec = _lookup(svc, note_id)
    fields = rec["fields"]
    if rec.get("recordType") != "Note":
        raise ValueError(f"'{note_id}' is not a note")
    if fields.get("Deleted", {}).get("value"):
        raise ValueError("That note was permanently deleted")
    if "TextDataEncrypted" not in fields:
        raise ValueError("That note is locked, so it can't be moved")
    previous = ((rec.get("parent") or {}).get("recordName")) or DEFAULT_FOLDER
    now = int(time.time() * 1000)
    update = CKWriteRecord(
        recordName=note_id, recordType="Note", recordChangeTag=rec.get("recordChangeTag"),
        fields={
            "Folder": {"type": "REFERENCE", "value": _ref(folder_id)},
            "Folders": {"type": "REFERENCE_LIST", "value": [_ref(folder_id)]},
            "FoldersModificationDate": {"type": "TIMESTAMP", "value": now},
            "ModificationDate": {"type": "TIMESTAMP", "value": now},
        },
        parent=CKWriteParent(recordName=folder_id),
    )
    resp = svc.raw._client.modify(operations=[CKModifyOperation(operationType="update", record=update)],
                                  zone_id=NOTES_ZONE_REQ, atomic=True)
    errs = [getattr(r, "reason", None) or getattr(r, "serverErrorCode", None)
            for r in resp.records if getattr(r, "serverErrorCode", None)]
    if errs:
        raise RuntimeError(f"iCloud rejected the move: {errs[0]}")
    return {"from": previous}


def undo_op(op: dict) -> str:
    svc = _notes()
    kind = op["op"]
    if kind == "trash_note":
        _move(svc, op["id"], TRASH_FOLDER)
        return "Moved the note to Recently Deleted"
    if kind == "restore_note":
        _move(svc, op["id"], op.get("folder_id") or DEFAULT_FOLDER)
        return "Restored the note"
    raise ValueError(f"Unknown note undo op '{kind}'")


def _read(svc, note_id: str) -> dict:
    rec = _lookup(svc, note_id)
    if "passwordprotected" in str(rec.get("recordType", "")).lower():
        raise ValueError("That note is locked and can't be read")
    fields = rec["fields"]
    if "TextDataEncrypted" not in fields:
        raise ValueError("That note has no readable text")
    note = note_from_text_data(fields["TextDataEncrypted"]["value"])
    text = next((v for fn, _, v in _fields(note) if fn == 2), b"").decode("utf-8")
    return {"id": note_id, "text": text, "characters": len(text)}


# ---- MCP tools ------------------------------------------------------------------------


def register(mcp):
    @mcp.tool()
    @reminders._reset_on_error
    def create_note(text: str, folder: str = "") -> dict:
        """Create an Apple Note. The first line becomes the title (shown in title style), the
        rest is the body. folder: a folder name or id; empty means the default "Notes" folder.
        Plain text only."""
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not text.strip():
            raise ValueError("The note is empty")
        svc = _notes()
        folder_id, folder_name = _folder(svc, folder)
        if folder_id == TRASH_FOLDER:
            raise ValueError("Can't create a note in Recently Deleted")
        rid, title, snippet = _create(svc, text, folder_id)
        out = {"id": rid, "title": title, "snippet": snippet, "folder": folder_name, "folder_id": folder_id}
        out["log_id"] = activity.record("create_note", f"Created note '{title[:60]}' in {folder_name}",
                                        undo={"op": "trash_note", "id": rid})
        return out

    @mcp.tool()
    @reminders._reset_on_error
    def move_note(id: str, folder: str) -> dict:
        """Move a note to another folder (name or id). Moving to "Recently Deleted" is the same
        as delete_note; moving a trashed note back to a normal folder restores it."""
        svc = _notes()
        folder_id, folder_name = _folder(svc, folder)
        info = _move(svc, id, folder_id)
        out = {"id": id, "folder": folder_name, "folder_id": folder_id}
        out["log_id"] = activity.record("move_note", f"Moved a note to {folder_name}",
                                        undo={"op": "restore_note", "id": id, "folder_id": info["from"]})
        return out

    @mcp.tool()
    @reminders._reset_on_error
    def delete_note(id: str) -> dict:
        """Move a note to Recently Deleted. It is not erased: Notes keeps it for 30 days and
        undo restores it."""
        svc = _notes()
        info = _move(svc, id, TRASH_FOLDER)
        out = {"id": id, "folder": "Recently Deleted"}
        out["log_id"] = activity.record("delete_note", "Moved a note to Recently Deleted",
                                        undo={"op": "restore_note", "id": id, "folder_id": info["from"]})
        return out

    @mcp.tool()
    @reminders._reset_on_error
    def list_note_folders() -> list:
        """List the Notes folders (id, name). Includes Recently Deleted."""
        return [{"id": f.id, "name": f.name} for f in _notes().folders()]

    @mcp.tool()
    @reminders._reset_on_error
    def list_notes(folder: str = "", limit: int = 20) -> list:
        """List the newest notes in a folder (name or id; empty is the default Notes folder):
        id, title, modified, locked. Titles only; use read_note for the text."""
        svc = _notes()
        folder_id, _ = _folder(svc, folder)
        limit = max(1, min(int(limit), 100))
        out = []
        for s in svc.in_folder(folder_id, limit=limit):
            out.append({"id": s.id, "title": s.title or "", "folder": s.folder_name,
                        "modified": s.modified_at.isoformat() if s.modified_at else None,
                        "locked": bool(s.is_locked)})
        return out

    @mcp.tool()
    @reminders._reset_on_error
    def read_note(id: str) -> dict:
        """Read the plain text of one note by id. Locked notes are refused. Formatting,
        checklists and attachments are not included."""
        return _read(_notes(), id)
