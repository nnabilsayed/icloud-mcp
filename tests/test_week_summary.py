"""week_summary numbers and health_check reporting, with no iCloud."""
from datetime import date, datetime, time

import pytest

import health
import stats as S
import server

D = date(2026, 10, 19)  # a Monday


def ev(h1, m1, h2, m2, uid="a", day=D):
    return (datetime.combine(day, time(h1, m1)), datetime.combine(day, time(h2, m2)), {"uid": uid})


def test_overlaps_count_once():
    rows = S.day_stats([ev(9, 0, 11, 0, "a"), ev(10, 0, 12, 0, "b")], [D], time(9), time(18))
    assert rows[0]["booked_hours"] == 3.0 and rows[0]["events"] == 2


def test_outside_working_hours_counts_only_in_total():
    rows = S.day_stats([ev(7, 0, 10, 0)], [D], time(9), time(18))
    assert (rows[0]["booked_hours"], rows[0]["booked_work_hours"]) == (3.0, 1.0)


def test_event_spanning_midnight_is_split_between_days():
    d2 = date(2026, 10, 20)
    busy = [(datetime.combine(D, time(22)), datetime.combine(d2, time(2)), {"uid": "n"})]
    rows = S.day_stats(busy, [D, d2], time(9), time(18))
    assert [r["booked_hours"] for r in rows] == [2.0, 2.0]


def test_summary_busiest_empty_and_crowded():
    d2 = date(2026, 10, 20)
    rows = S.day_stats([ev(9, 0, 17, 0, "a")], [D, d2], time(9), time(18))
    out = S.summarize(rows, time(9), time(18))
    assert out["busiest_day"] == "2026-10-19" and out["empty_days"] == ["2026-10-20"]
    assert out["crowded_days"] == ["2026-10-19"] and out["total_booked_hours"] == 8.0


def test_all_empty_has_no_busiest_day():
    out = S.summarize(S.day_stats([], [D], time(9), time(18)), time(9), time(18))
    assert out["busiest_day"] is None and out["empty_days"] == ["2026-10-19"]


def test_tool_validates_arguments():
    with pytest.raises(ValueError, match="days"):
        server.week_summary(days=0)
    with pytest.raises(ValueError, match="work_end"):
        server.week_summary(work_start="18:00", work_end="09:00")


def test_probe_reports_ok_and_failure_with_fix():
    assert health.probe("x", lambda: "3 lists")["x"]["detail"] == "3 lists"

    def boom():
        raise RuntimeError("iCloud session expired. Run login")
    r = health.probe("y", boom)["y"]
    assert r["ok"] is False and "login.py" in r["fix"] and "RuntimeError" in r["error"]
