"""
pcli.oneview.servers
~~~~~~~~~~~~~~~~~~~~~
Server hardware inventory from HPE OneView.

Key endpoints:
  GET /rest/server-hardware            → all managed servers (paginated)
  GET /rest/server-hardware/{id}       → single server detail
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcli.oneview.client import OneViewClient


def _mp_ip(server: dict) -> str:
    """Extract first iLO IP from mpIpAddresses list."""
    addrs = server.get("mpIpAddresses", [])
    for a in addrs:
        ip = a.get("address", "")
        # Skip link-local and empty
        if ip and not ip.startswith("169.254"):
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
    return {
        "name":        raw.get("name", ""),
        "model":       raw.get("model", ""),
        "serial":      raw.get("serialNumber", ""),
        "ilo_model":   raw.get("mpModel", ""),
        "ilo_version": raw.get("mpFirmwareVersion", ""),
        "ilo_ip":      _mp_ip(raw),
        "power":       raw.get("powerState", ""),
        "state":       raw.get("state", ""),
        "profile":     raw.get("serverProfileUri", "").rsplit("/", 1)[-1] if raw.get("serverProfileUri") else "",
        "uri":         raw.get("uri", ""),
        # Synergy-specific
        "enclosure":   raw.get("serverGroupUri", ""),
        "position":    raw.get("position", 0),
    }


async def list_servers(client: "OneViewClient") -> list[dict]:
    """Return all managed server hardware, normalized."""
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
