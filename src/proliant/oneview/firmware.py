"""
proliant.oneview.firmware
~~~~~~~~~~~~~~~~~~~~~~
Firmware inventory via HPE OneView.

Key endpoints:
  GET /rest/server-hardware/*/firmware      → all servers' firmware in ONE call
  GET /rest/server-hardware/{id}/firmware   → single server firmware
  POST /rest/server-hardware/firmware-compliance  → check compliance vs SPP
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


def parse_firmware_inventory(raw_firmware: dict) -> list[dict]:
    """Normalize /rest/server-hardware/{id}/firmware response.

    OneView API v7000+ uses 'components' key (not 'serverFirmwareInventory').
    Each component has: componentName, componentVersion, componentLocation, componentKey.
    """
    inventory = raw_firmware.get("components", [])
    result = []
    for item in inventory:
        result.append({
            "name":     item.get("componentName", ""),
            "version":  item.get("componentVersion", ""),
            "location": item.get("componentLocation", "").strip(),
            "key":      item.get("componentKey", ""),
        })
    return sorted(result, key=lambda x: x["name"].lower())


async def get_server_firmware(client: "OneViewClient", server_uri: str) -> list[dict]:
    """Fetch firmware inventory for a single server by its URI."""
    data = await client.get(f"{server_uri}/firmware")
    return parse_firmware_inventory(data)


async def get_fleet_firmware(client: "OneViewClient") -> list[dict]:
    """Fetch firmware for ALL managed servers in a single API call.

    Returns list of dicts, each with:
      server_name, server_uri, firmware (list of component dicts)
    """
    # OneView's wildcard endpoint: GET /rest/server-hardware/*/firmware
    # Returns { members: [ { serverHardwareName, serverHardwareUri, serverFirmwareInventory: [...] } ] }
    data = await client.get("/rest/server-hardware/*/firmware")
    members = data.get("members", [])

    results = []
    for member in members:
        results.append({
            "server_name": member.get("serverName", ""),
            "server_uri":  member.get("serverHardwareUri", ""),
            "firmware":    parse_firmware_inventory(member),
        })
    return sorted(results, key=lambda x: x["server_name"].lower())
