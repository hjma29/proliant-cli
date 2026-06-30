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
    def __init__(self, collections: dict[str, list[dict]],
                 fib: dict[str, list[dict]] | None = None):
        self._collections = collections
        self._fib = fib or {}

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])

    async def get(self, uri: str, params=None) -> dict:
        return {"members": self._fib.get(uri, [])}


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
             "nativeNetworkUri": NET_URI,
             "networkSetUris": [NS_URI],
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
                 {"type": "Enclosure", "value": ENCL}, {"type": "Bay", "value": "3"}]},
             "ports": [
                 {"portName": "Q5:2", "neighbor": {
                     "remoteSystemName": "aci-leaf-101", "remotePortId": "Eth1/5"}},
             ]},
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
        {"ic_name": "Enclosure-01, interconnect 3", "ic_uri": IC3_URI,
         "bay": "3", "port": "Q5:2",
         "neighbor_switch": "aci-leaf-101", "neighbor_port": "Eth1/5",
         "highlight": False},
    ]
    # uplink set networks carry vlan + native flag; the network-set is resolved
    assert up["networks"] == [
        {"name": "VLAN-160", "vlan": 160, "native": True},
    ]
    assert up["network_sets"] == ["set1"]

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


@pytest.mark.asyncio
async def test_build_network_map_by_vlan():
    client = FakeClient(_fabric_collections())
    m = await topology.build_network_map(client, vlan=160)
    assert m["network"] == {"name": "VLAN-160", "vlan": 160,
                            "type": "Tagged", "uri": NET_URI}


@pytest.mark.asyncio
async def test_build_network_map_unknown_vlan_raises():
    client = FakeClient(_fabric_collections())
    with pytest.raises(ValueError, match="No network found for VLAN 999"):
        await topology.build_network_map(client, vlan=999)


@pytest.mark.asyncio
async def test_trace_mac_highlights_uplink_port():
    # A MAC learned on uplink port Q5:2 of interconnect 3 lives upstream —
    # the matching uplink port should be highlighted, not a server downlink.
    fib = {
        "/rest/logical-interconnects/li1/forwarding-information-base": [
            {"macAddress": "00:2a:6a:13:c3:c1", "networkUri": NET_URI,
             "networkInterface": "Q5:2", "interconnectUri": IC3_URI,
             "entryType": "Learned"},
        ],
    }
    client = FakeClient(_fabric_collections(), fib=fib)
    maps = await topology.trace_mac(client, "00:2a:6a:13:c3:c1")

    assert len(maps) == 1
    nm = maps[0]
    ports = nm["uplinks"][0]["ports"]
    hl = [p for p in ports if p["highlight"]]
    assert len(hl) == 1
    assert hl[0]["port"] == "Q5:2"
    assert hl[0]["ic_uri"] == IC3_URI
    # no server connection should be marked for an uplink-learned MAC
    assert not any(c["highlight"] for s in nm["servers"] for c in s["connections"])
    # the diagram surfaces the highlight on the uplink port line
    out = topology.render_network_map_ascii(nm, mac="00:2a:6a:13:c3:c1")
    assert "Q5:2  →  aci-leaf-101 Eth1/5  ◀ Learned from" in out


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


def _diagram_map() -> dict:
    return {
        "network": {"name": "vlan-160", "vlan": 160, "type": "Tagged", "uri": NET_URI},
        "uplinks": [{"uplink_set": "uplink-set-name", "li_name": "LI1",
                     "ports": [{"ic_name": "Enclosure-01, interconnect 3",
                                "bay": "3", "port": "Q5:2",
                                "neighbor_switch": "aci-leaf-101",
                                "neighbor_port": "Eth1/5"},
                               {"ic_name": "Enclosure-01, interconnect 6",
                                "bay": "6", "port": "Q5:2",
                                "neighbor_switch": "", "neighbor_port": ""}],
                     "networks": [{"name": "vlan-160", "vlan": 160, "native": True},
                                  {"name": "other-network", "vlan": 161,
                                   "native": False}],
                     "network_sets": ["network-set-for-FM"]}],
        "servers": [
            {"profile": "server-profile-name-1", "server_name": "", "bay": 3,
             "connections": [{"name": "mgmt", "port_id": "", "mac": "",
                              "ic_name": "Enclosure-01, interconnect 6", "ic_bay": "6",
                              "downlink": 3, "highlight": False}]},
            {"profile": "server-profile-name-2", "server_name": "", "bay": 4,
             "connections": [{"name": "mgmt", "port_id": "", "mac": "",
                              "ic_name": "Enclosure-01, interconnect 6", "ic_bay": "6",
                              "downlink": 4, "highlight": True}]},
        ],
    }


def test_render_network_map_ascii_draws_boxes():
    out = topology.render_network_map_ascii(_diagram_map(), mac="AA:BB:CC:00:00:04")
    # header + both profile boxes appear
    assert "vlan-160" in out
    assert "server-profile-name-1" in out
    assert "server-profile-name-2" in out
    # box-drawing characters and the uplink/network annotations are present
    assert "┌" in out and "└" in out and "├" in out
    assert "uplink ports:" in out
    assert "vc-3 Q5:2" in out
    assert "vc-6 Q5:2" in out
    assert "networks:" in out
    # LLDP neighbor shown beside the uplink port
    assert "aci-leaf-101 Eth1/5" in out
    # one network per line with vlan + native tag
    assert "vlan-160  (vlan 160, native)" in out
    assert "other-network  (vlan 161)" in out
    # network set is shown when the uplink set uses one
    assert "network set:  network-set-for-FM" in out
    assert "uplinkset: uplink-set-name" in out
    assert "Logical Interconnect: LI1" in out
    # per-connection labels folded inside the profile boxes
    assert "mgmt  ·  vlan-160" in out
    # the highlighted (MAC-owning) connection is marked
    assert "◀ Learned from" in out
    # plain text only — no Rich markup leaks into the diagram
    assert "[bold" not in out


def test_render_network_map_ascii_mac_on_uplink_hides_servers():
    m = {
        "network": {"name": "VLAN-160", "vlan": 160, "type": "Tagged", "uri": NET_URI},
        "mac": "00:2a:6a:13:c3:c1", "mac_on_uplink": True, "learned_on": "Q5:2",
        "uplinks": [{"uplink_set": "ACI-MAP", "li_name": "LI1",
                     "ports": [{"bay": "3", "port": "Q5:2", "ic_uri": "i3",
                                "neighbor_switch": "leaf-01", "neighbor_port": "Eth1/6",
                                "highlight": False},
                               {"bay": "6", "port": "Q5:2", "ic_uri": "i6",
                                "neighbor_switch": "leaf-02", "neighbor_port": "Eth1/6",
                                "highlight": True}],
                     "networks": [{"name": "VLAN-160", "vlan": 160, "native": True}],
                     "network_sets": []}],
        "servers": [
            {"profile": "should-not-appear", "server_name": "", "bay": 3,
             "connections": [{"name": "mgmt", "mac": "", "highlight": False}]},
        ],
    }
    out = topology.render_network_map_ascii(m, mac="00:2a:6a:13:c3:c1")
    # MAC leads the header; the upstream port is the highlighted filter
    assert out.splitlines()[0].startswith("MAC 00:2a:6a:13:c3:c1")
    assert "vc-6 Q5:2  →  leaf-02 Eth1/6  ◀ Learned from" in out
    # server profiles are suppressed for an upstream-learned MAC
    assert "should-not-appear" not in out
    assert "MAC learned upstream" in out


def test_render_network_map_ascii_no_servers():
    m = {
        "network": {"name": "vlan-160", "vlan": 160, "type": "Tagged", "uri": NET_URI},
        "uplinks": [],
        "servers": [],
    }
    out = topology.render_network_map_ascii(m)
    assert "vlan-160" in out
    assert "no server profile connection" in out
