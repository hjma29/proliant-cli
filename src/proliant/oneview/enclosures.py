"""
proliant.oneview.enclosures
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Enclosures, Enclosure Groups (EG), and Logical Enclosures (LE).

Key endpoints:
  GET /rest/enclosures              -> physical enclosures
  GET /rest/enclosure-groups        -> enclosure groups
  GET /rest/logical-enclosures      -> logical enclosures
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── Enclosures ────────────────────────────────────────────────────────────────

def parse_enclosure(raw: dict) -> dict:
    return {
        "name":   raw.get("name", ""),
        "model":  raw.get("enclosureModel", ""),
        "serial": raw.get("serialNumber", ""),
        "state":  raw.get("state", ""),
        "status": raw.get("status", ""),
        "uri":    raw.get("uri", ""),
    }


async def list_enclosures(client: "OneViewClient") -> list[dict]:
    raw = await client.get_all("/rest/enclosures")
    return sorted([parse_enclosure(e) for e in raw], key=lambda e: e["name"])


def _location_map(raw: dict, field: str) -> dict[str, str]:
    entries = (raw.get(field) or {}).get("locationEntries", [])
    return {entry.get("type", ""): entry.get("value", "") for entry in entries}


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _server_enclosure_uri(raw: dict) -> str:
    return raw.get("locationUri") or raw.get("enclosureUri") or raw.get("serverGroupUri") or ""


def _server_bay(raw: dict) -> int:
    return _as_int(raw.get("position") or raw.get("bayNumber") or raw.get("bay"))


def _interconnect_enclosure_uri(raw: dict) -> str:
    return _location_map(raw, "interconnectLocation").get("Enclosure", "")


def _interconnect_bay(raw: dict) -> int:
    return _as_int(_location_map(raw, "interconnectLocation").get("Bay"))


def _resolve_logical_enclosure(enclosure_uri: str, raw_les: list[dict]) -> dict | None:
    for le in raw_les:
        if enclosure_uri in (le.get("enclosureUris") or []):
            return le
    return None


def _front_bay_count(model: str, servers: list[dict]) -> int:
    max_bay = max([s["bay"] for s in servers] or [0])
    if "synergy" in model.lower():
        return max(12, max_bay)
    return max(1, max_bay)


def _rear_bay_count(interconnects: list[dict]) -> int:
    return max(6, max([ic["bay"] for ic in interconnects] or [0]))


def _short_server_model(model: str) -> str:
    return (model or "").replace("Synergy ", "").strip()


async def describe_enclosure(client: "OneViewClient", name: str) -> dict:
    """Return enclosure summary plus front server and rear interconnect bay maps."""
    raw_encs, raw_servers, raw_profiles, raw_ics, raw_lis, raw_les, raw_egs = await asyncio.gather(
        client.get_all("/rest/enclosures"),
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/interconnects"),
        client.get_all("/rest/logical-interconnects"),
        client.get_all("/rest/logical-enclosures"),
        client.get_all("/rest/enclosure-groups"),
    )

    matched = [e for e in raw_encs if e.get("name", "").lower() == name.lower()]
    if not matched:
        known = ", ".join(e.get("name", "") for e in raw_encs)
        raise ValueError(f"Enclosure '{name}' not found. Known: {known}")

    raw_enc = matched[0]
    enc = parse_enclosure(raw_enc)
    enc_uri = enc["uri"]
    profile_map = {p.get("uri", ""): p.get("name", "") for p in raw_profiles}
    profile_status_map = {p.get("uri", ""): p.get("status", "") for p in raw_profiles}
    profile_state_map = {p.get("uri", ""): p.get("state", "") for p in raw_profiles}
    li_map = {li.get("uri", ""): li.get("name", "") for li in raw_lis}
    eg_map = {eg.get("uri", ""): eg.get("name", "") for eg in raw_egs}
    le = _resolve_logical_enclosure(enc_uri, raw_les)
    device_bay_map = {
        _as_int(bay.get("bayNumber") or bay.get("bay")): bay
        for bay in raw_enc.get("deviceBays") or []
        if _as_int(bay.get("bayNumber") or bay.get("bay"))
    }
    interconnect_bay_map = {
        _as_int(bay.get("bayNumber") or bay.get("bay")): bay
        for bay in raw_enc.get("interconnectBays") or []
        if _as_int(bay.get("bayNumber") or bay.get("bay"))
    }
    server_by_uri = {server.get("uri", ""): server for server in raw_servers}

    servers = []
    for raw in raw_servers:
        if _server_enclosure_uri(raw) != enc_uri:
            continue
        bay = _server_bay(raw)
        if not bay:
            continue
        device_bay = device_bay_map.get(bay, {})
        servers.append({
            "bay": bay,
            "name": raw.get("name", ""),
            "server_name": raw.get("serverName", ""),
            "model": _short_server_model(raw.get("model", "")),
            "serial": raw.get("serialNumber", ""),
            "part_number": raw.get("partNumber", ""),
            "profile": profile_map.get(raw.get("serverProfileUri", ""), ""),
            "profile_status": profile_status_map.get(raw.get("serverProfileUri", ""), ""),
            "profile_state": profile_state_map.get(raw.get("serverProfileUri", ""), ""),
            "power": raw.get("powerState", ""),
            "power_allocation_watts": device_bay.get("powerAllocationWatts", 0) or 0,
            "state": raw.get("state", ""),
            "status": raw.get("status", ""),
            "mp_model": raw.get("mpModel", ""),
            "mp_firmware_version": raw.get("mpFirmwareVersion", ""),
            "rom_version": raw.get("romVersion", ""),
            "ilo_ip": next(
                (addr.get("address", "") for addr in raw.get("mpIpAddresses", []) if addr.get("address")),
                "",
            ),
            "uri": raw.get("uri", ""),
        })

    interconnects = []
    for raw in raw_ics:
        if _interconnect_enclosure_uri(raw) != enc_uri:
            continue
        bay = _interconnect_bay(raw)
        if not bay:
            continue
        interconnect_bay = interconnect_bay_map.get(bay, {})
        interconnects.append({
            "bay": bay,
            "name": raw.get("name", ""),
            "model": raw.get("model", ""),
            "serial": raw.get("serialNumber", ""),
            "part_number": raw.get("partNumber", ""),
            "firmware_version": raw.get("firmwareVersion", ""),
            "power": raw.get("powerState", ""),
            "power_allocation_watts": interconnect_bay.get("powerAllocationWatts", 0) or 0,
            "logical_interconnect": li_map.get(raw.get("logicalInterconnectUri", ""), ""),
            "state": raw.get("state", ""),
            "status": raw.get("status", ""),
            "uri": raw.get("uri", ""),
        })

    servers.sort(key=lambda s: s["bay"])
    interconnects.sort(key=lambda ic: ic["bay"])
    appliances = []
    for raw in raw_enc.get("applianceBays") or []:
        bay = _as_int(raw.get("bayNumber") or raw.get("bay"))
        if not bay:
            continue
        appliances.append({
            "bay": bay,
            "model": raw.get("model", ""),
            "serial": raw.get("serialNumber", ""),
            "part_number": raw.get("partNumber", ""),
            "spare_part_number": raw.get("sparePartNumber", ""),
            "presence": raw.get("devicePresence", ""),
            "power": "On" if raw.get("poweredOn") is True else raw.get("bayPowerState", ""),
            "status": raw.get("status", ""),
        })
    appliances.sort(key=lambda a: a["bay"])
    power_supplies = []
    for raw in raw_enc.get("powerSupplyBays") or []:
        bay = _as_int(raw.get("bayNumber") or raw.get("bay") or raw.get("label"))
        if not bay:
            continue
        power_supplies.append({
            "bay": bay,
            "label": raw.get("label", str(bay)),
            "model": raw.get("model", ""),
            "serial": raw.get("serialNumber", ""),
            "part_number": raw.get("partNumber", ""),
            "spare_part_number": raw.get("sparePartNumber", ""),
            "presence": raw.get("devicePresence", ""),
            "capacity_watts": raw.get("outputCapacityWatts", 0),
            "status": raw.get("status", ""),
        })
    power_supplies.sort(key=lambda ps: ps["bay"])
    fans = []
    for raw in raw_enc.get("fanBays") or []:
        bay = _as_int(raw.get("bayNumber") or raw.get("bay"))
        if not bay:
            continue
        fans.append({
            "bay": bay,
            "model": raw.get("model", ""),
            "serial": raw.get("serialNumber", ""),
            "part_number": raw.get("partNumber", ""),
            "spare_part_number": raw.get("sparePartNumber", ""),
            "presence": raw.get("devicePresence", ""),
            "required": bool(raw.get("deviceRequired")),
            "status": raw.get("status", ""),
        })
    fans.sort(key=lambda fan: fan["bay"])
    frame_link_modules = []
    for raw in raw_enc.get("managerBays") or []:
        bay = _as_int(raw.get("bayNumber") or raw.get("bay"))
        if not bay:
            continue
        frame_link_modules.append({
            "bay": bay,
            "model": raw.get("model", ""),
            "serial": raw.get("serialNumber", ""),
            "part_number": raw.get("partNumber", ""),
            "spare_part_number": raw.get("sparePartNumber", ""),
            "role": raw.get("role", ""),
            "fw_version": raw.get("fwVersion", ""),
            "ip_address": raw.get("ipAddress", ""),
            "presence": raw.get("devicePresence", ""),
            "status": raw.get("status", ""),
        })
    frame_link_modules.sort(key=lambda flm: flm["bay"])
    devices = []
    for raw in raw_enc.get("deviceBays") or []:
        bay = _as_int(raw.get("bayNumber") or raw.get("bay"))
        if not bay:
            continue
        server = server_by_uri.get(raw.get("deviceUri", ""), {})
        profile_uri = raw.get("profileUri") or raw.get("coveredByProfile") or server.get("serverProfileUri", "")
        devices.append({
            "bay": bay,
            "hardware": server.get("name", "empty" if raw.get("devicePresence") == "Absent" else ""),
            "server_name": server.get("serverName", "not set" if raw.get("devicePresence") == "Absent" else ""),
            "model": _short_server_model(server.get("model", raw.get("model") or "")),
            "serial": server.get("serialNumber", ""),
            "profile": profile_map.get(profile_uri, ""),
            "status": server.get("status", ""),
            "profile_status": profile_status_map.get(profile_uri, ""),
            "presence": raw.get("devicePresence", ""),
            "power_allocation_watts": raw.get("powerAllocationWatts", 0) or 0,
        })
    devices.sort(key=lambda device: device["bay"])
    firmware = []
    for module in frame_link_modules:
        firmware.append({
            "name": f"{enc['name']}, frame link module {module['bay']}",
            "component": "Frame link module",
            "installed": module.get("fw_version", ""),
        })
    for server in servers:
        firmware.append({
            "name": server["name"],
            "component": server.get("profile") or server.get("server_name") or server.get("serial", ""),
            "installed": "",
        })
        if server.get("mp_firmware_version"):
            firmware.append({
                "name": "",
                "component": server.get("mp_model") or "iLO",
                "installed": server["mp_firmware_version"],
            })
        if server.get("rom_version"):
            firmware.append({
                "name": "",
                "component": "ROM",
                "installed": server["rom_version"],
            })
    for interconnect in interconnects:
        firmware.append({
            "name": interconnect["name"],
            "component": interconnect["model"],
            "installed": interconnect.get("firmware_version", ""),
        })
    return {
        "name": enc["name"],
        "model": enc["model"],
        "serial": enc["serial"],
        "state": enc["state"],
        "status": enc["status"],
        "uri": enc_uri,
        "logical_enclosure": le.get("name", "") if le else "",
        "enclosure_group": eg_map.get(le.get("enclosureGroupUri", ""), "") if le else "",
        "front_bay_count": _front_bay_count(enc["model"], servers),
        "rear_bay_count": _rear_bay_count(interconnects),
        "appliances": appliances,
        "power_supplies": power_supplies,
        "fans": fans,
        "frame_link_modules": frame_link_modules,
        "devices": devices,
        "firmware": firmware,
        "servers": servers,
        "interconnects": interconnects,
    }


# ── Enclosure Groups (EG) ─────────────────────────────────────────────────────

def parse_eg(raw: dict, lig_map: dict[str, str]) -> dict:
    lig_names = sorted({
        lig_map.get(m.get("logicalInterconnectGroupUri", ""), "")
        for m in raw.get("interconnectBayMappings", [])
        if m.get("logicalInterconnectGroupUri")
    })
    return {
        "name":      raw.get("name", ""),
        "lig_names": lig_names,
        "status":    raw.get("status", ""),
        "uri":       raw.get("uri", ""),
    }


async def list_enclosure_groups(client: "OneViewClient") -> list[dict]:
    raw_egs, raw_ligs = await asyncio.gather(
        client.get_all("/rest/enclosure-groups"),
        client.get_all("/rest/logical-interconnect-groups"),
    )
    lig_map = {lg["uri"]: lg.get("name", "") for lg in raw_ligs}
    return sorted([parse_eg(eg, lig_map) for eg in raw_egs], key=lambda eg: eg["name"])


# ── Logical Enclosures (LE) ───────────────────────────────────────────────────

def parse_le(raw: dict, eg_map: dict, enc_map: dict, li_map: dict) -> dict:
    enc_names = sorted([enc_map.get(u, u.rsplit("/", 1)[-1]) for u in raw.get("enclosureUris", [])])
    li_names  = sorted([li_map.get(u,  u.rsplit("/", 1)[-1]) for u in raw.get("logicalInterconnectUris", [])])
    return {
        "name":       raw.get("name", ""),
        "eg_name":    eg_map.get(raw.get("enclosureGroupUri", ""), ""),
        "enclosures": enc_names,
        "lis":        li_names,
        "state":      raw.get("state", ""),
        "status":     raw.get("status", ""),
        "uri":        raw.get("uri", ""),
    }


async def list_logical_enclosures(client: "OneViewClient") -> list[dict]:
    raw_les, raw_egs, raw_encs, raw_lis = await asyncio.gather(
        client.get_all("/rest/logical-enclosures"),
        client.get_all("/rest/enclosure-groups"),
        client.get_all("/rest/enclosures"),
        client.get_all("/rest/logical-interconnects"),
    )
    eg_map  = {eg["uri"]: eg.get("name", "") for eg in raw_egs}
    enc_map = {e["uri"]:  e.get("name",  "") for e in raw_encs}
    li_map  = {li["uri"]: li.get("name", "") for li in raw_lis}
    les = [parse_le(le, eg_map, enc_map, li_map) for le in raw_les]
    return sorted(les, key=lambda le: le["name"])
