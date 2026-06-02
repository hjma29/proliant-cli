"""
pcli.oneview.profiles
~~~~~~~~~~~~~~~~~~~~~~
Server profile inventory and detail from HPE OneView.

Key endpoints:
  GET /rest/server-profiles          → all server profiles
  GET /rest/server-hardware-types    → for display names
  GET /rest/enclosure-groups         → for display names
  GET /rest/firmware-drivers/{id}    → for baseline name/version
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcli.oneview.client import OneViewClient


def parse_profile(raw: dict) -> dict:
    return {
        "name":        raw.get("name", ""),
        "status":      raw.get("status", ""),
        "state":       raw.get("state", ""),
        "server_uri":  raw.get("serverHardwareUri", ""),
        "eg_uri":      raw.get("enclosureGroupUri", ""),
        "sht_uri":     raw.get("serverHardwareTypeUri", ""),
        "fw_uri":      raw.get("firmware", {}).get("firmwareBaselineUri", ""),
        "manage_fw":   raw.get("firmware", {}).get("manageFirmware", False),
        "boot_order":  raw.get("boot", {}).get("order", []),
        "description": raw.get("description", "") or "",
        "uri":         raw.get("uri", ""),
        "connections": raw.get("connections") or [],
    }


async def list_profiles(client: "OneViewClient") -> list[dict]:
    """Return all server profiles with resolved server hardware names."""
    raw_profiles, raw_hw = await asyncio.gather(
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/server-hardware"),
    )
    hw_map = {h["uri"]: h.get("name", "") for h in raw_hw}
    profiles = [parse_profile(p) for p in raw_profiles]
    for p in profiles:
        p["server_name"] = hw_map.get(p["server_uri"], "—")
    return sorted(profiles, key=lambda p: p["name"])


async def describe_profile(client: "OneViewClient", name: str) -> dict:
    """Return full detail for a single profile, with all URIs resolved."""
    raw_profiles, raw_hw, raw_egs = await asyncio.gather(
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/enclosure-groups"),
    )

    matched = [p for p in raw_profiles if p.get("name", "").lower() == name.lower()]
    if not matched:
        known = ", ".join(p.get("name", "") for p in raw_profiles)
        raise ValueError(f"Server profile '{name}' not found. Known: {known}")
    raw = matched[0]

    hw_map = {h["uri"]: h for h in raw_hw}
    eg_map = {eg["uri"]: eg.get("name", "") for eg in raw_egs}

    hw = hw_map.get(raw.get("serverHardwareUri", ""), {})

    # Resolve firmware baseline name
    fw_baseline = ""
    fw_version = ""
    fw_uri = raw.get("firmware", {}).get("firmwareBaselineUri", "")
    if fw_uri:
        try:
            fw_raw = await client.get(fw_uri)
            fw_baseline = fw_raw.get("name", "")
            fw_version  = fw_raw.get("version", "")
        except Exception:
            fw_baseline = fw_uri.rsplit("/", 1)[-1]

    p = parse_profile(raw)
    p.update({
        "server_name":     hw.get("name", "—"),
        "server_model":    hw.get("model", ""),
        "server_serial":   hw.get("serialNumber", ""),
        "server_power":    hw.get("powerState", ""),
        "eg_name":         eg_map.get(raw.get("enclosureGroupUri", ""), "—"),
        "fw_baseline":     fw_baseline,
        "fw_version":      fw_version,
        "manage_fw":       raw.get("firmware", {}).get("manageFirmware", False),
        "fw_install_type": raw.get("firmware", {}).get("firmwareInstallType", ""),
    })
    return p
