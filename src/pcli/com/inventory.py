"""
pcli.com.inventory
~~~~~~~~~~~~~~~~~~~
Hardware inventory reports from HPE COM.

Uses the /compute-ops-mgmt/v1beta2/servers/{id}/inventory endpoint,
which is a cached Redfish mirror collected by COM from each iLO.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcli.com.client import COMClient


_SKIP_STATUSES = {"NotPresent", "Unknown", ""}


async def _get_memory_inventory(client: "COMClient", base_url: str, server: dict) -> list[dict]:
    """Fetch DIMM list for one server. Returns [] on error."""
    rid = server["id"]
    name = server.get("name", rid)
    try:
        inv = await client.get(f"{base_url}/compute-ops-mgmt/v1beta2/servers/{rid}/inventory")
        dimms = inv.get("memory", {}).get("data", [])
        result = []
        for d in dimms:
            oem = d.get("Oem", {}).get("Hpe", {})
            status = oem.get("DIMMStatus", "")
            if status in _SKIP_STATUSES:
                continue
            cap_mib = d.get("CapacityMiB", 0) or 0
            result.append({
                "server":     name,
                "hpe_pn":     oem.get("PartNumber", "Unknown"),
                "vendor":     oem.get("VendorName", "") or d.get("Manufacturer", ""),
                "capacity_gb": cap_mib // 1024,
                "type":       d.get("BaseModuleType", ""),
                "speed_mts":  oem.get("MaxOperatingSpeedMTs", 0) or 0,
                "status":     status,
                "locator":    d.get("DeviceLocator", ""),
            })
        return result
    except Exception:
        return []


async def get_fleet_memory(client: "COMClient", base_url: str) -> list[dict]:
    """Return all populated DIMMs across the whole fleet, concurrently."""
    r = await client.get(f"{base_url}/compute-ops-mgmt/v1beta2/servers", params={"limit": 1000})
    servers = r.get("items", [])

    tasks = [_get_memory_inventory(client, base_url, s) for s in servers]
    results = await asyncio.gather(*tasks)

    dimms = []
    for batch in results:
        dimms.extend(batch)
    return dimms


_SKIP_GPU_MANUFACTURERS = {"", "intel"}


async def _get_gpu_inventory(client: "COMClient", base_url: str, server: dict) -> list[dict]:
    """Fetch discrete GPU list for one server. Returns [] on error or no GPUs."""
    rid = server["id"]
    name = server.get("name", rid)
    try:
        inv = await client.get(f"{base_url}/compute-ops-mgmt/v1beta2/servers/{rid}/inventory")
        procs = inv.get("processor", {}).get("data", [])
        result = []
        for p in procs:
            if (p.get("ProcessorType") or "").upper() != "GPU":
                continue
            mfr = (p.get("Manufacturer") or "").strip()
            if mfr.lower() in _SKIP_GPU_MANUFACTURERS:
                continue  # skip embedded video controllers
            result.append({
                "server":      name,
                "gpu":         (p.get("Name") or "—").strip(),
                "part_number": (p.get("PartNumber") or p.get("Model") or "—").strip(),
                "manufacturer": mfr,
                "serial":      (p.get("SerialNumber") or "—").strip(),
            })
        return result
    except Exception:
        return []


async def get_fleet_gpus(client: "COMClient", base_url: str) -> list[dict]:
    """Return all discrete GPUs across the whole fleet, concurrently."""
    r = await client.get(f"{base_url}/compute-ops-mgmt/v1beta2/servers", params={"limit": 1000})
    servers = r.get("items", [])

    tasks = [_get_gpu_inventory(client, base_url, s) for s in servers]
    results = await asyncio.gather(*tasks)

    gpus: list[dict] = []
    for batch in results:
        gpus.extend(batch)
    return gpus


def aggregate_gpus_by_model(gpus: list[dict]) -> list[dict]:
    """Group GPUs by (name, part_number). Returns rows sorted by count desc."""
    groups: dict[tuple, dict] = {}
    for g in gpus:
        key = (g["gpu"], g["part_number"])
        if key not in groups:
            groups[key] = {
                "gpu":         g["gpu"],
                "part_number": g["part_number"],
                "manufacturer": g["manufacturer"],
                "count":       0,
                "servers":     set(),
            }
        groups[key]["count"] += 1
        groups[key]["servers"].add(g["server"])
    rows = list(groups.values())
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows


def aggregate_by_part_number(dimms: list[dict]) -> list[dict]:
    """
    Group DIMMs by HPE part number.
    Returns rows sorted by total count desc.
    Each row: hpe_pn, vendor, capacity_gb, type, speed_mts, count, servers (set)
    """
    groups: dict[str, dict] = {}
    for d in dimms:
        key = d["hpe_pn"]
        if key not in groups:
            groups[key] = {
                "hpe_pn":      d["hpe_pn"],
                "vendor":      d["vendor"],
                "capacity_gb": d["capacity_gb"],
                "type":        d["type"],
                "speed_mts":   d["speed_mts"],
                "count":       0,
                "servers":     set(),
            }
        groups[key]["count"] += 1
        groups[key]["servers"].add(d["server"])

    rows = list(groups.values())
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows
