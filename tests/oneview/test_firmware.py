"""Tests for appliance/repository-level OneView firmware helpers."""

from __future__ import annotations

import pytest

from proliant.oneview.firmware import list_bundles, list_compliance, list_repositories


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]]):
        self._collections = collections

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])


# ── firmware bundles list ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_bundles_sorts_oldest_first_and_labels_repository():
    client = FakeClient({
        "/rest/firmware-drivers": [
            {"uri": "/rest/firmware-drivers/new", "baselineShortName": "SPP 2026.01",
             "version": "SY-2026.01.02", "bundleType": "ServicePack",
             "releaseDate": "2026-01-15T00:00:00Z", "bundleSize": 7_000_000_000,
             "locations": {"/rest/repositories/ext": "hst-fileserver"}},
            {"uri": "/rest/firmware-drivers/old", "baselineShortName": "SPP 2018.12",
             "version": "2018.12.05.00", "bundleType": "ServicePack",
             "releaseDate": "2018-12-05T00:00:00Z", "bundleSize": 5_000_000_000},
        ],
        "/rest/repositories": [
            {"uri": "/rest/repositories/ext", "name": "hst-fileserver",
             "repositoryType": "FirmwareExternalRepo", "totalSpace": 1, "availableSpace": 1},
        ],
    })

    bundles = await list_bundles(client)

    assert [b["name"] for b in bundles] == ["SPP 2018.12", "SPP 2026.01"]
    assert bundles[0]["repository_names"] == "Internal"
    assert bundles[1]["repository_names"] == "hst-fileserver"


# ── firmware repository list ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_repositories_counts_bundles_per_repository():
    client = FakeClient({
        "/rest/repositories": [
            {"uri": "/rest/repositories/internal", "name": "Internal",
             "repositoryType": "FirmwareInternalRepo", "totalSpace": 65_011_712,
             "availableSpace": 63_000_000},
            {"uri": "/rest/repositories/ext", "name": "hst-fileserver",
             "repositoryType": "FirmwareExternalRepo", "totalSpace": 262_144_000,
             "availableSpace": 105_611_656},
        ],
        "/rest/firmware-drivers": [
            {"uri": "/rest/firmware-drivers/a", "baselineShortName": "SPP A"},
            {"uri": "/rest/firmware-drivers/b", "baselineShortName": "SPP B",
             "locations": {"/rest/repositories/ext": "hst-fileserver"}},
            {"uri": "/rest/firmware-drivers/c", "baselineShortName": "SPP C",
             "locations": {"/rest/repositories/ext": "hst-fileserver"}},
        ],
    })

    repos = await list_repositories(client)

    by_name = {r["name"]: r for r in repos}
    assert by_name["Internal"]["bundle_count"] == 1
    assert by_name["hst-fileserver"]["bundle_count"] == 2
    assert by_name["Internal"]["total_gb"] == pytest.approx(62.0, abs=0.01)


# ── firmware compliance list ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_compliance_joins_profile_hardware_and_bundle():
    client = FakeClient({
        "/rest/server-profiles": [
            {
                "name": "aci-FM-host1",
                "serverHardwareUri": "/rest/server-hardware/server1",
                "firmware": {
                    "firmwareBaselineUri": "/rest/firmware-drivers/fw1",
                    "manageFirmware": True,
                    "consistencyState": "Consistent",
                },
            },
            {
                "name": "aci-Mapped-host1",
                "serverHardwareUri": "/rest/server-hardware/server2",
                "firmware": {"manageFirmware": False},
            },
        ],
        "/rest/server-hardware": [
            {"uri": "/rest/server-hardware/server1", "name": "Enclosure-01, bay 1",
             "model": "Synergy 480 Gen10"},
            {"uri": "/rest/server-hardware/server2", "name": "Enclosure-01, bay 2",
             "model": "Synergy 480 Gen10"},
        ],
        "/rest/firmware-drivers": [
            {"uri": "/rest/firmware-drivers/fw1", "baselineShortName": "SPP 2023.05",
             "version": "SY-2023.05.01"},
        ],
    })

    rows = await list_compliance(client)

    assert len(rows) == 2
    managed = next(r for r in rows if r["logical_resource"] == "aci-FM-host1")
    assert managed["model"] == "Synergy 480 Gen10"
    assert managed["bundle_name"] == "SPP 2023.05"
    assert managed["bundle_version"] == "SY-2023.05.01"
    assert managed["consistency_state"] == "Consistent"

    unmanaged = next(r for r in rows if r["logical_resource"] == "aci-Mapped-host1")
    assert unmanaged["bundle_name"] == ""
    assert unmanaged["consistency_state"] == "Not managed"
