"""
pcli.oneview.network
~~~~~~~~~~~~~~~~~~~~~
Ethernet networks, network sets, and uplink sets from HPE OneView.

Key endpoints:
  GET /rest/ethernet-networks        → all ethernet networks
  GET /rest/network-sets             → all network sets
  GET /rest/uplink-sets              → all uplink sets
  GET /rest/logical-interconnects    → for resolving LI names in uplink sets
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcli.oneview.client import OneViewClient


# ── ethernet networks ─────────────────────────────────────────────────────────

def parse_network(raw: dict) -> dict:
    return {
        "name":       raw.get("name", ""),
        "vlan":       raw.get("vlanId", 0),
        "type":       raw.get("ethernetNetworkType", ""),
        "purpose":    raw.get("purpose", ""),
        "status":     raw.get("status", ""),
        "state":      raw.get("state", ""),
        "smart_link": raw.get("smartLink", False),
        "private":    raw.get("privateNetwork", False),
        "uri":        raw.get("uri", ""),
    }


async def list_networks(client: "OneViewClient") -> list[dict]:
    raw = await client.get_all("/rest/ethernet-networks")
    return sorted([parse_network(n) for n in raw], key=lambda n: n["name"])


# ── network sets ──────────────────────────────────────────────────────────────

def parse_network_set(raw: dict, net_map: dict[str, str]) -> dict:
    native_uri = raw.get("nativeNetworkUri") or ""
    network_uris = raw.get("networkUris", [])
    return {
        "name":           raw.get("name", ""),
        "type":           raw.get("networkSetType", ""),
        "num_networks":   len(network_uris),
        "native_network": net_map.get(native_uri, ""),
        "status":         raw.get("status", ""),
        "state":          raw.get("state", ""),
        "uri":            raw.get("uri", ""),
    }


async def list_network_sets(client: "OneViewClient") -> list[dict]:
    raw_sets, raw_nets = await asyncio.gather(
        client.get_all("/rest/network-sets"),
        client.get_all("/rest/ethernet-networks"),
    )
    net_map = {n["uri"]: n.get("name", "") for n in raw_nets}
    return sorted(
        [parse_network_set(s, net_map) for s in raw_sets],
        key=lambda s: s["name"],
    )


# ── uplink sets ───────────────────────────────────────────────────────────────

def _ports_summary(port_config_infos: list[dict]) -> str:
    """Return compact port list e.g. 'Bay3:Q1:1, Bay6:Q1:1'."""
    parts = []
    for p in port_config_infos:
        entries = p.get("location", {}).get("locationEntries", [])
        bay = next((e["value"] for e in entries if e["type"] == "Bay"), "")
        port = next((e["value"] for e in entries if e["type"] == "Port"), "")
        if bay and port:
            parts.append(f"Bay{bay}:{port}")
    return ", ".join(parts)


def parse_uplink_set(raw: dict, li_map: dict[str, str]) -> dict:
    li_uri = raw.get("logicalInterconnectUri", "")
    return {
        "name":           raw.get("name", ""),
        "network_type":   raw.get("networkType", ""),
        "conn_mode":      raw.get("connectionMode", ""),
        "reachability":   raw.get("reachability", ""),
        "num_networks":   len(raw.get("networkUris", [])),
        "ports":          _ports_summary(raw.get("portConfigInfos", [])),
        "li_name":        li_map.get(li_uri, li_uri.rsplit("/", 1)[-1]),
        "status":         raw.get("status", ""),
        "state":          raw.get("state", ""),
        "uri":            raw.get("uri", ""),
    }


async def list_uplink_sets(client: "OneViewClient") -> list[dict]:
    raw_uplinks, raw_lis = await asyncio.gather(
        client.get_all("/rest/uplink-sets"),
        client.get_all("/rest/logical-interconnects"),
    )
    li_map = {li["uri"]: li.get("name", "") for li in raw_lis}
    return sorted(
        [parse_uplink_set(u, li_map) for u in raw_uplinks],
        key=lambda u: u["name"],
    )
