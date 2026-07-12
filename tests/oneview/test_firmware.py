"""Tests for appliance/repository-level OneView firmware helpers."""

from __future__ import annotations

import pytest

from proliant.oneview.firmware import list_bundles, list_compliance, list_repositories


class FakeClient:
    def __init__(
        self,
        collections: dict[str, list[dict]],
        post_responses: dict[tuple[str, str], dict] | None = None,
        objects: dict[str, dict] | None = None,
    ):
        self._collections = collections
        self._post_responses = post_responses or {}
        self._objects = objects or {}
        self.post_calls: list[tuple[str, dict]] = []

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])

    async def get(self, uri: str, params: dict | None = None) -> dict:
        result = self._objects.get(uri, {})
        if isinstance(result, Exception):
            raise result
        return result

    async def post(self, uri: str, body: dict) -> dict:
        self.post_calls.append((uri, body))
        key = (body.get("firmwareBaselineId"), body.get("serverUUID"))
        result = self._post_responses.get(key, {"componentMappingList": [], "serverFirmwareUpdateRequired": False})
        if isinstance(result, Exception):
            raise result
        return result


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

def _compliance_collections() -> dict[str, list[dict]]:
    return {
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
            {
                "uri": "/rest/server-hardware/server1",
                "name": "Enclosure-01, bay 1",
                "model": "Synergy 480 Gen10",
                "uuid": "uuid-1111",
                "locationUri": "/rest/enclosures/enc1",
            },
        ],
        "/rest/interconnects": [
            {
                "uri": "/rest/interconnects/ic3",
                "name": "Enclosure-01, interconnect 3",
                "model": "HPE Virtual Connect SE 100Gb F32 Module for Synergy",
                "productName": "HPE Virtual Connect SE 100Gb F32 Module for Synergy",
                "firmwareVersion": "2.6.0.1001",
                "logicalInterconnectUri": "/rest/logical-interconnects/li1",
                "interconnectLocation": {
                    "locationEntries": [
                        {"type": "Enclosure", "value": "/rest/enclosures/enc1"},
                        {"type": "Bay", "value": "3"},
                    ],
                },
            },
        ],
        "/rest/enclosures": [
            {
                "uri": "/rest/enclosures/enc1",
                "name": "Enclosure-01",
                "managerBays": [
                    {
                        "bayNumber": 1,
                        "model": "HPE Synergy Frame Link Module",
                        "fwVersion": "1.0.0",
                    },
                ],
            },
        ],
        "/rest/logical-interconnects": [
            {
                "uri": "/rest/logical-interconnects/li1",
                "name": "LE01-LI",
                "enclosureUris": ["/rest/enclosures/enc1"],
            },
        ],
        "/rest/logical-enclosures": [
            {
                "uri": "/rest/logical-enclosures/le1",
                "name": "LE01",
                "enclosureUris": ["/rest/enclosures/enc1"],
                "logicalInterconnectUris": ["/rest/logical-interconnects/li1"],
                "firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/fw1"},
            },
        ],
        "/rest/firmware-drivers": [
            {
                "uri": "/rest/firmware-drivers/fw1",
                "baselineShortName": "SPP 2023.05",
                "version": "SY-2023.05.01",
                "bundleType": "ServicePack",
                "releaseDate": "2023-05-01T00:00:00Z",
                "fwComponents": [
                    {
                        "name": "HPE Synergy Frame Link Module Firmware",
                        "componentVersion": "1.0.0",
                    },
                    {
                        "name": "HPE Virtual Connect SE 100Gb F32 Module for Synergy Firmware install package",
                        "componentVersion": "2.6.0.1001",
                    },
                ],
            },
            {
                "uri": "/rest/firmware-drivers/fw2",
                "baselineShortName": "SPP 2026.01",
                "version": "SY-2026.01.02",
                "bundleType": "ServicePack",
                "releaseDate": "2026-01-15T00:00:00Z",
                "locations": {"/rest/repositories/ext": "hst-fileserver"},
                "fwComponents": [
                    {
                        "name": "HPE Synergy Frame Link Module Firmware",
                        "componentVersion": "1.2.3",
                    },
                    {
                        "name": "HPE Virtual Connect SE 100Gb F32 Module for Synergy Firmware install package",
                        "componentVersion": "2.9.1.1001",
                    },
                ],
            },
        ],
    }


def _compliance_client(post_responses: dict[tuple[str, str], dict]) -> FakeClient:
    return FakeClient(
        collections=_compliance_collections(),
        post_responses=post_responses,
        objects={
            "/rest/logical-interconnects/li1/firmware": {
                "sppUri": "/rest/firmware-drivers/fw1",
                "sppName": "SPP 2023.05",
            },
        },
    )


@pytest.mark.asyncio
async def test_list_compliance_defaults_to_latest_service_pack_and_breaks_down_resources():
    client = _compliance_client({
        ("fw2", "uuid-1111"): {
            "serverFirmwareUpdateRequired": True,
            "componentMappingList": [
                {
                    "componentName": "System ROM",
                    "componentLocation": "System Board",
                    "componentVersion": "U46 v2.52",
                    "baselineVersion": "U46 v2.70",
                    "componentFirmwareUpdateRequired": True,
                },
                {
                    "componentName": "iLO",
                    "componentVersion": "3.00",
                    "baselineVersion": "3.00",
                    "componentFirmwareUpdateRequired": False,
                },
            ],
        },
    })

    rows = await list_compliance(client)

    assert [r["kind"] for r in rows] == ["server-profile", "frame-link-module", "interconnect"]
    assert {r["target_baseline_label"] for r in rows} == {"SPP 2026.01 SY-2026.01.02"}
    assert {r["current_baseline_label"] for r in rows} == {"SPP 2023.05 SY-2023.05.01"}

    server = rows[0]
    assert server["resource_name"] == "aci-FM-host1"
    assert server["hardware"] == "Enclosure-01, bay 1"
    assert server["components_needing_update"] == 1
    assert server["components"][0] == {
        "name": "System ROM",
        "location": "System Board",
        "current_version": "U46 v2.52",
        "target_version": "U46 v2.70",
        "update_required": True,
    }

    flm = rows[1]
    assert flm["resource_name"] == "Enclosure-01, frame link module 1"
    assert flm["components"] == [{
        "name": "HPE Synergy Frame Link Module",
        "location": "Bay 1",
        "current_version": "1.0.0",
        "target_version": "1.2.3",
        "update_required": True,
    }]

    interconnect = rows[2]
    assert interconnect["resource_name"] == "Enclosure-01, interconnect 3"
    assert interconnect["components"] == [{
        "name": "HPE Virtual Connect SE 100Gb F32 Module for Synergy",
        "location": "",
        "current_version": "2.6.0.1001",
        "target_version": "2.9.1.1001",
        "update_required": True,
    }]

    assert client.post_calls == [
        ("/rest/server-hardware/firmware-compliance",
         {"firmwareBaselineId": "fw2", "serverUUID": "uuid-1111"}),
    ]


@pytest.mark.asyncio
async def test_list_compliance_honors_baseline_override():
    client = _compliance_client({
        ("fw1", "uuid-1111"): {
            "serverFirmwareUpdateRequired": False,
            "componentMappingList": [
                {
                    "componentName": "System ROM",
                    "componentVersion": "U46 v2.52",
                    "baselineVersion": "U46 v2.52",
                    "componentFirmwareUpdateRequired": False,
                },
            ],
        },
    })

    rows = await list_compliance(client, baseline="SY-2023.05.01")

    assert {r["target_baseline_label"] for r in rows} == {"SPP 2023.05 SY-2023.05.01"}
    assert rows[1]["components"][0]["update_required"] is False
    assert rows[2]["components"][0]["update_required"] is False
    assert client.post_calls == [
        ("/rest/server-hardware/firmware-compliance",
         {"firmwareBaselineId": "fw1", "serverUUID": "uuid-1111"}),
    ]


@pytest.mark.asyncio
async def test_list_compliance_raises_for_unknown_baseline():
    client = _compliance_client({})

    with pytest.raises(ValueError, match="not found"):
        await list_compliance(client, baseline="SY-2099.01.01")


@pytest.mark.asyncio
async def test_list_compliance_keeps_other_resources_when_server_check_fails():
    client = _compliance_client({
        ("fw2", "uuid-1111"): RuntimeError("server compliance endpoint failed"),
    })

    rows = await list_compliance(client)

    assert [r["kind"] for r in rows] == ["server-profile", "frame-link-module", "interconnect"]
    server = rows[0]
    assert server["error"] == "server compliance endpoint failed"
    assert server["components"][0]["name"] == "Compliance check failed"
    assert server["components"][0]["location"] == "server compliance endpoint failed"
    assert rows[1]["resource_name"] == "Enclosure-01, frame link module 1"


@pytest.mark.asyncio
async def test_list_compliance_falls_back_when_li_firmware_lookup_fails():
    client = FakeClient(
        collections=_compliance_collections(),
        post_responses={
            ("fw2", "uuid-1111"): {
                "serverFirmwareUpdateRequired": False,
                "componentMappingList": [],
            },
        },
        objects={
            "/rest/logical-interconnects/li1/firmware": RuntimeError("li firmware unavailable"),
        },
    )

    rows = await list_compliance(client)

    assert [r["kind"] for r in rows] == ["server-profile", "frame-link-module", "interconnect"]
    assert rows[1]["current_baseline_label"] == "SPP 2023.05 SY-2023.05.01"
    assert rows[2]["current_baseline_label"] == "SPP 2023.05 SY-2023.05.01"
