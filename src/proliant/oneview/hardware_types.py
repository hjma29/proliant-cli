"""
proliant.oneview.hardware_types
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Read-only model for HPE OneView's *Server Hardware Types* page — the
mezzanine/adapter "shape" definitions that server hardware and server
profiles are matched against (a server profile can only be assigned to
server hardware sharing the same hardware type).

  GET /rest/server-hardware-types        -> name, model, mezzanine adapters
  GET /rest/server-hardware              -> which physical servers use each type
  GET /rest/server-profiles              -> which profiles use each type
  GET /rest/server-profile-templates     -> which templates use each type

The GUI's own Server Hardware Types page only shows the tile list (name +
per-slot adapter model) and, on this appliance, its detail view returns
"Unable to locate the item you requested" for every type (a scope/GUI
issue, not a data problem — the REST objects themselves are intact). This
module reproduces the tile-list data and adds the cross-reference the GUI
doesn't surface at all: which server hardware, server profiles, and server
profile templates are actually using each hardware type.

The formatting/normalization helpers are pure functions (no I/O) so
they're unit-tested directly; ``fetch_hardware_types``/
``fetch_hardware_type_detail`` are the only coroutines.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── formatting (pure) ──────────────────────────────────────────────────────

def fmt_adapter_location(location: str | None, slot: int | None) -> str:
    """Render an adapter's slot the way the GUI does, e.g. ``Mezz``/``3`` ->
    ``Mezzanine 3``. Unknown locations fall back to ``LOCATION slot``."""
    loc = (location or "").strip()
    if loc.lower() == "mezz":
        return f"Mezzanine {slot}" if slot is not None else "Mezzanine"
    if not loc:
        return f"Slot {slot}" if slot is not None else "—"
    return f"{loc} {slot}" if slot is not None else loc


def normalize_adapters(raw_type: dict[str, Any]) -> list[dict[str, str]]:
    """Normalize a hardware type's ``adapters`` into the GUI tile's rows
    (Location, Model)."""
    adapters = []
    for a in raw_type.get("adapters") or []:
        adapters.append({
            "location": fmt_adapter_location(a.get("location"), a.get("slot")),
            "model": a.get("model") or "",
            "device_type": a.get("deviceType") or "",
        })
    adapters.sort(key=lambda a: a["location"])
    return adapters


# ── normalization (pure) ──────────────────────────────────────────────────────

def _group_by_uri(items: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        uri = item.get(key)
        if uri:
            groups.setdefault(uri, []).append(item)
    return groups


def build_hardware_types_list(
    raw_types: list[dict[str, Any]],
    raw_servers: list[dict[str, Any]] | None = None,
    raw_profiles: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the Server Hardware Types list view: GUI tile fields (name,
    model, per-slot adapters) plus server/profile usage counts."""
    servers_by_type = _group_by_uri(raw_servers or [], "serverHardwareTypeUri")
    profiles_by_type = _group_by_uri(raw_profiles or [], "serverHardwareTypeUri")

    result = []
    for t in raw_types:
        uri = t.get("uri", "")
        result.append({
            "name": t.get("name") or "",
            "model": t.get("model") or "",
            "form_factor": t.get("formFactor") or "",
            "adapters": normalize_adapters(t),
            "server_count": len(servers_by_type.get(uri, [])),
            "profile_count": len(profiles_by_type.get(uri, [])),
            "uri": uri,
        })
    result.sort(key=lambda x: x["name"].lower())
    return result


def build_hardware_type_detail(
    raw_type: dict[str, Any],
    raw_servers: list[dict[str, Any]] | None = None,
    raw_profiles: list[dict[str, Any]] | None = None,
    raw_templates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the enhanced Server Hardware Type detail: adapters plus every
    server hardware, server profile, and server-profile template actually
    using it (the cross-reference the GUI doesn't show at all)."""
    raw_servers = raw_servers or []
    raw_profiles = raw_profiles or []
    raw_templates = raw_templates or []
    uri = raw_type.get("uri", "")

    profile_by_server_uri = {
        p["serverHardwareUri"]: p.get("name") or ""
        for p in raw_profiles if p.get("serverHardwareUri")
    }
    server_name_by_uri = {s.get("uri"): s.get("name") or "" for s in raw_servers}

    servers = []
    for s in raw_servers:
        if s.get("serverHardwareTypeUri") != uri:
            continue
        servers.append({
            "name": s.get("name") or "",
            "status": s.get("status") or "",
            "state": s.get("state") or "",
            "power": s.get("powerState") or "",
            "profile": profile_by_server_uri.get(s.get("uri"), ""),
        })
    servers.sort(key=lambda x: x["name"].lower())

    profiles = []
    for p in raw_profiles:
        if p.get("serverHardwareTypeUri") != uri:
            continue
        profiles.append({
            "name": p.get("name") or "",
            "status": p.get("status") or "",
            "state": p.get("state") or "",
            "server": server_name_by_uri.get(p.get("serverHardwareUri"), ""),
        })
    profiles.sort(key=lambda x: x["name"].lower())

    templates = sorted(
        {t.get("name") or "" for t in raw_templates if t.get("serverHardwareTypeUri") == uri},
        key=str.lower,
    )

    return {
        "name": raw_type.get("name") or "",
        "model": raw_type.get("model") or "",
        "form_factor": raw_type.get("formFactor") or "",
        "uefi_class": raw_type.get("uefiClass") or "",
        "adapters": normalize_adapters(raw_type),
        "servers": servers,
        "profiles": profiles,
        "templates": templates,
    }


# ── fetch (I/O) ───────────────────────────────────────────────────────────────

async def fetch_hardware_types(client: "OneViewClient") -> list[dict[str, Any]]:
    """Fetch + normalize the Server Hardware Types list, enhanced with
    server/profile usage counts."""
    raw_types, raw_servers, raw_profiles = await asyncio.gather(
        client.get_all("/rest/server-hardware-types"),
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/server-profiles"),
    )
    return build_hardware_types_list(raw_types, raw_servers, raw_profiles)


async def fetch_hardware_type_detail(client: "OneViewClient", name: str) -> dict[str, Any]:
    """Fetch + normalize a single hardware type's full cross-reference:
    every server hardware, server profile, and template using it."""
    raw_types, raw_servers, raw_profiles, raw_templates = await asyncio.gather(
        client.get_all("/rest/server-hardware-types"),
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/server-profile-templates"),
    )
    matched = [t for t in raw_types if (t.get("name") or "").lower() == name.lower()]
    if not matched:
        known = ", ".join(t.get("name", "") for t in raw_types)
        raise ValueError(f"Server hardware type '{name}' not found. Known types: {known}")
    return build_hardware_type_detail(matched[0], raw_servers, raw_profiles, raw_templates)
