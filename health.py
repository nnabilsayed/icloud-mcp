"""health_check: is each service this server talks to reachable and signed in? Reports, never writes."""
import time

import reminders


def probe(name, fn):
    """Run one check; return {'ok', 'ms'} or the failure kind and the exact fix when we know it."""
    t = time.monotonic()
    try:
        detail = fn()
        out = {"ok": True}
        if detail is not None:
            out["detail"] = detail
    except Exception as exc:
        text = str(exc)
        out = {"ok": False, "error": type(exc).__name__ + (": " + text[:160] if text else "")}
        if "session expired" in text:
            out["fix"] = "Run `uv run python login.py` in the icloud-mcp folder and enter the 2FA code."
    out["ms"] = round((time.monotonic() - t) * 1000)
    return {name: out}


def register(mcp, calendar_probe):
    @mcp.tool()
    @reminders._reset_on_error
    def health_check() -> dict:
        """Check that Calendar (CalDAV) and Reminders/Notes (CloudKit session) are reachable and signed in.
        Read-only and cheap. Each service reports ok, how long it took, and for an expired Reminders
        session the exact command that fixes it. Call this first when another tool fails oddly."""
        out = {}
        out.update(probe("calendar", calendar_probe))
        out.update(probe("reminders", lambda: f"{len(list(reminders._service().lists()))} lists"))
        out["all_ok"] = all(v["ok"] for v in out.values())
        return out
