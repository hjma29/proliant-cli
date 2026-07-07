"""
proliant.oneview.firmware
~~~~~~~~~~~~~~~~~~~~~~
Firmware inventory via HPE OneView.

Per-server inventory (``proliant oneview servers firmware list``):
  GET /rest/server-hardware/*/firmware      → all servers' firmware in ONE call
  GET /rest/server-hardware/{id}/firmware   → single server firmware

Appliance/repository level (``proliant oneview firmware bundles|repository|compliance``):
  GET /rest/firmware-drivers   → registered SPP/SSP bundles
  GET /rest/repositories       → Internal + external Firmware Bundles repositories
  GET /rest/server-profiles    → per-profile firmware.consistencyState (drift signal)
  GET /rest/server-hardware    → Model / Hardware name for the compliance join

Note: the GUI's "Firmware Compliance" tab shows Update Category / Estimated
Update Time columns that are computed by OneView's internal SPP
component-diff engine — that data isn't exposed via any documented REST
endpoint (confirmed live: no ``/rest/*compliance*`` endpoint returns it).
``list_compliance()`` instead surfaces the real, available signal: each
server profile's own ``firmware.consistencyState``.
"""

from __future__ import annotations

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


async def list_compliance(client: "OneViewClient") -> list[dict[str, Any]]:
    """Per-server-profile firmware compliance vs each profile's assigned bundle.

    Uses ``firmware.consistencyState`` — OneView's own drift indicator for
    managed firmware — rather than the GUI's Update Category / Estimated
    Update Time columns, which are computed by an internal SPP
    component-diff engine not exposed via the public REST API.
    """
    from proliant.oneview.upgrade import normalize_baselines

    profiles = await client.get_all("/rest/server-profiles")
    hardware = await client.get_all("/rest/server-hardware")
    raw_baselines = await client.get_all("/rest/firmware-drivers")

    hw_by_uri = {h.get("uri", ""): h for h in hardware}
    baseline_by_uri = {b["uri"].split("?")[0]: b for b in normalize_baselines(raw_baselines) if b["uri"]}

    rows: list[dict[str, Any]] = []
    for p in profiles:
        fw = p.get("firmware") or {}
        hw = hw_by_uri.get(p.get("serverHardwareUri") or "", {})
        baseline_uri = (fw.get("firmwareBaselineUri") or "").split("?")[0]
        baseline = baseline_by_uri.get(baseline_uri)
        managed = bool(fw.get("manageFirmware"))
        rows.append({
            "hardware": hw.get("name", ""),
            "model": hw.get("model", ""),
            "logical_resource": p.get("name", ""),
            "bundle_name": baseline["name"] if baseline else "",
            "bundle_version": baseline["version"] if baseline else "",
            "managed": managed,
            "consistency_state": fw.get("consistencyState", "Unknown") if managed else "Not managed",
        })
    return sorted(rows, key=lambda r: r["hardware"].lower())
