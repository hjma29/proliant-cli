"""
proliant.oneview.targets
~~~~~~~~~~~~~~~~~~~~~~~~
Shared OneView resource lookups: resolving server hardware, server profiles,
and interconnects by name or by enclosure/bay location. Used by both
``proliant.oneview.power`` (graceful on/off/shutdown) and
``proliant.oneview.efuse`` (hard eFuse power-cycle).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


def as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def known_names(items: list[dict[str, Any]]) -> str:
    names = [str(item.get("name", "")) for item in items if item.get("name")]
    return ", ".join(sorted(names, key=str.lower))


def _location_map(raw: dict[str, Any], field: str) -> dict[str, str]:
    entries = (raw.get(field) or {}).get("locationEntries", [])
    return {
        str(entry.get("type", "")): str(entry.get("value", ""))
        for entry in entries
        if entry.get("type")
    }


def server_enclosure_uri(raw: dict[str, Any]) -> str:
    return str(raw.get("locationUri") or raw.get("enclosureUri") or raw.get("serverGroupUri") or "")


def server_bay(raw: dict[str, Any]) -> int:
    return as_int(raw.get("position") or raw.get("bayNumber") or raw.get("bay"))


def interconnect_enclosure_uri(raw: dict[str, Any]) -> str:
    return str(raw.get("enclosureUri") or _location_map(raw, "interconnectLocation").get("Enclosure", ""))


def interconnect_bay(raw: dict[str, Any]) -> int:
    return as_int(raw.get("bayNumber") or raw.get("bay") or _location_map(raw, "interconnectLocation").get("Bay"))


def target_summary(raw: dict[str, Any], fallback: str = "") -> dict[str, Any]:
    return {
        "name": raw.get("name") or fallback,
        "uri": raw.get("uri", ""),
        "power_state": raw.get("powerState", ""),
        "state": raw.get("state", ""),
        "status": raw.get("status", ""),
    }


async def get_enclosure(client: "OneViewClient", name: str) -> dict[str, Any]:
    enclosures = await client.get_all("/rest/enclosures")
    matched = [e for e in enclosures if str(e.get("name", "")).lower() == name.lower()]
    if not matched:
        known = known_names(enclosures)
        raise ValueError(f"Enclosure '{name}' not found. Known enclosures: {known}")
    return matched[0]


async def get_server(client: "OneViewClient", name: str) -> dict[str, Any]:
    servers = await client.get_all("/rest/server-hardware")
    matched = [s for s in servers if str(s.get("name", "")).lower() == name.lower()]
    if not matched:
        known = known_names(servers)
        raise ValueError(f"Server '{name}' not found. Known servers: {known}")
    return matched[0]


async def get_server_by_location(client: "OneViewClient", enclosure_name: str, bay: int) -> dict[str, Any]:
    if bay <= 0:
        raise ValueError("--bay must be a positive integer")
    enclosure = await get_enclosure(client, enclosure_name)
    enclosure_uri = str(enclosure.get("uri", ""))
    servers = await client.get_all("/rest/server-hardware")
    for server in servers:
        if server_enclosure_uri(server) == enclosure_uri and server_bay(server) == bay:
            return server
    raise ValueError(f"No server hardware found in enclosure '{enclosure_name}' bay {bay}")


async def get_profile_server(client: "OneViewClient", name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    profiles = await client.get_all("/rest/server-profiles")
    matched = [p for p in profiles if str(p.get("name", "")).lower() == name.lower()]
    if not matched:
        known = known_names(profiles)
        raise ValueError(f"Server profile '{name}' not found. Known profiles: {known}")

    profile = matched[0]
    server_uri = str(profile.get("serverHardwareUri") or "")
    if not server_uri:
        raise ValueError(f"Server profile '{name}' has no assigned server hardware")

    server = await client.get(server_uri)
    if not server.get("uri"):
        server = {**server, "uri": server_uri}
    return profile, server


async def get_interconnect(client: "OneViewClient", name: str) -> dict[str, Any]:
    interconnects = await client.get_all("/rest/interconnects")
    matched = [ic for ic in interconnects if str(ic.get("name", "")).lower() == name.lower()]
    if not matched:
        known = known_names(interconnects)
        raise ValueError(f"Interconnect '{name}' not found. Known interconnects: {known}")
    return matched[0]


async def get_interconnect_by_location(client: "OneViewClient", enclosure_name: str, bay: int) -> dict[str, Any]:
    if bay <= 0:
        raise ValueError("--bay must be a positive integer")
    enclosure = await get_enclosure(client, enclosure_name)
    enclosure_uri = str(enclosure.get("uri", ""))
    interconnects = await client.get_all("/rest/interconnects")
    for interconnect in interconnects:
        if interconnect_enclosure_uri(interconnect) == enclosure_uri and interconnect_bay(interconnect) == bay:
            return interconnect
    raise ValueError(f"No interconnect found in enclosure '{enclosure_name}' bay {bay}")


def _resolve_server_selector(
    name: str | None,
    enclosure: str | None,
    bay: int | None,
) -> tuple[str, str | None, int | None]:
    has_location = bool(enclosure or bay is not None)
    if name and has_location:
        raise ValueError("Specify either a server NAME or --enclosure/--bay, not both")
    if has_location:
        if not enclosure or bay is None:
            raise ValueError("Server location targeting requires both --enclosure and --bay")
        return "", enclosure, bay
    if not name:
        raise ValueError("Server targeting requires NAME or --enclosure/--bay")
    return name, None, None


def _resolve_interconnect_selector(
    name: str | None,
    enclosure: str | None,
    bay: int | None,
) -> tuple[str, str | None, int | None]:
    has_location = bool(enclosure or bay is not None)
    if name and has_location:
        raise ValueError("Specify either an interconnect NAME or --enclosure/--bay, not both")
    if has_location:
        if not enclosure or bay is None:
            raise ValueError("Interconnect location targeting requires both --enclosure and --bay")
        return "", enclosure, bay
    if not name:
        raise ValueError("Interconnect targeting requires NAME or --enclosure/--bay")
    return name, None, None


async def resolve_server_target(
    client: "OneViewClient",
    *,
    name: str | None = None,
    enclosure: str | None = None,
    bay: int | None = None,
) -> dict[str, Any]:
    server_name, enclosure_name, location_bay = _resolve_server_selector(name, enclosure, bay)
    if enclosure_name and location_bay is not None:
        return await get_server_by_location(client, enclosure_name, location_bay)
    return await get_server(client, server_name)


async def resolve_interconnect_target(
    client: "OneViewClient",
    *,
    name: str | None = None,
    enclosure: str | None = None,
    bay: int | None = None,
) -> dict[str, Any]:
    interconnect_name, enclosure_name, location_bay = _resolve_interconnect_selector(name, enclosure, bay)
    if enclosure_name and location_bay is not None:
        return await get_interconnect_by_location(client, enclosure_name, location_bay)
    return await get_interconnect(client, interconnect_name)
