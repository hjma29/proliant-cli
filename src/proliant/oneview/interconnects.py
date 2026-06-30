"""
proliant.oneview.interconnects
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Logical Interconnects (LI), Logical Interconnect Groups (LIG),
Interconnect hardware, and the MAC forwarding-information-base.

Key endpoints:
  GET /rest/logical-interconnects                   -> all LIs
  GET /rest/logical-interconnect-groups             -> all LIGs
  GET /rest/interconnects                           -> IC hardware
  GET {li_uri}/forwarding-information-base          -> MAC address table
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── Logical Interconnects (LI) ────────────────────────────────────────────────

def parse_li(raw: dict) -> dict:
    return {
        "name":        raw.get("name", ""),
        "consistency": raw.get("consistencyStatus", ""),
        "stacking":    raw.get("stackingHealth", ""),
        "state":       raw.get("state", ""),
        "status":      raw.get("status", ""),
        "lig_uri":     raw.get("logicalInterconnectGroupUri", ""),
        "uri":         raw.get("uri", ""),
    }


async def list_lis(client: "OneViewClient") -> list[dict]:
    raw_lis, raw_ligs = await asyncio.gather(
        client.get_all("/rest/logical-interconnects"),
        client.get_all("/rest/logical-interconnect-groups"),
    )
    lig_map = {lg["uri"]: lg.get("name", "") for lg in raw_ligs}
    lis = [parse_li(li) for li in raw_lis]
    for li in lis:
        li["lig_name"] = lig_map.get(li["lig_uri"], "")
    return sorted(lis, key=lambda li: li["name"])


# ── Logical Interconnect Groups (LIG) ────────────────────────────────────────

def parse_lig(raw: dict) -> dict:
    return {
        "name":   raw.get("name", ""),
        "state":  raw.get("state", ""),
        "status": raw.get("status", ""),
        "uri":    raw.get("uri", ""),
    }


async def list_ligs(client: "OneViewClient") -> list[dict]:
    raw = await client.get_all("/rest/logical-interconnect-groups")
    return sorted([parse_lig(lg) for lg in raw], key=lambda lg: lg["name"])


# ── Interconnect hardware ─────────────────────────────────────────────────────

def parse_ic(raw: dict) -> dict:
    return {
        "name":   raw.get("name", ""),
        "model":  raw.get("model", ""),
        "state":  raw.get("state", ""),
        "status": raw.get("status", ""),
        "serial": raw.get("serialNumber", ""),
        "li_uri": raw.get("logicalInterconnectUri", ""),
        "uri":    raw.get("uri", ""),
    }


async def list_interconnects(client: "OneViewClient") -> list[dict]:
    raw_ics, raw_lis = await asyncio.gather(
        client.get_all("/rest/interconnects"),
        client.get_all("/rest/logical-interconnects"),
    )
    li_map = {li["uri"]: li.get("name", "") for li in raw_lis}
    ics = [parse_ic(ic) for ic in raw_ics]
    for ic in ics:
        ic["li_name"] = li_map.get(ic["li_uri"], "")
    return sorted(ics, key=lambda ic: ic["name"])


# ── MAC address table ─────────────────────────────────────────────────────────

def parse_mac_entry(raw: dict) -> dict:
    return {
        "mac":        raw.get("macAddress", ""),
        "ic_name":    raw.get("interconnectName", ""),
        "ic_uri":     raw.get("interconnectUri", ""),
        "port":       raw.get("networkInterface", ""),
        "network":    raw.get("networkName", ""),
        "net_uri":    raw.get("networkUri", ""),
        "vlan":       raw.get("externalVlan", ""),
        "entry_type": raw.get("entryType", ""),
        "profile":    "",
        "connection": "",
        "internal_vlan": False,
    }


def _downlink_port_num(iface: str) -> int | None:
    """Return the interconnect downlink port number from a FIB port label.

    Downlink labels are server-facing, e.g. ``downlink 6:1-2`` → 6.  The
    leading number equals the ``interconnectPort`` of the server profile
    connection cabled to that downlink.  Uplink labels (``Q5:2``) → None.
    """
    s = (iface or "").strip().lower()
    if not s.startswith("downlink"):
        return None
    head = s[len("downlink"):].strip().split(":", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


async def build_profile_maps(
    client: "OneViewClient",
) -> tuple[
    dict[str, tuple[str, str]],
    dict[tuple[str, int], str],
    dict[tuple[str, int], list[tuple[str, str, frozenset]]],
]:
    """Build server-profile lookup maps for MAC table enrichment.

    Returns ``(mac_map, port_map, port_conns)``:
      * ``mac_map``  — ``mac.lower()`` → ``(profile_name, connection_name)``;
        exact match for a connection's assigned virtual MAC.
      * ``port_map`` — ``(interconnect_uri, interconnectPort)`` → ``profile_name``;
        resolves any MAC learned on a downlink to the owning server profile,
        even VM/guest MACs behind the server.
      * ``port_conns`` — ``(interconnect_uri, interconnectPort)`` → list of
        ``(connection_name, network_uri, netset_member_uris)``; used to pin the
        specific connection by matching the learned network/VLAN.
    """
    profiles, netsets = await asyncio.gather(
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/network-sets"),
    )
    ns_members = {
        ns["uri"]: frozenset(ns.get("networkUris") or [])
        for ns in netsets
        if ns.get("uri")
    }

    mac_map: dict[str, tuple[str, str]] = {}
    port_map: dict[tuple[str, int], str] = {}
    port_conns: dict[tuple[str, int], list[tuple[str, str, frozenset]]] = {}
    for p in profiles:
        pname = p.get("name", "") or ""
        cs = p.get("connectionSettings") or {}
        for c in cs.get("connections") or p.get("connections") or []:
            mac = (c.get("mac") or "").lower()
            cname = c.get("name", "") or ""
            if mac:
                mac_map[mac] = (pname, cname)
            ic = c.get("interconnectUri")
            port = c.get("interconnectPort")
            if ic and isinstance(port, int):
                key = (ic, port)
                port_map.setdefault(key, pname)
                nu = c.get("networkUri") or ""
                port_conns.setdefault(key, []).append(
                    (cname, nu, ns_members.get(nu, frozenset()))
                )
    return mac_map, port_map, port_conns


async def build_tunnel_port_map(
    client: "OneViewClient",
) -> dict[tuple[str, str], str]:
    """Map ``(interconnect_uri, uplink_port)`` → tunnel network name.

    Virtual Connect carries a *tunnel* network transparently and represents it
    with an internal VLAN (e.g. 4094), so MACs learned on a tunnel uplink come
    back from the FIB with a blank ``networkName`` and that internal VLAN.  This
    map lets the MAC table attribute such entries to the owning tunnel network.
    """
    uplinks, nets, ics = await asyncio.gather(
        client.get_all("/rest/uplink-sets"),
        client.get_all("/rest/ethernet-networks"),
        client.get_all("/rest/interconnects"),
    )
    net_type = {n["uri"]: n.get("ethernetNetworkType", "")
                for n in nets if n.get("uri")}
    net_name = {n["uri"]: n.get("name", "") for n in nets if n.get("uri")}

    ic_by_encl_bay: dict[tuple[str, str], str] = {}
    for ic in ics:
        entries = (ic.get("interconnectLocation") or {}).get("locationEntries", [])
        loc = {e.get("type"): e.get("value") for e in entries}
        if loc.get("Enclosure") and loc.get("Bay") and ic.get("uri"):
            ic_by_encl_bay[(loc["Enclosure"], loc["Bay"])] = ic["uri"]

    out: dict[tuple[str, str], str] = {}
    for u in uplinks:
        # A tunnel uplink set either declares the type itself or carries a
        # single Tunnel-type ethernet network.
        tunnel_name = ""
        if u.get("ethernetNetworkType") == "Tunnel":
            for nu in u.get("networkUris") or []:
                tunnel_name = net_name.get(nu, "")
                if tunnel_name:
                    break
        if not tunnel_name:
            for nu in u.get("networkUris") or []:
                if net_type.get(nu) == "Tunnel":
                    tunnel_name = net_name.get(nu, "")
                    break
        if not tunnel_name:
            continue
        for pci in u.get("portConfigInfos") or []:
            loc = {e.get("type"): e.get("value")
                   for e in (pci.get("location") or {}).get("locationEntries", [])}
            ic_uri = ic_by_encl_bay.get((loc.get("Enclosure", ""), loc.get("Bay", "")))
            port = loc.get("Port", "")
            if ic_uri and port:
                out[(ic_uri, port)] = tunnel_name
    return out


def _resolve_connection(
    net_uri: str,
    conns: list[tuple[str, str, frozenset]],
) -> str:
    """Pick the connection on a downlink that carries the learned network.

    Direct network match wins; falls back to network-set membership.  Returns
    "" when ambiguous or unmatched (e.g. the learned VLAN is on no connection).
    """
    if not net_uri:
        return ""
    for cname, nu, _members in conns:
        if nu and nu == net_uri:
            return cname
    for cname, _nu, members in conns:
        if net_uri in members:
            return cname
    return ""


def enrich_mac_entries(
    entries: list[dict],
    mac_map: dict[str, tuple[str, str]],
    port_map: dict[tuple[str, int], str],
    port_conns: dict[tuple[str, int], list[tuple[str, str, frozenset]]],
    tunnel_ports: dict[tuple[str, str], str] | None = None,
) -> None:
    """Populate ``profile`` / ``connection`` on each MAC entry, in place.

    Exact MAC match resolves both profile and connection.  Otherwise a downlink
    port maps to the owning server profile, and the connection is resolved by
    matching the entry's learned network against that profile's connections.

    Entries learned on a *tunnel* uplink come back with a blank network name
    (Virtual Connect uses an internal VLAN for tunnels); they are attributed to
    the owning tunnel network via ``tunnel_ports``.
    """
    tunnel_ports = tunnel_ports or {}
    for e in entries:
        if not e.get("network"):
            tname = tunnel_ports.get((e.get("ic_uri", ""), e.get("port", "")))
            if tname:
                e["network"] = tname
                e["internal_vlan"] = True
        mac = (e.get("mac") or "").lower()
        if mac in mac_map:
            e["profile"], e["connection"] = mac_map[mac]
            continue
        port_num = _downlink_port_num(e.get("port", ""))
        if port_num is None:
            continue
        key = (e.get("ic_uri", ""), port_num)
        prof = port_map.get(key)
        if prof:
            e["profile"] = prof
            e["connection"] = _resolve_connection(
                e.get("net_uri", ""), port_conns.get(key, [])
            )


async def get_mac_table(
    client: "OneViewClient",
    address: str = "",
    vlan: int = 0,
) -> list[dict]:
    """Query MAC forwarding-information-base across all active LIs.

    OneView returns the forwarding table per LI (Virtual Connect domain).
    Requires at least one of address or vlan to avoid pulling the full table.
    Entries are enriched with the owning server profile / connection name.
    """
    raw_lis = await client.get_all("/rest/logical-interconnects")
    # Only VC stacking LIs (NotApplicable = standalone/non-VC)
    active_lis = [li for li in raw_lis if li.get("stackingHealth", "") != "NotApplicable"]

    filters: list[str] = []
    if address:
        filters.append(f"macAddress='{address}'")
    if vlan:
        filters.append(f"externalVlan='{vlan}'")

    params = {"filter": filters} if filters else None

    results: list[dict] = []
    lock = asyncio.Lock()
    maps: dict[str, tuple] = {}

    async def _query_one(li: dict) -> None:
        uri = li.get("uri", "") + "/forwarding-information-base"
        data = await client.get(uri, params=params)
        entries = [parse_mac_entry(e) for e in data.get("members", [])]
        async with lock:
            results.extend(entries)

    async def _load_profiles() -> None:
        try:
            maps["v"] = await build_profile_maps(client)
        except Exception:
            maps["v"] = ({}, {}, {})

    async def _load_tunnels() -> None:
        try:
            maps["t"] = await build_tunnel_port_map(client)
        except Exception:
            maps["t"] = {}

    await asyncio.gather(
        _load_profiles(), _load_tunnels(), *[_query_one(li) for li in active_lis]
    )

    # Deduplicate
    seen: set[tuple] = set()
    unique: list[dict] = []
    for m in results:
        key = (m["mac"], m["ic_name"], m["vlan"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    mac_map, port_map, port_conns = maps.get("v", ({}, {}, {}))
    tunnel_ports = maps.get("t", {})
    enrich_mac_entries(unique, mac_map, port_map, port_conns, tunnel_ports)

    return sorted(unique, key=lambda m: (m["mac"], m["ic_name"]))
