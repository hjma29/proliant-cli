"""Tests for OneView upgrade readiness + firmware baseline cleanup helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proliant.oneview.upgrade import (
    assess_readiness,
    classify_baselines,
    compute_upgrade_path,
    normalize_baselines,
    parse_health,
    parse_size_to_gb,
    parse_version,
    summarize_alerts,
)


# ── version parsing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("9.20.00-0500184", (9, 20, 0)),
    ("9.20.00", (9, 20, 0)),
    ("9.20", (9, 20, 0)),
    ("11.01", (11, 1, 0)),
    ("10.0.0", (10, 0, 0)),
    ("", (0, 0, 0)),
    ("garbage", (0, 0, 0)),
])
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


# ── upgrade path ─────────────────────────────────────────────────────────────

def test_upgrade_path_from_9_20():
    up = compute_upgrade_path("9.20.00-0500184")
    assert up["recommended_next"] == "10.0"
    assert up["path_to_latest"] == ["10.0", "11.01", "11.3"]
    assert up["latest"] == "11.3"
    assert up["hops_to_latest"] == 3
    assert up["at_latest"] is False


def test_upgrade_path_old_version_includes_early_milestones():
    up = compute_upgrade_path("5.60.01")
    # 5.60.01 -> first milestone must be 6.0
    assert up["recommended_next"] == "6.0"
    assert up["path_to_latest"][0] == "6.0"
    assert "8.30.01" in up["path_to_latest"]


def test_upgrade_path_at_latest():
    up = compute_upgrade_path("11.30.00")
    assert up["at_latest"] is True
    assert up["recommended_next"] is None
    assert up["path_to_latest"] == []


def test_upgrade_path_between_milestones():
    # 8.40 is past 8.30.01 but before 8.60.02
    up = compute_upgrade_path("8.40.00")
    assert up["recommended_next"] == "8.60.02"


# ── size parsing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("18.27 GB", 18.27),
    ("4095 MB", pytest.approx(4.0, abs=0.01)),
    ("894 GB", 894.0),
    ("1 TB", 1024.0),
    ("", None),
])
def test_parse_size_to_gb(raw, expected):
    assert parse_size_to_gb(raw) == expected


def test_parse_health():
    members = [
        {"resourceType": "DISK", "severity": "INFO", "available": "18.27 GB",
         "capacity": "894 GB", "statusMessage": "SUFFICIENT_DISK_SPACE"},
        {"resourceType": "MEMORY", "severity": "INFO", "available": "4095 MB",
         "capacity": "64126 MB", "statusMessage": "SUFFICIENT_MEMORY"},
    ]
    health = parse_health(members)
    assert health["DISK"]["available_gb"] == 18.27
    assert health["MEMORY"]["message"] == "SUFFICIENT_MEMORY"


# ── alerts ───────────────────────────────────────────────────────────────────

def test_summarize_alerts():
    members = (
        [{"severity": "Critical"}] * 6
        + [{"severity": "Warning"}] * 9
        + [{"severity": "OK"}] * 2
    )
    s = summarize_alerts(members)
    assert s == {"critical": 6, "warning": 9, "other": 2, "total": 17}


# ── baseline classification ──────────────────────────────────────────────────

def _baselines_raw():
    return [
        {"uri": "/rest/firmware-drivers/used", "baselineShortName": "SPP 2023.05",
         "version": "SY-2023.05.01", "bundleType": "ServicePack",
         "releaseDate": "2023-04-18T00:00:00Z", "bundleSize": 8_000_000_000, "state": "Created"},
        {"uri": "/rest/firmware-drivers/old", "baselineShortName": "SPP 2018.12",
         "version": "2018.12.05.00", "bundleType": "ServicePack",
         "releaseDate": "2018-12-05T00:00:00Z", "bundleSize": 6_000_000_000, "state": "Created"},
    ]


def test_classify_baselines_marks_in_use_and_prunable():
    raw = _baselines_raw()
    baselines = normalize_baselines(raw)
    logical_enclosures = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/used"}}]
    summary = classify_baselines(baselines, logical_enclosures, [], [], raw_members=raw)

    assert [b["name"] for b in summary["in_use"]] == ["SPP 2023.05"]
    # 2018.12 is older than the assigned 2023.05 -> prunable
    assert [b["name"] for b in summary["prunable"]] == ["SPP 2018.12"]
    assert summary["reclaimable_bytes"] == 6_000_000_000
    assert summary["reclaimable_gb"] == pytest.approx(5.59, abs=0.01)


def test_classify_baselines_retains_newer_unused_as_upgrade_target():
    # Assigned baseline is 2023.05; a newer unused 2026 SPP must be RETAINED, not pruned.
    raw = _baselines_raw()
    raw.append({"uri": "/rest/firmware-drivers/new", "baselineShortName": "SPP 2026.01",
                "version": "SY-2026.01.02", "bundleType": "ServicePack",
                "releaseDate": "2026-03-05T00:00:00Z", "bundleSize": 6_500_000_000, "state": "Created"})
    baselines = normalize_baselines(raw)
    logical_enclosures = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/used"}}]
    summary = classify_baselines(baselines, logical_enclosures, [], [], raw_members=raw)

    prunable_names = {b["name"] for b in summary["prunable"]}
    retained_names = {b["name"] for b in summary["retained_newer"]}
    assert "SPP 2018.12" in prunable_names       # older -> prune
    assert "SPP 2026.01" in retained_names       # newer -> keep as upgrade target
    assert "SPP 2026.01" not in prunable_names


def test_classify_baselines_respects_server_profile_and_li_and_parent():
    raw = _baselines_raw()
    # add a child baseline whose parent is 'old' -> old must be protected
    raw.append({"uri": "/rest/firmware-drivers/child", "baselineShortName": "Custom",
                "version": "custom", "bundleType": "CustomServicePack",
                "releaseDate": "", "bundleSize": 100, "state": "Created",
                "parentBundle": {"uri": "/rest/firmware-drivers/old"}})
    baselines = normalize_baselines(raw)
    # 'used' referenced by a server profile; 'child' referenced by an LI
    server_profiles = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/used"}}]
    logical_interconnects = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/child"}}]
    summary = classify_baselines(baselines, [], logical_interconnects, server_profiles, raw_members=raw)
    prunable_uris = {b["uri"] for b in summary["prunable"]}
    # nothing prunable: used(profile), child(LI), old(parent of child)
    assert prunable_uris == set()


# ── readiness assessment verdict ─────────────────────────────────────────────

_NOW = datetime(2026, 7, 7, tzinfo=timezone.utc)


def _base_data(**over):
    data = {
        "version": {"software_version": "9.20.00", "model": "Synergy Composer2"},
        "upgrade_path": compute_upgrade_path("9.20.00"),
        "health": {"DISK": {"available_gb": 200.0, "available": "200 GB", "capacity": "894 GB", "severity": "INFO"}},
        "alerts": {"critical": 0, "warning": 0, "other": 0, "total": 0},
        "backup": {"state": "Succeeded", "created": "2026-07-06T00:00:00Z"},
        "logical_interconnects": [{"name": "LI1", "status": "OK", "consistency": "CONSISTENT"}],
        "interconnect_count": 2,
        "baseline_summary": {"prunable": [], "reclaimable_gb": 0},
    }
    data.update(over)
    return data


def test_readiness_all_pass():
    r = assess_readiness(_base_data(), now=_NOW)
    assert r["verdict"] == "PASS"


def test_readiness_fails_on_critical_alerts():
    r = assess_readiness(_base_data(alerts={"critical": 6, "warning": 9, "other": 0, "total": 15}), now=_NOW)
    assert r["verdict"] == "FAIL"
    alert_check = next(c for c in r["checks"] if c["name"] == "Active alerts")
    assert alert_check["status"] == "FAIL"


def test_readiness_fails_on_low_disk():
    data = _base_data(health={"DISK": {"available_gb": 5.0, "available": "5 GB", "capacity": "894 GB", "severity": "INFO"}})
    r = assess_readiness(data, now=_NOW)
    assert r["verdict"] == "FAIL"


def test_readiness_warns_on_tight_disk_and_inconsistent_li():
    data = _base_data(
        health={"DISK": {"available_gb": 18.27, "available": "18.27 GB", "capacity": "894 GB", "severity": "INFO"}},
        logical_interconnects=[{"name": "LI1", "status": "Warning", "consistency": "NOT_CONSISTENT"}],
    )
    r = assess_readiness(data, now=_NOW)
    assert r["verdict"] == "WARN"
    disk = next(c for c in r["checks"] if c["name"] == "Appliance disk space")
    assert disk["status"] == "WARN"
    cons = next(c for c in r["checks"] if c["name"] == "Logical interconnect consistency")
    assert cons["status"] == "WARN"


def test_readiness_warns_on_stale_backup():
    data = _base_data(backup={"state": "Succeeded", "created": "2026-06-01T00:00:00Z"})
    r = assess_readiness(data, now=_NOW)
    backup = next(c for c in r["checks"] if c["name"] == "Appliance backup")
    assert backup["status"] == "WARN"


def test_readiness_single_interconnect_warns():
    r = assess_readiness(_base_data(interconnect_count=1), now=_NOW)
    ic = next(c for c in r["checks"] if c["name"] == "Interconnect redundancy")
    assert ic["status"] == "WARN"
