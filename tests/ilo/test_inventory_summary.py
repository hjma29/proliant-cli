"""Tests for fleet summary and storage fallback helpers."""

from __future__ import annotations

import pytest

from pcli.ilo import inventory


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses

    async def get_system_uri(self) -> str:
        return "/redfish/v1/Systems/1"

    async def get_manager_uri(self) -> str:
        return "/redfish/v1/Managers/1"

    async def get_chassis_uri(self) -> str:
        return "/redfish/v1/Chassis/1"

    async def get_firmware_inventory_uri(self) -> str:
        return "/redfish/v1/UpdateService/FirmwareInventory"

    async def get(self, uri: str) -> dict:
        value = self.responses.get(uri, {})
        if isinstance(value, dict):
            return value
        raise AssertionError(f"Unexpected GET response for {uri!r}: {value!r}")


@pytest.mark.asyncio
async def test_fetch_fleet_summary_uses_firmware_inventory_storage_fallback():
    client = FakeClient({
        "/redfish/v1/Systems/1": {
            "Model": "HPE ProLiant Compute DL325 Gen12",
            "BiosVersion": "A66 v1.40 (01/09/2026)",
            "Storage": {"@odata.id": "/redfish/v1/Systems/1/Storage"},
        },
        "/redfish/v1/Managers/1": {
            "Model": "iLO 7",
            "FirmwareVersion": "1.21.00 Apr 07 2026",
        },
        "/redfish/v1/Chassis/1": {
            "NetworkAdapters": {"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters"},
        },
        "/redfish/v1/Chassis/1/NetworkAdapters": {
            "Members": [{"@odata.id": "/redfish/v1/Chassis/1/NetworkAdapters/1"}]
        },
        "/redfish/v1/Chassis/1/NetworkAdapters/1": {
            "Controllers": [{"FirmwarePackageVersion": "235.1.164.14"}],
        },
        "/redfish/v1/Systems/1/Storage": {
            "Members": [{"@odata.id": "/redfish/v1/Systems/1/Storage/1"}]
        },
        "/redfish/v1/Systems/1/Storage/1": {
            "StorageControllers": [],
            "Controllers": {},
        },
        "/redfish/v1/UpdateService/FirmwareInventory": {
            "Members": [{"@odata.id": "/redfish/v1/UpdateService/FirmwareInventory/1"}]
        },
        "/redfish/v1/UpdateService/FirmwareInventory/1": {
            "Name": "HPE MR416i-p Gen11 Controller",
            "Version": "52.36.3-6584",
        },
    })

    result = await inventory.fetch_fleet_summary(client)

    assert dict(result)["Storage-FW"] == "52.36.3-6584"
