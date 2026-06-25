"""Unit tests for iLO network adapter inventory helpers."""

from __future__ import annotations

import pytest

from proliant.ilo import inventory


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
async def test_fetch_network_versions_uses_raw_adapter_model_with_locations():
    client = FakeClient({
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"},
            "Oem": {"Hpe": {"Links": {"Devices": {"@odata.id": "/redfish/v1/Chassis/1/Devices/"}}}},
        },
        "/redfish/v1/Chassis/1/Devices/": {"Members": []},
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
            "Ports": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE07A000/Ports"},
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE085000": {
            "Model": "BCM57414",
            "SKU": "10/25Gb 2-port SFP28 BCM57414 Adapter",
            "PartNumber": "P26264-001",
            "Location": {"PartLocation": {"ServiceLabel": "PCIE Slot 6"}},
            "Ports": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE085000/Ports"},
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE07A000/Ports": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE07A000/Ports/1"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE07A000/Ports/1": {
            "PortId": "1",
            "Ethernet": {"AssociatedMACAddresses": ["00:11:22:33:44:55"]},
            "LinkStatus": "LinkUp",
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE085000/Ports": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE085000/Ports/2"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE085000/Ports/2": {
            "PortId": "2",
            "Ethernet": {"AssociatedMACAddresses": ["66:77:88:99:aa:bb"]},
            "LinkStatus": "NoLink",
        },
    })

    result = await inventory.fetch_network_versions(client)

    assert result == [
        {
            "Name": "BCM57414",
            "PartNumber": "P10113-001",
            "Version": "235.1.164.14",
            "Location": "OCP Slot 21",
            "Port": "p1",
            "MACAddress": "00:11:22:33:44:55",
            "LinkStatus": "Link Up",
        },
        {
            "Name": "BCM57414",
            "PartNumber": "P26264-001",
            "Version": "235.1.164.14",
            "Location": "PCIE Slot 6",
            "Port": "p2",
            "MACAddress": "66:77:88:99:aa:bb",
            "LinkStatus": "No Link",
        },
    ]


@pytest.mark.asyncio
async def test_fetch_network_versions_preserves_raw_descriptive_model_names():
    client = FakeClient({
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"},
            "Oem": {"Hpe": {"Links": {"Devices": {"@odata.id": "/redfish/v1/Chassis/1/Devices/"}}}},
        },
        "/redfish/v1/Chassis/1/Devices/": {"Members": []},
        "/redfish/v1/Chassis/1/NetworkAdapters": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE099999"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE099999": {
            "Model": "Broadcom P225p NetXtreme-E 10Gb/25Gb Ethernet PCIe Adapter - NIC",
            "PartNumber": "P26264-001",
            "Location": {"PartLocation": {"ServiceLabel": "PCI-E Slot 6"}},
            "Ports": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE099999/Ports"},
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE099999/Ports": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE099999/Ports/1"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE099999/Ports/1": {
            "PortId": "1",
            "Ethernet": {"AssociatedMACAddresses": ["bc:97:e1:e3:35:00"]},
            "LinkStatus": "LinkUp",
        },
    })

    result = await inventory.fetch_network_versions(client)

    assert result == [
        {
            "Name": "Broadcom P225p NetXtreme-E 10Gb/25Gb Ethernet PCIe Adapter - NIC",
            "PartNumber": "P26264-001",
            "Version": "235.1.164.14",
            "Location": "PCI-E Slot 6",
            "Port": "p1",
            "MACAddress": "bc:97:e1:e3:35:00",
            "LinkStatus": "Link Up",
        }
    ]


@pytest.mark.asyncio
async def test_fetch_network_versions_uses_oem_location_fallback_for_ilo6():
    client = FakeClient({
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"},
            "Oem": {"Hpe": {"Links": {"Devices": {"@odata.id": "/redfish/v1/Chassis/1/Devices/"}}}},
        },
        "/redfish/v1/Chassis/1/Devices/": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/Devices/2/"}]
        },
        "/redfish/v1/Chassis/1/Devices/2/": {
            "SerialNumber": "VNM2450RTD",
            "Location": "OCP 3.0 Slot 15",
            "ProductPartNumber": "P10113-001",
        },
        "/redfish/v1/Chassis/1/NetworkAdapters": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE009000"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE009000": {
            "Model": "BCM57414",
            "SKU": "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter",
            "PartNumber": "",
            "SerialNumber": "VNM2450RTD",
            "Location": {"PartLocation": {"ServiceLabel": None, "LocationOrdinalValue": None}},
            "Ports": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE009000/Ports"},
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE009000/Ports": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/DE009000/Ports/1"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/DE009000/Ports/1": {
            "PortId": "1",
            "Ethernet": {"AssociatedMACAddresses": ["00:62:0b:a2:9d:7a"]},
            "LinkStatus": "NoLink",
        },
    })

    result = await inventory.fetch_network_versions(client)

    assert result == [
        {
            "Name": "BCM57414",
            "PartNumber": "P10113-001",
            "Version": "235.1.164.14",
            "Location": "OCP 3.0 Slot 15",
            "Port": "p1",
            "MACAddress": "00:62:0b:a2:9d:7a",
            "LinkStatus": "No Link",
        }
    ]
