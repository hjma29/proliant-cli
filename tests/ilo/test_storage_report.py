"""Tests for fetch_storage_report_data / summarize_storage_report /
summarize_storage_for_list.

Fixture mirrors real Redfish behavior confirmed live against an HPE
ProLiant DL380 Gen11 (iLO 6): the Storage resource's own Id prefix (DE*
vs DA*) is the correct RAID-vs-direct-attach signal — *not* whether a
StorageControllers/Controllers entry exists, since direct-attached (DA*)
NVMe drives also expose one, describing the drive's own embedded NVMe
controller chip (Model/Name == the SSD's own part number) rather than a
real RAID/HBA product.
"""

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
                {"@odata.id": "/redfish/v1/Systems/1/Storage/DE00C000"},
                {"@odata.id": "/redfish/v1/Systems/1/Storage/DA000201"},
                {"@odata.id": "/redfish/v1/Systems/1/Storage/DA000202"},
                {"@odata.id": "/redfish/v1/Systems/1/Storage/Empty"},
            ]
        },
        # DE* = RDE-capable: a genuine hardware RAID/boot controller.
        # 2 drives sit "behind" it.
        "/redfish/v1/Systems/1/Storage/DE00C000": {
            "Id": "DE00C000",
            "Name": "NS204i-u",
            "StorageControllers": [
                {
                    "Model": "HPE NS204i-u Gen11 Boot Controller",
                    "FirmwareVersion": "1.2.14.1001",
                    "SupportedRAIDTypes": ["RAID1"],
                }
            ],
            "Drives": [
                {"@odata.id": "/redfish/v1/Systems/1/Storage/DE00C000/Drives/1"},
                {"@odata.id": "/redfish/v1/Systems/1/Storage/DE00C000/Drives/2"},
            ],
        },
        "/redfish/v1/Systems/1/Storage/DE00C000/Drives/1": {
            "Id": "1",
            "Name": "Drive 1",
            "CapacityBytes": 480103981056,  # ~447 GiB
            "MediaType": "SSD",
            "Protocol": "NVMe",
            "Model": "MO000480KYCXW",
            "SerialNumber": "SN001",
            "PartNumber": "PN001",
            "Revision": "HPD8",
            "Status": {"Health": "OK"},
            "PhysicalLocation": {"PartLocation": {"ServiceLabel": "Slot 16 Bay 1"}},
        },
        "/redfish/v1/Systems/1/Storage/DE00C000/Drives/2": {
            "Id": "2",
            "Name": "Drive 2",
            "CapacityBytes": 480103981056,
            "MediaType": "SSD",
            "Protocol": "NVMe",
            "Model": "MO000480KYCXW",
            "SerialNumber": "SN002",
            "PartNumber": "PN001",
            "Revision": "HPD8",
            "Status": {"Health": "OK"},
            "PhysicalLocation": {"PartLocation": {"ServiceLabel": "Slot 16 Bay 2"}},
        },
        # DA* = direct attached: each is its own Storage resource with
        # exactly one drive. It DOES have a StorageControllers entry, but
        # that entry describes the drive's own embedded NVMe controller
        # (Model == the SSD's own part number) — must NOT be classified
        # as "RAID Controller" or surfaced as a controller model.
        "/redfish/v1/Systems/1/Storage/DA000201": {
            "Id": "DA000201",
            "Name": "Direct 1",
            "StorageControllers": [
                {"Model": "MO003200KXAVU", "Name": "PE8030", "FirmwareVersion": "HPK3"}
            ],
            "Drives": [
                {"@odata.id": "/redfish/v1/Systems/1/Storage/DA000201/Drives/1"},
            ],
        },
        "/redfish/v1/Systems/1/Storage/DA000201/Drives/1": {
            "Id": "DA000201",
            "Name": "Drive 3",
            "CapacityBytes": 3200631791616,  # ~2981 GiB
            "MediaType": "SSD",
            "Protocol": "NVMe",
            "Model": "MO003200KXAVU",
            "SerialNumber": "SN003",
            "PartNumber": "PN003",
            "Revision": "HPK3",
            "Status": {"Health": "OK"},
            "PhysicalLocation": {"PartLocation": {"ServiceLabel": "Embedded:Bay=1"}},
        },
        "/redfish/v1/Systems/1/Storage/DA000202": {
            "Id": "DA000202",
            "Name": "Direct 2",
            "StorageControllers": [
                {"Model": "MO003200KXAVU", "Name": "PE8030", "FirmwareVersion": "HPK3"}
            ],
            "Drives": [
                {"@odata.id": "/redfish/v1/Systems/1/Storage/DA000202/Drives/1"},
            ],
        },
        "/redfish/v1/Systems/1/Storage/DA000202/Drives/1": {
            "Id": "DA000202",
            "Name": "Drive 4",
            "CapacityBytes": 3200631791616,
            "MediaType": "SSD",
            "Protocol": "NVMe",
            "Model": "MO003200KXAVU",
            "SerialNumber": "SN004",
            "PartNumber": "PN003",
            "Revision": "HPK3",
            "Status": {"Health": "OK"},
            "PhysicalLocation": {"PartLocation": {"ServiceLabel": "Embedded:Bay=2"}},
        },
        # A subsystem with no drives should be skipped entirely.
        "/redfish/v1/Systems/1/Storage/Empty": {
            "Id": "Empty",
            "Name": "Empty Subsystem",
            "StorageControllers": [],
            "Drives": [],
        },
    })


@pytest.mark.asyncio
async def test_fetch_storage_report_data_classifies_raid_vs_direct():
    report = await inventory.fetch_storage_report_data(_client())

    # 3 storage subsystems with drives: 1 RAID (DE00C000), 2 direct (DA*).
    assert len(report) == 3

    raid_ctrl = next(c for c in report if c["attach_mode"] == "RAID Controller")
    assert raid_ctrl["controller"] == "HPE NS204i-u Gen11 Boot Controller"
    assert raid_ctrl["firmware"] == "1.2.14.1001"
    assert len(raid_ctrl["drives"]) == 2
    assert all(d["attached_via"] == "RAID Controller" for d in raid_ctrl["drives"])
    assert raid_ctrl["drives"][0]["capacity_gib"] == 447
    assert raid_ctrl["drives"][0]["serial"] == "SN001"

    direct_ctrls = [c for c in report if c["attach_mode"] == "Direct/HBA"]
    assert len(direct_ctrls) == 2
    for ctrl in direct_ctrls:
        # Direct-attached drives must NOT surface their embedded NVMe
        # controller's Model as a "storage controller" — there is no
        # real RAID/HBA product behind them.
        assert ctrl["controller"] == "—"
        assert len(ctrl["drives"]) == 1
        assert ctrl["drives"][0]["protocol"] == "NVMe"
        assert ctrl["drives"][0]["attached_via"] == "Direct/HBA"
        assert ctrl["drives"][0]["type_label"] == "NVMe"


@pytest.mark.asyncio
async def test_summarize_storage_report_groups_by_capacity_and_mode():
    report = await inventory.fetch_storage_report_data(_client())
    summary = inventory.summarize_storage_report(report)

    assert "1 ctrl, 4 disks" in summary
    assert "2x447GiB SSD(RAID)" in summary
    assert "2x2981GiB SSD(Direct)" in summary


def test_summarize_storage_report_empty():
    assert inventory.summarize_storage_report([]) == "—"


@pytest.mark.asyncio
async def test_summarize_storage_for_list_splits_by_attachment():
    report = await inventory.fetch_storage_report_data(_client())
    summary = inventory.summarize_storage_for_list(report)

    # Only the real hardware controller model shows up — the direct-
    # attached drives' own embedded-controller Model is never surfaced.
    # Controller name is trimmed to just the part number for the list view.
    assert summary["controller"] == "NS204i-u Gen11"
    assert summary["behind"] == "2 x 480GB NVMe"
    assert summary["direct"] == "2 x 3201GB NVMe"


def test_summarize_storage_for_list_empty():
    summary = inventory.summarize_storage_for_list([])
    assert summary == {"controller": "—", "behind": "—", "direct": "—"}


@pytest.mark.asyncio
async def test_fetch_storage_list_row():
    rows = await inventory.fetch_storage_list_row(_client())
    row_dict = dict(rows)
    assert row_dict["Storage Controller"] == "NS204i-u Gen11"
    assert row_dict["RAID Attached"] == "2 x 480GB NVMe"
    assert row_dict["Direct Attached"] == "2 x 3201GB NVMe"
