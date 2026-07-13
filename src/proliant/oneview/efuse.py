"""
proliant.oneview.efuse
~~~~~~~~~~~~~~~~~~~~~~
OneView Synergy enclosure eFuse: a hard power-cycle of a bay, performed by
PATCHing the enclosure resource's ``bayPowerState`` to ``"E-Fuse"`` rather
than the graceful ``server-hardware powerState`` used for on/off/shutdown
(see ``proliant.oneview.power``). This is the same mechanism OneView's GUI
uses for "Momentary press and hold" style hard resets on Synergy bays:
server hardware, server profiles (via their assigned server), interconnects,
and frame link modules (FLMs).

Reachable via ``proliant oneview efuse <target>``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from proliant.oneview.targets import as_int

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


_EFUSE_COMPONENTS: dict[str, tuple[str, str, str]] = {
    "server": ("Device", "deviceBays", "server hardware"),
    "profile": ("Device", "deviceBays", "server profile hardware"),
    "interconnect": ("ICM", "interconnectBays", "interconnect"),
    "flm": ("FLM", "managerBays", "frame link module"),
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
        etag = enclosure.get("eTag") or enclosure.get("etag") or "*"
        body = await client.patch(enclosure_uri, patch, headers={"If-Match": etag})
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
    from proliant.oneview.targets import server_bay, server_enclosure_uri

    enclosure_uri = server_enclosure_uri(server)
    bay = server_bay(server)
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
    from proliant.oneview.targets import interconnect_bay, interconnect_enclosure_uri

    enclosure_uri = interconnect_enclosure_uri(interconnect)
    bay = interconnect_bay(interconnect)
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
    known = {as_int(item.get("bayNumber") or item.get("bay")) for item in bays}
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
    from proliant.oneview.targets import get_enclosure

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


async def run_efuse_action(
    client: "OneViewClient",
    target_type: str,
    *,
    name: str | None = None,
    enclosure: str | None = None,
    bay: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Dispatch a hard eFuse power-cycle to a server, profile, interconnect, or FLM.

    This is a pure hard power-cycle: unlike ``proliant.oneview.power``, there
    is no separate on/off/shutdown concept here -- eFuse always performs the
    same enclosure ``bayPowerState`` PATCH described in ``efuse_enclosure_component``.
    """
    from proliant.oneview.targets import get_profile_server, resolve_interconnect_target, resolve_server_target

    normalized_target = target_type.lower()

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

    raise ValueError(f"Unsupported eFuse target '{target_type}'")
