"""
hpeilo.inventory
~~~~~~~~~~~~~~~~
Read-only fetch functions — every function here performs only GET requests.

All functions accept an already-authenticated RedfishClient and return
``list[tuple[str, str]]`` — a list of (label, version_or_info) pairs.
This uniform return type lets cli.py use a single dispatch table and a
single generic table printer for all modes.

Redfish endpoint references (iLO 7 v1.20):
  Full inventory  : /redfish/v1/UpdateService/FirmwareInventory
  NIC firmware    : /redfish/v1/Chassis/1/NetworkAdapters/{id}
                    → Controllers[0].FirmwarePackageVersion, Model
  Storage firmware: /redfish/v1/Systems/1/Storage/{id}
                    → StorageControllers[].FirmwareVersion / Drives[].FirmwareVersion
  CPU microcode   : /redfish/v1/Systems/1/Processors/{id}
                    → ProcessorId.MicrocodeInfo, Model
  DIMM info       : /redfish/v1/Systems/1/Memory/{id}
                    → FirmwareRevision, CapacityMiB, PartNumber
"""

import json
import re

from redfish import RedfishClient

from pcli.ilo.client import (
    get_chassis_uri,
    get_firmware_inventory_uri,
    get_manager_uri,
    get_system_uri,
)

# Strip port-count phrases from NIC model names (e.g. "2-Port", "Dual Port")
_PORT_RE = re.compile(r"\s*\b(?:\d+-port|dual[ -]port|quad[ -]port)\b", re.IGNORECASE)

# Sentinel used when a fetch returns no results
_EMPTY: list[tuple[str, str]] = [("N/A", "N/A")]


def fetch_all_firmware(client: RedfishClient) -> list[tuple[str, str]]:
    """Return (name, version) for every entry in FirmwareInventory."""
    inventory_uri = get_firmware_inventory_uri(client)
    members = client.get(inventory_uri).obj.get("Members", [])
    return [
        (resp.dict.get("Name", "N/A"), resp.dict.get("Version", "N/A"))
        for item in members
        for resp in [client.get(item["@odata.id"])]
    ]


def fetch_firmware_inventory_full(client: RedfishClient) -> list[dict]:
    """Return full FirmwareInventory dicts including Updateable flag.

    Each dict has at minimum: Name, Version, Updateable, SoftwareId.
    Used by the --fw-upgrade command to know which components can actually
    be flashed via iLO.
    """
    inventory_uri = get_firmware_inventory_uri(client)
    members = client.get(inventory_uri).obj.get("Members", [])
    return [client.get(item["@odata.id"]).obj for item in members]


def fetch_nic_firmware_inventory(client: RedfishClient) -> list[dict]:
    """Return NIC firmware as FirmwareInventory-style dicts for upgrade matching.

    NICs do not appear in FirmwareInventory, so this supplements it.
    Each dict has: Name, Version, Updateable, chip_model.

    The ``chip_model`` key (e.g. "BCM57414") is used by sdr.find_upgrades()
    to match against SDR NIC packages (e.g. BCM235.1.164.14_BCM957414A4142HC.fwpkg).
    """
    chassis = client.get(get_chassis_uri(client)).obj
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    if not na_uri:
        return []

    entries = []
    seen: set[str] = set()
    for item in client.get(na_uri).obj.get("Members", []):
        adapter = client.get(item["@odata.id"]).obj
        chip_model = adapter.get("Model", "")       # e.g. "BCM57414"
        sku = adapter.get("SKU", "")                # e.g. "10/25Gb 2-port SFP28 BCM57414 OCP3 Adapter"
        name = sku or chip_model or adapter.get("Name", "N/A")
        controllers = adapter.get("Controllers", [])
        version = controllers[0].get("FirmwarePackageVersion", "N/A") if controllers else "N/A"

        # De-duplicate: same chip on different ports shows identical chip_model+version
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


def fetch_ilo_version(client: RedfishClient) -> list[tuple[str, str]]:
    """Return [(name, version)] for the iLO firmware entry.

    Stops at the first match — avoids fetching all N inventory members
    when we only care about iLO itself.
    """
    inventory_uri = get_firmware_inventory_uri(client)
    members = client.get(inventory_uri).obj.get("Members", [])
    for item in members:
        resp = client.get(item["@odata.id"])
        name = resp.dict.get("Name", "")
        if "ilo" in name.lower():
            return [(name, resp.dict.get("Version", "N/A"))]
    return _EMPTY


def fetch_network_versions(client: RedfishClient) -> list[tuple[str, str]]:
    """Return (model, firmware_version) for every NIC via /Chassis/1/NetworkAdapters/.

    Uses the typed NetworkAdapter schema — no keyword string matching on names.
    Redfish field: Controllers[0].FirmwarePackageVersion  (iLO 7 v1.11+)
    """
    chassis = client.get(get_chassis_uri(client)).obj
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    if not na_uri:
        return _EMPTY

    found = []
    for item in client.get(na_uri).obj.get("Members", []):
        adapter = client.get(item["@odata.id"]).obj
        label = adapter.get("Model") or adapter.get("Name", "N/A")
        label = _PORT_RE.sub("", label).strip()
        controllers = adapter.get("Controllers", [])
        version = controllers[0].get("FirmwarePackageVersion", "N/A") if controllers else "N/A"
        found.append((label, version or "N/A"))
    return found or _EMPTY


def fetch_storage_versions(client: RedfishClient) -> list[tuple[str, str]]:
    """Return (name, firmware_version) for storage controllers and drives.

    Handles two Redfish patterns:
      Gen10 and older: StorageControllers[] inline in each Storage member
      Gen11+:          Controllers sub-collection at Storage/{id}/Controllers/

    Falls back to FirmwareInventory for servers with no Storage members
    (e.g. servers with no drives attached to the SATA/NVMe controller).
    Drives with no firmware version are omitted — controllers are always shown.
    """
    system = client.get(get_system_uri(client)).obj
    storage_uri = system.get("Storage", {}).get("@odata.id")

    found = []
    seen_ctrl_names: set[str] = set()

    if storage_uri:
        for item in client.get(storage_uri).obj.get("Members", []):
            storage = client.get(item["@odata.id"]).obj

            # Gen11+ pattern: Controllers sub-collection
            ctrl_link = (storage.get("Controllers") or {}).get("@odata.id")
            if ctrl_link:
                for c in client.get(ctrl_link).obj.get("Members", []):
                    ctrl = client.get(c["@odata.id"]).obj
                    name = ctrl.get("Model") or ctrl.get("Name", "N/A")
                    fw   = ctrl.get("FirmwareVersion") or "N/A"
                    if name not in seen_ctrl_names:
                        found.append((name, fw))
                        seen_ctrl_names.add(name)

            # Gen10 pattern: StorageControllers[] inline
            for ctrl in storage.get("StorageControllers", []):
                name = ctrl.get("Model") or ctrl.get("Name", "N/A")
                fw   = ctrl.get("FirmwareVersion") or "N/A"
                if name not in seen_ctrl_names:
                    found.append((name, fw))
                    seen_ctrl_names.add(name)

            # Drives — only show those with a known firmware version
            for drive_link in storage.get("Drives", []):
                drive = client.get(drive_link["@odata.id"]).obj
                fw = drive.get("FirmwareVersion") or ""
                if fw:
                    found.append((drive.get("Name", "N/A"), fw))

    # Fallback: if Storage has no members, pull storage-related items from
    # FirmwareInventory (covers servers with no drives, e.g. dl325-gen12)
    if not found:
        _STORAGE_KEYWORDS = ("sata controller", "nvme", "raid", "storage controller",
                             "boot controller", "smart array", "mr4", "mr8", "p408", "p816")
        fw_inv = client.get("/redfish/v1/UpdateService/FirmwareInventory").obj
        for m in fw_inv.get("Members", []):
            item = client.get(m["@odata.id"]).obj
            name = item.get("Name", "")
            if any(kw in name.lower() for kw in _STORAGE_KEYWORDS):
                found.append((name, item.get("Version") or "N/A"))

    return found or _EMPTY


def _drive_identifiers(drive: dict) -> str:
    """Return all Identifiers[] entries as 'FORMAT:value' strings joined by spaces.

    iLO drives may expose NAA, NGUID, EUI64, or other formats.
    - NAA (8 bytes / 16 hex chars)  → matches ``lsblk -d -o NAME,WWN``
    - NGUID (16 bytes / 32 hex chars) → matches ``lsblk -d -o NAME,SERIAL`` for NVMe
    Showing all formats lets you correlate whichever field Linux exposes.
    """
    parts = []
    for ident in drive.get("Identifiers", []):
        fmt = (ident.get("DurableNameFormat") or "?").upper()
        val = (ident.get("DurableName") or "").lower().replace("-", "").replace(":", "")
        if val:
            parts.append(f"{fmt}:{val}")
    return "  ".join(parts) if parts else "N/A"


def fetch_disk_map(client: RedfishClient) -> list[tuple[str, str]]:
    """Return (volume_label, drive_bays) for every SmartArray logical volume.

    Linux (via SmartArray) sees *Volumes*, not raw drives.  The definitive
    mapping is via the Volume NAA identifier which Linux exposes as the wwid:

      /sys/block/<dev>/device/wwid  →  "naa.600062b21c0531c030544f..."
      Volume.Identifiers[NAA]       →  "600062b21c0531c030544f..."  (strip "naa." prefix)

    On CoreOS debug pod (oc debug node/<node> -- chroot /host):
      for d in sda sdb sdc sdd; do
        echo "$d: $(cat /sys/block/$d/device/wwid)"
      done
    """
    system = client.get(get_system_uri(client)).obj
    storage_uri = system.get("Storage", {}).get("@odata.id")
    if not storage_uri:
        return _EMPTY

    found = []
    for item in client.get(storage_uri).obj.get("Members", []):
        storage = client.get(item["@odata.id"]).obj

        vol_uri = storage.get("Volumes", {}).get("@odata.id")
        if not vol_uri:
            continue
        for vol_link in client.get(vol_uri).obj.get("Members", []):
            vol = client.get(vol_link["@odata.id"]).obj

            lun = vol.get("LogicalUnitNumber", "?")
            raid = vol.get("RAIDType") or vol.get("VolumeType") or "N/A"
            capacity_bytes = vol.get("CapacityBytes") or 0
            capacity_str = f"{capacity_bytes / (1024**3):.0f}GiB" if capacity_bytes else "N/A"

            # EUI from Volume.Identifiers — matches VPD page 83 on Linux
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

            # Physical drives this volume spans
            bays = []
            for drive_link in vol.get("Links", {}).get("Drives", []):
                drive = client.get(drive_link["@odata.id"]).obj
                loc = drive.get("PhysicalLocation", {}).get("PartLocation", {})
                bay_label = loc.get("ServiceLabel") or f"Bay {loc.get('LocationOrdinalValue', '?')}"
                serial = drive.get("SerialNumber") or "N/A"
                bays.append(f"{bay_label}(Serial:{serial})")

            found.append((label, "  ".join(bays) if bays else "N/A"))
    return found or _EMPTY


def fetch_disk_map_raw(client: RedfishClient) -> list[tuple[str, str]]:
    """Return raw JSON for each Drive and Volume URI.

    Volumes are shown first because their OEM WWN identifier is what Linux
    exposes as /sys/block/<dev>/device/wwid (NAA-6 format).
    """
    system = client.get(get_system_uri(client)).obj
    storage_uri = system.get("Storage", {}).get("@odata.id")
    if not storage_uri:
        return _EMPTY
    found = []
    for item in client.get(storage_uri).obj.get("Members", []):
        storage = client.get(item["@odata.id"]).obj
        # Volumes first — contain the SmartArray logical-drive WWN
        vol_uri = storage.get("Volumes", {}).get("@odata.id")
        if vol_uri:
            for vol_link in client.get(vol_uri).obj.get("Members", []):
                uri = vol_link["@odata.id"]
                found.append((uri, json.dumps(client.get(uri).obj, indent=2)))
        for drive_link in storage.get("Drives", []):
            uri = drive_link["@odata.id"]
            found.append((uri, json.dumps(client.get(uri).obj, indent=2)))
    return found or _EMPTY


def fetch_cpu_info(client: RedfishClient) -> list[tuple[str, str]]:
    """Return (model, microcode_version) for each processor.

    Redfish field: ProcessorId.MicrocodeInfo
    """
    system = client.get(get_system_uri(client)).obj
    proc_uri = system.get("Processors", {}).get("@odata.id")
    if not proc_uri:
        return _EMPTY

    found = []
    for item in client.get(proc_uri).obj.get("Members", []):
        proc = client.get(item["@odata.id"]).obj
        proc_type = (proc.get("ProcessorType") or "").upper()
        if proc_type == "GPU":
            label = proc.get("Name", "N/A")
        else:
            label = proc.get("Model", "N/A")
        microcode = (proc.get("ProcessorId") or {}).get("MicrocodeInfo", "N/A")
        found.append((label, microcode or "N/A"))
    return found or _EMPTY


def fetch_memory_info(client: RedfishClient) -> list[tuple[str, str]]:
    """Return (part_number + capacity, firmware_revision) for populated DIMMs.

    Skips empty slots (CapacityMiB is null/0 for absent DIMMs).
    Redfish fields: PartNumber, CapacityMiB, FirmwareRevision
    """
    system = client.get(get_system_uri(client)).obj
    memory_uri = system.get("Memory", {}).get("@odata.id")
    if not memory_uri:
        return _EMPTY

    found = []
    for item in client.get(memory_uri).obj.get("Members", []):
        dimm = client.get(item["@odata.id"]).obj
        capacity_mib = dimm.get("CapacityMiB")
        if not capacity_mib:
            continue  # empty slot
        part = (dimm.get("PartNumber") or "").strip() or dimm.get("Name", "N/A")
        label = f"{part} ({capacity_mib} MiB)"
        found.append((label, dimm.get("FirmwareRevision", "N/A") or "N/A"))
    return found or _EMPTY


def _build_nic_label_map(client: RedfishClient) -> dict[str, str]:
    """Build {mac_lower: "slot_label port N"} by walking NetworkAdapters.

    Two paths tried in order:
      1. HPE OEM (iLO 6 / Gen11+): Oem.Hpe.PhysicalPorts[].MacAddress + PortNumber
         Slot label from Controllers[0].Location.PartLocation.ServiceLabel
         e.g. "OCP Slot B port 1", "PCI-E Slot 6 port 2"
      2. Standard Redfish NDF: NetworkDeviceFunctions → Ethernet.PermanentMACAddress
         or Links.EthernetInterfaces URI (stored as URI key, not MAC key)

    Returns an empty dict if the chassis has no NetworkAdapters collection.
    """
    chassis = client.get(get_chassis_uri(client)).obj
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    if not na_uri:
        return {}

    label_map: dict[str, str] = {}
    for adapter_item in client.get(na_uri).obj.get("Members", []):
        adapter = client.get(adapter_item["@odata.id"]).obj
        model = (adapter.get("Model") or adapter.get("Name") or "NIC").strip()
        model = _PORT_RE.sub("", model).strip()

        controllers = adapter.get("Controllers", [])
        slot_label = (
            controllers[0]
            .get("Location", {})
            .get("PartLocation", {})
            .get("ServiceLabel", "")
            if controllers
            else ""
        ) or ""
        short_model = re.sub(r'\W+$', '', model[:35])
        abbrev_slot = re.sub(r'\bSlot (\S+)', r'S_\1', slot_label) if slot_label else ""
        label_prefix = f"{abbrev_slot}({short_model})" if slot_label else short_model

        # ── HPE OEM path ─────────────────────────────────────────────────────
        hpe_ports = adapter.get("Oem", {}).get("Hpe", {}).get("PhysicalPorts", [])
        if hpe_ports:
            for port in hpe_ports:
                mac = (port.get("MacAddress") or "").lower().strip()
                port_num = port.get("PortNumber", "?")
                if mac:
                    label_map[mac] = f"{label_prefix}p{port_num}"
            continue  # HPE path handled; skip NDF approach for this adapter

        # ── Standard Redfish NDF fallback ────────────────────────────────────
        ndf_col = adapter.get("NetworkDeviceFunctions", {}).get("@odata.id")
        if not ndf_col:
            continue
        ndfs = client.get(ndf_col).obj.get("Members", [])
        for port_idx, ndf_item in enumerate(ndfs, start=1):
            ndf = client.get(ndf_item["@odata.id"]).obj
            label = f"{label_prefix}p{port_idx}"
            mac = ((ndf.get("Ethernet") or {}).get("PermanentMACAddress") or "").lower().strip()
            if mac:
                label_map[mac] = label
                continue
            for eth_link in ndf.get("Links", {}).get("EthernetInterfaces", []):
                uri = eth_link.get("@odata.id", "")
                if uri:
                    label_map[uri] = label  # URI key for URI-based fallback below

    return label_map


def fetch_nic_status(client: RedfishClient) -> list[tuple[str, str]]:
    """Return (port_name, link_status + MAC) for each EthernetInterface on the system.

    Port name priority:
      1. EthernetInterface Name  — if it differs from the bare Id
      2. NetworkAdapter-derived  — "OCP Slot B port 1" / "PCI-E Slot 6 port 2" etc.
         matched by MAC address (HPE OEM) or NDF URI link (standard Redfish)
      3. Id                      — last resort

    Redfish path: GET /Systems/1/EthernetInterfaces/{id}
    """
    system = client.get(get_system_uri(client)).obj
    eth_uri = system.get("EthernetInterfaces", {}).get("@odata.id")
    if not eth_uri:
        return _EMPTY

    label_map = _build_nic_label_map(client)

    found = []
    for item in client.get(eth_uri).obj.get("Members", []):
        iface = client.get(item["@odata.id"]).obj
        raw_name = iface.get("Name") or ""
        iface_id = iface.get("Id") or ""
        if raw_name and raw_name != iface_id:
            name = raw_name
        else:
            mac_key = (iface.get("MACAddress") or "").lower().strip()
            name = (
                label_map.get(mac_key)
                or label_map.get(item["@odata.id"])  # URI-based fallback
                or iface_id
                or "N/A"
            )
        link = (
            iface.get("LinkStatus")
            or iface.get("Status", {}).get("State")
            or "N/A"
        )
        mac = iface.get("MACAddress") or "N/A"
        speed = iface.get("SpeedMbps")
        speed_str = f"  {speed} Mbps" if speed else ""
        found.append((name, f"{link:<10} {mac}{speed_str}"))
    return found or _EMPTY


# ---------------------------------------------------------------------------
# Raw JSON fetchers (--raw flag) — one per mode
# ---------------------------------------------------------------------------

def _fetch_members_raw(client: RedfishClient, collection_uri: str) -> list[tuple[str, str]]:
    """Return (member_uri, raw_json) for every member of a Redfish collection."""
    members = client.get(collection_uri).obj.get("Members", [])
    return [
        (item["@odata.id"], json.dumps(client.get(item["@odata.id"]).dict, indent=2))
        for item in members
    ] or _EMPTY


def fetch_nic_raw(client: RedfishClient) -> list[tuple[str, str]]:
    system = client.get(get_system_uri(client)).obj
    uri = system.get("EthernetInterfaces", {}).get("@odata.id")
    return _fetch_members_raw(client, uri) if uri else _EMPTY


def fetch_network_raw(client: RedfishClient) -> list[tuple[str, str]]:
    chassis = client.get(get_chassis_uri(client)).obj
    uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    return _fetch_members_raw(client, uri) if uri else _EMPTY


def fetch_storage_raw(client: RedfishClient) -> list[tuple[str, str]]:
    system = client.get(get_system_uri(client)).obj
    uri = system.get("Storage", {}).get("@odata.id")
    return _fetch_members_raw(client, uri) if uri else _EMPTY


def fetch_cpu_raw(client: RedfishClient) -> list[tuple[str, str]]:
    system = client.get(get_system_uri(client)).obj
    uri = system.get("Processors", {}).get("@odata.id")
    return _fetch_members_raw(client, uri) if uri else _EMPTY


def fetch_memory_raw(client: RedfishClient) -> list[tuple[str, str]]:
    system = client.get(get_system_uri(client)).obj
    uri = system.get("Memory", {}).get("@odata.id")
    return _fetch_members_raw(client, uri) if uri else _EMPTY


def fetch_firmware_raw(client: RedfishClient) -> list[tuple[str, str]]:
    return _fetch_members_raw(client, get_firmware_inventory_uri(client))


def fetch_com_raw(client: RedfishClient) -> list[tuple[str, str]]:
    manager_uri = get_manager_uri(client)
    manager = client.get(manager_uri)
    return [(manager_uri, json.dumps(manager.dict, indent=2))]


def fetch_com_status(client: RedfishClient) -> list[tuple[str, str]]:
    """Return HPE Compute Ops Management registration status.

    Redfish path: GET /Managers/1 → Oem.Hpe.CloudConnect

    Fields returned as (label, value) pairs:
      CloudConnectStatus   — "Connected" / "NotConnected" / "Connecting" etc.
      ConnectionType       — "Gateway" etc.
      WorkspaceId          — COM workspace UUID (empty if not registered)
      NetworkConfig        — Extended status
      WebConnectivity      — Extended status
      iLOConfigForCloud    — Extended status
    """
    manager = client.get(get_manager_uri(client)).obj
    cloud = manager.get("Oem", {}).get("Hpe", {}).get("CloudConnect", {})
    if not cloud:
        return [("CloudConnect", "Not supported on this iLO version")]

    extended = cloud.get("ExtendedStatusInfo", {})
    return [
        ("CloudConnectStatus",    cloud.get("CloudConnectStatus", "N/A")),
        ("ConnectionType",        cloud.get("ConnectionType", "N/A")),
        ("WorkspaceId",           cloud.get("WorkspaceId") or "(not registered)"),
        ("NetworkConfig",         extended.get("NetworkConfig", "N/A")),
        ("WebConnectivity",       extended.get("WebConnectivity", "N/A")),
        ("iLOConfigForCloud",     extended.get("iLOConfigForCloudConnect", "N/A")),
    ]


# Prefixes to strip from model strings (HPE marketing noise)
_MODEL_STRIP_RE = re.compile(
    r"^\s*(?:HPE\s+)?(?:ProLiant\s+)?(?:Compute\s+)?",
    re.IGNORECASE,
)


def fetch_serial_info(client: RedfishClient) -> list[tuple[str, str]]:
    """Return server identity: trimmed model, serial number, and product ID (SKU).

    Returns:
        [("Model", <trimmed model>), ("Serial", <serial>), ("ProductID", <sku>)]
    """
    system = client.get(get_system_uri(client)).obj
    raw_model = system.get("Model", "N/A")
    model = _MODEL_STRIP_RE.sub("", raw_model).strip() or raw_model
    serial = system.get("SerialNumber", "N/A") or "N/A"
    sku = system.get("SKU", "N/A") or "N/A"
    return [
        ("Model",     model),
        ("Serial",    serial),
        ("ProductID", sku),
    ]


# Fixed keys returned by fetch_fleet_summary — used by the fleet table printer
FLEET_KEYS = ("Model", "iLO", "BIOS", "NIC-FW", "Storage-FW")


def fetch_fleet_summary(client: RedfishClient) -> list[tuple[str, str]]:
    """Return a compact fixed set of key firmware/hardware facts for fleet comparison.

    Returns exactly these keys (in order), matching FLEET_KEYS:
      Model       — server model string
      iLO         — iLO generation + version
      BIOS        — System ROM version
      NIC-FW      — first NIC adapter firmware version
      Storage-FW  — first storage controller firmware version

    Designed for the --fleet view where servers are rows and these fields
    are the columns.
    """
    results: dict[str, str] = {k: "N/A" for k in FLEET_KEYS}

    # Model
    system = client.get(get_system_uri(client)).obj
    results["Model"] = system.get("Model", "N/A")

    # iLO + BIOS from FirmwareInventory
    inv_uri = get_firmware_inventory_uri(client)
    for item in client.get(inv_uri).obj.get("Members", []):
        fw = client.get(item["@odata.id"]).obj
        name = fw.get("Name", "")
        ver  = fw.get("Version", "N/A") or "N/A"
        nl = name.lower()
        if "ilo" in nl and results["iLO"] == "N/A":
            results["iLO"] = f"{name} {ver}".strip()
        elif any(k in nl for k in ("system rom", "bios", "system firmware")) and results["BIOS"] == "N/A":
            results["BIOS"] = ver

    # Primary NIC firmware
    chassis = client.get(get_chassis_uri(client)).obj
    na_uri = chassis.get("NetworkAdapters", {}).get("@odata.id")
    if na_uri:
        members = client.get(na_uri).obj.get("Members", [])
        if members:
            adapter = client.get(members[0]["@odata.id"]).obj
            controllers = adapter.get("Controllers", [])
            nic_ver = controllers[0].get("FirmwarePackageVersion", "N/A") if controllers else "N/A"
            results["NIC-FW"] = nic_ver or "N/A"

    # Primary storage controller firmware
    storage_uri = system.get("Storage", {}).get("@odata.id")
    if storage_uri:
        s_members = client.get(storage_uri).obj.get("Members", [])
        if s_members:
            storage = client.get(s_members[0]["@odata.id"]).obj
            ctrls = storage.get("StorageControllers", [])
            if ctrls:
                results["Storage-FW"] = ctrls[0].get("FirmwareVersion", "N/A") or "N/A"

    return [(k, results[k]) for k in FLEET_KEYS]
