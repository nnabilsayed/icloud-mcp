"""Read-only iCloud account facts: storage used and the plan size."""
import reminders


_PLANS = [(5, "Free (5 GB)"), (50, "iCloud+ 50 GB"), (200, "iCloud+ 200 GB"), (2000, "iCloud+ 2 TB"),
          (6000, "iCloud+ 6 TB"), (12000, "iCloud+ 12 TB")]
WARN_PERCENT = 90.0


def _gib(n: int) -> float:
    return round(n / 1024 ** 3, 2)


def plan_name(total_bytes: int) -> str:
    """Apple reports no plan name, only the size; match it to the nearest sold plan."""
    gib = total_bytes / 1024 ** 3
    for size, name in _PLANS:
        if abs(gib - size) / size < 0.1:
            return name
    return f"{round(gib)} GB (unrecognised plan size)"


def summarize(raw: dict) -> dict:
    info = raw.get("storageUsageInfo") or {}
    quota = raw.get("quotaStatus") or {}
    total, used = int(info.get("totalStorageInBytes", 0)), int(info.get("usedStorageInBytes", 0))
    if total <= 0:
        raise ValueError("iCloud returned no storage figures")
    pct = round(used * 100 / total, 1)
    by_media = sorted(({"name": m.get("displayLabel") or m.get("mediaKey"), "gb": _gib(int(m.get("usageInBytes", 0)))}
                       for m in raw.get("storageUsageByMedia") or []), key=lambda x: -x["gb"])
    out = {"plan": plan_name(total), "paid": bool(quota.get("paidQuota")), "total_gb": _gib(total),
           "used_gb": _gib(used), "free_gb": _gib(total - used), "used_percent": pct,
           "over_quota": bool(quota.get("overQuota")), "by_category": by_media}
    if out["over_quota"]:
        out["warning"] = "Over quota: iCloud backup and sync can stop."
    elif pct >= WARN_PERCENT:
        out["warning"] = f"{pct}% full. Free up space or upgrade the plan before it fills."
    return out


def register(mcp):
    @mcp.tool()
    @reminders._reset_on_error
    def icloud_storage() -> dict:
        """iCloud storage and plan summary: plan size, used and free space, percent used, a warning
        when nearly full, and usage by category (photos, backups, ...). Sizes are in GiB, matching
        what Settings shows. The plan name is inferred from the size because Apple does not report it."""
        reminders._service()
        acc = reminders._api.account
        raw = acc.session.post(acc._acc_storage_url, params=acc.params).json()
        return summarize(raw)
