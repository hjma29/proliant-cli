"""Unit tests for OneView end-to-end topology maps."""

from __future__ import annotations

import pytest

from proliant.oneview import topology


NET_URI = "/rest/ethernet-networks/vlan160"
NS_URI = "/rest/network-sets/set1"
IC3_URI = "/rest/interconnects/ic3"
IC6_URI = "/rest/interconnects/ic6"
ENCL = "/rest/enclosures/encl1"
HW3_URI = "/rest/server-hardware/hw3"


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]]):
        self._collections = collections

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])


def _fabric_collections() -> dict[str, list[dict]]:
    return {
        "/rest/ethernet-networks": [
            {"uri": NET_URI, "name": "VLAN-160", "vlanId": 160,
             "ethernetNetworkType": "Tagged"},
        ],
        "/rest/network-sets": [
            {"uri": NS_URI, "name": "set1", "networkUris": [NET_URI]},
        ],
        "/rest/uplink-sets": [
            {"name": "ACI-MAP", "networkUris": [NET_URI],
             "logicalInterconnectUri": "/rest/logical-interconnects/li1",
             "portConfigInfos": [
                 {"location": {"locationEntries": [
                     {"type": "Port", "value": "Q5:2"},
                     {"type": "Enclosure", "value": ENCL},
                     {"type": "Bay", "value": "3"},
                 ]}},
             ]},
        ],
        "/rest/interconnects": [
            {"uri": IC3_URI, "name": "Enclosure-01, interconnect 3",
             "interconnectLocation": {"locationEntries": [
                 {"type": "Enclosure", "value": ENCL}, {"type": "Bay", "value": "3"}]}},
            {"uri": IC6_URI, "name": "Enclosure-01, interconnect 6",
             "interconnectLocation": {"locationEntries": [
                 {"type": "Enclosure", "value": ENCL}, {"type": "Bay", "value": "6"}]}},
        ],
        "/rest/server-hardware": [
            {"uri": HW3_URI, "name": "Enclosure-01, bay 3", "position": 3},
        ],
        "/rest/logical-interconnects": [
            {"uri": "/rest/logical-interconnects/li1", "name": "LE01-LIG-VC100",
             "stackingHealth": "OK"},
        ],
        "/rest/server-profiles": [
            {"name": "aci-FM-host1", "serverHardwareUri": HW3_URI,
             "connectionSettings": {"connections": [
                 {"name": "mgmt1", "portId": "Mezz 3:1-a", "mac": "AA:BB:CC:00:00:01",
                  "networkUri": NET_URI, "interconnectUri": IC3_URI, "interconnectPort": 3},
                 {"name": "trunk", "portId": "Mezz 3:1-d", "mac": "AA:BB:CC:00:00:02",
                  "networkUri": NS_URI, "interconnectUri": IC6_URI, "interconnectPort": 3},
             ]}},
        ],
    }


@pytest.mark.asyncio
async def test_build_network_map_resolves_uplinks_and_servers():
    client = FakeClient(_fabric_collections())
    m = await topology.build_network_map(client, "VLAN-160")

    assert m["network"] == {"name": "VLAN-160", "vlan": 160,
                            "type": "Tagged", "uri": NET_URI}

    # Uplink port resolves to the interconnect by (enclosure, bay)
    assert len(m["uplinks"]) == 1
    up = m["uplinks"][0]
    assert up["uplink_set"] == "ACI-MAP"
    assert up["li_name"] == "LE01-LIG-VC100"
    assert up["ports"] == [
        {"ic_name": "Enclosure-01, interconnect 3", "bay": "3", "port": "Q5:2"},
    ]

    # Server connections: direct network + network-set membership both included
    assert len(m["servers"]) == 1
    srv = m["servers"][0]
    assert srv["profile"] == "aci-FM-host1"
    assert srv["server_name"] == "Enclosure-01, bay 3"
    assert srv["bay"] == 3
    conns = {c["name"]: c for c in srv["connections"]}
    assert set(conns) == {"mgmt1", "trunk"}
    # downlink number equals interconnectPort
    assert conns["mgmt1"]["downlink"] == 3
    assert conns["mgmt1"]["ic_name"] == "Enclosure-01, interconnect 3"
    # network-set connection resolved via membership
    assert conns["trunk"]["ic_name"] == "Enclosure-01, interconnect 6"


@pytest.mark.asyncio
async def test_build_network_map_unknown_network_raises():
    client = FakeClient(_fabric_collections())
    with pytest.raises(ValueError, match="Network 'nope' not found"):
        await topology.build_network_map(client, "nope")


def test_render_network_map_returns_tree():
    m = {
        "network": {"name": "VLAN-160", "vlan": 160, "type": "Tagged", "uri": NET_URI},
        "uplinks": [],
        "servers": [],
    }
    tree = topology.render_network_map(m)
    # internal network + no servers render the empty-state leaves
    assert tree.label.startswith("[bold cyan]VLAN-160")
    assert len(tree.children) == 2
