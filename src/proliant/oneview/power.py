"""
proliant.oneview.power
~~~~~~~~~~~~~~~~~~~~~~
OneView-managed power operations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


SERVER_POWER_ACTIONS: dict[str, tuple[str, str | None, str]] = {
    "on": ("On", None, "power on"),
    "off": ("Off", "PressAndHold", "force power off"),
    "shutdown": ("Off", "MomentaryPress", "gracefully shut down"),
}

HARD_CYCLE_ACTIONS = frozenset({"cycle", "reset"})

_EFUSE_COMPONENTS: dict[str, tuple[str, str, str]] = {
    "server": ("Device", "deviceBays", "server hardware"),
    "profile": ("Device", "deviceBays", "server profile hardware"),
    "interconnect": ("ICM", "interconnectBays", "interconnect"),
    "flm": ("FLM", "managerBays", "frame link module"),
}


def is_hard_cycle_action(action: str) -> bool:
    return action.lower() in HARD_CYCLE_ACTIONS


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _known_names(items: list[dict[str, Any]]) -> str:
    names = [str(item.get("name", "")) for item in items if item.get("name")]
    return ", ".join(sorted(names, key=str.lower))


def _location_map(raw: dict[str, Any], field: str) -> dict[str, str]:
    entries = (raw.get(field) or {}).get("locationEntries", [])
    return {
        str(entry.get("type", "")): str(entry.get("value", ""))
        for entry in entries
        if entry.get("type")
    }


def _server_enclosure_uri(raw: dict[str, Any]) -> str:
    return str(raw.get("locationUri") or raw.get("enclosureUri") or raw.get("serverGroupUri") or "")


def _server_bay(raw: dict[str, Any]) -> int:
    return _as_int(raw.get("position") or raw.get("bayNumber") or raw.get("bay"))


def _interconnect_enclosure_uri(raw: dict[str, Any]) -> str:
    return str(raw.get("enclosureUri") or _location_map(raw, "interconnectLocation").get("Enclosure", ""))


def _interconnect_bay(raw: dict[str, Any]) -> int:
    return _as_int(raw.get("bayNumber") or raw.get("bay") or _location_map(raw, "interconnectLocation").get("Bay"))


def _target_summary(raw: dict[str, Any], fallback: str = "") -> dict[str, Any]:
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
        known = _known_names(enclosures)
        raise ValueError(f"Enclosure '{name}' not found. Known enclosures: {known}")
    return matched[0]


async def get_server(client: "OneViewClient", name: str) -> dict[str, Any]:
    servers = await client.get_all("/rest/server-hardware")
    matched = [s for s in servers if str(s.get("name", "")).lower() == name.lower()]
    if not matched:
        known = _known_names(servers)
        raise ValueError(f"Server '{name}' not found. Known servers: {known}")
    return matched[0]


async def get_server_by_location(client: "OneViewClient", enclosure_name: str, bay: int) -> dict[str, Any]:
    if bay <= 0:
        raise ValueError("--bay must be a positive integer")
    enclosure = await get_enclosure(client, enclosure_name)
    enclosure_uri = str(enclosure.get("uri", ""))
    servers = await client.get_all("/rest/server-hardware")
    for server in servers:
        if _server_enclosure_uri(server) == enclosure_uri and _server_bay(server) == bay:
            return server
    raise ValueError(f"No server hardware found in enclosure '{enclosure_name}' bay {bay}")


async def get_profile_server(client: "OneViewClient", name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    profiles = await client.get_all("/rest/server-profiles")
    matched = [p for p in profiles if str(p.get("name", "")).lower() == name.lower()]
    if not matched:
        known = _known_names(profiles)
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
        known = _known_names(interconnects)
        raise ValueError(f"Interconnect '{name}' not found. Known interconnects: {known}")
    return matched[0]


async def get_interconnect_by_location(client: "OneViewClient", enclosure_name: str, bay: int) -> dict[str, Any]:
    if bay <= 0:
        raise ValueError("--bay must be a positive integer")
    enclosure = await get_enclosure(client, enclosure_name)
    enclosure_uri = str(enclosure.get("uri", ""))
    interconnects = await client.get_all("/rest/interconnects")
    for interconnect in interconnects:
        if _interconnect_enclosure_uri(interconnect) == enclosure_uri and _interconnect_bay(interconnect) == bay:
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


async def set_server_power_state(
    client: "OneViewClient",
    server: dict[str, Any],
    *,
    action: str,
    target_type: str,
    target_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_action = action.lower()
    if normalized_action not in SERVER_POWER_ACTIONS:
        raise ValueError(f"Unsupported server power action '{action}'")

    power_state, power_control, label = SERVER_POWER_ACTIONS[normalized_action]
    uri = str(server.get("uri") or "")
    if not uri:
        raise ValueError("Selected server hardware has no OneView URI")

    payload: dict[str, Any] = {"powerState": power_state}
    if power_control:
        payload["powerControl"] = power_control

    url = f"{uri.rstrip('/')}/powerState"
    if dry_run:
        body: dict[str, Any] = {}
        status = "dry-run"
    else:
        body = await client.put(url, payload)
        status = "accepted"

    return {
        "status": status,
        "action": normalized_action,
        "action_label": label,
        "method": "server-hardware powerState",
        "target_type": target_type,
        "target": target_name or server.get("name", ""),
        "server": _target_summary(server),
        "url": url,
        "payload": payload,
        "task_uri": body.get("uri", ""),
        "task": body,
    }


async def efuse_enclosure_component(
    client: "OneViewClient",
    *,
    enclosure: dict[str, Any],
    component: str,
    bay: int,
    target_type: str,
    target_name: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    if bay <= 0:
        raise ValueError("Bay must be a positive integer")
    if component not in _EFUSE_COMPONENTS:
        raise ValueError(f"Unsupported eFuse component '{component}'")

    component_name, bay_collection, component_label = _EFUSE_COMPONENTS[component]
    enclosure_uri = str(enclosure.get("uri") or "")
    if not enclosure_uri:
        raise ValueError("Selected enclosure has no OneView URI")

    patch = [{"op": "replace", "path": f"/{bay_collection}/{bay}/bayPowerState", "value": "E-Fuse"}]
    if dry_run:
        body: dict[str, Any] = {}
        status = "dry-run"
    else:
        body = await client.patch(enclosure_uri, patch)
        status = "accepted"

    return {
        "status": status,
        "action": "cycle",
        "action_label": "hard power-cycle",
        "method": "enclosure eFuse",
        "target_type": target_type,
        "target": target_name,
        "component": component_name,
        "component_label": component_label,
        "enclosure": {
            "name": enclosure.get("name", ""),
            "uri": enclosure_uri,
        },
        "bay": bay,
        "url": enclosure_uri,
        "payload": patch,
        "task_uri": body.get("uri", ""),
        "task": body,
    }


async def efuse_server(
    client: "OneViewClient",
    server: dict[str, Any],
    *,
    target_type: str,
    target_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    enclosure_uri = _server_enclosure_uri(server)
    bay = _server_bay(server)
    if not enclosure_uri or not bay:
        raise ValueError("Selected server hardware does not have an enclosure bay; eFuse is only available for Synergy bays")

    enclosure = await client.get(enclosure_uri)
    return await efuse_enclosure_component(
        client,
        enclosure=enclosure,
        component="server",
        bay=bay,
        target_type=target_type,
        target_name=target_name or str(server.get("name", "")),
        dry_run=dry_run,
    )


async def efuse_interconnect(
    client: "OneViewClient",
    interconnect: dict[str, Any],
    *,
    target_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    enclosure_uri = _interconnect_enclosure_uri(interconnect)
    bay = _interconnect_bay(interconnect)
    if not enclosure_uri or not bay:
        raise ValueError("Selected interconnect does not have an enclosure bay")

    enclosure = await client.get(enclosure_uri)
    return await efuse_enclosure_component(
        client,
        enclosure=enclosure,
        component="interconnect",
        bay=bay,
        target_type="interconnect",
        target_name=target_name or str(interconnect.get("name", "")),
        dry_run=dry_run,
    )


def _validate_flm_bay(enclosure: dict[str, Any], bay: int) -> None:
    bays = enclosure.get("managerBays") or []
    known = {_as_int(item.get("bayNumber") or item.get("bay")) for item in bays}
    known.discard(0)
    if known and bay not in known:
        known_text = ", ".join(str(item) for item in sorted(known))
        raise ValueError(f"Frame link module bay {bay} not found in enclosure '{enclosure.get('name', '')}'. Known FLM bays: {known_text}")


async def efuse_flm(
    client: "OneViewClient",
    *,
    enclosure_name: str,
    bay: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    enclosure = await get_enclosure(client, enclosure_name)
    _validate_flm_bay(enclosure, bay)
    return await efuse_enclosure_component(
        client,
        enclosure=enclosure,
        component="flm",
        bay=bay,
        target_type="flm",
        target_name=f"{enclosure.get('name', enclosure_name)}, frame link module {bay}",
        dry_run=dry_run,
    )


async def run_power_action(
    client: "OneViewClient",
    action: str,
    target_type: str,
    *,
    name: str | None = None,
    enclosure: str | None = None,
    bay: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_action = action.lower()
    normalized_target = target_type.lower()

    if normalized_action in SERVER_POWER_ACTIONS:
        if normalized_target == "server":
            server = await resolve_server_target(client, name=name, enclosure=enclosure, bay=bay)
            return await set_server_power_state(
                client, server, action=normalized_action, target_type="server", dry_run=dry_run
            )
        if normalized_target == "profile":
            profile_name = name or ""
            _profile, server = await get_profile_server(client, profile_name)
            return await set_server_power_state(
                client,
                server,
                action=normalized_action,
                target_type="profile",
                target_name=profile_name,
                dry_run=dry_run,
            )
        raise ValueError(
            f"OneView does not expose '{normalized_action}' for {normalized_target}. "
            "Use 'cycle' for an eFuse hard power-cycle where supported."
        )

    if normalized_action not in HARD_CYCLE_ACTIONS:
        raise ValueError(f"Unsupported OneView power action '{action}'")

    if normalized_target == "server":
        server = await resolve_server_target(client, name=name, enclosure=enclosure, bay=bay)
        return await efuse_server(client, server, target_type="server", dry_run=dry_run)
    if normalized_target == "profile":
        profile_name = name or ""
        _profile, server = await get_profile_server(client, profile_name)
        return await efuse_server(client, server, target_type="profile", target_name=profile_name, dry_run=dry_run)
    if normalized_target == "interconnect":
        interconnect = await resolve_interconnect_target(client, name=name, enclosure=enclosure, bay=bay)
        return await efuse_interconnect(client, interconnect, dry_run=dry_run)
    if normalized_target == "flm":
        if not enclosure or bay is None:
            raise ValueError("FLM cycle requires ENCLOSURE and BAY")
        return await efuse_flm(client, enclosure_name=enclosure, bay=bay, dry_run=dry_run)

    raise ValueError(f"Unsupported OneView power target '{target_type}'")
