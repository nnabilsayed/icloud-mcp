"""iCloud storage summary, from the raw response shape iCloud returns."""
import pytest

import account_info as A

GIB = 1024 ** 3


def raw(used_gib, total_gib=50, over=False, paid=True):
    return {"storageUsageInfo": {"usedStorageInBytes": int(used_gib * GIB), "totalStorageInBytes": int(total_gib * GIB)},
            "quotaStatus": {"overQuota": over, "paidQuota": paid},
            "storageUsageByMedia": [{"mediaKey": "docs", "displayLabel": "Documents", "usageInBytes": 2 * GIB},
                                    {"mediaKey": "photos", "displayLabel": "Photos", "usageInBytes": 15 * GIB}]}


def test_summary_numbers_and_biggest_category_first():
    out = A.summarize(raw(20))
    assert (out["plan"], out["total_gb"], out["used_gb"], out["free_gb"], out["used_percent"]) == ("iCloud+ 50 GB", 50.0, 20.0, 30.0, 40.0)
    assert [c["name"] for c in out["by_category"]] == ["Photos", "Documents"] and "warning" not in out


def test_nearly_full_warns_at_90_percent_even_if_apple_says_fine():
    assert "full" in A.summarize(raw(45))["warning"]
    assert "warning" not in A.summarize(raw(44.9))


def test_over_quota_warning_wins():
    assert "Over quota" in A.summarize(raw(51, over=True))["warning"]


@pytest.mark.parametrize("gib,name", [(5, "Free (5 GB)"), (200, "iCloud+ 200 GB"), (2048, "iCloud+ 2 TB")])
def test_plan_names(gib, name):
    assert A.plan_name(int(gib * GIB)) == name


def test_unknown_size_is_reported_not_guessed():
    assert "unrecognised" in A.plan_name(77 * GIB)


def test_empty_response_is_an_error():
    with pytest.raises(ValueError):
        A.summarize({})
