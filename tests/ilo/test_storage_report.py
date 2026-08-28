"""Tests for fetch_storage_report_data / summarize_storage_report."""

from __future__ import annotations

import pytest

from proliant.ilo import inventory


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses

    async def get_system_uri(self) -> str:
        return "/redfish/v1/Systems/1"

    async def get_manager_uri(self) -> str:
        return "/redfish/v1/Managers/1"

    async def get(self, uri: str) -> dict:
        value = self.responses.get(uri, {})
        if isinstance(value, dict):
            return value
        raise AssertionError(f"Unexpected GET response for {uri!r}: {value!r}")


def _client() -> FakeClient:
    return FakeClient({
        "/redfish/v1/Systems/1": {
            "Storage": {"@odata.id": "/redfish/v1/Systems/1/Storage"},
        },
        "/redfish/v1/Systems/1/Storage": {
            "Members": [
                {"@odata.id": "/redfish/v1/Systems/1/Storage/RAID"},
                {"@odata.id": "/redfish/v1/Systems/1/Storage/Direct"},
                {"@odata.id": "/redfish/v1/Systems/1/Storage/Empty"},
            ]
        },
        # RAID-controlled subsystem: one Smart Array controller + 2 drives.
        "/redfish/v1/Systems/1/Storage/RAID": {
            "Name": "RAID Storage",
            "StorageControllers": [
                {"Name": "HPE Smart Array P816i-a", "FirmwareVersion": "3.10"}
            ],
            "Drives": [
                {"@odata.id": "/redfish/v1/Systems/1/Storage/RAID/Drives/1"},
                {"@odata.id": "/redfish/v1/Systems/1/Storage/RAID/Drives/2"},
            ],
        },
        "/redfish/v1/Systems/1/Storage/RAID/Drives/1": {
            "Id": "1",
            "Name": "Drive 1",
            "CapacityBytes": 1920383410176,  # ~1788 GiB
            "MediaType": "SSD",
            "Protocol": "SAS",
            "Model": "MO001920JWTBA",
            "SerialNumber": "SN001",
            "PartNumber": "PN001",
            "Revision": "HPD8",
            "Status": {"Health": "OK"},
            "PhysicalLocation": {"PartLocation": {"ServiceLabel": "Box 1 Bay 1"}},
        },
        "/redfish/v1/Systems/1/Storage/RAID/Drives/2": {
            "Id": "2",
            "Name": "Drive 2",
            "CapacityBytes": 1920383410176,
            "MediaType": "SSD",
            "Protocol": "SAS",
            "Model": "MO001920JWTBA",
            "SerialNumber": "SN002",
            "PartNumber": "PN001",
            "Revision": "HPD8",
            "Status": {"Health": "OK"},
            "PhysicalLocation": {"PartLocation": {"ServiceLabel": "Box 1 Bay 2"}},
        },
        # Direct-attached (no RAID controller): 1 embedded NVMe drive.
        "/redfish/v1/Systems/1/Storage/Direct": {
            "Name": "Embedded NVMe",
            "StorageControllers": [],
            "Controllers": {},
            "Drives": [
                {"@odata.id": "/redfish/v1/Systems/1/Storage/Direct/Drives/1"},
            ],
        },
        "/redfish/v1/Systems/1/Storage/Direct/Drives/1": {
            "Id": "3",
            "Name": "Drive 3",
            "CapacityBytes": 480103981056,  # ~447 GiB
            "MediaType": "SSD",
            "Protocol": "NVMe",
            "Model": "NVMe-A",
            "SerialNumber": "SN003",
            "PartNumber": "PN003",
            "Revision": "1.0",
            "Status": {"Health": "OK"},
            "PhysicalLocation": {"PartLocation": {"ServiceLabel": "NVMe Bay 1"}},
        },
        # A subsystem with no drives should be skipped entirely.
        "/redfish/v1/Systems/1/Storage/Empty": {
            "Name": "Empty Subsystem",
            "StorageControllers": [],
            "Drives": [],
        },
    })


@pytest.mark.asyncio
async def test_fetch_storage_report_data_classifies_raid_vs_direct():
    report = await inventory.fetch_storage_report_data(_client())

    assert len(report) == 2

    raid_ctrl = next(c for c in report if c["attach_mode"] == "RAID Controller")
    assert raid_ctrl["controller"] == "HPE Smart Array P816i-a"
    assert raid_ctrl["firmware"] == "3.10"
    assert len(raid_ctrl["drives"]) == 2
    assert all(d["attached_via"] == "RAID Controller" for d in raid_ctrl["drives"])
    assert raid_ctrl["drives"][0]["capacity_gib"] == 1788
    assert raid_ctrl["drives"][0]["serial"] == "SN001"

    direct_ctrl = next(c for c in report if c["attach_mode"] == "Direct/HBA")
    assert direct_ctrl["controller"] == "Embedded NVMe"
    assert len(direct_ctrl["drives"]) == 1
    assert direct_ctrl["drives"][0]["protocol"] == "NVMe"
    assert direct_ctrl["drives"][0]["attached_via"] == "Direct/HBA"


@pytest.mark.asyncio
async def test_summarize_storage_report_groups_by_capacity_and_mode():
    report = await inventory.fetch_storage_report_data(_client())
    summary = inventory.summarize_storage_report(report)

    assert "2 ctrl, 3 disks" in summary
    assert "2x1788GiB SSD(RAID)" in summary
    assert "1x447GiB SSD(Direct)" in summary


def test_summarize_storage_report_empty():
    assert inventory.summarize_storage_report([]) == "—"
