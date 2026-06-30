"""Unit tests for tunnel/internal VLAN resolution in network listing."""

from __future__ import annotations

import pytest

from proliant.oneview import network


TUN_URI = "/rest/ethernet-networks/span-tunnel"
NET_URI = "/rest/ethernet-networks/vlan160"
LI_URI = "/rest/logical-interconnects/li1"


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]]):
        self._collections = collections

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])


def _collections() -> dict[str, list[dict]]:
    return {
        "/rest/ethernet-networks": [
            {"name": "VLAN-160", "uri": NET_URI, "vlanId": 160,
             "ethernetNetworkType": "Tagged"},
            {"name": "span-tunnel", "uri": TUN_URI, "vlanId": 0,
             "ethernetNetworkType": "Tunnel"},
        ],
        "/rest/logical-interconnects": [
            {"uri": LI_URI, "name": "LE01-LIG"},
        ],
        LI_URI + "/internalVlans": [
            {"type": "internal-vlan-association",
             "generalNetworkUri": TUN_URI, "internalVlanId": 4093,
             "logicalInterconnectUri": LI_URI},
            # -1 means VC has not assigned an internal VLAN — must be ignored
            {"type": "internal-vlan-association",
             "generalNetworkUri": NET_URI, "internalVlanId": -1,
             "logicalInterconnectUri": LI_URI},
        ],
    }


@pytest.mark.asyncio
async def test_get_internal_vlans_maps_tunnel():
    client = FakeClient(_collections())
    mapping = await network.get_internal_vlans(client)
    assert mapping == {TUN_URI: 4093}


@pytest.mark.asyncio
async def test_list_networks_attaches_internal_vlan():
    client = FakeClient(_collections())
    nets = await network.list_networks(client)
    by_name = {n["name"]: n for n in nets}
    assert by_name["span-tunnel"]["vlan"] == 0
    assert by_name["span-tunnel"]["internal_vlan"] == 4093
    assert by_name["VLAN-160"]["internal_vlan"] == 0


@pytest.mark.asyncio
async def test_get_internal_vlans_survives_endpoint_error():
    cols = _collections()
    cols.pop(LI_URI + "/internalVlans")

    class Boom(FakeClient):
        async def get_all(self, uri: str):
            if uri.endswith("/internalVlans"):
                raise RuntimeError("not supported")
            return await super().get_all(uri)

    mapping = await network.get_internal_vlans(Boom(cols))
    assert mapping == {}
