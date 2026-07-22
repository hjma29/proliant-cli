"""
proliant.oneview.server_detail
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Read-only "describe" model for a single OneView-managed server hardware — the
data behind the GUI's *Server Hardware -> Overview* page (as distinct from the
*Server Profile* page): Hardware, Management Processor, Device Inventory, and
Utilization.

  GET /rest/server-hardware                 -> locate the server by name
  GET /rest/server-hardware-types/{id}      -> resolved "Server hardware type"
  GET /rest/server-profiles/{id}            -> resolved "Server profile" name
  GET /rest/server-hardware/{id}/firmware   -> Device Inventory table
  GET /rest/server-hardware/{id}/utilization -> CPU / Power / Temperature gauges

The formatting helpers are pure functions (no I/O) so they're unit-tested
directly; ``fetch_server_detail`` is the only coroutine and is exercised with
a fake client like the rest of the oneview suite.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── value formatting (pure) ───────────────────────────────────────────────────

_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def fmt_state(state: str | None) -> str:
    """Turn a camelCase OneView state into spaced words, e.g. ``ProfileApplied``
    -> ``Profile Applied``, ``NoProfileApplied`` -> ``No Profile Applied``."""
    if not state:
        return "—"
    return _CAMEL_BOUNDARY.sub(" ", state)


def fmt_memory_gb(memory_mb: int | float | None) -> str:
    """Render memory in MB as whole/decimal GB, e.g. ``262144`` -> ``256 GB``."""
    if not memory_mb:
        return "—"
    gb = memory_mb / 1024
    if gb == int(gb):
        return f"{int(gb)} GB"
    return f"{gb:.1f} GB"


def fmt_cpu(
    processor_count: int | None,
    processor_type: str | None,
    processor_speed_mhz: int | float | None,
    processor_core_count: int | None,
) -> str:
    """Render the GUI's CPU summary, e.g.
    ``2 processors, Intel(R) Xeon(R) Silver 4214R CPU (2.40 GHz / 12-core)``."""
    if not processor_count and not processor_type:
        return "—"
    count = processor_count or 0
    plural = "processor" if count == 1 else "processors"
    model = (processor_type or "").split(" @ ")[0].strip() or "—"
    tail_bits = []
    if processor_speed_mhz:
        tail_bits.append(f"{processor_speed_mhz / 1000:.2f} GHz")
    if processor_core_count:
        tail_bits.append(f"{processor_core_count}-core")
    tail = f" ({' / '.join(tail_bits)})" if tail_bits else ""
    return f"{count} {plural}, {model}{tail}"


def fmt_ip_addresses(mp_host_info: dict[str, Any] | None) -> list[str]:
    """Order management-processor IPs the way the GUI does: routable/DHCP
    addresses first, link-local addresses last."""
    addrs = (mp_host_info or {}).get("mpIpAddresses") or []
    routed = [a["address"] for a in addrs if a.get("address") and a.get("type", "").lower() != "linklocal"]
    link_local = [a["address"] for a in addrs if a.get("address") and a.get("type", "").lower() == "linklocal"]
    return routed + link_local


def fmt_temperature_f(celsius: float | int | None) -> int | None:
    """Convert an ambient-temperature sample (°C, as OneView stores it) to the
    whole-number °F the GUI displays."""
    if celsius is None:
        return None
    return round(celsius * 9 / 5 + 32)


# ── normalization (pure) ──────────────────────────────────────────────────────

# Device Inventory only lists physical adapters/drives/batteries with a real
# slot location — OS/driver-level firmware components (blank location) and the
# system-board entry itself (covered by "System ROM" already) are excluded,
# matching what the GUI's Device Inventory table shows vs. its separate
# Firmware tab.
_DEVICE_INVENTORY_EXCLUDE_LOCATIONS = {"", "System Board"}


def normalize_device_inventory(firmware_payload: dict[str, Any] | None) -> list[dict[str, str]]:
    """Filter+normalize ``/rest/server-hardware/{id}/firmware`` into the GUI's
    Device Inventory rows (Location, Product Name, Firmware Version)."""
    components = (firmware_payload or {}).get("components") or []
    devices = []
    for c in components:
        location = (c.get("componentLocation") or "").strip()
        if location in _DEVICE_INVENTORY_EXCLUDE_LOCATIONS:
            continue
        devices.append({
            "location": location,
            "name": c.get("componentName") or "",
            "version": c.get("componentVersion") or "",
        })
    devices.sort(key=lambda d: d["location"].lower())
    return devices


def normalize_utilization(utilization_payload: dict[str, Any] | None) -> dict[str, Any]:
    """Extract the newest sample of each gauge the Overview page shows."""
    metrics = {m.get("metricName"): m for m in (utilization_payload or {}).get("metricList") or []}

    def _latest(name: str) -> float | int | None:
        samples = (metrics.get(name) or {}).get("metricSamples") or []
        return samples[0][1] if samples else None

    return {
        "cpu_percent": _latest("CpuUtilization"),
        "power_w": _latest("AveragePower"),
        "temperature_f": fmt_temperature_f(_latest("AmbientTemperature")),
    }


def build_server_detail(
    raw: dict[str, Any],
    profile_name: str | None,
    hardware_type_name: str | None,
    firmware_payload: dict[str, Any] | None,
    utilization_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the normalized server-describe model from the raw payloads."""
    mp_host_info = raw.get("mpHostInfo") or {}
    core_count = raw.get("processorCoreCount")
    proc_count = raw.get("processorCount")
    cores_total = (proc_count or 0) * (core_count or 0) or None

    util = normalize_utilization(utilization_payload)
    if cores_total and util.get("cpu_percent") is not None:
        util["cpu_cores_total"] = cores_total

    return {
        "name": raw.get("name") or "",
        "status": raw.get("status") or "",
        "state": raw.get("state") or "",
        "state_display": fmt_state(raw.get("state")),
        "power": raw.get("powerState") or "",
        "server_name": raw.get("serverName") or "",
        "profile": profile_name,
        "profile_uri": raw.get("serverProfileUri"),
        "model": raw.get("model") or "",
        "operating_system": raw.get("operatingSystem") or "",
        "cpu": fmt_cpu(proc_count, raw.get("processorType"), raw.get("processorSpeedMhz"), core_count),
        "memory": fmt_memory_gb(raw.get("memoryMb")),
        "serial_number": raw.get("serialNumber") or "",
        "location": raw.get("name") or "",
        "system_rom": raw.get("romVersion") or "",
        "hardware_type": hardware_type_name,
        "hardware_type_uri": raw.get("serverHardwareTypeUri"),
        "mp_model": raw.get("mpModel") or "",
        "mp_firmware_version": raw.get("mpFirmwareVersion") or "",
        "mp_host_name": mp_host_info.get("mpHostName") or "",
        "mp_ip_addresses": fmt_ip_addresses(mp_host_info),
        "device_inventory": normalize_device_inventory(firmware_payload),
        "utilization": util,
    }


# ── fetch (I/O) ───────────────────────────────────────────────────────────────

async def fetch_server_detail(client: "OneViewClient", name: str) -> dict[str, Any]:
    """Fetch + normalize the server-describe model over the OneView REST API."""
    servers = await client.get_all("/rest/server-hardware")
    matched = [s for s in servers if (s.get("name") or "").lower() == name.lower()]
    if not matched:
        known = ", ".join(s.get("name", "") for s in servers)
        raise ValueError(f"Server '{name}' not found. Known servers: {known}")
    raw = matched[0]

    profile_name: str | None = None
    if raw.get("serverProfileUri"):
        try:
            profile = await client.get(raw["serverProfileUri"])
            profile_name = profile.get("name")
        except Exception:  # noqa: BLE001 — best-effort name resolution
            profile_name = None

    hardware_type_name: str | None = None
    if raw.get("serverHardwareTypeUri"):
        try:
            hwt = await client.get(raw["serverHardwareTypeUri"])
            hardware_type_name = hwt.get("name")
        except Exception:  # noqa: BLE001 — best-effort name resolution
            hardware_type_name = None

    try:
        firmware_payload = await client.get(f"{raw['uri']}/firmware")
    except Exception:  # noqa: BLE001 — device inventory is best-effort/optional
        firmware_payload = None

    try:
        utilization_payload = await client.get(f"{raw['uri']}/utilization")
    except Exception:  # noqa: BLE001 — utilization is best-effort/optional
        utilization_payload = None

    return build_server_detail(raw, profile_name, hardware_type_name, firmware_payload, utilization_payload)
