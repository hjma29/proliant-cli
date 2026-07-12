"""
proliant.oneview.firmware
~~~~~~~~~~~~~~~~~~~~~~
Firmware inventory via HPE OneView.

Per-server inventory (``proliant oneview servers firmware list``):
  GET /rest/server-hardware/*/firmware      → all servers' firmware in ONE call
  GET /rest/server-hardware/{id}/firmware   → single server firmware

Appliance/repository level (``proliant oneview firmware bundles|repository|compliance``):
  GET  /rest/firmware-drivers                       → registered SPP/SSP bundles
  GET  /rest/repositories                           → Internal + external Firmware Bundles repositories
  POST /rest/server-hardware/firmware-compliance     → real per-component compliance check (one server x one bundle)

``list_compliance()`` builds the same operator-facing shape as the OneView
Firmware Compliance page: resource name, currently installed/assigned baseline,
target baseline, and per-component installed-vs-target firmware comparison.
Server-profile component details come from OneView's real compliance engine
(``POST /rest/server-hardware/firmware-compliance``; the body takes the bundle's
short id and the server hardware UUID). Shared-infrastructure details are
assembled from inventory resources because OneView does not expose a single
public FLM/interconnect compliance endpoint: interconnect current versions come
from ``/rest/interconnects`` + ``{logical-interconnect-uri}/firmware`` and FLM
versions come from enclosure ``managerBays``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

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


# ── appliance / repository level (Firmware Bundles / Repositories / Compliance) ──

async def list_bundles(client: "OneViewClient") -> list[dict[str, Any]]:
    """List all registered firmware bundles (SPP/SSP), oldest -> newest by release date."""
    from proliant.oneview.upgrade import _sort_by_release_date, fetch_repositories, normalize_baselines

    raw = await client.get_all("/rest/firmware-drivers")
    repos_by_uri = {r["uri"]: r["name"] for r in await fetch_repositories(client)}

    bundles = normalize_baselines(raw)
    for b in bundles:
        locations = b.get("locations") or {}
        if locations:
            names = sorted({repos_by_uri.get(uri, name) for uri, name in locations.items()})
        else:
            # No locations entry means the bundle was uploaded directly rather
            # than referenced from a registered repository — this appliance's
            # Internal repository always shows empty repocontents/locations
            # for such bundles (verified live), so label it accordingly.
            names = ["Internal"]
        b["repository_names"] = ", ".join(names)
    return _sort_by_release_date(bundles)


async def list_repositories(client: "OneViewClient") -> list[dict[str, Any]]:
    """List firmware repositories (Internal + external) with bundle counts."""
    from proliant.oneview.upgrade import fetch_repositories, normalize_baselines

    repos = await fetch_repositories(client)
    raw_baselines = await client.get_all("/rest/firmware-drivers")
    baselines = normalize_baselines(raw_baselines)

    for r in repos:
        if "internal" in r["repository_type"].lower():
            count = sum(1 for b in baselines if not (b.get("locations") or {}))
        else:
            count = sum(1 for b in baselines if r["uri"] in (b.get("locations") or {}))
        r["bundle_count"] = count
    return repos


def _baseline_id(uri: str) -> str:
    return (uri or "").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _baseline_label(ref: dict[str, Any]) -> str:
    parts = [ref.get("name", ""), ref.get("version", "")]
    label = " ".join(part for part in parts if part)
    return label or ref.get("uri", "").rsplit("/", 1)[-1] or "—"


def _baseline_ref(
    uri: str,
    baselines_by_uri: dict[str, dict[str, Any]],
    *,
    name_hint: str = "",
) -> dict[str, str]:
    baseline = baselines_by_uri.get(uri, {})
    ref = {
        "uri": uri or baseline.get("uri", "") or "",
        "name": name_hint or baseline.get("name", "") or "",
        "version": baseline.get("version", "") or "",
    }
    ref["label"] = _baseline_label(ref)
    return ref


_COMPONENT_NAME_KEYS = ("componentName", "componentDisplayName", "name", "displayName", "componentKey")
_CURRENT_VERSION_KEYS = (
    "componentVersion",
    "installedVersion",
    "installedFirmwareVersion",
    "currentVersion",
    "fromVersion",
    "installedFirmware",
)
_TARGET_VERSION_KEYS = (
    "baselineVersion",
    "componentBaselineVersion",
    "targetVersion",
    "toVersion",
    "firmwareBaselineVersion",
    "firmwareBaselineComponentVersion",
)


def _first_text(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _norm(text: str) -> str:
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text or "").split())


def _component_version(raw: dict[str, Any]) -> str:
    return str(raw.get("componentVersion") or raw.get("version") or "")


def _target_component_version(components: list[dict[str, Any]], *labels: str) -> str:
    needles = [_norm(label) for label in labels if _norm(label)]
    if not needles:
        return ""
    for component in components or []:
        name = _norm(str(component.get("name") or component.get("componentName") or ""))
        if not name:
            continue
        if any(needle in name or name in needle for needle in needles):
            return _component_version(component)
    return ""


def _component_row(
    name: str,
    current_version: str,
    target_version: str,
    update_required: bool | None,
    *,
    location: str = "",
) -> dict[str, Any]:
    if update_required is None and current_version and target_version:
        update_required = current_version.strip() != target_version.strip()
    return {
        "name": name or "Unknown component",
        "location": location,
        "current_version": current_version or "",
        "target_version": target_version or "",
        "update_required": update_required,
    }


def _server_component_rows(
    components: list[dict[str, Any]],
    target_components: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for component in components or []:
        name = _first_text(component, _COMPONENT_NAME_KEYS)
        current = _first_text(component, _CURRENT_VERSION_KEYS)
        target = _first_text(component, _TARGET_VERSION_KEYS)
        if not target:
            target = _target_component_version(target_components, name, str(component.get("componentKey") or ""))
        required = component.get("componentFirmwareUpdateRequired")
        if required is None:
            required = component.get("firmwareUpdateRequired", component.get("updateRequired"))
        rows.append(_component_row(
            name,
            current,
            target,
            bool(required) if required is not None else None,
            location=str(component.get("componentLocation") or component.get("location") or ""),
        ))
    return rows


def _row(
    *,
    kind: str,
    resource_name: str,
    current_baseline: dict[str, str],
    target_baseline: dict[str, str],
    components: list[dict[str, Any]],
    hardware: str = "",
    model: str = "",
    error: str = "",
) -> dict[str, Any]:
    known_components = [c for c in components if c.get("update_required") is not None]
    updates = sum(1 for c in components if c.get("update_required") is True)
    update_required = any(c.get("update_required") is True for c in components)
    row = {
        "kind": kind,
        "resource_name": resource_name,
        "hardware": hardware,
        "model": model,
        "current_baseline": current_baseline,
        "current_baseline_label": current_baseline["label"],
        "target_baseline": target_baseline,
        "target_baseline_label": target_baseline["label"],
        "update_required": update_required,
        "components_needing_update": updates,
        "components_total": len(known_components) if known_components else len(components),
        "components": components,
    }
    if error:
        row["error"] = error
    return row


def _location_value(raw: dict[str, Any], location_field: str, key: str) -> str:
    for entry in (raw.get(location_field) or {}).get("locationEntries", []):
        if entry.get("type") == key:
            return str(entry.get("value") or "")
    return ""


def _interconnect_enclosure_uri(raw: dict[str, Any]) -> str:
    return _location_value(raw, "interconnectLocation", "Enclosure")


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _logical_interconnects_for_le(le: dict[str, Any], logical_interconnects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    li_uris = set(le.get("logicalInterconnectUris") or [])
    if li_uris:
        return [li for li in logical_interconnects if li.get("uri") in li_uris]
    enc_uris = set(le.get("enclosureUris") or [])
    return [
        li for li in logical_interconnects
        if enc_uris and enc_uris.intersection(set(li.get("enclosureUris") or []))
    ]


def _current_shared_infra_baseline(
    le: dict[str, Any] | None,
    li_fw_by_uri: dict[str, dict[str, Any]],
    logical_interconnects: list[dict[str, Any]],
    baselines_by_uri: dict[str, dict[str, Any]],
) -> dict[str, str]:
    if le:
        current_refs = [
            li_fw_by_uri.get(li.get("uri", ""))
            for li in _logical_interconnects_for_le(le, logical_interconnects)
        ]
        current_refs = [ref for ref in current_refs if ref and ref.get("sppUri")]
        uris = {ref.get("sppUri", "") for ref in current_refs}
        if len(uris) == 1:
            ref = current_refs[0]
            return _baseline_ref(ref.get("sppUri", ""), baselines_by_uri, name_hint=ref.get("sppName", ""))
        uri = ((le.get("firmware") or {}).get("firmwareBaselineUri") or "")
        return _baseline_ref(uri, baselines_by_uri)
    return _baseline_ref("", baselines_by_uri)


async def _li_firmware_by_uri(client: "OneViewClient", logical_interconnects: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    li_uris = sorted({li.get("uri", "") for li in logical_interconnects if li.get("uri")})
    async def _safe_get(uri: str) -> dict[str, Any]:
        try:
            return await client.get(f"{uri}/firmware")
        except Exception as exc:  # noqa: BLE001 - this optional LI sub-resource is best-effort
            return {"error": str(exc)}

    results = await asyncio.gather(*(_safe_get(uri) for uri in li_uris))
    return dict(zip(li_uris, results))


async def list_compliance(client: "OneViewClient", baseline: str | None = None) -> list[dict[str, Any]]:
    """Firmware compliance matrix for server profiles, FLMs, and interconnects.

    *baseline* matches a registered SSP/SPP by version, short name, URI id, or
    substring. When omitted, the latest registered ServicePack baseline is used.
    """
    from proliant.oneview.ssp_update import FW_DRIVERS_URI, select_baseline, service_pack_baselines
    from proliant.oneview.upgrade import normalize_baselines

    raw_drivers = await client.get_all(FW_DRIVERS_URI)
    service_packs = service_pack_baselines(raw_drivers)
    target = select_baseline(service_packs, baseline)
    if target is None:
        if baseline:
            known = ", ".join(b.get("version") or b.get("name", "") for b in service_packs)
            raise ValueError(f"Firmware baseline '{baseline}' not found. Known baselines: {known}")
        raise ValueError("No registered SSP/SPP firmware baselines found on this appliance.")

    baselines_by_uri = {b.get("uri", ""): b for b in normalize_baselines(raw_drivers)}
    raw_driver_by_uri = {d.get("uri", ""): d for d in raw_drivers}
    target_ref = _baseline_ref(target.get("uri", ""), baselines_by_uri)
    target_components = raw_driver_by_uri.get(target.get("uri", ""), {}).get("fwComponents") or []
    target_id = _baseline_id(target.get("uri", ""))

    profiles, hardware, interconnects, enclosures, logical_interconnects, logical_enclosures = await asyncio.gather(
        client.get_all("/rest/server-profiles"),
        client.get_all("/rest/server-hardware"),
        client.get_all("/rest/interconnects"),
        client.get_all("/rest/enclosures"),
        client.get_all("/rest/logical-interconnects"),
        client.get_all("/rest/logical-enclosures"),
    )
    li_fw_by_uri = await _li_firmware_by_uri(client, logical_interconnects)

    rows: list[dict[str, Any]] = []
    hw_by_uri = {h.get("uri", ""): h for h in hardware}

    async def _server_profile_row(profile: dict[str, Any]) -> dict[str, Any] | None:
        hw = hw_by_uri.get(profile.get("serverHardwareUri") or "", {})
        server_uuid = hw.get("uuid")
        if not server_uuid:
            return None
        profile_fw = profile.get("firmware") or {}
        try:
            result = await client.post("/rest/server-hardware/firmware-compliance", {
                "firmwareBaselineId": target_id,
                "serverUUID": server_uuid,
            })
        except Exception as exc:  # noqa: BLE001 - keep remaining resources visible
            return _row(
                kind="server-profile",
                resource_name=profile.get("name", ""),
                hardware=hw.get("name", ""),
                model=hw.get("model", ""),
                current_baseline=_baseline_ref(profile_fw.get("firmwareBaselineUri", ""), baselines_by_uri),
                target_baseline=target_ref,
                components=[_component_row(
                    "Compliance check failed",
                    "",
                    "",
                    None,
                    location=str(exc),
                )],
                error=str(exc),
            )
        components = _server_component_rows(result.get("componentMappingList") or [], target_components)
        if not components:
            components = [_component_row(
                "Server firmware",
                "",
                "",
                bool(result.get("serverFirmwareUpdateRequired")),
            )]
        return _row(
            kind="server-profile",
            resource_name=profile.get("name", ""),
            hardware=hw.get("name", ""),
            model=hw.get("model", ""),
            current_baseline=_baseline_ref(profile_fw.get("firmwareBaselineUri", ""), baselines_by_uri),
            target_baseline=target_ref,
            components=components,
        )

    profile_rows = await asyncio.gather(*(
        _server_profile_row(profile)
        for profile in profiles
        if profile.get("serverHardwareUri")
    ))
    rows.extend(row for row in profile_rows if row is not None)

    le_by_enclosure_uri: dict[str, dict[str, Any]] = {}
    for le in logical_enclosures:
        for enclosure_uri in le.get("enclosureUris") or []:
            le_by_enclosure_uri[enclosure_uri] = le

    for enclosure in enclosures:
        enclosure_uri = enclosure.get("uri", "")
        current = _current_shared_infra_baseline(
            le_by_enclosure_uri.get(enclosure_uri),
            li_fw_by_uri,
            logical_interconnects,
            baselines_by_uri,
        )
        for manager in enclosure.get("managerBays") or []:
            bay = _as_int(manager.get("bayNumber") or manager.get("bay"))
            if not bay:
                continue
            model = manager.get("model", "") or "Frame Link Module"
            target_version = _target_component_version(target_components, model, "Frame Link Module")
            components = [_component_row(
                model,
                str(manager.get("fwVersion") or ""),
                target_version,
                None,
                location=f"Bay {bay}",
            )]
            rows.append(_row(
                kind="frame-link-module",
                resource_name=f"{enclosure.get('name', '')}, frame link module {bay}",
                model=model,
                current_baseline=current,
                target_baseline=target_ref,
                components=components,
            ))

    le_by_enclosure_for_ic = le_by_enclosure_uri
    for interconnect in interconnects:
        li_uri = interconnect.get("logicalInterconnectUri", "")
        li_fw = li_fw_by_uri.get(li_uri, {})
        enc_uri = _interconnect_enclosure_uri(interconnect)
        current = (
            _baseline_ref(li_fw.get("sppUri", ""), baselines_by_uri, name_hint=li_fw.get("sppName", ""))
            if li_fw.get("sppUri")
            else _current_shared_infra_baseline(
                le_by_enclosure_for_ic.get(enc_uri),
                li_fw_by_uri,
                logical_interconnects,
                baselines_by_uri,
            )
        )
        product = interconnect.get("productName") or interconnect.get("model", "")
        target_version = _target_component_version(target_components, product, interconnect.get("model", ""))
        components = [_component_row(
            product,
            str(interconnect.get("firmwareVersion") or ""),
            target_version,
            None,
        )]
        rows.append(_row(
            kind="interconnect",
            resource_name=interconnect.get("name", ""),
            model=interconnect.get("model", ""),
            current_baseline=current,
            target_baseline=target_ref,
            components=components,
        ))

    kind_order = {"server-profile": 0, "frame-link-module": 1, "interconnect": 2}
    return sorted(
        rows,
        key=lambda r: (
            kind_order.get(r["kind"], 99),
            r["resource_name"].lower(),
        ),
    )
