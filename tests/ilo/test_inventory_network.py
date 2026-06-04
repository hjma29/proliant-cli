"""Unit tests for iLO network adapter inventory helpers."""

from __future__ import annotations

import pytest

from pcli.ilo import inventory


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses

    async def get_chassis_uri(self) -> str:
        return "/redfish/v1/Chassis/1"

    async def get(self, uri: str) -> dict:
        value = self.responses.get(uri, {})
        if isinstance(value, dict):
            return value
        raise AssertionError(f"Unexpected GET response for {uri!r}: {value!r}")


@pytest.mark.asyncio
async def test_fetch_network_versions_uses_descriptive_labels_and_locations():
    client = FakeClient({
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"}
        },
        "/redfish/v1/Chassis/1/NetworkAdapters": {
            "Members": [
                {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE07A000"},
                {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE085000"},
            ]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE07A000": {
            "Model": "BCM57414",
            "SKU": "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter",
            "PartNumber": "P10113-001",
            "Location": {"PartLocation": {"ServiceLabel": "OCP Slot 21"}},
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE085000": {
            "Model": "BCM57414",
            "SKU": "10/25Gb 2-port SFP28 BCM57414 Adapter",
            "PartNumber": "P26264-001",
            "Location": {"PartLocation": {"ServiceLabel": "PCIE Slot 6"}},
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
    })

    result = await inventory.fetch_network_versions(client)

    assert result == [
        {
            "Name": "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter",
            "Version": "235.1.164.14",
            "Location": "OCP Slot 21",
        },
        {
            "Name": "Broadcom P225p NetXtreme-E Dual-port 10Gb/25Gb Ethernet PCIe Adapter - NIC",
            "Version": "235.1.164.14",
            "Location": "PCIE Slot 6",
        },
    ]


@pytest.mark.asyncio
async def test_fetch_network_versions_preserves_descriptive_model_names():
    client = FakeClient({
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"}
        },
        "/redfish/v1/Chassis/1/NetworkAdapters": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE099999"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE099999": {
            "Model": "Broadcom P225p NetXtreme-E 10Gb/25Gb Ethernet PCIe Adapter - NIC",
            "PartNumber": "P26264-001",
            "Location": {"PartLocation": {"ServiceLabel": "PCI-E Slot 6"}},
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
    })

    result = await inventory.fetch_network_versions(client)

    assert result == [
        {
            "Name": "Broadcom P225p NetXtreme-E Dual-port 10Gb/25Gb Ethernet PCIe Adapter - NIC",
            "Version": "235.1.164.14",
            "Location": "PCI-E Slot 6",
        }
    ]
