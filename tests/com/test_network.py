"""Tests for proliant.com.network: physical NIC inventory via COM's
/servers/{id}/inventory "networkAdapter" section (same Redfish
`NetworkAdapter` schema iLO exposes directly, with Ports fully inlined --
see tests/ilo/test_network_report.py for the shared classifier itself)."""

from __future__ import annotations

from proliant.com import network


def _adapter():
    return {
        "Id": "1",
        "Name": "BCM 5719 1Gb 4p BASE-T OCP Adptr",
        "Model": "BCM 5719 1Gb 4p BASE-T OCP Adptr",
        "PartNumber": "PN001",
        "Controllers": [{
            "FirmwarePackageVersion": "20.27.42",
            "Location": {"PartLocation": {"ServiceLabel": "OCP 3.0 Slot 22"}},
        }],
        "Ports": [
            {
                "Id": "1",
                "LinkStatus": "LinkDown",
                "Ethernet": {"AssociatedMACAddresses": ["04:32:01:5a:0b:52"]},
            },
            {
                "Id": "2",
                "LinkStatus": "LinkUp",
                "CurrentSpeedGbps": 1,
                "Ethernet": {"AssociatedMACAddresses": ["04:32:01:5a:0b:53"]},
            },
        ],
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


async def test_fetch_network_report_data():
    client = _FakeClient({
        _inventory_url("abc123"): {"networkAdapter": {"data": [_adapter()]}},
    })
    report = await network.fetch_network_report_data(client, "abc123")

    assert len(report) == 1
    adapter = report[0]
    assert adapter["model"] == "BCM 5719 1Gb 4p BASE-T OCP Adptr"
    assert adapter["location"] == "OCP 3.0 Slot 22"
    assert adapter["firmware"] == "20.27.42"
    assert len(adapter["ports"]) == 2

    # "LinkDown" (seen live on real Gen10/Gen11 hardware) must normalize
    # to "No Link", same as "NoLink"/"down".
    assert adapter["ports"][0]["link_status"] == "No Link"
    assert adapter["ports"][1]["link_status"] == "Link Up"


async def test_fetch_network_report_data_no_network_key_returns_empty():
    client = _FakeClient({_inventory_url("abc123"): {}})
    report = await network.fetch_network_report_data(client, "abc123")
    assert report == []


async def test_fetch_network_report_data_empty_data_list_returns_empty():
    client = _FakeClient({_inventory_url("abc123"): {"networkAdapter": {"data": []}}})
    report = await network.fetch_network_report_data(client, "abc123")
    assert report == []


async def test_fetch_network_report_data_returns_empty_on_error():
    client = _FakeClient({})  # no matching URL -> client.get() raises
    report = await network.fetch_network_report_data(client, "missing-server")
    assert report == []


async def test_fetch_network_list_row():
    client = _FakeClient({
        _inventory_url("abc123"): {"networkAdapter": {"data": [_adapter()]}},
    })
    rows = await network.fetch_network_list_row(client, {"id": "abc123", "name": "host1"})
    row_dict = dict(rows)

    assert row_dict["Location"] == "OCP 3.0 Slot 22\nOCP 3.0 Slot 22"
    assert row_dict["Port"] == "1\n2"
    assert row_dict["MAC"] == "04:32:01:5a:0b:52\n04:32:01:5a:0b:53"
    assert row_dict["Link Status"] == "No Link\nLink Up"
