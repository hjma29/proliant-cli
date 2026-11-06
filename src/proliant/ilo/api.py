"""
proliant.ilo.api
~~~~~~~~~~~~~~~~
Stable, machine-friendly iLO inventory API for scripts and AI agents.

Every resource fetcher returns plain JSON-serializable data (dicts, lists,
strings, numbers, booleans, None) with stable keys. ``collect()`` queries
many servers concurrently and isolates failures: an unreachable server or a
failing resource is reported as ``{"error": "..."}`` without affecting the
others. The CLI exposes the same data as ``proliant ilo inventory`` (always
JSON) and ``proliant ilo --json servers describe``.

Schema changes that remove or rename keys bump ``SCHEMA_VERSION``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from proliant.ilo import inventory
from proliant.ilo.bios import _SECTIONS as _BIOS_SECTIONS, _nic_boot_keys, fetch_bios
from proliant.ilo.boot import fetch_boot_order
from proliant.ilo.client import ILOClient, ilo_session
from proliant.ilo.config import MAX_WORKERS

SCHEMA_VERSION = 1


def _uid_state(system: dict[str, Any]) -> str | None:
    # iLO 7 exposes LocationIndicatorActive (bool); iLO 6 IndicatorLED (str).
    active = system.get("LocationIndicatorActive")
    if isinstance(active, bool):
        return "Lit" if active else "Off"
    return system.get("IndicatorLED")


async def fetch_summary(client: ILOClient) -> dict[str, Any]:
    """Identity, power/health/POST state, UID light and iLO details."""
    system = await client.get(await client.get_system_uri())
    manager = await client.get(await client.get_manager_uri())
    hpe = (system.get("Oem") or {}).get("Hpe") or {}
    return {
        "model": system.get("Model"),
        "serial": system.get("SerialNumber"),
        "product_id": system.get("SKU"),
        "uuid": system.get("UUID"),
        "os_hostname": system.get("HostName") or None,
        "bios_version": system.get("BiosVersion"),
        "power_state": system.get("PowerState"),
        "health": (system.get("Status") or {}).get("Health"),
        "post_state": hpe.get("PostState"),
        "uid": _uid_state(system),
        "ilo": {
            "model": manager.get("Model"),
            "firmware": manager.get("FirmwareVersion"),
            "hostname": manager.get("HostName") or None,
            "health": (manager.get("Status") or {}).get("Health"),
        },
    }


async def fetch_power(client: ILOClient) -> dict[str, Any]:
    """Power state plus what happens after AC power is lost and restored."""
    system = await client.get(await client.get_system_uri())
    hpe = (system.get("Oem") or {}).get("Hpe") or {}
    try:
        bios = await fetch_bios(client)
    except Exception:  # noqa: BLE001 -- BIOS attributes are best-effort here
        bios = {}
    return {
        "power_state": system.get("PowerState"),
        "post_state": hpe.get("PostState"),
        # Standard Redfish property (newer firmware); HPE OEM / BIOS equivalents below.
        "power_restore_policy": system.get("PowerRestorePolicy"),
        "auto_power_on": hpe.get("PowerAutoOn") or bios.get("AutoPowerOn"),
        "power_on_delay": bios.get("PowerOnDelay"),
    }


async def fetch_firmware(client: ILOClient) -> list[dict[str, Any]]:
    """Every firmware inventory component."""
    return [
        {
            "name": item.get("Name"),
            "version": item.get("Version"),
            "updateable": item.get("Updateable"),
            "device_context": ((item.get("Oem") or {}).get("Hpe") or {}).get("DeviceContext"),
        }
        for item in await inventory.fetch_firmware_inventory_full(client)
    ]


async def fetch_memory(client: ILOClient) -> dict[str, Any]:
    """Populated DIMMs and totals."""
    dimms = await inventory.fetch_memory_report_data(client)
    return {
        "dimm_count": len(dimms),
        "total_gb": sum(d.get("capacity_gb") or 0 for d in dimms),
        "dimms": dimms,
    }


async def fetch_bios_settings(client: ILOClient) -> dict[str, Any]:
    """Key BIOS settings grouped by section (current, not pending, values)."""
    attrs = await fetch_bios(client)
    sections = {
        section: {key: attrs[key] for key, _ in keys if key in attrs}
        for section, keys in _BIOS_SECTIONS
    }
    nic_boot = {key: attrs[key] for key, _ in _nic_boot_keys(attrs)}
    if nic_boot:
        sections["NIC Boot"] = nic_boot
    return {name: values for name, values in sections.items() if values}


async def fetch_license(client: ILOClient) -> dict[str, Any]:
    return {key.lower(): value for key, value in await inventory.fetch_license_info(client)}


RESOURCES: dict[str, Callable[[ILOClient], Awaitable[Any]]] = {
    "summary": fetch_summary,
    "power": fetch_power,
    "firmware": fetch_firmware,
    "cpu": inventory.fetch_cpu_report_data,
    "gpu": inventory.fetch_gpu_report_data,
    "memory": fetch_memory,
    "storage": inventory.fetch_storage_report_data,
    "network": inventory.fetch_network_report_data,
    "network_ilo": inventory.fetch_ilo_nic_details,
    "boot": fetch_boot_order,
    "bios": fetch_bios_settings,
    "license": fetch_license,
}

RESOURCE_DESCRIPTIONS: dict[str, str] = {
    "summary": "model, serial, product ID, power, health, POST state, UID, BIOS version, iLO model/firmware",
    "power": "power and POST state, and auto power-on behaviour after an AC power loss",
    "firmware": "every firmware component with version and updateability",
    "cpu": "processors: socket, model, cores, threads, speed",
    "gpu": "installed GPUs",
    "memory": "populated DIMMs with part numbers, totals",
    "storage": "storage controllers and drives (RAID vs direct-attached)",
    "network": "host NIC adapters and ports: MAC, link, speed, LLDP neighbor",
    "network_ilo": "iLO dedicated NIC: MAC, link, IPv4, DNS, LLDP neighbor",
    "boot": "boot mode, one-time override and boot order",
    "bios": "key BIOS settings (workload profile, power, boot, virtualization, security, serial console)",
    "license": "iLO license",
}

DEFAULT_RESOURCES: tuple[str, ...] = ("summary",)


def parse_resources(value: str | None) -> list[str]:
    """Parse ``"summary,gpu"`` / ``"all"`` into a validated resource list."""
    if not value:
        return list(DEFAULT_RESOURCES)
    names = [part.strip().replace("-", "_") for part in value.split(",") if part.strip()]
    if names == ["all"]:
        return list(RESOURCES)
    unknown = [name for name in names if name not in RESOURCES]
    if unknown:
        raise ValueError(f"Unknown resource(s): {', '.join(unknown)}. Valid: all, {', '.join(RESOURCES)}")
    return list(dict.fromkeys(names))


async def collect_server(host: dict, resources: list[str]) -> dict[str, Any]:
    """Fetch resources for one server. Resources run sequentially in one
    session -- iLO 6 does not handle concurrent requests per session reliably."""
    out: dict[str, Any] = {}
    try:
        async with ilo_session(host) as client:
            for name in resources:
                try:
                    out[name] = await RESOURCES[name](client)
                except Exception as exc:  # noqa: BLE001 -- isolate per-resource failures
                    out[name] = {"error": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 -- unreachable/auth failures
        return {"error": f"{type(exc).__name__}: {exc}"}
    return out


async def collect(hosts: list[dict], resources: list[str] | None = None,
                  max_workers: int = MAX_WORKERS) -> dict[str, Any]:
    """Collect resources from many servers concurrently.

    Returns ``{"schema_version", "collected_at", "resources", "servers": {name: {resource: data}}}``.
    """
    names = list(resources or DEFAULT_RESOURCES)
    unknown = [name for name in names if name not in RESOURCES]
    if unknown:
        raise ValueError(f"Unknown resource(s): {', '.join(unknown)}")
    semaphore = asyncio.Semaphore(max_workers)

    async def one(host: dict) -> tuple[str, dict[str, Any]]:
        async with semaphore:
            return host["name"], await collect_server(host, names)

    results = await asyncio.gather(*(one(host) for host in hosts))
    return {
        "schema_version": SCHEMA_VERSION,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "resources": names,
        "servers": dict(results),
    }


def query(resources: list[str] | None = None, server: str | None = None) -> dict[str, Any]:
    """Synchronous convenience wrapper: load inventory.ini hosts (all, one, or
    comma-separated) and collect resources from them."""
    from proliant.ilo.config import load_hosts

    if server and "," in server:
        hosts = [host for name in server.split(",") if name.strip() for host in load_hosts(name.strip())]
    else:
        hosts = load_hosts(server) if server else load_hosts()
    return asyncio.run(collect(hosts, resources))
