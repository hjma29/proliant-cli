"""Tests for OneView upgrade readiness + firmware baseline cleanup helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from proliant.oneview.upgrade import (
    _is_external_baseline,
    assess_readiness,
    classify_baselines,
    compute_upgrade_path,
    normalize_baselines,
    normalize_repositories,
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


def test_classify_baselines_sorts_prunable_and_external_by_release_date():
    raw = [
        {"uri": "/rest/firmware-drivers/used", "baselineShortName": "SPP assigned",
         "version": "v9", "bundleType": "ServicePack",
         "releaseDate": "2026-01-01T00:00:00Z", "bundleSize": 1, "state": "Created"},
        # Unused internal baselines, added out of chronological order.
        {"uri": "/rest/firmware-drivers/mid", "baselineShortName": "SPP mid",
         "version": "v2", "bundleType": "ServicePack",
         "releaseDate": "2020-06-01T00:00:00Z", "bundleSize": 1, "state": "Created"},
        {"uri": "/rest/firmware-drivers/oldest", "baselineShortName": "SPP oldest",
         "version": "v1", "bundleType": "ServicePack",
         "releaseDate": "2018-01-01T00:00:00Z", "bundleSize": 1, "state": "Created"},
        {"uri": "/rest/firmware-drivers/mid2", "baselineShortName": "SPP mid2",
         "version": "v3", "bundleType": "ServicePack",
         "releaseDate": "2022-01-01T00:00:00Z", "bundleSize": 1, "state": "Created"},
        # Unused external baselines, also out of order.
        {"uri": "/rest/firmware-drivers/ext-new", "baselineShortName": "SPP ext new",
         "version": "e2", "bundleType": "ServicePack",
         "releaseDate": "2021-01-01T00:00:00Z", "bundleSize": 1, "state": "Created",
         "locations": {"/rest/repositories/ext": "hst-fileserver"}},
        {"uri": "/rest/firmware-drivers/ext-old", "baselineShortName": "SPP ext old",
         "version": "e1", "bundleType": "ServicePack",
         "releaseDate": "2019-01-01T00:00:00Z", "bundleSize": 1, "state": "Created",
         "locations": {"/rest/repositories/ext": "hst-fileserver"}},
        # Unused baseline with no release date -> must sort last.
        {"uri": "/rest/firmware-drivers/nodate", "baselineShortName": "SPP nodate",
         "version": "v0", "bundleType": "ServicePack",
         "releaseDate": "", "bundleSize": 1, "state": "Created"},
    ]
    baselines = normalize_baselines(raw)
    logical_enclosures = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/used"}}]
    repo_types = {"/rest/repositories/ext": "FirmwareExternalRepo"}
    summary = classify_baselines(
        baselines, logical_enclosures, [], [], raw_members=raw, repo_types=repo_types,
    )

    assert [b["name"] for b in summary["prunable"]] == [
        "SPP oldest", "SPP mid", "SPP mid2", "SPP nodate",
    ]
    assert [b["name"] for b in summary["external_unused"]] == ["SPP ext old", "SPP ext new"]


# ── external-repository baseline detection ───────────────────────────────────

def test_is_external_baseline_no_locations_is_internal():
    assert _is_external_baseline({}, None) is False
    assert _is_external_baseline({}, {"/rest/repositories/x": "FirmwareExternalRepo"}) is False


def test_is_external_baseline_unknown_repo_types_assumes_external():
    # repo_types couldn't be fetched at all -> conservative: any locations = external.
    assert _is_external_baseline({"/rest/repositories/x": "hst-fileserver"}, None) is True


def test_is_external_baseline_uses_repo_type_when_known():
    repo_types = {
        "/rest/repositories/ext": "FirmwareExternalRepo",
        "/rest/repositories/internal": "FirmwareInternalRepo",
    }
    assert _is_external_baseline({"/rest/repositories/ext": "hst-fileserver"}, repo_types) is True
    assert _is_external_baseline({"/rest/repositories/internal": "Internal"}, repo_types) is False


def test_is_external_baseline_unknown_uri_in_known_map_assumes_external():
    # repo_types fetch succeeded but doesn't contain this specific URI -> still conservative.
    repo_types = {"/rest/repositories/internal": "FirmwareInternalRepo"}
    assert _is_external_baseline({"/rest/repositories/other": "mystery-repo"}, repo_types) is True


def test_classify_baselines_excludes_external_repo_from_prunable_and_reclaimable():
    raw = _baselines_raw()
    # 'old' is hosted only in an external repo -> must never be prunable.
    raw[1]["locations"] = {"/rest/repositories/ext": "hst-fileserver"}
    baselines = normalize_baselines(raw)
    logical_enclosures = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/used"}}]
    repo_types = {"/rest/repositories/ext": "FirmwareExternalRepo"}
    summary = classify_baselines(
        baselines, logical_enclosures, [], [], raw_members=raw, repo_types=repo_types,
    )

    assert [b["name"] for b in summary["prunable"]] == []
    assert [b["name"] for b in summary["external_unused"]] == ["SPP 2018.12"]
    assert summary["reclaimable_bytes"] == 0
    assert summary["reclaimable_gb"] == 0


def test_classify_baselines_internal_locations_stay_prunable():
    raw = _baselines_raw()
    raw[1]["locations"] = {"/rest/repositories/internal": "Internal"}
    baselines = normalize_baselines(raw)
    logical_enclosures = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/used"}}]
    repo_types = {"/rest/repositories/internal": "FirmwareInternalRepo"}
    summary = classify_baselines(
        baselines, logical_enclosures, [], [], raw_members=raw, repo_types=repo_types,
    )

    assert [b["name"] for b in summary["prunable"]] == ["SPP 2018.12"]
    assert summary["external_unused"] == []
    assert summary["reclaimable_bytes"] == 6_000_000_000


def test_classify_baselines_no_repo_types_treats_locations_as_external():
    # /rest/repositories fetch failed entirely (repo_types=None) -> conservative fallback.
    raw = _baselines_raw()
    raw[1]["locations"] = {"/rest/repositories/ext": "hst-fileserver"}
    baselines = normalize_baselines(raw)
    logical_enclosures = [{"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/used"}}]
    summary = classify_baselines(baselines, logical_enclosures, [], [], raw_members=raw)

    assert summary["prunable"] == []
    assert [b["name"] for b in summary["external_unused"]] == ["SPP 2018.12"]


# ── repository normalization (KiB → GB) ──────────────────────────────────────

def test_normalize_repositories_converts_kib_to_gb():
    # Verified live against the GUI: 65,011,712 KiB == exactly 62.00 GB.
    raw = [
        {
            "uri": "/rest/repositories/internal",
            "name": "Internal",
            "repositoryType": "FirmwareInternalRepo",
            "totalSpace": 65_011_712,
            "availableSpace": 63_500_000,
            "state": "Normal",
        },
        {
            "uri": "/rest/repositories/ext",
            "name": "hst-fileserver",
            "repositoryType": "FirmwareExternalRepo",
            "totalSpace": 262_144_000,
            "availableSpace": 105_611_656,
            "repositoryUrl": "https://hst-fileserver/",
        },
    ]
    repos = normalize_repositories(raw)

    assert repos[0]["total_gb"] == pytest.approx(62.0, abs=0.01)
    assert repos[1]["total_gb"] == pytest.approx(250.0, abs=0.01)
    assert repos[1]["available_gb"] == pytest.approx(100.72, abs=0.01)
    assert repos[1]["url"] == "https://hst-fileserver/"


def test_normalize_repositories_handles_missing_space_fields():
    repos = normalize_repositories([{"uri": "/rest/repositories/x", "name": "X",
                                      "repositoryType": "FirmwareInternalRepo"}])
    assert repos[0]["total_gb"] == 0.0
    assert repos[0]["available_gb"] == 0.0


def test_normalize_repositories_empty_input():
    assert normalize_repositories([]) == []
    assert normalize_repositories(None) == []


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


def test_readiness_reports_external_unused_baselines_informationally():
    data = _base_data(baseline_summary={
        "prunable": [],
        "reclaimable_gb": 0,
        "external_unused": [
            {"name": "SPP 2018.12", "locations": {"/rest/repositories/ext": "hst-fileserver"}},
        ],
    })
    r = assess_readiness(data, now=_NOW)
    check = next(c for c in r["checks"] if c["name"] == "External-repository baselines")
    assert check["status"] == "INFO"
    assert "hst-fileserver" in check["detail"]
    # Purely informational -- must never affect the overall verdict.
    assert r["verdict"] == "PASS"


def test_readiness_no_check_when_no_external_unused_baselines():
    r = assess_readiness(_base_data(), now=_NOW)
    assert not any(c["name"] == "External-repository baselines" for c in r["checks"])
