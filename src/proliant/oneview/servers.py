"""
proliant.oneview.servers
~~~~~~~~~~~~~~~~~~~~~
Server hardware inventory from HPE OneView.

Key endpoints:
  GET /rest/server-hardware            → all managed servers (paginated)
  GET /rest/server-hardware/{id}       → single server detail
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


def _mp_ip(server: dict) -> str:
    """Extract first iLO IP from mpIpAddresses list."""
    addrs = []
    addrs.extend(server.get("mpIpAddresses") or [])
    addrs.extend((server.get("mpHostInfo") or {}).get("mpIpAddresses") or [])
    for a in addrs:
        ip = a.get("address", "")
        # Skip link-local and empty addresses.
        if ip and not ip.startswith("169.254") and not ip.lower().startswith("fe80:"):
            return ip
    return ""


def _enclosure_location(server: dict) -> str:
    """Return enclosure + bay string e.g. 'Enc1, bay 3' or rack + U for DL."""
    loc = server.get("locationUri", "")
    # OneView provides a nice 'name' field that already includes bay info
    # for Synergy: "Enclosure1, bay 3"
    # For DL rack: just the server name
    name = server.get("name", "")
    return name


def parse_server(raw: dict) -> dict:
    """Normalize a raw /rest/server-hardware member into a flat dict."""
    model = raw.get("model", "")
    # Strip redundant "Synergy" prefix — context is already OneView/Synergy
    model = model.removeprefix("Synergy ").strip()
    return {
        "name":            raw.get("name", ""),
        "model":           model,
        "serial":          raw.get("serialNumber", ""),
        "ilo_model":       raw.get("mpModel", ""),
        "ilo_version":     raw.get("mpFirmwareVersion", ""),
        "ilo_ip":          _mp_ip(raw),
        "power":           raw.get("powerState", ""),
        "state":           raw.get("state", ""),
        "profile_uri":     raw.get("serverProfileUri", ""),
        "profile":         "",  # resolved after profile name lookup
        "uri":             raw.get("uri", ""),
        "enclosure":       raw.get("serverGroupUri", ""),
        "position":        raw.get("position", 0),
    }


async def list_servers_with_profiles(client: "OneViewClient") -> list[dict]:
    """Return all managed servers with resolved profile names."""
    import asyncio
    raw_servers, profiles = await asyncio.gather(
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/server-profiles"),
    )
    # Build URI → name map
    profile_map = {p["uri"]: p.get("name", "") for p in profiles}
    servers = [parse_server(s) for s in raw_servers]
    for s in servers:
        s["profile"] = profile_map.get(s["profile_uri"], "")
    return servers


async def list_servers(client: "OneViewClient") -> list[dict]:
    """Return all managed server hardware, normalized (no profile name resolution)."""
    raw = await client.get_all("/rest/server-hardware")
    return [parse_server(s) for s in raw]


async def get_server(client: "OneViewClient", name: str) -> dict:
    """Return a single server by name. Raises ValueError if not found."""
    servers = await list_servers(client)
    matched = [s for s in servers if s["name"].lower() == name.lower()]
    if not matched:
        known = ", ".join(s["name"] for s in servers)
        raise ValueError(f"Server '{name}' not found. Known servers: {known}")
    return matched[0]


_SKIP_STATUSES = {"NotPresent", "Unknown", ""}


async def get_fleet_memory(client: "OneViewClient") -> list[dict]:
    """Return all populated DIMMs across all OneView-managed servers."""
    import asyncio

    servers = await client.get_all("/rest/server-hardware")

    async def _get_server_memory(s: dict) -> list[dict]:
        name = s.get("serverName") or s.get("name", "")
        try:
            mem_data = await client.get(s["uri"] + "/memory")
        except Exception:
            return []
        result = []
        for dimm in mem_data.get("data", []):
            cap_mib = dimm.get("CapacityMiB") or 0
            if not cap_mib:
                continue
            oem = dimm.get("Oem", {}).get("Hpe", {})
            status = oem.get("DIMMStatus", "")
            if status in _SKIP_STATUSES:
                continue
            hpe_pn = (oem.get("PartNumber") or dimm.get("PartNumber") or "Unknown").strip() or "Unknown"
            result.append({
                "server":      name,
                "hpe_pn":      hpe_pn,
                "vendor":      oem.get("VendorName") or dimm.get("Manufacturer", ""),
                "capacity_gb": cap_mib // 1024,
                "type":        dimm.get("BaseModuleType", ""),
                "speed_mts":   oem.get("MaxOperatingSpeedMTs", 0) or 0,
            })
        return result

    results = await asyncio.gather(*[_get_server_memory(s) for s in servers])
    dimms: list[dict] = []
    for batch in results:
        dimms.extend(batch)
    return dimms
