"""Tests for proliant.oneview.storage: RAID vs. direct-attach classification
via OneView's localStorageV2 sub-resource (same Redfish schema as iLO, but
with Controllers/Drives already inlined -- see tests/ilo/test_storage_report.py
for the shared classifier itself)."""

from __future__ import annotations

from proliant.oneview import storage


def _drive(id_, capacity_bytes, protocol="NVMe", media_type="SSD"):
    return {
        "Id": id_,
        "Name": f"drive {id_}",
        "PhysicalLocation": {"PartLocation": {"ServiceLabel": f"Bay={id_}"}},
        "CapacityBytes": capacity_bytes,
        "Protocol": protocol,
        "MediaType": media_type,
        "Model": "SOMEMODEL",
        "SerialNumber": "SN123",
        "PartNumber": "PN123",
        "Revision": "1.0",
        "Status": {"Health": "OK"},
    }


def _raid_storage_entry():
    return {
        "Id": "DE07A000",
        "Name": "HPE Smart Array E208i-c SR Gen10",
        "Controllers": [{
            "Model": "HPE Smart Array E208i-c SR Gen10",
            "Name": "HPE Smart Array E208i-c SR Gen10",
            "FirmwareVersion": "7.81",
        }],
        "Drives": [
            _drive("0", 960197124096, protocol="SATA", media_type="SSD"),
            _drive("1", 960197124096, protocol="SATA", media_type="SSD"),
        ],
    }


def _direct_attach_storage_entry():
    return {
        "Id": "DA000201",
        "Name": "HPE NVMe Direct Attach Storage Controller",
        # Direct-attached drives also carry a Controllers entry describing
        # the drive's own embedded chip -- must NOT be surfaced as a RAID
        # controller (see proliant.common.storage_report).
        "Controllers": [{"Model": "MO003200KXAVU", "Name": "PE8030"}],
        "Drives": [_drive("2", 3200631791616, protocol="NVMe", media_type="SSD")],
    }


class _FakeClient:
    def __init__(self, responses: dict[str, dict]):
        self._responses = responses

    async def get(self, uri):
        if uri not in self._responses:
            from proliant.oneview.client import OneViewError
            raise OneViewError(f"GET {uri} failed — HTTP 404: not found")
        return self._responses[uri]


async def test_fetch_storage_report_data_classifies_raid_vs_direct():
    client = _FakeClient({
        "/rest/server-hardware/1/localStorageV2": {
            "data": [_raid_storage_entry(), _direct_attach_storage_entry()],
        },
    })
    report = await storage.fetch_storage_report_data(client, "/rest/server-hardware/1")

    assert len(report) == 2
    raid = next(c for c in report if c["attach_mode"] == "RAID Controller")
    direct = next(c for c in report if c["attach_mode"] == "Direct/HBA")

    assert raid["controller"] == "HPE Smart Array E208i-c SR Gen10"
    assert raid["firmware"] == "7.81"
    assert len(raid["drives"]) == 2

    # The direct-attached drive's own embedded-controller Model must never
    # be surfaced as a real controller.
    assert direct["controller"] == "—"
    assert direct["firmware"] == "—"
    assert len(direct["drives"]) == 1


async def test_fetch_storage_report_data_falls_back_to_v1_local_storage():
    client = _FakeClient({
        "/rest/server-hardware/1/localStorage": {"data": [_raid_storage_entry()]},
    })
    report = await storage.fetch_storage_report_data(client, "/rest/server-hardware/1")
    assert len(report) == 1
    assert report[0]["attach_mode"] == "RAID Controller"


async def test_fetch_storage_report_data_no_storage_returns_empty():
    client = _FakeClient({"/rest/server-hardware/1/localStorageV2": {"data": []}})
    report = await storage.fetch_storage_report_data(client, "/rest/server-hardware/1")
    assert report == []


async def test_fetch_storage_list_row():
    client = _FakeClient({
        "/rest/server-hardware/1/localStorageV2": {
            "data": [_raid_storage_entry(), _direct_attach_storage_entry()],
        },
    })
    rows = await storage.fetch_storage_list_row(client, {"uri": "/rest/server-hardware/1"})
    row_dict = dict(rows)

    assert row_dict["Storage Controller"] == "Smart Array E208i-c SR Gen10"
    assert row_dict["RAID Attached"] == "2 x 960GB SSD"
    assert row_dict["Direct Attached"] == "1 x 3201GB NVMe"
