"""Local activity log: what each agent changed, plus what's needed to undo it.

An append-only JSON-lines file, so several agents (each runs its own copy of the server)
can write without clobbering each other. "Undone" is recorded as its own line rather than by
rewriting history. Stays on this machine (it holds event and reminder titles), and the
directory is git-ignored. Set ICLOUD_MCP_DATA_DIR to move it.
"""
import contextlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

MAX_ENTRIES = 2000  # the file is trimmed to this many actions when it grows well past it

_batch: str | None = None
_agent_resolver = None  # set by server.py: returns the connected client's name


def log_path() -> Path:
    base = Path(os.environ.get("ICLOUD_MCP_DATA_DIR") or Path(__file__).with_name(".activity"))
    base.mkdir(parents=True, exist_ok=True)
    return base / "activity.jsonl"


def set_agent_resolver(fn) -> None:
    global _agent_resolver
    _agent_resolver = fn


def agent_name() -> str:
    """Who is calling: the MCP client's name if the connection tells us, else ICLOUD_MCP_AGENT."""
    if _agent_resolver is not None:
        try:
            name = _agent_resolver()
            if name:
                return str(name)
        except Exception:
            pass
    return os.environ.get("ICLOUD_MCP_AGENT") or "unknown"


@contextlib.contextmanager
def batch():
    """Group the actions made inside (e.g. one bulk operation) so they can be undone together."""
    global _batch
    previous, _batch = _batch, uuid.uuid4().hex[:8]
    try:
        yield _batch
    finally:
        _batch = previous


def _append(obj: dict) -> None:
    with open(log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _warn(message: str) -> None:
    try:
        sys.stderr.write(f"[icloud-mcp activity log] {message}\n")
    except Exception:
        pass


def record(tool: str, summary: str, undo: dict | None = None) -> str:
    """Log one change. `undo` is a self-contained instruction for reversing it (or None).
    Best-effort: the change has already happened, so a logging problem (a locked file on Windows,
    a full disk) is reported on stderr and never turns into a failed tool call."""
    entry_id = uuid.uuid4().hex[:8]
    try:
        _append({"event": "action", "id": entry_id, "ts": datetime.now().isoformat(timespec="seconds"),
                 "agent": agent_name(), "tool": tool, "summary": summary, "undo": undo, "batch": _batch})
    except Exception as exc:
        _warn(f"could not record '{summary}': {type(exc).__name__}: {exc}")
        return "unlogged"
    try:
        _maybe_trim()
    except Exception as exc:
        _warn(f"could not trim the log: {type(exc).__name__}: {exc}")
    return entry_id


def mark_undone(entry_id: str, by: str = "") -> None:
    try:
        _append({"event": "undone", "id": entry_id, "ts": datetime.now().isoformat(timespec="seconds"),
                 "agent": by or agent_name()})
    except Exception as exc:
        _warn(f"could not mark {entry_id} as undone: {type(exc).__name__}: {exc}")


def _read() -> list[dict]:
    path = log_path()
    if not path.exists():
        return []
    actions, undone = {}, {}
    order = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn or hand-edited line must not break the log
        if row.get("event") == "action":
            actions[row["id"]] = row
            order.append(row["id"])
        elif row.get("event") == "undone":
            undone[row["id"]] = row
    out = []
    for i in order:
        row = dict(actions[i])
        row["undone"] = i in undone
        row["undone_by"] = undone[i]["agent"] if i in undone else None
        out.append(row)
    return out


def entries(limit: int = 20, agent: str = "", tool: str = "", only_undoable: bool = False) -> list[dict]:
    """Newest first."""
    rows = _read()
    if agent:
        rows = [r for r in rows if r["agent"].lower() == agent.strip().lower()]
    if tool:
        rows = [r for r in rows if r["tool"] == tool.strip()]
    if only_undoable:
        rows = [r for r in rows if r["undo"] and not r["undone"]]
    return list(reversed(rows))[:limit]


def get(entry_id: str) -> dict | None:
    return next((r for r in _read() if r["id"] == entry_id.strip()), None)


def batch_entries(batch_id: str) -> list[dict]:
    """The undoable, not-yet-undone actions of one batch, newest first (the order to undo them)."""
    return [r for r in reversed(_read()) if r.get("batch") == batch_id.strip() and r["undo"] and not r["undone"]]


def _maybe_trim() -> None:
    path = log_path()
    try:
        if path.stat().st_size < MAX_ENTRIES * 2000:  # cheap check before parsing anything
            return
    except OSError:
        return
    rows = _read()
    if len(rows) <= MAX_ENTRIES:
        return
    keep = {r["id"] for r in rows[-MAX_ENTRIES:]}
    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("id") in keep:
            kept.append(line)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    for attempt in range(5):  # on Windows the swap fails while another process holds the file open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink()
    except OSError:
        pass
    _warn("could not trim the log (file busy); will try again next time")
