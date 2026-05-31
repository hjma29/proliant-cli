"""
hpeilo.inventory
~~~~~~~~~~~~~~~~
Read-only async fetch functions for iLO Redfish inventory.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from pcli.ilo.client import ILOClient

_PORT_RE = re.compile(r"\s*\b(?:\d+-port|dual[ -]port|quad[ -]port)\b", re.IGNORECASE)
_MODEL_STRIP_RE = re.compile(r"^\s*(?:HPE\s+)?(?:ProLiant\s+)?(?:Compute\s+)?", re.IGNORECASE)
_EMPTY: list[tuple[str, str]] = [("N/A", "N/A")]
FLEET_KEYS = ("Model", "iLO", "BIOS", "NIC-FW", "Storage-FW")


async def _collection_members(client: ILOClient, collection_uri: str) -> list[dict[str, Any]]:
    return (await client.get(collection_uri)).get("Members", [])


async def _member_resources(client: ILOClient, collection_uri: str) -> list[dict[str, Any]]:
    members = await _collection_members(client, collection_uri)
    coros = [client.get(item["@odata.id"]) for item in members if "@odata.id" in item]
    return list(await asyncio.gather(*coros)) if coros else []


async def _resource_list(client: ILOClient, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    coros = [client.get(item["@odata.id"]) for item in links if "@odata.id" in item]
    return list(await asyncio.gather(*coros)) if coros else []


async def fetch_all_firmware(client: ILOClient) -> list[tuple[str, str]]:
    inventory_uri = await client.get_firmware_inventory_uri()
    members = await _member_resources(client, inventory_uri)
    return [(item.get("Name", "N/A"), item.get("Version", "N/A")) for item in members]


async def fetch_firmware_inventory_full(client: ILOClient) -> list[dict]:
    return await _member_resources(client, await client.get_firmware_inventory_uri())


async def fetch_nic_firmware_inventory(client: ILOClient) -> list[dict]:
    chassis = await client.get(await client.get_chassis_uri())
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    if not na_uri:
        return []

    entries = []
    seen: set[str] = set()
    for adapter in await _member_resources(client, na_uri):
        chip_model = adapter.get("Model", "")
        sku = adapter.get("SKU", "")
        name = sku or chip_model or adapter.get("Name", "N/A")
        controllers = adapter.get("Controllers", [])
        version = controllers[0].get("FirmwarePackageVersion", "N/A") if controllers else "N/A"
        key = f"{chip_model}:{version}"
        if key in seen or not chip_model:
            continue
        seen.add(key)
        entries.append({
            "Name": name,
            "Version": version or "N/A",
            "Updateable": True,
            "chip_model": chip_model,
        })
    return entries


async def fetch_ilo_version(client: ILOClient) -> list[tuple[str, str]]:
    for item in await _member_resources(client, await client.get_firmware_inventory_uri()):
        name = item.get("Name", "")
        if "ilo" in name.lower():
            return [(name, item.get("Version", "N/A"))]
    return _EMPTY


async def fetch_network_versions(client: ILOClient) -> list[tuple[str, str]]:
    chassis = await client.get(await client.get_chassis_uri())
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    if not na_uri:
        return _EMPTY

    found = []
    for adapter in await _member_resources(client, na_uri):
        label = adapter.get("Model") or adapter.get("Name", "N/A")
        label = _PORT_RE.sub("", label).strip()
        controllers = adapter.get("Controllers", [])
        version = controllers[0].get("FirmwarePackageVersion", "N/A") if controllers else "N/A"
        found.append((label, version or "N/A"))
    return found or _EMPTY


async def _storage_members(client: ILOClient) -> list[dict[str, Any]]:
    system = await client.get(await client.get_system_uri())
    storage_uri = system.get("Storage", {}).get("@odata.id")
    if not storage_uri:
        return []
    return await _member_resources(client, storage_uri)


async def fetch_storage_versions(client: ILOClient) -> list[tuple[str, str]]:
    found = []
    seen_ctrl_names: set[str] = set()

    for storage in await _storage_members(client):
        ctrl_link = (storage.get("Controllers") or {}).get("@odata.id")
        if ctrl_link:
            for ctrl in await _member_resources(client, ctrl_link):
                name = ctrl.get("Model") or ctrl.get("Name", "N/A")
                fw = ctrl.get("FirmwareVersion") or "N/A"
                if name not in seen_ctrl_names:
                    found.append((name, fw))
                    seen_ctrl_names.add(name)

        for ctrl in storage.get("StorageControllers", []):
            name = ctrl.get("Model") or ctrl.get("Name", "N/A")
            fw = ctrl.get("FirmwareVersion") or "N/A"
            if name not in seen_ctrl_names:
                found.append((name, fw))
                seen_ctrl_names.add(name)

        for drive in await _resource_list(client, storage.get("Drives", [])):
            fw = drive.get("FirmwareVersion") or ""
            if fw:
                found.append((drive.get("Name", "N/A"), fw))

    if not found:
        storage_keywords = (
            "sata controller", "nvme", "raid", "storage controller", "boot controller",
            "smart array", "mr4", "mr8", "p408", "p816",
        )
        for item in await _member_resources(client, await client.get_firmware_inventory_uri()):
            name = item.get("Name", "")
            if any(keyword in name.lower() for keyword in storage_keywords):
                found.append((name, item.get("Version") or "N/A"))

    return found or _EMPTY


def _drive_identifiers(drive: dict) -> str:
    parts = []
    for ident in drive.get("Identifiers", []):
        fmt = (ident.get("DurableNameFormat") or "?").upper()
        val = (ident.get("DurableName") or "").lower().replace("-", "").replace(":", "")
        if val:
            parts.append(f"{fmt}:{val}")
    return "  ".join(parts) if parts else "N/A"


async def fetch_disk_map(client: ILOClient) -> list[tuple[str, str]]:
    found = []
    for storage in await _storage_members(client):
        vol_uri = storage.get("Volumes", {}).get("@odata.id")
        if not vol_uri:
            continue
        for vol in await _member_resources(client, vol_uri):
            lun = vol.get("LogicalUnitNumber", "?")
            raid = vol.get("RAIDType") or vol.get("VolumeType") or "N/A"
            capacity_bytes = vol.get("CapacityBytes") or 0
            capacity_str = f"{capacity_bytes / (1024**3):.0f}GiB" if capacity_bytes else "N/A"
            vol_eui = next(
                (
                    (i.get("DurableName") or "").lower()
                    for i in vol.get("Identifiers", [])
                    if (i.get("DurableNameFormat") or "").upper() in ("EUI", "NGUID", "NAA")
                ),
                "N/A",
            )
            vol_fmt = next(
                (
                    (i.get("DurableNameFormat") or "").upper()
                    for i in vol.get("Identifiers", [])
                    if (i.get("DurableNameFormat") or "").upper() in ("EUI", "NGUID", "NAA")
                ),
                "",
            )
            label = f"LUN:{lun:<3} {raid:<6} {capacity_str:<9} {vol_fmt}:{vol_eui}"
            drives = await _resource_list(client, vol.get("Links", {}).get("Drives", []))
            bays = []
            for drive in drives:
                loc = drive.get("PhysicalLocation", {}).get("PartLocation", {})
                bay_label = loc.get("ServiceLabel") or f"Bay {loc.get('LocationOrdinalValue', '?')}"
                serial = drive.get("SerialNumber") or "N/A"
                bays.append(f"{bay_label}(Serial:{serial})")
            found.append((label, "  ".join(bays) if bays else "N/A"))
    return found or _EMPTY


async def fetch_disk_map_raw(client: ILOClient) -> list[tuple[str, str]]:
    found = []
    for storage in await _storage_members(client):
        vol_uri = storage.get("Volumes", {}).get("@odata.id")
        if vol_uri:
            for vol_link in await _collection_members(client, vol_uri):
                uri = vol_link.get("@odata.id")
                if uri:
                    found.append((uri, json.dumps(await client.get(uri), indent=2, default=str)))
        for drive_link in storage.get("Drives", []):
            uri = drive_link.get("@odata.id")
            if uri:
                found.append((uri, json.dumps(await client.get(uri), indent=2, default=str)))
    return found or _EMPTY


async def fetch_cpu_info(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    proc_uri = system.get("Processors", {}).get("@odata.id")
    if not proc_uri:
        return _EMPTY

    found = []
    for proc in await _member_resources(client, proc_uri):
        proc_type = (proc.get("ProcessorType") or "").upper()
        label = proc.get("Name", "N/A") if proc_type == "GPU" else proc.get("Model", "N/A")
        microcode = (proc.get("ProcessorId") or {}).get("MicrocodeInfo", "N/A")
        found.append((label, microcode or "N/A"))
    return found or _EMPTY


async def fetch_memory_info(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    memory_uri = system.get("Memory", {}).get("@odata.id")
    if not memory_uri:
        return _EMPTY

    found = []
    for dimm in await _member_resources(client, memory_uri):
        capacity_mib = dimm.get("CapacityMiB")
        if not capacity_mib:
            continue
        part = (dimm.get("PartNumber") or "").strip() or dimm.get("Name", "N/A")
        label = f"{part} ({capacity_mib} MiB)"
        found.append((label, dimm.get("FirmwareRevision", "N/A") or "N/A"))
    return found or _EMPTY


async def _build_nic_label_map(client: ILOClient) -> dict[str, str]:
    chassis = await client.get(await client.get_chassis_uri())
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    if not na_uri:
        return {}

    label_map: dict[str, str] = {}
    for adapter in await _member_resources(client, na_uri):
        model = (adapter.get("Model") or adapter.get("Name") or "NIC").strip()
        model = _PORT_RE.sub("", model).strip()
        controllers = adapter.get("Controllers", [])
        slot_label = (
            controllers[0].get("Location", {}).get("PartLocation", {}).get("ServiceLabel", "")
            if controllers else ""
        ) or ""
        short_model = re.sub(r"\W+$", "", model[:35])
        abbrev_slot = re.sub(r"\bSlot (\S+)", r"S_\1", slot_label) if slot_label else ""
        label_prefix = f"{abbrev_slot}({short_model})" if slot_label else short_model

        hpe_ports = adapter.get("Oem", {}).get("Hpe", {}).get("PhysicalPorts", [])
        if hpe_ports:
            for port in hpe_ports:
                mac = (port.get("MacAddress") or "").lower().strip()
                port_num = port.get("PortNumber", "?")
                if mac:
                    label_map[mac] = f"{label_prefix}p{port_num}"
            continue

        ndf_col = adapter.get("NetworkDeviceFunctions", {}).get("@odata.id")
        if not ndf_col:
            continue
        for port_idx, ndf in enumerate(await _member_resources(client, ndf_col), start=1):
            label = f"{label_prefix}p{port_idx}"
            mac = ((ndf.get("Ethernet") or {}).get("PermanentMACAddress") or "").lower().strip()
            if mac:
                label_map[mac] = label
                continue
            for eth_link in ndf.get("Links", {}).get("EthernetInterfaces", []):
                uri = eth_link.get("@odata.id", "")
                if uri:
                    label_map[uri] = label
    return label_map


async def fetch_nic_status(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    eth_uri = system.get("EthernetInterfaces", {}).get("@odata.id")
    if not eth_uri:
        return _EMPTY

    label_map = await _build_nic_label_map(client)
    found = []
    for item, iface in zip(await _collection_members(client, eth_uri), await _member_resources(client, eth_uri)):
        raw_name = iface.get("Name") or ""
        iface_id = iface.get("Id") or ""
        if raw_name and raw_name != iface_id:
            name = raw_name
        else:
            mac_key = (iface.get("MACAddress") or "").lower().strip()
            name = label_map.get(mac_key) or label_map.get(item.get("@odata.id", "")) or iface_id or "N/A"
        link = iface.get("LinkStatus") or iface.get("Status", {}).get("State") or "N/A"
        mac = iface.get("MACAddress") or "N/A"
        speed = iface.get("SpeedMbps")
        speed_str = f"  {speed} Mbps" if speed else ""
        found.append((name, f"{link:<10} {mac}{speed_str}"))
    return found or _EMPTY


async def _fetch_members_raw(client: ILOClient, collection_uri: str) -> list[tuple[str, str]]:
    rows = []
    for item in await _collection_members(client, collection_uri):
        uri = item.get("@odata.id")
        if uri:
            rows.append((uri, json.dumps(await client.get(uri), indent=2, default=str)))
    return rows or _EMPTY


async def fetch_nic_raw(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    uri = system.get("EthernetInterfaces", {}).get("@odata.id")
    return await _fetch_members_raw(client, uri) if uri else _EMPTY


async def fetch_network_raw(client: ILOClient) -> list[tuple[str, str]]:
    chassis = await client.get(await client.get_chassis_uri())
    uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    return await _fetch_members_raw(client, uri) if uri else _EMPTY


async def fetch_storage_raw(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    uri = system.get("Storage", {}).get("@odata.id")
    return await _fetch_members_raw(client, uri) if uri else _EMPTY


async def fetch_cpu_raw(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    uri = system.get("Processors", {}).get("@odata.id")
    return await _fetch_members_raw(client, uri) if uri else _EMPTY


async def fetch_memory_raw(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    uri = system.get("Memory", {}).get("@odata.id")
    return await _fetch_members_raw(client, uri) if uri else _EMPTY


async def fetch_firmware_raw(client: ILOClient) -> list[tuple[str, str]]:
    return await _fetch_members_raw(client, await client.get_firmware_inventory_uri())


async def fetch_com_raw(client: ILOClient) -> list[tuple[str, str]]:
    manager_uri = await client.get_manager_uri()
    manager = await client.get(manager_uri)
    return [(manager_uri, json.dumps(manager, indent=2, default=str))]


async def fetch_com_status(client: ILOClient) -> list[tuple[str, str]]:
    manager = await client.get(await client.get_manager_uri())
    cloud = manager.get("Oem", {}).get("Hpe", {}).get("CloudConnect", {})
    if not cloud:
        return [("CloudConnect", "Not supported on this iLO version")]

    extended = cloud.get("ExtendedStatusInfo", {})
    return [
        ("CloudConnectStatus", cloud.get("CloudConnectStatus", "N/A")),
        ("ConnectionType", cloud.get("ConnectionType", "N/A")),
        ("WorkspaceId", cloud.get("WorkspaceId") or "(not registered)"),
        ("NetworkConfig", extended.get("NetworkConfig", "N/A")),
        ("WebConnectivity", extended.get("WebConnectivity", "N/A")),
        ("iLOConfigForCloud", extended.get("iLOConfigForCloudConnect", "N/A")),
    ]


async def fetch_serial_info(client: ILOClient) -> list[tuple[str, str]]:
    system = await client.get(await client.get_system_uri())
    raw_model = system.get("Model", "N/A")
    model = _MODEL_STRIP_RE.sub("", raw_model).strip() or raw_model
    serial = system.get("SerialNumber", "N/A") or "N/A"
    sku = system.get("SKU", "N/A") or "N/A"
    return [("Model", model), ("Serial", serial), ("ProductID", sku)]


async def fetch_fleet_summary(client: ILOClient) -> list[tuple[str, str]]:
    # Fetch system + manager in parallel — both are available without hitting
    # firmware inventory (which can be slow on some iLOs)
    sys_uri, mgr_uri = await asyncio.gather(
        client.get_system_uri(),
        client.get_manager_uri(),
    )
    system, manager = await asyncio.gather(
        client.get(sys_uri),
        client.get(mgr_uri),
    )

    model = system.get("Model", "N/A")
    bios = system.get("BiosVersion", "N/A") or "N/A"

    # iLO: compose "iLO 7 1.21.00 Apr 07 2026" from manager fields
    mgr_model = manager.get("Model", "")       # e.g. "iLO 7"
    mgr_fw    = manager.get("FirmwareVersion", "N/A") or "N/A"
    # iLO 6 includes model in FirmwareVersion ("iLO 6 v1.74"); iLO 7 does not
    if mgr_model and not mgr_fw.startswith(mgr_model):
        ilo_str = f"{mgr_model} {mgr_fw}".strip()
    else:
        ilo_str = mgr_fw

    chassis = await client.get(await client.get_chassis_uri())
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    nic_ver = "N/A"
    if na_uri:
        na_members = await _collection_members(client, na_uri)
        if na_members:
            adapter = await client.get(na_members[0]["@odata.id"])
            controllers = adapter.get("Controllers", [])
            nic_ver = (controllers[0].get("FirmwarePackageVersion", "N/A") if controllers else "N/A") or "N/A"

    storage_ver = "N/A"
    storage_uri = system.get("Storage", {}).get("@odata.id")
    if storage_uri:
        s_members = await _collection_members(client, storage_uri)
        if s_members:
            storage = await client.get(s_members[0]["@odata.id"])
            controllers = storage.get("StorageControllers", [])
            if controllers:
                storage_ver = controllers[0].get("FirmwareVersion", "N/A") or "N/A"
            else:
                ctrl_link = (storage.get("Controllers") or {}).get("@odata.id")
                if ctrl_link:
                    ctrl_members = await _collection_members(client, ctrl_link)
                    if ctrl_members:
                        ctrl = await client.get(ctrl_members[0]["@odata.id"])
                        storage_ver = ctrl.get("FirmwareVersion", "N/A") or "N/A"

    return [
        ("Model",      model),
        ("iLO",        ilo_str),
        ("BIOS",       bios),
        ("NIC-FW",     nic_ver),
        ("Storage-FW", storage_ver),
    ]
