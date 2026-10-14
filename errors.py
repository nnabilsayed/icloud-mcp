"""Structured tool errors: every failure reaches the agent as
"[category] retryable=true|false: <original message> (hint)" so it can decide what to do.

Categories: not_found, auth, network, conflict, invalid, unavailable, error.
Messages are scrubbed of URLs and credentials before they leave the server.
"""
import functools
import inspect
import os
import re

CATEGORIES = ("not_found", "auth", "network", "conflict", "invalid", "unavailable", "error")

_HINTS = {
    "not_found": "check the id or name; list the items again to find the right one",
    "auth": "credentials or the iCloud session need attention; the user must fix this, retrying will not help",
    "network": "transient connection problem; retry in a few seconds",
    "conflict": "the item changed under us; re-read it and try again",
    "invalid": "fix the arguments and call again",
    "unavailable": "the service is busy or down; wait and retry later",
    "error": "unexpected failure; do not retry blindly",
}
_RETRYABLE = {"network", "conflict", "unavailable"}

_SECRET_ENV = ("ICLOUD_APP_PASSWORD", "GMAIL_APP_PASSWORD")
_URL = re.compile(r"https?://\S+")
_APP_PASSWORD = re.compile(r"\b[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}\b", re.I)


class ToolFailure(Exception):
    """An exception whose message is already classified."""


def _status(msg: str):
    m = re.search(r"\b(?:HTTP\s*)?([1-5]\d\d)\b\s*(?:[A-Za-z]+)?", msg)
    return int(m.group(1)) if m else None


def classify(exc: BaseException) -> str:
    name = type(exc).__name__
    msg = str(exc)
    low = msg.lower()
    full = f"{name} {low}"
    if isinstance(exc, ToolFailure):
        return "error"
    if (name in ("NotFoundError",) or "not_found" in low or "not found" in low
            or "no such" in low or "does not exist" in low or re.search(r"\b404\b", low)):
        return "not_found"
    if (name in ("AuthorizationError", "PyiCloudFailedLoginException", "PyiCloudAPIResponseException")
            and "401" in low) or any(k in full for k in (
            "authenticationfailed", "authorizationerror", "unauthorized", "session expired",
            "requires_2fa", "invalid credentials", "login failed")) or re.search(r"\b(401|403)\b", low):
        return "auth"
    if isinstance(exc, KeyError) and str(exc).strip("'\"") in ("ICLOUD_USERNAME", "ICLOUD_APP_PASSWORD",
                                                              "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"):
        return "auth"
    if any(k in low for k in ("conflict", "oplock", "precondition failed")) or re.search(r"\b(409|412)\b", low):
        return "conflict"
    if (isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(exc, FileNotFoundError)) \
            or any(k in full for k in ("connectionerror", "connectionexception", "timeout", "timed out",
                                       "remote end closed", "name resolution", "getaddrinfo",
                                       "connection reset", "connection aborted")):
        return "network"
    if re.search(r"\b(429|5\d\d)\b", low) or any(k in low for k in ("too many requests", "temporarily", "service unavailable")):
        return "unavailable"
    if isinstance(exc, (ValueError, TypeError)) or "bad_request" in low or re.search(r"\b(400|422)\b", low):
        return "invalid"
    return "error"


def scrub(text: str) -> str:
    for var in _SECRET_ENV:
        secret = os.environ.get(var)
        if secret and len(secret) >= 4:
            text = text.replace(secret, "<redacted>")
    text = _URL.sub("<url>", text)
    return _APP_PASSWORD.sub("<redacted>", text)


def format_error(exc: BaseException) -> str:
    cat = classify(exc)
    msg = scrub(str(exc).strip()) or type(exc).__name__
    return f"[{cat}] retryable={'true' if cat in _RETRYABLE else 'false'}: {msg} ({_HINTS[cat]})"


def structured(fn):
    """Wrap a tool so failures become ToolFailure with a classified message.
    functools.wraps keeps the signature so FastMCP builds the same schema."""
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def awrapper(*a, **kw):
            try:
                return await fn(*a, **kw)
            except ToolFailure:
                raise
            except Exception as exc:
                raise ToolFailure(format_error(exc)) from None
        return awrapper

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except ToolFailure:
            raise
        except Exception as exc:
            raise ToolFailure(format_error(exc)) from None
    return wrapper


def install(mcp):
    """Make every @mcp.tool() registered from now on report structured errors.
    The decorated function itself is returned unchanged, so direct calls (tests) are unaffected."""
    original = mcp.tool

    def tool(*args, **kwargs):
        register = original(*args, **kwargs)

        def decorator(fn):
            register(structured(fn))
            return fn
        return decorator

    mcp.tool = tool
    return mcp
