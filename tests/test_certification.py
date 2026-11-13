import pandas as pd
import pytest

from myquant.research.certification import certified_ranges, membership_coverage, require_certified_range


def _members(day: str) -> list[dict[str, object]]:
    return [{"trade_date": day, "ts_code": f"{i:06d}.SZ", "is_member": True, "source": "official_manifest"} for i in range(300)]


def _weights(day: str) -> list[dict[str, object]]:
    return [{"trade_date": day, "ts_code": f"{i:06d}.SZ", "weight": 1 / 300, "source": "official_license"} for i in range(300)]


def test_certification_requires_complete_membership_and_weights() -> None:
    snapshots = pd.DataFrame(_members("2026-06-15") + _members("2026-06-16"))
    weights = pd.DataFrame(_weights("2026-06-15"))
    coverage = membership_coverage(snapshots, weights)
    assert coverage["certified"].tolist() == [True, False]
    assert coverage["reason"].tolist()[1] == "missing_or_incomplete_official_weights"
    ranges = certified_ranges(coverage)
    assert len(ranges) == 1
    assert ranges[0].start_date == pd.Timestamp("2026-06-15")
    assert ranges[0].end_date == pd.Timestamp("2026-06-15")
    assert ranges[0].trading_days == 1


def test_certification_rejects_partial_request() -> None:
    snapshots = pd.DataFrame(_members("2026-06-15") + _members("2026-06-16"))
    weights = pd.DataFrame(_weights("2026-06-15"))
    coverage = membership_coverage(snapshots, weights)
    result = require_certified_range(coverage, "2026-06-15", "2026-06-15")
    assert result.trading_days == 1
    with pytest.raises(ValueError, match="Certified CSI 300 research range unavailable"):
        require_certified_range(coverage, "2026-06-15", "2026-06-16")


def test_certification_rejects_modelled_free_float_weights() -> None:
    snapshots = pd.DataFrame(_members("2026-06-15"))
    weights = pd.DataFrame(_weights("2026-06-15"))
    weights["source"] = "free_float_market_cap"
    coverage = membership_coverage(snapshots, weights)
    assert not bool(coverage.loc[0, "certified"])
    assert coverage.loc[0, "reason"] == "missing_or_incomplete_official_weights"
