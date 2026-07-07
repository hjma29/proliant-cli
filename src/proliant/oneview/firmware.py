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

Note on ``list_compliance()``: the GUI's "Firmware Compliance" page shows one
row per (server, *candidate* bundle) pair — confirmed live by matching the
GUI's candidate bundle set for a hardware bay against this CLI's own
"retained newer" baselines (see ``upgrade.classify_baselines``). The actual
compliance data comes from ``POST /rest/server-hardware/firmware-compliance``
(schema reverse-engineered from HPE's own ``oneview-python`` SDK — it is NOT
documented in the OneView REST API guide): body is
``{"firmwareBaselineId": <bundle's short id, not full URI>, "serverUUID": <server-hardware's uuid, not its URI>}``,
response is ``{"componentMappingList": [...], "serverFirmwareUpdateRequired": bool}``.
The GUI's "Update Category" (Recommended/Optional) and "Estimated Update
Time" columns are computed by an internal SPP diff engine and are not
present anywhere in this response or in a bundle's own ``fwComponents``
list (checked live) — only real per-component "needs update" counts are
available, so those are surfaced instead.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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


def _candidate_compliance_bundles(stale: dict[str, Any]) -> list[dict[str, Any]]:
    """Unused baselines newer than what's assigned anywhere, regardless of repo.

    ``classify_baselines()``'s own ``retained_newer`` only covers *internal*
    (deletable) baselines — it deliberately excludes external-repository
    baselines from that bucket since those can never be reclaimed by
    ``upgrade cleanup``. But the GUI's Firmware Compliance page checks
    servers against newer candidates from **any** repository (confirmed
    live: the appliance's actual "newer unused" candidates are all hosted in
    an external repo, yet still appear as compliance candidates in the GUI).
    So for compliance purposes, combine ``retained_newer`` with any
    ``external_unused`` baseline released after the same cutoff.
    """
    from proliant.oneview.upgrade import _parse_iso

    candidates = list(stale.get("retained_newer", []))
    cutoff = _parse_iso(stale.get("cutoff_date", ""))
    if cutoff is not None:
        for b in stale.get("external_unused", []):
            released = _parse_iso(b.get("release_date", ""))
            if released is not None and released > cutoff:
                candidates.append(b)
    return candidates


async def list_compliance(client: "OneViewClient") -> list[dict[str, Any]]:
    """Per-(server, candidate-bundle) firmware compliance matrix.

    Mirrors the GUI's Firmware Compliance page shape: for every server
    profile with firmware management enabled, checks real compliance
    against each "candidate" bundle — every registered baseline that's
    newer than what's currently assigned anywhere on the appliance, from
    any repository (internal or external) — via
    ``POST /rest/server-hardware/firmware-compliance``.

    Returns one row per (server, candidate bundle) with a real count of
    components needing an update, rather than the GUI's internal-only
    Update Category / Estimated Update Time (see module docstring).
    """
    from proliant.oneview.upgrade import gather_stale_baselines

    stale = await gather_stale_baselines(client)
    candidates = _candidate_compliance_bundles(stale)
    if not candidates:
        return []

    profiles = await client.get_all("/rest/server-profiles")
    hardware = await client.get_all("/rest/server-hardware")
    hw_by_uri = {h.get("uri", ""): h for h in hardware}

    managed_profiles = [p for p in profiles if (p.get("firmware") or {}).get("manageFirmware")]
    if not managed_profiles:
        return []

    async def _check(profile: dict, bundle: dict) -> dict[str, Any] | None:
        hw = hw_by_uri.get(profile.get("serverHardwareUri") or "", {})
        server_uuid = hw.get("uuid")
        if not server_uuid:
            return None
        baseline_id = bundle["uri"].rstrip("/").rsplit("/", 1)[-1]
        try:
            result = await client.post("/rest/server-hardware/firmware-compliance", {
                "firmwareBaselineId": baseline_id,
                "serverUUID": server_uuid,
            })
        except Exception:  # noqa: BLE001 — best-effort per (server, bundle) pair
            return None
        components = result.get("componentMappingList") or []
        needing = sum(1 for c in components if c.get("componentFirmwareUpdateRequired"))
        return {
            "hardware": hw.get("name", ""),
            "model": hw.get("model", ""),
            "logical_resource": profile.get("name", ""),
            "bundle_name": bundle["name"],
            "bundle_version": bundle["version"],
            "release_date": bundle.get("release_date", ""),
            "update_required": bool(result.get("serverFirmwareUpdateRequired")),
            "components_needing_update": needing,
            "components_total": len(components),
        }

    results = await asyncio.gather(*(_check(p, b) for p in managed_profiles for b in candidates))
    rows = [r for r in results if r is not None]

    from proliant.oneview.upgrade import _parse_iso

    return sorted(rows, key=lambda r: (r["hardware"].lower(),
                                        _parse_iso(r["release_date"]) or datetime.min.replace(tzinfo=timezone.utc)))
