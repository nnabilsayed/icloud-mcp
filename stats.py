"""Pure helpers for week_summary: how booked each day is, from busy intervals."""
from datetime import datetime, timedelta


def _merge(spans):
    """Union of overlapping (start, end) spans, so double-booked time counts once."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def day_stats(busy, days, work_start, work_end):
    """Per-day figures. busy: (start, end, info) with naive local datetimes; days: dates.
    booked_hours counts all busy time that day (merged); booked_work_hours only inside working hours."""
    rows = []
    for day in days:
        lo, hi = datetime.combine(day, datetime.min.time()), datetime.combine(day, datetime.min.time()) + timedelta(days=1)
        wlo, whi = datetime.combine(day, work_start), datetime.combine(day, work_end)
        spans = _merge([(max(a, lo), min(b, hi)) for a, b, _ in busy if a < hi and b > lo])
        total = sum((b - a).total_seconds() for a, b in spans) / 3600
        work = sum(max(0.0, (min(b, whi) - max(a, wlo)).total_seconds()) for a, b in spans) / 3600
        count = len({i["uid"] for a, b, i in busy if a < hi and b > lo})
        rows.append({"date": day.isoformat(), "weekday": day.strftime("%a"), "events": count,
                     "booked_hours": round(total, 2), "booked_work_hours": round(work, 2)})
    return rows


def summarize(rows, work_start, work_end):
    window = (datetime.combine(datetime.min, work_end) - datetime.combine(datetime.min, work_start)).total_seconds() / 3600
    busiest = max(rows, key=lambda r: (r["booked_hours"], r["events"]), default=None)
    return {
        "days": rows,
        "total_booked_hours": round(sum(r["booked_hours"] for r in rows), 2),
        "busiest_day": busiest["date"] if busiest and busiest["events"] else None,
        "empty_days": [r["date"] for r in rows if r["events"] == 0],
        "crowded_days": [r["date"] for r in rows if window and r["booked_work_hours"] >= 0.75 * window],
    }
