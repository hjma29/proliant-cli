"""Tests for proliant.com.storage: RAID vs. direct-attach classification via
COM's /servers/{id}/inventory "storage" section (same Redfish `Storage`
schema as iLO, with Controllers/Drives already inlined like OneView's
localStorageV2 -- see tests/ilo/test_storage_report.py for the shared
classifier itself)."""

from __future__ import annotations

from proliant.com import storage


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
        "Id": "DE000103",
        "Name": "HPE MR216i-p Gen10+",
        "Controllers": [{
            "Model": "HPE MR216i-p Gen10+",
            "Name": "HPE MR216i-p Gen10+",
            "FirmwareVersion": "52.24.0-4696",
        }],
        # COM's response includes both keys with identical content --
        # confirmed live against a real DL385 with an MR216i-p installed.
        "StorageControllers": [{
            "Model": "HPE MR216i-p Gen10+",
            "Name": "HPE MR216i-p Gen10+",
            "FirmwareVersion": "52.24.0-4696",
        }],
        "Drives": [
            _drive("0", 960197124096, protocol="SATA", media_type="SSD"),
            _drive("1", 960197124096, protocol="SATA", media_type="SSD"),
        ],
    }


def _direct_attach_storage_entry():
    return {
        "Id": "DA000103",
        "Name": "HPE NVMe Direct Attach Storage Controller",
        # Direct-attached drives also carry a controller entry describing
        # the drive's own embedded chip -- must NOT be surfaced as a RAID
        # controller (see proliant.common.storage_report).
        "Controllers": [{"Model": "MO003200KXAVU", "Name": "PE8030"}],
        "Drives": [_drive("2", 3200631791616, protocol="NVMe", media_type="SSD")],
    }


class _FakeSession:
    base_url = "https://us1.api.compute.cloud.hpe.com"


class _FakeClient:
    def __init__(self, responses: dict[str, dict]):
        self._responses = responses
        self.session = _FakeSession()

    async def get(self, uri, **kwargs):
        if uri not in self._responses:
            raise RuntimeError(f"GET {uri} failed — not found")
        return self._responses[uri]


def _inventory_url(server_id: str) -> str:
    return f"https://us1.api.compute.cloud.hpe.com/compute-ops-mgmt/v1/servers/{server_id}/inventory"


async def test_fetch_storage_report_data_classifies_raid_vs_direct():
    client = _FakeClient({
        _inventory_url("abc123"): {
            "storage": {"data": [_raid_storage_entry(), _direct_attach_storage_entry()]},
        },
    })
    report = await storage.fetch_storage_report_data(client, "abc123")

    assert len(report) == 2
    raid = next(c for c in report if c["attach_mode"] == "RAID Controller")
    direct = next(c for c in report if c["attach_mode"] == "Direct/HBA")

    assert raid["controller"] == "HPE MR216i-p Gen10+"
    assert raid["firmware"] == "52.24.0-4696"
    assert len(raid["drives"]) == 2

    # The direct-attached drive's own embedded-controller Model must never
    # be surfaced as a real controller.
    assert direct["controller"] == "—"
    assert direct["firmware"] == "—"
    assert len(direct["drives"]) == 1


async def test_fetch_storage_report_data_no_storage_key_returns_empty():
    client = _FakeClient({_inventory_url("abc123"): {}})
    report = await storage.fetch_storage_report_data(client, "abc123")
    assert report == []


async def test_fetch_storage_report_data_empty_data_list_returns_empty():
    client = _FakeClient({_inventory_url("abc123"): {"storage": {"data": []}}})
    report = await storage.fetch_storage_report_data(client, "abc123")
    assert report == []


async def test_fetch_storage_report_data_returns_empty_on_error():
    client = _FakeClient({})  # no matching URL -> client.get() raises
    report = await storage.fetch_storage_report_data(client, "missing-server")
    assert report == []


async def test_fetch_storage_list_row():
    client = _FakeClient({
        _inventory_url("abc123"): {
            "storage": {"data": [_raid_storage_entry(), _direct_attach_storage_entry()]},
        },
    })
    rows = await storage.fetch_storage_list_row(client, {"id": "abc123", "name": "host1"})
    row_dict = dict(rows)

    assert row_dict["Storage Controller"] == "MR216i-p Gen10+"
    assert row_dict["RAID Attached"] == "2 x 960GB SSD"
    assert row_dict["Direct Attached"] == "1 x 3201GB NVMe"
