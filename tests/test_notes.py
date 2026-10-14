"""Creating, moving and deleting Apple Notes. Offline: the note body is checked byte for byte
against one the Notes app itself wrote, and the tools run against fakes."""
import gzip
from types import SimpleNamespace

import pytest

import notes as N

# From a note the Notes app wrote by pasting three lines (one insertion, 54 characters).
APP_TEXT = "Pasted note title\nsecond line pasted\nthird line pasted"
APP_REPLICA = bytes.fromhex("00112233445566778899aabbccddeeff")  # a made-up id; the real one is per account
APP_CRDT = bytes.fromhex(
    "1a100a040800100010001a04080010002801"            # root substring
    "1a100a040801100010361a04080110002802"            # the 54-character insertion
    "1a160a08080010ffffffff0f10001a08080010ffffffff0f"  # end sentinel
    "221c0a1a0a1000112233445566778899aabbccddeeff1202083612020801"  # version vector (replica id made up)
)


def test_crdt_structure_matches_the_one_the_app_wrote_byte_for_byte():
    assert len(APP_TEXT) == 54
    built = N.build_note(APP_TEXT, APP_REPLICA, runs=[])
    expected = N._ld(2, APP_TEXT.encode()) + APP_CRDT
    assert built == expected


def test_lengths_are_utf16_code_units_so_an_emoji_counts_twice():
    assert N.utf16_len("abc") == 3
    assert N.utf16_len("ملاحظة") == 6   # Arabic stays one unit each
    assert N.utf16_len("\U0001F600") == 2
    text = "Z5 \U0001F600\nx"
    note = N.build_note(text, APP_REPLICA, runs=[])
    version = next(v for fn, _, v in N._fields(note) if fn == 4)
    entry = next(v for fn, _, v in N._fields(version) if fn == 1)
    clocks = [dict((k, c) for k, _, c in N._fields(x))[1] for n, _, x in N._fields(entry) if n == 2]
    assert clocks == [N.utf16_len(text), 1]


@pytest.mark.parametrize("n", [1, 127, 128, 300, 20000])
def test_text_length_varints_round_trip_across_byte_boundaries(n):
    note = N.build_note("a" * n, APP_REPLICA, runs=[])
    subs = [N._fields(v) for fn, _, v in N._fields(note) if fn == 3]
    assert dict((k, v) for k, _, v in subs[1])[2] == n  # second substring holds the whole text


def test_wrapped_body_unpacks_and_carries_the_apps_gzip_header():
    note = N.build_note("Title\nbody", APP_REPLICA)
    packed = N.wrap_note(note)
    assert N.note_from_text_data(packed) == note
    raw = __import__("base64").b64decode(packed)
    assert raw[:10] == bytes.fromhex("1f8b0800000000000013")
    assert gzip.decompress(raw)  # a valid gzip stream


def test_replica_is_found_in_the_version_vector_and_absent_without_one():
    assert N.replica_of(N.build_note("x", APP_REPLICA, runs=[])) == APP_REPLICA
    assert N.replica_of(N._ld(2, b"no crdt here")) is None


def _runs(note):
    """[(length, style_fields, timestamp_present)] for each formatting run of a Note message."""
    out = []
    for fn, _, v in N._fields(note):
        if fn == 5:
            f = {k: x for k, _, x in N._fields(v)}
            style = {k: x for k, _, x in N._fields(f[2])}
            out.append((f[1], style, 13 in f))
    return out


def test_first_line_is_an_explicit_title_run_and_the_rest_a_body_run():
    text = "Title line\nbody one\nbody two"
    runs = _runs(N.build_note(text, APP_REPLICA))
    assert [r[0] for r in runs] == [len("Title line\n"), len("body one\nbody two")]
    # the app marks a title with an explicit style_type of 0 and leaves body text unmarked
    assert runs[0][1].get(1) == 0 and 1 in runs[0][1]
    assert 1 not in runs[1][1]
    assert all(len(r[1][9]) == 16 and r[2] for r in runs)  # paragraph uuid + timestamp
    assert sum(r[0] for r in runs) == N.utf16_len(text)


def test_a_single_line_note_is_one_title_run():
    runs = _runs(N.build_note("Just a title", APP_REPLICA))
    assert len(runs) == 1 and runs[0][0] == len("Just a title") and runs[0][1].get(1) == 0


def test_run_lengths_count_utf16_units_for_title_and_body():
    text = "T\U0001F600\nملاحظة\U0001F600"
    runs = _runs(N.build_note(text, APP_REPLICA))
    assert [r[0] for r in runs] == [N.utf16_len("T\U0001F600\n"), N.utf16_len("ملاحظة\U0001F600")]


# ---- tools, against fakes -------------------------------------------------------------
class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture
def tools(monkeypatch):
    mcp = FakeMCP()
    N.register(mcp)
    logged = []
    monkeypatch.setattr(N.activity, "record", lambda tool, summary, undo=None: logged.append((tool, summary, undo)) or "log1")
    monkeypatch.setattr(N, "_notes", lambda: SimpleNamespace())
    folders = {"": ("DefaultFolder-CloudKit", "Notes"), "work": ("F-WORK", "Work"),
               "recently deleted": (N.TRASH_FOLDER, "Recently Deleted")}
    monkeypatch.setattr(N, "_folder", lambda svc, ident: folders[ident.strip().lower()])
    return SimpleNamespace(t=mcp.tools, logged=logged)


def test_create_note_returns_the_title_and_logs_an_undo_that_trashes_it(tools, monkeypatch):
    seen = {}
    monkeypatch.setattr(N, "_create", lambda svc, text, fid: seen.update(text=text, fid=fid) or ("NOTE-1", "Shopping", "milk"))
    out = tools.t["create_note"]("Shopping\nmilk\n", folder="Work")
    assert seen == {"text": "Shopping\nmilk", "fid": "F-WORK"}
    assert out["id"] == "NOTE-1" and out["folder"] == "Work" and out["log_id"] == "log1"
    assert tools.logged[0][2] == {"op": "trash_note", "id": "NOTE-1"}


def test_create_note_normalises_line_endings(tools, monkeypatch):
    seen = {}
    monkeypatch.setattr(N, "_create", lambda svc, text, fid: seen.update(text=text) or ("N", "a", "b"))
    tools.t["create_note"]("a\r\nb\rc")
    assert seen["text"] == "a\nb\nc"


def test_create_note_rejects_empty_text_and_the_trash_folder(tools):
    for bad in ("", "   ", "\n\n"):
        with pytest.raises(ValueError, match="empty"):
            tools.t["create_note"](bad)
    with pytest.raises(ValueError, match="Recently Deleted"):
        tools.t["create_note"]("hello", folder="Recently Deleted")
    assert tools.logged == []


def test_delete_note_moves_to_recently_deleted_and_remembers_where_it_was(tools, monkeypatch):
    moved = []
    monkeypatch.setattr(N, "_move", lambda svc, nid, fid: moved.append((nid, fid)) or {"from": "F-WORK"})
    out = tools.t["delete_note"]("N-9")
    assert moved == [("N-9", N.TRASH_FOLDER)] and out["folder"] == "Recently Deleted"
    assert tools.logged[0][2] == {"op": "restore_note", "id": "N-9", "folder_id": "F-WORK"}


def test_move_note_logs_the_way_back(tools, monkeypatch):
    monkeypatch.setattr(N, "_move", lambda svc, nid, fid: {"from": "DefaultFolder-CloudKit"})
    tools.t["move_note"]("N-2", "Work")
    assert tools.logged[0][2] == {"op": "restore_note", "id": "N-2", "folder_id": "DefaultFolder-CloudKit"}


def test_undo_ops(monkeypatch):
    moved = []
    monkeypatch.setattr(N, "_notes", lambda: SimpleNamespace())
    monkeypatch.setattr(N, "_move", lambda svc, nid, fid: moved.append((nid, fid)) or {"from": "x"})
    assert "Recently Deleted" in N.undo_op({"op": "trash_note", "id": "N-1"})
    assert "Restored" in N.undo_op({"op": "restore_note", "id": "N-1", "folder_id": "F-WORK"})
    assert N.undo_op({"op": "restore_note", "id": "N-1"})  # missing folder falls back to Notes
    assert moved == [("N-1", N.TRASH_FOLDER), ("N-1", "F-WORK"), ("N-1", N.DEFAULT_FOLDER)]
    with pytest.raises(ValueError, match="Unknown"):
        N.undo_op({"op": "nope"})
    assert N.UNDO_OPS == {"trash_note", "restore_note"}
