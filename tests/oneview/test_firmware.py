"""Tests for appliance/repository-level OneView firmware helpers."""

from __future__ import annotations

import pytest

from proliant.oneview.firmware import list_bundles, list_compliance, list_repositories


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]], post_responses: dict[tuple[str, str], dict] | None = None):
        self._collections = collections
        self._post_responses = post_responses or {}
        self.post_calls: list[tuple[str, dict]] = []

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])

    async def post(self, uri: str, body: dict) -> dict:
        self.post_calls.append((uri, body))
        key = (body.get("firmwareBaselineId"), body.get("serverUUID"))
        return self._post_responses.get(key, {"componentMappingList": [], "serverFirmwareUpdateRequired": False})


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
async def test_list_compliance_checks_managed_profiles_against_newer_unused_bundles():
    client = FakeClient(
        collections={
            "/rest/server-profiles": [
                {
                    "name": "aci-FM-host1",
                    "serverHardwareUri": "/rest/server-hardware/server1",
                    "firmware": {
                        "firmwareBaselineUri": "/rest/firmware-drivers/fw1",
                        "manageFirmware": True,
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
                 "model": "Synergy 480 Gen10", "uuid": "uuid-1111"},
                {"uri": "/rest/server-hardware/server2", "name": "Enclosure-01, bay 2",
                 "model": "Synergy 480 Gen10", "uuid": "uuid-2222"},
            ],
            "/rest/firmware-drivers": [
                {"uri": "/rest/firmware-drivers/fw1", "baselineShortName": "SPP 2023.05",
                 "version": "SY-2023.05.01", "releaseDate": "2023-05-01T00:00:00Z"},
                {"uri": "/rest/firmware-drivers/fw2", "baselineShortName": "SPP 2026.01",
                 "version": "SY-2026.01.02", "releaseDate": "2026-01-15T00:00:00Z"},
            ],
            "/rest/logical-enclosures": [],
            "/rest/logical-interconnects": [],
        },
        post_responses={
            ("fw2", "uuid-1111"): {
                "serverFirmwareUpdateRequired": True,
                "componentMappingList": [
                    {"componentKey": "1", "componentFirmwareUpdateRequired": True},
                    {"componentKey": "2", "componentFirmwareUpdateRequired": False},
                ],
            },
        },
    )

    rows = await list_compliance(client)

    # Only the managed profile is checked, against the one baseline that's
    # newer than what's assigned and otherwise unused (fw1 is in-use/assigned
    # so it's excluded as a candidate, matching "retained_newer" semantics).
    assert len(rows) == 1
    row = rows[0]
    assert row["hardware"] == "Enclosure-01, bay 1"
    assert row["model"] == "Synergy 480 Gen10"
    assert row["logical_resource"] == "aci-FM-host1"
    assert row["bundle_name"] == "SPP 2026.01"
    assert row["bundle_version"] == "SY-2026.01.02"
    assert row["update_required"] is True
    assert row["components_needing_update"] == 1
    assert row["components_total"] == 2

    # The real endpoint takes the bundle's short id and the server-hardware's
    # uuid — never the full "/rest/..." URIs (schema confirmed via HPE's own
    # oneview-python SDK).
    assert client.post_calls == [
        ("/rest/server-hardware/firmware-compliance",
         {"firmwareBaselineId": "fw2", "serverUUID": "uuid-1111"}),
    ]


@pytest.mark.asyncio
async def test_list_compliance_returns_empty_when_no_newer_unused_bundles():
    client = FakeClient(collections={
        "/rest/server-profiles": [
            {
                "name": "aci-FM-host1",
                "serverHardwareUri": "/rest/server-hardware/server1",
                "firmware": {
                    "firmwareBaselineUri": "/rest/firmware-drivers/fw1",
                    "manageFirmware": True,
                },
            },
        ],
        "/rest/server-hardware": [
            {"uri": "/rest/server-hardware/server1", "name": "Enclosure-01, bay 1",
             "model": "Synergy 480 Gen10", "uuid": "uuid-1111"},
        ],
        "/rest/firmware-drivers": [
            {"uri": "/rest/firmware-drivers/fw1", "baselineShortName": "SPP 2023.05",
             "version": "SY-2023.05.01", "releaseDate": "2023-05-01T00:00:00Z"},
        ],
        "/rest/logical-enclosures": [],
        "/rest/logical-interconnects": [],
    })

    rows = await list_compliance(client)

    assert rows == []
    assert client.post_calls == []


@pytest.mark.asyncio
async def test_list_compliance_includes_newer_external_repo_bundles_as_candidates():
    """Regression test: live appliance data showed every "newer unused"
    baseline hosted only in an external repo (e.g. hst-fileserver) — which
    `classify_baselines()` deliberately excludes from `retained_newer` since
    those aren't deletable/reclaimable. The GUI's compliance page still
    checks servers against them, so `list_compliance()` must too.
    """
    client = FakeClient(
        collections={
            "/rest/server-profiles": [
                {
                    "name": "aci-FM-host1",
                    "serverHardwareUri": "/rest/server-hardware/server1",
                    "firmware": {
                        "firmwareBaselineUri": "/rest/firmware-drivers/fw1",
                        "manageFirmware": True,
                    },
                },
            ],
            "/rest/server-hardware": [
                {"uri": "/rest/server-hardware/server1", "name": "Enclosure-01, bay 1",
                 "model": "Synergy 480 Gen10", "uuid": "uuid-1111"},
            ],
            "/rest/firmware-drivers": [
                {"uri": "/rest/firmware-drivers/fw1", "baselineShortName": "SPP 2023.05",
                 "version": "SY-2023.05.01", "releaseDate": "2023-05-01T00:00:00Z"},
                {"uri": "/rest/firmware-drivers/fw2", "baselineShortName": "SPP 2026.01",
                 "version": "SY-2026.01.02", "releaseDate": "2026-01-15T00:00:00Z",
                 "locations": {"/rest/repositories/ext": "hst-fileserver"}},
            ],
            "/rest/repositories": [
                {"uri": "/rest/repositories/ext", "name": "hst-fileserver",
                 "repositoryType": "FirmwareExternalRepo", "totalSpace": 1, "availableSpace": 1},
            ],
            "/rest/logical-enclosures": [],
            "/rest/logical-interconnects": [],
        },
        post_responses={
            ("fw2", "uuid-1111"): {
                "serverFirmwareUpdateRequired": False,
                "componentMappingList": [{"componentKey": "1", "componentFirmwareUpdateRequired": False}],
            },
        },
    )

    rows = await list_compliance(client)

    assert len(rows) == 1
    assert rows[0]["bundle_name"] == "SPP 2026.01"
    assert client.post_calls == [
        ("/rest/server-hardware/firmware-compliance",
         {"firmwareBaselineId": "fw2", "serverUUID": "uuid-1111"}),
    ]
