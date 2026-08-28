"""Tests for fetch_network_report_data / fetch_network_list_row (ilo).

Fixture mirrors real Redfish behavior confirmed live against a DL380
Gen11 (iLO 6, unified `Port` schema with inlined MAC/LLDP) and a Synergy
Gen10 blade (iLO 5, separate `NetworkPort` schema where MAC instead lives
on a linked `NetworkDeviceFunction` sibling resource) -- see
proliant.common.network_report for the shared classifier itself.
"""

from __future__ import annotations

import pytest

from proliant.ilo import inventory


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses

    async def get_chassis_uri(self) -> str:
        return "/redfish/v1/Chassis/1"

    async def get_firmware_inventory_uri(self) -> str:
        return "/redfish/v1/UpdateService/FirmwareInventory"

    async def get(self, uri: str) -> dict:
        value = self.responses.get(uri, {})
        if isinstance(value, dict):
            return value
        raise AssertionError(f"Unexpected GET response for {uri!r}: {value!r}")


def _unified_schema_client() -> FakeClient:
    """One adapter whose Ports inline MAC/LLDP directly (iLO 6/7 style)."""
    return FakeClient({
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"},
            "Oem": {"Hpe": {"Links": {"Devices": {"@odata.id": "/redfish/v1/Chassis/1/Devices"}}}},
        },
        "/redfish/v1/Chassis/1/Devices": {"Members": []},
        "/redfish/v1/Chassis/1/NetworkAdapters": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1": {
            "Id": "1",
            "Name": "HPE Ethernet 1Gb 4-port 331i Adapter",
            "Model": "HPE Ethernet 1Gb 4-port 331i Adapter",
            "PartNumber": "PN001",
            "Controllers": [{"FirmwarePackageVersion": "20.27.42"}],
            "Ports": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports"},
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/1"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/1": {
            "Id": "1",
            "LinkStatus": "LinkUp",
            "CurrentSpeedGbps": 1,
            "Ethernet": {
                "AssociatedMACAddresses": ["AA:BB:CC:DD:EE:01"],
                "LLDPReceive": {
                    "ChassisId": "sw-01",
                    "SystemDescription": "Switch Model X\nfirmware 1.0",
                    "PortId": "GigabitEthernet1/0/1",
                    "ManagementAddressIPv4": "10.0.0.1",
                },
            },
            "Status": {"Health": "OK"},
        },
    })


@pytest.mark.asyncio
async def test_fetch_network_report_data_unified_schema():
    report = await inventory.fetch_network_report_data(_unified_schema_client())

    assert len(report) == 1
    adapter = report[0]
    assert adapter["model"] == "HPE Ethernet 1Gb 4-port 331i Adapter"
    assert adapter["part_number"] == "PN001"
    assert adapter["firmware"] == "20.27.42"
    assert len(adapter["ports"]) == 1

    port = adapter["ports"][0]
    assert port["mac"] == "aa:bb:cc:dd:ee:01"
    assert port["link_status"] == "Link Up"
    assert port["speed_gbps"] == 1
    assert port["lldp_neighbor"]["chassis"] == "Switch Model X"
    assert port["lldp_neighbor"]["port"] == "GigabitEthernet1/0/1"


@pytest.mark.asyncio
async def test_fetch_network_list_row_unified_schema():
    rows = await inventory.fetch_network_list_row(_unified_schema_client())
    row_dict = dict(rows)
    assert row_dict["Port"] == "1"
    assert row_dict["MAC"] == "aa:bb:cc:dd:ee:01"
    assert row_dict["Link Status"] == "Link Up"


def _ndf_schema_client() -> FakeClient:
    """One adapter whose Ports do NOT inline MAC -- MAC instead lives on a
    linked NetworkDeviceFunction sibling (older Synergy Gen10 / iLO 5
    style)."""
    return FakeClient({
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"},
            "Oem": {"Hpe": {"Links": {"Devices": {"@odata.id": "/redfish/v1/Chassis/1/Devices"}}}},
        },
        "/redfish/v1/Chassis/1/Devices": {"Members": []},
        "/redfish/v1/Chassis/1/NetworkAdapters": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1": {
            "Id": "1",
            "Name": "Synergy 4820C 10/20/25Gb CNA",
            "Model": "Synergy 4820C 10/20/25Gb CNA",
            "PartNumber": "PN002",
            "Controllers": [{"FirmwarePackageVersion": "8.65.23"}],
            "Ports": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports"},
            "NetworkDeviceFunctions": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/NDF"},
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/1"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1/Ports/1": {
            "Id": "1",
            "LinkStatus": "LinkUp",
            "CurrentSpeedGbps": 25,
            "Ethernet": {},
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1/NDF": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1/NDF/1"}],
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1/NDF/1": {
            "Ethernet": {"PermanentMACAddress": "22:00:A3:E0:00:08"},
        },
    })


@pytest.mark.asyncio
async def test_fetch_network_report_data_ndf_fallback_schema():
    report = await inventory.fetch_network_report_data(_ndf_schema_client())

    assert len(report) == 1
    port = report[0]["ports"][0]
    assert port["mac"] == "22:00:a3:e0:00:08"
    assert port["link_status"] == "Link Up"
    # NDF-derived MAC path never resolves LLDP.
    assert port["lldp_neighbor"] is None


@pytest.mark.asyncio
async def test_fetch_network_report_data_no_adapters_returns_empty():
    client = FakeClient({
        "/redfish/v1/Chassis/1": {},
    })
    report = await inventory.fetch_network_report_data(client)
    assert report == []
