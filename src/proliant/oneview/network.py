"""
proliant.oneview.network
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
    from proliant.oneview.client import OneViewClient


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


# ── describe helpers ──────────────────────────────────────────────────────────

async def describe_uplink_set(client: "OneViewClient", name: str) -> dict:
    """Return full detail for a single uplink set, with resolved names."""
    raw_uplinks, raw_nets, raw_lis = await asyncio.gather(
        client.get_all("/rest/uplink-sets"),
        client.get_all("/rest/ethernet-networks"),
        client.get_all("/rest/logical-interconnects"),
    )
    matched = [u for u in raw_uplinks if u.get("name", "").lower() == name.lower()]
    if not matched:
        known = ", ".join(u.get("name", "") for u in raw_uplinks)
        raise ValueError(f"Uplink set '{name}' not found. Known: {known}")
    u = matched[0]

    net_map = {n["uri"]: n for n in raw_nets}
    li_map  = {li["uri"]: li.get("name", "") for li in raw_lis}

    # Resolve ports
    ports = []
    for p in u.get("portConfigInfos", []):
        entries = p.get("location", {}).get("locationEntries", [])
        bay  = next((e["value"] for e in entries if e["type"] == "Bay"), "")
        port = next((e["value"] for e in entries if e["type"] == "Port"), "")
        ports.append({
            "bay":   bay,
            "port":  port,
            "speed": p.get("desiredSpeed", ""),
            "fec":   p.get("desiredFecMode", ""),
        })

    # Resolve member networks
    networks = []
    for uri in u.get("networkUris", []):
        n = net_map.get(uri, {})
        networks.append({
            "name":   n.get("name", uri.rsplit("/", 1)[-1]),
            "vlan":   n.get("vlanId", 0),
            "type":   n.get("ethernetNetworkType", ""),
            "status": n.get("status", ""),
        })

    return {
        "name":         u.get("name", ""),
        "li_name":      li_map.get(u.get("logicalInterconnectUri", ""), ""),
        "network_type": u.get("networkType", ""),
        "conn_mode":    u.get("connectionMode", ""),
        "reachability": u.get("reachability", ""),
        "status":       u.get("status", ""),
        "state":        u.get("state", ""),
        "ports":        ports,
        "networks":     networks,
    }


async def describe_network_set(client: "OneViewClient", name: str) -> dict:
    """Return full detail for a single network set, with resolved network info."""
    raw_sets, raw_nets = await asyncio.gather(
        client.get_all("/rest/network-sets"),
        client.get_all("/rest/ethernet-networks"),
    )
    matched = [s for s in raw_sets if s.get("name", "").lower() == name.lower()]
    if not matched:
        known = ", ".join(s.get("name", "") for s in raw_sets)
        raise ValueError(f"Network set '{name}' not found. Known: {known}")
    s = matched[0]

    net_map = {n["uri"]: n for n in raw_nets}
    native_uri = s.get("nativeNetworkUri") or ""

    networks = []
    for uri in s.get("networkUris", []):
        n = net_map.get(uri, {})
        networks.append({
            "name":    n.get("name", uri.rsplit("/", 1)[-1]),
            "vlan":    n.get("vlanId", 0),
            "type":    n.get("ethernetNetworkType", ""),
            "purpose": n.get("purpose", ""),
            "status":  n.get("status", ""),
            "native":  uri == native_uri,
        })
    networks.sort(key=lambda n: n["name"])

    return {
        "name":           s.get("name", ""),
        "type":           s.get("networkSetType", ""),
        "status":         s.get("status", ""),
        "state":          s.get("state", ""),
        "native_network": net_map.get(native_uri, {}).get("name", "") if native_uri else "",
        "networks":       networks,
    }
