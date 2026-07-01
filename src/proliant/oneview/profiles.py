"""
proliant.oneview.profiles
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
    from proliant.oneview.client import OneViewClient


def _short_server_model(model: str) -> str:
    return (model or "").replace("Synergy ", "").strip()


def parse_profile(raw: dict) -> dict:
    firmware = raw.get("firmware") or {}
    boot = raw.get("boot") or {}
    bios = raw.get("bios") or {}
    connections = raw.get("connections") or (raw.get("connectionSettings") or {}).get("connections") or []
    return {
        "name":        raw.get("name", ""),
        "status":      raw.get("status", ""),
        "state":       raw.get("state", ""),
        "server_uri":  raw.get("serverHardwareUri", ""),
        "template_uri": raw.get("serverProfileTemplateUri", ""),
        "eg_uri":      raw.get("enclosureGroupUri", ""),
        "sht_uri":     raw.get("serverHardwareTypeUri", ""),
        "fw_uri":      firmware.get("firmwareBaselineUri", ""),
        "manage_fw":   firmware.get("manageFirmware", False),
        "fw_consistency": firmware.get("consistencyState", ""),
        "fw_reapply_state": firmware.get("reapplyState", ""),
        "fw_install_action": firmware.get("firmwareInstallAction", ""),
        "fw_activation_type": firmware.get("firmwareActivationType", ""),
        "boot_order":  boot.get("order", []),
        "manage_boot": boot.get("manageBoot", False),
        "manage_bios": bios.get("manageBios", False),
        "bios_consistency": bios.get("consistencyState", ""),
        "bios_overrides": bios.get("overriddenSettings", []),
        "affinity": raw.get("affinity", ""),
        "serial_number_type": raw.get("serialNumberType", ""),
        "serial_number": raw.get("serialNumber", ""),
        "mac_type": raw.get("macType", ""),
        "wwn_type": raw.get("wwnType", ""),
        "iscsi_initiator_name_type": raw.get("iscsiInitiatorNameType", ""),
        "iscsi_initiator_name": raw.get("iscsiInitiatorName", ""),
        "description": raw.get("description", "") or "",
        "uri":         raw.get("uri", ""),
        "connections": connections,
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
    raw_profiles, raw_hw, raw_egs, raw_shts, raw_templates, raw_networks, raw_network_sets = await asyncio.gather(
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/enclosure-groups"),
        client.get_all("/rest/server-hardware-types"),
        client.get_all("/rest/server-profile-templates"),
        client.get_all("/rest/ethernet-networks"),
        client.get_all("/rest/network-sets"),
    )

    matched = [p for p in raw_profiles if p.get("name", "").lower() == name.lower()]
    if not matched:
        known = ", ".join(p.get("name", "") for p in raw_profiles)
        raise ValueError(f"Server profile '{name}' not found. Known: {known}")
    raw = matched[0]

    hw_map = {h["uri"]: h for h in raw_hw}
    eg_map = {eg["uri"]: eg.get("name", "") for eg in raw_egs}
    sht_map = {sht.get("uri", ""): sht.get("name", "") for sht in raw_shts}
    template_map = {t.get("uri", ""): t.get("name", "") for t in raw_templates}
    network_map = {n.get("uri", ""): n.get("name", "") for n in raw_networks}
    network_map.update({ns.get("uri", ""): ns.get("name", "") for ns in raw_network_sets})

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
    connections = []
    for connection in p["connections"]:
        network_uri = connection.get("networkUri") or connection.get("networkSetUri") or ""
        connections.append({
            "id": connection.get("id", ""),
            "name": connection.get("name", ""),
            "function_type": connection.get("functionType", ""),
            "network": network_map.get(network_uri, network_uri.rsplit("/", 1)[-1] if network_uri else ""),
            "network_uri": network_uri,
            "port_id": connection.get("portId", ""),
            "mac": connection.get("mac", ""),
            "requested_mbps": connection.get("requestedMbps", ""),
            "allocated_mbps": connection.get("allocatedMbps", ""),
            "maximum_mbps": connection.get("maximumMbps", ""),
            "state": connection.get("state", ""),
            "status": connection.get("status", ""),
        })
    p.update({
        "server_name":     hw.get("name", "—"),
        "server_model":    _short_server_model(hw.get("model", "")),
        "server_serial":   hw.get("serialNumber", ""),
        "server_power":    hw.get("powerState", ""),
        "server_status":   hw.get("status", ""),
        "server_state":    hw.get("state", ""),
        "server_bay":      hw.get("position", ""),
        "eg_name":         eg_map.get(raw.get("enclosureGroupUri", ""), "—"),
        "server_hardware_type": sht_map.get(raw.get("serverHardwareTypeUri", ""), raw.get("serverHardwareTypeUri", "").rsplit("/", 1)[-1]),
        "template_name":    template_map.get(raw.get("serverProfileTemplateUri", ""), raw.get("serverProfileTemplateUri", "").rsplit("/", 1)[-1]),
        "fw_baseline":     fw_baseline,
        "fw_version":      fw_version,
        "manage_fw":       (raw.get("firmware") or {}).get("manageFirmware", False),
        "fw_install_type": (raw.get("firmware") or {}).get("firmwareInstallType", ""),
        "connections":      connections,
    })
    return p
