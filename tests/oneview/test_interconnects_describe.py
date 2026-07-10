"""Unit tests for describe_interconnect() and its pure helper functions."""

from __future__ import annotations

import pytest

from proliant.oneview import interconnects as ic


# ── pure helpers ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Speed10G", "10"),
    ("Speed4x25G", "4x25"),
    ("Speed0M", ""),        # "no link negotiated", not a real 0 Gb/s
    ("SpeedUnknown", "unknown"),
    ("Auto", "unknown"),
    ("", ""),
    (None, ""),
])
def test_parse_port_speed(raw, expected):
    assert ic.parse_port_speed(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Mezz 3:2-a", "Mezzanine 3:2"),
    ("Mezz 3:2-e", "Mezzanine 3:2"),
    ("Mezz 1:1", "Mezzanine 1:1"),
    ("FlexLOM 1:1-a", "FlexLOM 1:1"),
    ("", ""),
])
def test_format_adapter_port(raw, expected):
    assert ic.format_adapter_port(raw) == expected


def test_connected_to_falls_back_to_chassis_and_port():
    # No LLDP system-name TLV advertised (e.g. plain switch/NIC) -- chassis ID stands in.
    neighbor = {"remoteChassisId": "b8:e9:24:8f:2c:62", "remotePortId": "swp1"}
    assert ic._connected_to(neighbor) == "b8:e9:24:8f:2c:62 (swp1)"


def test_connected_to_prefers_system_name_over_chassis_mac():
    # Real switch hostname (LLDP System Name) beats the chassis MAC OneView's GUI shows.
    neighbor = {
        "remoteChassisId": "78:0c:f0:7d:8a:c3",
        "remoteSystemName": "hst-acileaf-01",
        "remotePortId": "Eth1/5",
        "remotePortDescription": "topology/pod-1/protpaths-101-102/pathep-[leaf12-eth5_PolGrp]",
    }
    assert ic._connected_to(neighbor) == "hst-acileaf-01 (Eth1/5)"


def test_connected_to_empty_system_name_falls_back_to_chassis():
    # OneView returns "" (not absent) for neighbors with no system-name TLV
    # (e.g. Synergy-internal stacking links) -- must fall back, not show blank.
    neighbor = {"remoteChassisId": "7C9M3700FM", "remoteSystemName": "", "remotePortId": "Q7"}
    assert ic._connected_to(neighbor) == "7C9M3700FM (Q7)"


def test_connected_to_none_neighbor():
    assert ic._connected_to(None) == "none"


def test_uplink_port_visible_subport_always_shown():
    assert ic._uplink_port_visible({"portName": "Q1:1", "portStatusReason": "None"}) is True


def test_uplink_port_visible_hides_split_parent():
    # Q1 is populated+split into Q1:1..Q1:4 -- OneView hides the parent row.
    assert ic._uplink_port_visible({"portName": "Q1", "portStatusReason": "None"}) is False


def test_uplink_port_visible_shows_unpopulated_parent():
    # Q3 has no transceiver -- shown alongside its placeholder subports.
    assert ic._uplink_port_visible({"portName": "Q3", "portStatusReason": "Unpopulated"}) is True


@pytest.mark.parametrize("name,expected", [
    ("Q1", (1, -1)),
    ("Q1:1", (1, 1)),
    ("Q1:4", (1, 4)),
    ("Q10:2", (10, 2)),
    ("d1", (1, -1)),
    ("l4", (4, -1)),
])
def test_port_sort_key(name, expected):
    assert ic._port_sort_key(name) == expected


def test_component_version_for_product_matches_substring():
    components = [
        {"name": "HPE Virtual Connect SE 100Gb F32 Module for Synergy Firmware install package",
         "componentVersion": "2.6.0.1001"},
        {"name": "Some other component", "componentVersion": "9.9.9"},
    ]
    version = ic._component_version_for_product(components, "Virtual Connect SE 100Gb F32 Module for Synergy")
    assert version == "2.6.0.1001"


def test_component_version_for_product_no_match():
    assert ic._component_version_for_product([{"name": "Unrelated", "componentVersion": "1"}], "Nope") == ""


def test_resolve_baseline_uri_finds_owning_logical_enclosure():
    raw_les = [
        {"enclosureUris": ["/rest/enclosures/e1"], "firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/ssp1"}},
        {"enclosureUris": ["/rest/enclosures/e2"], "firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/ssp2"}},
    ]
    assert ic._resolve_baseline_uri("/rest/enclosures/e2", raw_les) == "/rest/firmware-drivers/ssp2"
    assert ic._resolve_baseline_uri("/rest/enclosures/unknown", raw_les) == ""


def test_build_downlink_map_keys_by_interconnect_and_port():
    raw_profiles = [
        {"name": "ocp-single-node", "serverHardwareUri": "/rest/server-hardware/hw6",
         "connectionSettings": {"connections": [
             {"interconnectUri": "/rest/interconnects/ic6", "interconnectPort": 6, "portId": "Mezz 3:2-a"},
         ]}},
    ]
    raw_server_hw = [{"uri": "/rest/server-hardware/hw6", "name": "Enclosure-01, bay 6"}]
    dmap = ic._build_downlink_map(raw_profiles, raw_server_hw)
    assert dmap[("/rest/interconnects/ic6", 6)] == {
        "server_profile": "ocp-single-node",
        "server_hardware": "Enclosure-01, bay 6",
        "adapter_port": "Mezzanine 3:2",
    }


# ── describe_interconnect() end-to-end (FakeClient) ───────────────────────────

IC_URI = "/rest/interconnects/ic6"
ENC_URI = "/rest/enclosures/e1"
LI_URI = "/rest/logical-interconnects/li1"
BASELINE_URI = "/rest/firmware-drivers/ssp1"
UPLINKSET_URI = "/rest/uplink-sets/us1"


class FakeClient:
    def __init__(self, collections: dict, singles: dict | None = None):
        self._collections = collections
        self._singles = singles or {}

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])

    async def get(self, uri: str, params=None) -> dict:
        return self._singles.get(uri, {})


def _base_collections() -> dict:
    return {
        "/rest/interconnects": [{
            "name": "Enclosure-01, interconnect 6",
            "uri": IC_URI,
            "status": "OK",
            "state": "Configured",
            "powerState": "On",
            "enclosureUri": ENC_URI,
            "enclosureName": "Enclosure-01",
            "logicalInterconnectUri": LI_URI,
            "productName": "Virtual Connect SE 100Gb F32 Module for Synergy",
            "firmwareVersion": "2.6.0.1001",
            "mgmtInterface": None,
            "stackingDomainId": 3,
            "stackingMemberId": 1,
            "stackingDomainRole": "Master",
            "hostName": "VC100-bay6",
            "interconnectMAC": "5C:ED:8C:74:80:60",
            "baseWWN": "10:00:00:11:0A:07:89:28",
            "serialNumber": "7C9M3700FM",
            "partNumber": "867796-B21",
            "sparePartNumber": "879438-001",
            "interconnectHardwareHealth": "Ok",
            "interconnectLocation": {"locationEntries": [
                {"type": "Enclosure", "value": ENC_URI}, {"type": "Bay", "value": "6"},
            ]},
            "ipAddressList": [
                {"ipAddressType": "Ipv6LinkLocal", "ipAddress": "fe80::5eed:8cff:fe74:8060"},
                {"ipAddressType": "Ipv4Dhcp", "ipAddress": "10.16.41.18"},
            ],
            "remoteSupport": {"supportState": "Disabled"},
            "ports": [
                {"portName": "l1", "portType": "Extension", "portStatus": "Unlinked", "neighbor": None},
                {"portName": "Q1", "portType": "Uplink", "portStatus": "Unlinked",
                 "portStatusReason": "None", "operationalSpeed": "Speed0M",
                 "associatedUplinkSetUri": None, "connectorType": None, "neighbor": None},
                {"portName": "Q1:1", "portType": "Uplink", "portStatus": "Unlinked",
                 "portStatusReason": "None", "operationalSpeed": "Speed0M",
                 "associatedUplinkSetUri": UPLINKSET_URI, "connectorType": "SFP-SR", "neighbor": None},
                {"portName": "Q3", "portType": "Uplink", "portStatus": "Unlinked",
                 "portStatusReason": "Unpopulated", "operationalSpeed": "Speed0M",
                 "associatedUplinkSetUri": None, "connectorType": None, "neighbor": None},
                {"portName": "d1", "portType": "Downlink", "portStatus": "Linked",
                 "operationalSpeed": "Speed25G",
                 "neighbor": {"remoteChassisId": "hw1", "remotePortId": "aa:bb"}},
                {"portName": "d7", "portType": "Downlink", "portStatus": "Linked",
                 "operationalSpeed": "Speed50G",
                 "neighbor": {"remoteChassisId": "hw7", "remotePortId": "cc:dd"}},
                {"portName": "d8", "portType": "Downlink", "portStatus": "Unlinked",
                 "operationalSpeed": "Speed0M", "neighbor": None},
            ],
        }],
        "/rest/logical-interconnects": [{"uri": LI_URI, "name": "LE01-LIG-VC100"}],
        "/rest/logical-enclosures": [{
            "enclosureUris": [ENC_URI],
            "firmware": {"firmwareBaselineUri": BASELINE_URI},
        }],
        "/rest/firmware-drivers": [{
            "uri": BASELINE_URI, "name": "HPE Synergy Service Pack", "version": "SY-2023.05.01",
            "fwComponents": [
                {"name": "HPE Virtual Connect SE 100Gb F32 Module for Synergy Firmware install package",
                 "componentVersion": "2.6.0.1001"},
            ],
        }],
        "/rest/uplink-sets": [{"uri": UPLINKSET_URI, "name": "pvlan-uplinkset", "networkType": "Ethernet"}],
        "/rest/server-profiles": [{
            "name": "aci-vc-tunnel-host1", "serverHardwareUri": "/rest/server-hardware/hw1",
            "connectionSettings": {"connections": [
                {"interconnectUri": IC_URI, "interconnectPort": 1, "portId": "Mezz 3:2-a"},
            ]},
        }],
        "/rest/server-hardware": [
            {"uri": "/rest/server-hardware/hw1", "name": "Enclosure-01, bay 1"},
            {"uri": "/rest/server-hardware/hw7", "name": "Enclosure-01, bay 7"},
        ],
    }


def _singles() -> dict:
    return {
        f"{IC_URI}/statistics": {"moduleStatistics": {"cpuUsage": "8", "memoryUsage": "21"}},
        f"{IC_URI}/utilization": {"metricList": [
            {"metricName": "Cpu", "metricSamples": [[[1, 5], [2, 6], [3, 4]]], "metricCapacity": 100},
            {"metricName": "Memory", "metricSamples": [[[1, 1680]]], "metricCapacity": 8000},
            {"metricName": "Temperature", "metricSamples": [[[1, 174]]], "metricCapacity": 212},
            {"metricName": "PowerAverageWatts", "metricSamples": [[[1, 155]]], "metricCapacity": 270},
        ]},
    }


@pytest.mark.asyncio
async def test_describe_interconnect_general_and_hardware():
    client = FakeClient(_base_collections(), _singles())
    d = await ic.describe_interconnect(client, "Enclosure-01, interconnect 6")

    assert d["name"] == "Enclosure-01, interconnect 6"
    g = d["general"]
    assert g["logical_interconnect"] == "LE01-LIG-VC100"
    assert g["power"] == "On"
    assert g["firmware_baseline_name"] == "HPE Synergy Service Pack SY-2023.05.01"
    assert g["firmware_version_from_baseline"] == "2.6.0.1001"
    assert g["installed_firmware_version"] == "2.6.0.1001"
    assert g["mgmt_interface"] == "none"
    assert g["ipv4"] == "10.16.41.18" and g["ipv4_type"] == "DHCP"
    assert g["ipv6"] == "fe80::5eed:8cff:fe74:8060" and g["ipv6_type"] == "link-local"

    h = d["hardware"]
    assert h["product_name"] == "Virtual Connect SE 100Gb F32 Module for Synergy"
    assert h["location"] == "Enclosure-01, interconnect bay 6"
    assert h["health"] == "Ok"


@pytest.mark.asyncio
async def test_describe_interconnect_hides_split_uplink_parent_shows_unpopulated():
    client = FakeClient(_base_collections(), _singles())
    d = await ic.describe_interconnect(client, "Enclosure-01, interconnect 6")

    uplink_names = [u["port"] for u in d["uplink_ports"]]
    assert "Q1" not in uplink_names   # populated+split parent hidden
    assert "Q1:1" in uplink_names     # subport shown
    assert "Q3" in uplink_names       # unpopulated parent shown

    q1_1 = next(u for u in d["uplink_ports"] if u["port"] == "Q1:1")
    assert q1_1["type"] == "Ethernet"
    assert q1_1["uplink_set"] == "pvlan-uplinkset"
    assert q1_1["speed"] == ""  # Speed0M -> unlinked, blank not "0"


@pytest.mark.asyncio
async def test_describe_interconnect_downlink_uses_neighbor_over_profile_gap():
    client = FakeClient(_base_collections(), _singles())
    d = await ic.describe_interconnect(client, "Enclosure-01, interconnect 6")

    by_port = {p["port"]: p for p in d["downlink_ports"]}
    # d1 has both a neighbor AND a profile connection -- agree.
    assert by_port["1"]["server_hardware"] == "Enclosure-01, bay 1"
    assert by_port["1"]["adapter_port"] == "Mezzanine 3:2"
    assert by_port["1"]["server_profile"] == "aci-vc-tunnel-host1"
    # d7 has a neighbor but NO profile connection -- physical wiring still resolves hardware.
    assert by_port["7"]["server_hardware"] == "Enclosure-01, bay 7"
    assert by_port["7"]["adapter_port"] == ""
    assert by_port["7"]["server_profile"] == ""
    assert by_port["7"]["speed"] == "50"
    # d8 unlinked, no neighbor, no profile -- all blank.
    assert by_port["8"]["server_hardware"] == ""
    assert by_port["8"]["speed"] == ""


@pytest.mark.asyncio
async def test_describe_interconnect_utilization_and_remote_support():
    client = FakeClient(_base_collections(), _singles())
    d = await ic.describe_interconnect(client, "Enclosure-01, interconnect 6")

    u = d["utilization"]
    assert u["cpu_pct"] == 5.0
    assert u["memory_used_mb"] == 1680
    assert u["memory_capacity_mb"] == 8000
    assert u["memory_pct"] == 21
    assert u["power_avg_w"] == 155
    assert u["power_capacity_w"] == 270
    assert u["temperature_f"] == 174

    assert d["remote_support"] == {"enabled": False, "state": "Disabled"}


@pytest.mark.asyncio
async def test_describe_interconnect_not_found_lists_known_names():
    client = FakeClient(_base_collections(), _singles())
    with pytest.raises(ValueError, match="Known: Enclosure-01, interconnect 6"):
        await ic.describe_interconnect(client, "no such interconnect")
