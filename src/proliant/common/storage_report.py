"""
proliant.common.storage_report
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared RAID-vs-direct-attach storage classification and reporting helpers.

Both `proliant ilo` (direct Redfish) and `proliant oneview` (OneView's
`localStorageV2` sub-resource, which proxies the exact same iLO Redfish
`Storage` schema) surface identical data shapes for each physical server:
a list of Redfish `Storage` resources, each with an `Id`, an optional
`Controllers`/`StorageControllers` array, and a `Drives` array. This
module classifies and summarizes that data once so both modules render
identical, consistently correct output.

Classification is by the Storage resource's own `Id` prefix, which HPE's
Redfish implementation uses consistently across Gen10/Gen11:
  DE* = RDE-capable device — a genuine hardware controller (Smart Array /
        MegaRAID RAID card, or e.g. the NS204i-u Boot Controller). Drives
        sit "behind" it.
  DA* = Direct Attached — drives connect straight to CPU/PCH PCIe lanes,
        no RAID/HBA card in between.
Confirmed live: direct-attached (DA*) NVMe drives *also* expose a
Controllers/StorageControllers entry, but it describes the drive's own
embedded NVMe controller chip (Model/Name == the SSD's own part number)
— not a purchasable RAID/HBA product — so presence of a controller entry
alone is not a reliable RAID-vs-direct signal. It's only used as a
fallback for storage resources whose Id doesn't start with DA/DE (e.g.
older iLO 5 schema).
"""

from __future__ import annotations

import re


def drive_bay_label(drive: dict) -> str:
    loc = (drive.get("PhysicalLocation") or {}).get("PartLocation", {})
    ordinal = loc.get("LocationOrdinalValue")
    return loc.get("ServiceLabel") or (f"Bay {ordinal}" if ordinal is not None else "N/A")


def _build_drive_dict(drive: dict, attach_mode: str) -> dict:
    cap_bytes = drive.get("CapacityBytes") or 0
    protocol = drive.get("Protocol") or "N/A"
    media_type = drive.get("MediaType") or "N/A"
    return {
        "id":           drive.get("Id") or drive_bay_label(drive),
        "name":         drive.get("Name", "N/A"),
        "bay":          drive_bay_label(drive),
        "capacity_gib": round(cap_bytes / (1024 ** 3)) if cap_bytes else 0,
        "capacity_gb":  round(cap_bytes / (1000 ** 3)) if cap_bytes else 0,
        "media_type":   media_type,
        "protocol":     protocol,
        # Simplified type for compact list views: "NVMe" takes priority
        # over media type (e.g. an NVMe SSD shows as "NVMe", not "SSD")
        # since that's the more useful signal for at-a-glance fleet views.
        "type_label":   "NVMe" if protocol.upper() == "NVME" else media_type,
        "model":        drive.get("Model") or "N/A",
        "serial":       drive.get("SerialNumber") or "N/A",
        "part_number":  drive.get("PartNumber") or "N/A",
        "firmware":     drive.get("Revision") or "N/A",
        "health":       (drive.get("Status") or {}).get("Health") or "N/A",
        "attached_via": attach_mode,
    }


def classify_storage_resource(
    storage_id: str,
    storage_name: str,
    ctrl_entries: list[dict],
    drives: list[dict],
) -> dict | None:
    """Classify one Redfish `Storage` resource into a report entry.

    `ctrl_entries` and `drives` must already be fully-resolved dicts (any
    `@odata.id` navigation links followed) — this function does no
    network I/O. Returns None if there are no drives to report.

    Returns: {"controller": str, "firmware": str, "attach_mode": str, "drives": [...]}
    """
    if not drives:
        return None

    sid = (storage_id or "").upper()
    if sid.startswith("DE"):
        is_raid = True
    elif sid.startswith("DA"):
        is_raid = False
    else:
        # Unrecognized Id scheme (e.g. legacy iLO 5) — fall back to the
        # old heuristic: presence of a controller entry implies RAID.
        is_raid = bool(ctrl_entries)

    attach_mode = "RAID Controller" if is_raid else "Direct/HBA"

    if is_raid:
        if ctrl_entries:
            first = ctrl_entries[0]
            ctrl_name = first.get("Model") or first.get("Name") or storage_name or "N/A"
            ctrl_fw = first.get("FirmwareVersion") or "N/A"
        else:
            ctrl_name = storage_name or "N/A"
            ctrl_fw = "N/A"
    else:
        # Direct-attached: any Controllers/StorageControllers entry
        # describes the drive's own embedded controller, not a real
        # RAID/HBA product — don't surface it as a "controller".
        ctrl_name = "—"
        ctrl_fw = "—"

    return {
        "controller":  ctrl_name,
        "firmware":    ctrl_fw,
        "attach_mode": attach_mode,
        "drives":      [_build_drive_dict(d, attach_mode) for d in drives],
    }


def summarize_storage_report(report: list[dict]) -> str:
    """Compact one-line storage summary for the `servers list` table.

    e.g. "1 ctrl, 8 disks: 6x1920GiB SSD(RAID), 2x480GiB SSD(Direct)"

    "ctrl" only counts real hardware storage controllers (RAID/boot
    controllers) — direct-attached (DA*) Storage subsystems each carry
    their own Storage-resource entry per drive but are not controllers,
    so they're excluded from this count (see classify_storage_resource).
    """
    if not report:
        return "—"
    total_disks = sum(len(c["drives"]) for c in report)
    if not total_disks:
        return "—"
    ctrl_count = sum(1 for c in report if c["attach_mode"] == "RAID Controller")

    groups: dict[tuple, int] = {}
    for c in report:
        for d in c["drives"]:
            key = (d["capacity_gib"], d["media_type"], c["attach_mode"])
            groups[key] = groups.get(key, 0) + 1

    parts = []
    for (cap, media, mode), count in sorted(groups.items(), key=lambda kv: -kv[1]):
        cap_str = f"{cap}GiB" if cap else "?"
        media_str = f" {media}" if media and media != "N/A" else ""
        mode_short = "RAID" if mode == "RAID Controller" else "Direct"
        parts.append(f"{count}x{cap_str}{media_str}({mode_short})")

    return f"{ctrl_count} ctrl, {total_disks} disks: " + ", ".join(parts)


def _summarize_drive_group(drives: list[dict]) -> str:
    """Compact '(N x SIZE TYPE), ...' summary for a group of drives.

    Groups by (capacity, type_label) — e.g. "(4 x 2981GB NVMe), (2 x 480GB SSD)".
    Sizes are shown in decimal GB (drive-vendor convention), not GiB.
    """
    if not drives:
        return "—"
    groups: dict[tuple, int] = {}
    for d in drives:
        key = (d["capacity_gb"], d.get("type_label") or d["media_type"])
        groups[key] = groups.get(key, 0) + 1
    parts = []
    for (cap, type_label), count in sorted(groups.items(), key=lambda kv: -kv[1]):
        cap_str = f"{cap}GB" if cap else "?"
        type_str = f" {type_label}" if type_label and type_label != "N/A" else ""
        parts.append(f"({count} x {cap_str}{type_str})")
    return ", ".join(parts)


def clean_controller_model(name: str) -> str:
    """Trim a controller's full Redfish Name/Model down to just the part
    number (e.g. "HPE NS204i-u Gen11 Boot Controller" -> "NS204i-u Gen11",
    "HPE MR416i-o Gen11" -> "MR416i-o Gen11") for the compact list view.
    """
    if not name or name in ("—", "N/A"):
        return name
    cleaned = re.sub(r"^\s*HPE\s+", "", name, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+Boot Controller\s*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip() or name


def summarize_storage_for_list(report: list[dict]) -> dict[str, str]:
    """Storage summary split by attachment, for the `storage list` table.

    Returns a dict with three columns:
      controller — distinct storage controller part number(s) (RAID/HBA
                   cards only; direct-attached drives have no real
                   controller), with brand/"Boot Controller" wording
                   trimmed off — e.g. "NS204i-u Gen11"
      behind     — disks behind a storage controller, e.g. "(2 x 900GB SSD)"
      direct     — disks directly attached to the CPU/PCH, no controller,
                   e.g. "(4 x 2981GB NVMe)"
    """
    controller_models: list[str] = []
    behind_drives: list[dict] = []
    direct_drives: list[dict] = []

    for ctrl in report:
        if ctrl["attach_mode"] == "RAID Controller":
            model = clean_controller_model(ctrl["controller"])
            if model not in ("—", "N/A") and model not in controller_models:
                controller_models.append(model)
            behind_drives.extend(ctrl["drives"])
        else:
            direct_drives.extend(ctrl["drives"])

    return {
        "controller": ", ".join(controller_models) if controller_models else "—",
        "behind":     _summarize_drive_group(behind_drives),
        "direct":     _summarize_drive_group(direct_drives),
    }
