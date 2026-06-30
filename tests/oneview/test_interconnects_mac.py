"""Unit tests for MAC forwarding-table tunnel-network attribution."""

from __future__ import annotations

import pytest

from proliant.oneview import interconnects as ic


ENCL = "/rest/enclosures/e1"
TUN_URI = "/rest/ethernet-networks/tun"
IC6_URI = "/rest/interconnects/ic6"


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]]):
        self._collections = collections

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])


def _tunnel_collections() -> dict[str, list[dict]]:
    return {
        "/rest/uplink-sets": [
            {"name": "ACI-Tunnel", "ethernetNetworkType": "Tunnel",
             "networkUris": [TUN_URI],
             "portConfigInfos": [
                 {"location": {"locationEntries": [
                     {"type": "Port", "value": "Q6:1"},
                     {"type": "Enclosure", "value": ENCL},
                     {"type": "Bay", "value": "6"},
                 ]}},
             ]},
        ],
        "/rest/ethernet-networks": [
            {"uri": TUN_URI, "name": "ACI-Tunnel-Net",
             "ethernetNetworkType": "Tunnel"},
        ],
        "/rest/interconnects": [
            {"uri": IC6_URI, "interconnectLocation": {"locationEntries": [
                {"type": "Enclosure", "value": ENCL},
                {"type": "Bay", "value": "6"}]}},
        ],
    }


@pytest.mark.asyncio
async def test_build_tunnel_port_map():
    client = FakeClient(_tunnel_collections())
    tmap = await ic.build_tunnel_port_map(client)
    assert tmap == {(IC6_URI, "Q6:1"): "ACI-Tunnel-Net"}


def test_enrich_mac_entries_attributes_tunnel_network():
    # A MAC learned on the tunnel uplink (blank network, internal VLAN 4094)
    # is attributed to the tunnel network name.
    entries = [
        {"mac": "00:00:0c:07:ac:a0", "ic_uri": IC6_URI, "port": "Q6:1",
         "network": "", "net_uri": "", "vlan": 4094,
         "profile": "", "connection": ""},
    ]
    ic.enrich_mac_entries(entries, {}, {}, {}, {(IC6_URI, "Q6:1"): "ACI-Tunnel-Net"})
    assert entries[0]["network"] == "ACI-Tunnel-Net"


def test_enrich_mac_entries_keeps_named_network():
    # An entry that already has a named network is left untouched.
    entries = [
        {"mac": "aa:bb:cc:00:00:01", "ic_uri": IC6_URI, "port": "Q5:2",
         "network": "VLAN-160", "net_uri": "", "vlan": 160,
         "profile": "", "connection": ""},
    ]
    ic.enrich_mac_entries(entries, {}, {}, {}, {(IC6_URI, "Q6:1"): "ACI-Tunnel-Net"})
    assert entries[0]["network"] == "VLAN-160"
