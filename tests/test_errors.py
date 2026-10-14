import asyncio

import pytest

import errors
import server


class NotFoundError(Exception):
    pass


class PyiCloudConnectionException(Exception):
    pass


CASES = [
    (ValueError("Calendar 'Work' not found"), "not_found", False),
    (NotFoundError("gone"), "not_found", False),
    (Exception("NOT_FOUND record missing"), "not_found", False),
    (Exception("HTTP 401 Unauthorized"), "auth", False),
    (Exception("403 Forbidden"), "auth", False),
    (RuntimeError("iCloud session expired. Run login.py"), "auth", False),
    (Exception("[AUTHENTICATIONFAILED] Invalid credentials"), "auth", False),
    (KeyError("ICLOUD_APP_PASSWORD"), "auth", False),
    (Exception("412 Precondition Failed"), "conflict", True),
    (Exception("CONFLICT (client oplock error updating record)"), "conflict", True),
    (PyiCloudConnectionException("Remote end closed connection"), "network", True),
    (ConnectionError("reset"), "network", True),
    (TimeoutError("slow"), "network", True),
    (Exception("getaddrinfo failed"), "network", True),
    (Exception("503 Service Unavailable"), "unavailable", True),
    (Exception("429 Too Many Requests"), "unavailable", True),
    (ValueError("repeat must be one of daily"), "invalid", False),
    (TypeError("bad arg"), "invalid", False),
    (Exception("BAD_REQUEST defined to be ENCRYPTED_STRING"), "invalid", False),
    (RuntimeError("something odd"), "error", False),
]


@pytest.mark.parametrize("exc,cat,retry", CASES)
def test_classification(exc, cat, retry):
    text = errors.format_error(exc)
    assert text.startswith(f"[{cat}] retryable={'true' if retry else 'false'}: ")
    assert errors.classify(exc) == cat


def test_original_message_kept():
    assert "Calendar 'Work' not found" in errors.format_error(ValueError("Calendar 'Work' not found"))


def test_urls_and_credentials_scrubbed(monkeypatch):
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "hunter2-secret")
    text = errors.format_error(Exception(
        "failed https://caldav.icloud.com/123/calendars/x with hunter2-secret and abcd-efgh-ijkl-mnop"))
    assert "icloud.com" not in text and "hunter2" not in text and "abcd-efgh" not in text
    assert "<url>" in text and "<redacted>" in text


def test_wrapper_raises_structured_and_keeps_signature():
    import inspect

    def tool(a: int, b: str = "x") -> dict:
        """doc"""
        raise ValueError("bad thing")

    wrapped = errors.structured(tool)
    assert inspect.signature(wrapped) == inspect.signature(tool)
    assert wrapped.__doc__ == "doc"
    with pytest.raises(errors.ToolFailure, match=r"^\[invalid\] retryable=false: bad thing"):
        wrapped(1)


def test_no_double_wrap():
    def tool():
        raise errors.ToolFailure("[auth] retryable=false: x")

    with pytest.raises(errors.ToolFailure, match=r"^\[auth\] retryable=false: x$"):
        errors.structured(tool)()


def test_success_passes_through():
    assert errors.structured(lambda: {"ok": 1})() == {"ok": 1}


def test_async_tool_wrapped():
    async def tool():
        raise ConnectionError("down")

    with pytest.raises(errors.ToolFailure, match=r"^\[network\] retryable=true"):
        asyncio.run(errors.structured(tool)())


def test_registered_tools_are_wrapped_but_direct_calls_are_not():
    registered = server.mcp._tool_manager.get_tool("get_event")
    assert registered.fn.__wrapped__ is server.get_event  # structured wrapper around the original
    with pytest.raises(Exception) as direct:
        server.get_event("no-such-uid", "NoSuchCalendar")
    assert not isinstance(direct.value, errors.ToolFailure)  # tests calling functions see raw errors


def test_every_registered_tool_is_wrapped():
    tools = server.mcp._tool_manager.list_tools()
    assert len(tools) >= 38
    assert {"create_reminder", "read_note", "icloud_storage", "create_event"} <= {t.name for t in tools}
    assert all(hasattr(t.fn, "__wrapped__") for t in tools)


def test_end_to_end_through_fastmcp():
    async def go():
        return await server.mcp.call_tool("get_event", {"uid": "x", "calendar": "NoSuchCalendar"})

    with pytest.raises(Exception) as e:
        asyncio.run(go())
    assert "[not_found] retryable=false:" in str(e.value)
