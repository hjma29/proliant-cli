"""Tests for proliant.oneview.network_adapters: physical NIC inventory via
OneView's server-hardware `networkAdapters` sub-resource (same Redfish
`NetworkAdapter` schema iLO/COM expose -- see
tests/ilo/test_network_report.py for the shared classifier itself)."""

from __future__ import annotations

from proliant.oneview import network_adapters


def _port(port_id, mac=None, link="LinkUp", speed=25):
    port = {"Id": port_id, "LinkStatus": link, "CurrentSpeedGbps": speed}
    if mac:
        port["Ethernet"] = {"AssociatedMACAddresses": [mac]}
    return port


def _adapter_inlined_mac():
    """Ports directly inline MAC -- confirmed live against this
    appliance's Synergy 4820C CNA."""
    return {
        "Id": "1",
        "Name": "Synergy 4820C 10/20/25Gb CNA",
        "Model": "Synergy 4820C 10/20/25Gb CNA",
        "PartNumber": "PN001",
        "Controllers": [{
            "FirmwarePackageVersion": "8.65.23",
            "Location": {"PartLocation": {"ServiceLabel": "Mezzanine Slot 3"}},
        }],
        "Ports": [
            _port("1", mac="22:00:a3:e0:00:08"),
            _port("2", mac="22:00:a3:e0:00:09"),
        ],
    }


def _adapter_ndf_fallback():
    """Ports do NOT inline MAC -- resolved via the adapter's already-inlined
    NetworkDeviceFunctions, matched by AssignablePhysicalPorts URI."""
    return {
        "Id": "2",
        "Name": "HPE FlexFabric 20Gb 2-port 650FLB Adapter",
        "Model": "HPE FlexFabric 20Gb 2-port 650FLB Adapter",
        "PartNumber": "PN002",
        "Controllers": [{"FirmwarePackageVersion": "12.8.528.38"}],
        "Ports": [_port("1", mac=None, link="LinkDown", speed=None)],
        "NetworkDeviceFunctions": [{
            "Ethernet": {"PermanentMACAddress": "AA:BB:CC:DD:EE:FF"},
            "AssignablePhysicalPorts": [
                {"@odata.id": "/rest/server-hardware/1/networkAdapters/2/Ports/1"},
            ],
        }],
    }


class _FakeClient:
    def __init__(self, responses: dict[str, dict]):
        self._responses = responses

    async def get(self, uri):
        if uri not in self._responses:
            from proliant.oneview.client import OneViewError
            raise OneViewError(f"GET {uri} failed — HTTP 404: not found")
        return self._responses[uri]


async def test_fetch_network_report_data_inlined_mac():
    client = _FakeClient({
        "/rest/server-hardware/1/networkAdapters": {"data": [_adapter_inlined_mac()]},
    })
    report = await network_adapters.fetch_network_report_data(client, "/rest/server-hardware/1")

    assert len(report) == 1
    adapter = report[0]
    assert adapter["model"] == "Synergy 4820C 10/20/25Gb CNA"
    assert adapter["location"] == "Mezzanine Slot 3"
    assert adapter["firmware"] == "8.65.23"
    assert len(adapter["ports"]) == 2
    assert adapter["ports"][0]["mac"] == "22:00:a3:e0:00:08"
    assert adapter["ports"][0]["link_status"] == "Link Up"


async def test_fetch_network_report_data_ndf_mac_fallback():
    client = _FakeClient({
        "/rest/server-hardware/1/networkAdapters": {"data": [_adapter_ndf_fallback()]},
    })
    report = await network_adapters.fetch_network_report_data(client, "/rest/server-hardware/1")

    assert len(report) == 1
    port = report[0]["ports"][0]
    assert port["mac"] == "aa:bb:cc:dd:ee:ff"
    assert port["link_status"] == "No Link"


async def test_fetch_network_report_data_no_adapters_returns_empty():
    client = _FakeClient({"/rest/server-hardware/1/networkAdapters": {"data": []}})
    report = await network_adapters.fetch_network_report_data(client, "/rest/server-hardware/1")
    assert report == []


async def test_fetch_network_report_data_returns_empty_on_error():
    client = _FakeClient({})  # no matching URL -> client.get() raises OneViewError
    report = await network_adapters.fetch_network_report_data(client, "/rest/server-hardware/missing")
    assert report == []


async def test_fetch_network_list_row():
    client = _FakeClient({
        "/rest/server-hardware/1/networkAdapters": {"data": [_adapter_inlined_mac()]},
    })
    rows = await network_adapters.fetch_network_list_row(client, {"uri": "/rest/server-hardware/1"})
    row_dict = dict(rows)

    assert row_dict["Location"] == "Mezzanine Slot 3\nMezzanine Slot 3"
    assert row_dict["Port"] == "1\n2"
    assert row_dict["MAC"] == "22:00:a3:e0:00:08\n22:00:a3:e0:00:09"
    assert row_dict["Link Status"] == "Link Up\nLink Up"
