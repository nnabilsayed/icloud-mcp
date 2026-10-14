"""Read-only Gmail over IMAP, against a fake IMAP connection."""
import email.message

import pytest

import gmail_server as G


def raw_mail(subject="Interview confirmed", body="See you on the 18th at 1pm", html=None, attach=None):
    m = email.message.EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = "HR <hr@acme.com>", "me@gmail.com", subject, "Mon, 12 Oct 2026 10:00:00 +0000"
    if html:
        m.set_content("fallback")
        m.clear_content()
        m.set_content(html, subtype="html")
    else:
        m.set_content(body)
    if attach:
        m.add_attachment(b"x", maintype="application", subtype="pdf", filename=attach)
    return m.as_bytes()


class FakeIMAP:
    log = []

    def __init__(self, mails):
        self.mails = mails  # uid -> bytes

    def uid(self, cmd, *args):
        FakeIMAP.log.append((cmd, args))
        if cmd == "SEARCH":
            return "OK", [" ".join(self.mails).encode()]
        uids = args[0].split(",")
        out = []
        for u in uids:
            raw = self.mails[u]
            if "HEADER" in args[1]:
                head = raw.split(b"\n\n", 1)[0] + b"\n\n"
                out += [(f"{u} (UID {u} BODY[HEADER.FIELDS (FROM SUBJECT DATE)]".encode(), head), b")"]
            else:
                out += [(f"{u} (UID {u} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", out


@pytest.fixture
def box(monkeypatch):
    FakeIMAP.log = []
    f = FakeIMAP({"1": raw_mail("Old"), "2": raw_mail("Middle"), "3": raw_mail("Newest")})
    monkeypatch.setattr(G, "_with_mailbox", lambda folder, fn: fn(f))
    return f


def test_search_returns_newest_first_and_the_total(box):
    out = G.gmail_search("interview", limit=2)
    assert [m["subject"] for m in out["messages"]] == ["Newest", "Middle"]
    assert out["total_matches"] == 3 and out["messages"][0]["from"] == "HR <hr@acme.com>"


def test_search_passes_gmail_syntax_quoted_and_clamps_limit(box):
    G.gmail_search('from:"a b" x', limit=9999)
    cmd, args = FakeIMAP.log[0]
    assert cmd == "SEARCH" and args[0] == "X-GM-RAW" and args[1] == '"from:\\"a b\\" x"'


def test_search_rejects_non_ascii(box):
    with pytest.raises(ValueError, match="ASCII"):
        G.gmail_search("مقابلة")


def test_read_gives_text_and_never_marks_read(box):
    out = G.gmail_read("3")
    assert out["text"] == "See you on the 18th at 1pm" and out["subject"] == "Newest"
    assert all("BODY.PEEK" in a[1] for c, a in FakeIMAP.log if c == "FETCH")


def test_read_html_only_mail_is_turned_into_text_and_attachments_are_names_only(box):
    box.mails["9"] = raw_mail(html="<html><style>p{x}</style><body><p>Hello</p><p>Meet at <b>1pm</b></p></body></html>",
                              attach="invite.pdf")
    out = G.gmail_read("9")
    assert out["text"] == "Hello\nMeet at 1pm" and out["attachments"] == ["invite.pdf"]


def test_read_truncates_long_mail(box):
    box.mails["5"] = raw_mail(body="a" * 3000)
    out = G.gmail_read("5", max_chars=500)
    assert len(out["text"]) == 500 and out["truncated"]


def test_read_rejects_a_non_numeric_uid(box):
    with pytest.raises(ValueError):
        G.gmail_read("1:*")


def test_mailbox_is_opened_read_only(monkeypatch):
    seen = {}

    class M:
        def __init__(self, host): pass
        def login(self, u, p): pass
        def select(self, folder, readonly=False): seen["ro"] = readonly; return "OK", [b"1"]
        def logout(self): pass

    monkeypatch.setattr(G.imaplib, "IMAP4_SSL", M)
    monkeypatch.setenv("GMAIL_ADDRESS", "a@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    G._connect("INBOX")
    assert seen["ro"] is True


def test_missing_credentials_say_what_to_set(monkeypatch):
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    with pytest.raises(RuntimeError, match="GMAIL_ADDRESS"):
        G._connect("INBOX")
