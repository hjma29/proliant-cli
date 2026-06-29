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
        "vlan":       raw.get("externalVlan", ""),
        "entry_type": raw.get("entryType", ""),
        "profile":    "",
        "connection": "",
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
) -> tuple[dict[str, tuple[str, str]], dict[tuple[str, int], str]]:
    """Build server-profile lookup maps for MAC table enrichment.

    Returns ``(mac_map, port_map)``:
      * ``mac_map``  — ``mac.lower()`` → ``(profile_name, connection_name)``;
        exact match for a connection's assigned virtual MAC.
      * ``port_map`` — ``(interconnect_uri, interconnectPort)`` → ``profile_name``;
        resolves any MAC learned on a downlink to the owning server profile,
        even VM/guest MACs behind the server.
    """
    profiles = await client.get_all("/rest/server-profiles")
    mac_map: dict[str, tuple[str, str]] = {}
    port_map: dict[tuple[str, int], str] = {}
    for p in profiles:
        pname = p.get("name", "") or ""
        cs = p.get("connectionSettings") or {}
        for c in cs.get("connections") or p.get("connections") or []:
            mac = (c.get("mac") or "").lower()
            if mac:
                mac_map[mac] = (pname, c.get("name", "") or "")
            ic = c.get("interconnectUri")
            port = c.get("interconnectPort")
            if ic and isinstance(port, int):
                port_map[(ic, port)] = pname
    return mac_map, port_map


def enrich_mac_entries(
    entries: list[dict],
    mac_map: dict[str, tuple[str, str]],
    port_map: dict[tuple[str, int], str],
) -> None:
    """Populate ``profile`` / ``connection`` on each MAC entry, in place.

    Exact MAC match resolves both profile and connection.  Otherwise a
    downlink port maps to the owning server profile (connection unknown).
    """
    for e in entries:
        mac = (e.get("mac") or "").lower()
        if mac in mac_map:
            e["profile"], e["connection"] = mac_map[mac]
            continue
        port_num = _downlink_port_num(e.get("port", ""))
        if port_num is not None:
            prof = port_map.get((e.get("ic_uri", ""), port_num))
            if prof:
                e["profile"] = prof


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
            maps["v"] = ({}, {})

    await asyncio.gather(_load_profiles(), *[_query_one(li) for li in active_lis])

    # Deduplicate
    seen: set[tuple] = set()
    unique: list[dict] = []
    for m in results:
        key = (m["mac"], m["ic_name"], m["vlan"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    mac_map, port_map = maps.get("v", ({}, {}))
    enrich_mac_entries(unique, mac_map, port_map)

    return sorted(unique, key=lambda m: (m["mac"], m["ic_name"]))
