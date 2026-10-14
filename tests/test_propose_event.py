"""propose_event: the dry-run check for an event an agent read out of an email. Writes nothing."""
from datetime import datetime

import pytest

import server as S

NOW = "2026-10-14T09:00"


def span(a, b, summary, uid="u", all_day=False):
    return (datetime.fromisoformat(a), datetime.fromisoformat(b),
            {"calendar": "Work", "summary": summary, "uid": uid, "all_day": all_day})


@pytest.fixture
def world(monkeypatch):
    state = {"events": []}
    monkeypatch.setattr(S, "_event_calendar_list", lambda: [])
    monkeypatch.setattr(S, "_calendar", lambda name: object())
    monkeypatch.setattr(S, "_busy_intervals", lambda cals, a, b, ad: [x for x in state["events"] if x[0] < b and x[1] > a])
    return state


def propose(**kw):
    kw.setdefault("now", NOW)
    return S.propose_event(**kw)


def test_clean_proposal_gives_ready_to_use_create_args(world):
    out = propose(summary="Interview at Acme", start="2026-10-18T13:00", calendar="Work", source="Re: interview, hr@acme.com")
    assert out["ok_to_create"] and out["conflicts"] == [] and out["possible_duplicates"] == []
    assert "No end time given; assuming one hour." in out["warnings"]
    a = out["create_event_args"]
    assert (a["calendar"], a["summary"], a["start"]) == ("Work", "Interview at Acme", "2026-10-18T13:00")
    assert a["description"] == "Source: Re: interview, hr@acme.com"
    assert "end" in a


def test_past_dates_and_far_future_dates_are_flagged(world):
    assert any("past" in w for w in propose(summary="x", start="2026-10-10T13:00", end="2026-10-10T14:00")["warnings"])
    assert any("year" in w for w in propose(summary="x", start="2028-01-01T13:00", end="2028-01-01T14:00")["warnings"])


def test_overlap_is_reported_as_a_conflict(world):
    world["events"].append(span("2026-10-18T12:30", "2026-10-18T13:30", "Dentist"))
    out = propose(summary="Interview", start="2026-10-18T13:00", end="2026-10-18T14:00")
    assert [c["summary"] for c in out["conflicts"]] == ["Dentist"] and out["ok_to_create"]


def test_same_day_similar_title_is_a_duplicate_even_at_another_time(world):
    world["events"].append(span("2026-10-18T09:00", "2026-10-18T10:00", "Interview - Acme Corp"))
    out = propose(summary="Acme interview", start="2026-10-18T13:00", end="2026-10-18T14:00")
    assert not out["ok_to_create"] and len(out["possible_duplicates"]) == 1
    assert out["conflicts"] == []


def test_unrelated_titles_are_not_duplicates():
    assert not S._similar("Dentist", "Team standup")
    assert S._similar("Interview with Acme", "acme interview")


def test_all_day_proposal(world):
    out = propose(summary="Conference", start="2026-10-20", end="2026-10-22")
    assert out["create_event_args"]["end"] == "2026-10-22" and "No end time given" not in " ".join(out["warnings"])


def test_bad_input_is_rejected(world):
    with pytest.raises(ValueError):
        propose(summary="  ", start="2026-10-18T13:00")
    with pytest.raises(ValueError):
        propose(summary="x", start="2026-10-18T14:00", end="2026-10-18T13:00")
    with pytest.raises(ValueError):
        propose(summary="x", start="2026-10-18T14:00", end="2026-10-18")


def test_it_never_creates_anything(world, monkeypatch):
    monkeypatch.setattr(S, "create_event", lambda *a, **k: pytest.fail("created"))
    propose(summary="x", start="2026-10-18T13:00")
