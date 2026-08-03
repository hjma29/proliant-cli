"""Tests for OneView server hardware normalization."""

from __future__ import annotations

from proliant.oneview.servers import get_fleet_memory, get_server, parse_server


def test_parse_server_uses_mp_host_info_ip_and_compacts_synergy_model():
    result = parse_server({
        "name": "Enclosure-01, bay 1",
        "model": "Synergy 480 Gen10",
        "serialNumber": "MXQ1240F2Q",
        "mpModel": "iLO5",
        "mpFirmwareVersion": "2.81 Mar 07 2023",
        "mpHostInfo": {
            "mpIpAddresses": [
                {"address": "fe80::1602:ecff:fe44:bd50", "type": "LinkLocal"},
                {"address": "10.16.41.9", "type": "DHCP"},
            ],
        },
        "powerState": "On",
        "state": "ProfileApplied",
        "serverProfileUri": "/rest/server-profiles/profile1",
        "uri": "/rest/server-hardware/server1",
        "position": 1,
    })

    assert result["model"] == "480 Gen10"
    assert result["ilo_ip"] == "10.16.41.9"


def test_parse_server_extracts_status():
    result = parse_server({"name": "Enclosure-01, bay 1", "status": "Critical"})
    assert result["status"] == "Critical"


class _FakeClient:
    def __init__(self, items: list[dict]):
        self._items = items

    async def get_all(self, _path):
        return self._items


async def test_get_server_tolerates_missing_space_after_comma():
    client = _FakeClient([{"name": "Enclosure-01, bay 7", "uri": "/rest/server-hardware/7"}])
    server = await get_server(client, "Enclosure-01,bay 7")
    assert server["uri"] == "/rest/server-hardware/7"


def _dimm(part_number="P03051-091", capacity_mib=16384, status="Good"):
    return {
        "CapacityMiB": capacity_mib,
        "PartNumber": part_number,
        "Manufacturer": "SK Hynix",
        "BaseModuleType": "RDIMM",
        "Oem": {
            "Hpe": {
                "DIMMStatus": status,
                "PartNumber": part_number,
                "VendorName": "SK Hynix",
                "MaxOperatingSpeedMTs": 2933,
            },
        },
    }


class _FakeMemoryClient:
    """Fake client for get_fleet_memory: multiple get_all() paths + per-server get()."""

    def __init__(self, servers: list[dict], profiles: list[dict], memory_by_uri: dict[str, dict]):
        self._paths = {
            "/rest/server-hardware": servers,
            "/rest/server-profiles": profiles,
        }
        self._memory_by_uri = memory_by_uri

    async def get_all(self, path):
        return self._paths[path]

    async def get(self, path):
        uri = path.removesuffix("/memory")
        return self._memory_by_uri[uri]


async def test_get_fleet_memory_prefers_server_profile_name():
    client = _FakeMemoryClient(
        servers=[{
            "name": "Enclosure-01, bay 7",
            "uri": "/rest/server-hardware/7",
            "serverProfileUri": "/rest/server-profiles/p7",
            "serverName": "localhost",
        }],
        profiles=[{"uri": "/rest/server-profiles/p7", "name": "bay7-6820-cna"}],
        memory_by_uri={"/rest/server-hardware/7": {"data": [_dimm()]}},
    )
    dimms = await get_fleet_memory(client)
    assert len(dimms) == 1
    assert dimms[0]["server"] == "bay7-6820-cna"


async def test_get_fleet_memory_includes_vendor_part_number():
    client = _FakeMemoryClient(
        servers=[{
            "name": "Enclosure-01, bay 7",
            "uri": "/rest/server-hardware/7",
            "serverProfileUri": "",
            "serverName": "",
        }],
        profiles=[],
        memory_by_uri={"/rest/server-hardware/7": {"data": [_dimm(part_number="HMA82GR7CJR4N-WM  ")]}},
    )
    dimms = await get_fleet_memory(client)
    assert len(dimms) == 1
    # Top-level Redfish PartNumber is the raw vendor P/N; trimmed of padding.
    assert dimms[0]["vendor_pn"] == "HMA82GR7CJR4N-WM"


async def test_get_fleet_memory_falls_back_to_hardware_bay_name_when_no_profile():
    client = _FakeMemoryClient(
        servers=[{
            "name": "Enclosure-01, bay 6",
            "uri": "/rest/server-hardware/6",
            "serverProfileUri": "",
            "serverName": "",
        }],
        profiles=[],
        memory_by_uri={"/rest/server-hardware/6": {"data": [_dimm()]}},
    )
    dimms = await get_fleet_memory(client)
    assert len(dimms) == 1
    assert dimms[0]["server"] == "Enclosure-01, bay 6"


async def test_get_fleet_memory_includes_dimm_status():
    client = _FakeMemoryClient(
        servers=[{
            "name": "Enclosure-01, bay 7",
            "uri": "/rest/server-hardware/7",
            "serverProfileUri": "",
            "serverName": "",
        }],
        profiles=[],
        memory_by_uri={"/rest/server-hardware/7": {"data": [_dimm(status="Degraded")]}},
    )
    dimms = await get_fleet_memory(client)
    assert len(dimms) == 1
    assert dimms[0]["status"] == "Degraded"
