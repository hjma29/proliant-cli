"""
proliant.ilo.boot
~~~~~~~~~~~~~
Boot order inspection and one-time PXE boot override operations.
"""

from __future__ import annotations

import re
from typing import Any

from proliant.ilo.client import ILOClient

_MAC_RE = re.compile(r"MAC\(([0-9A-Fa-f]+),")


def _normalize_mac(value: str) -> str:
    return re.sub(r"[^0-9a-f]", "", value.lower())


def _boot_option_kind(device_path: str, display_name: str) -> str:
    path = (device_path or "").lower()
    name = (display_name or "").lower()
    if "mac(" in path and "ipv4" in path and "/uri()" not in path:
        return "PXE IPv4"
    if "mac(" in path and "ipv6" in path and "/uri()" not in path:
        return "PXE IPv6"
    if "mac(" in path and "ipv4" in path and "/uri()" in path:
        return "HTTP(S) IPv4"
    if "mac(" in path and "ipv6" in path and "/uri()" in path:
        return "HTTP(S) IPv6"
    if path.startswith("usbclass("):
        return "USB"
    if path.startswith("hd("):
        return "UEFI OS"
    if "nvme(" in path:
        return "Local Disk"
    if "shell" in name:
        return "UEFI Shell"
    return "Other"


def _boot_option_mac(device_path: str) -> str:
    match = _MAC_RE.search(device_path or "")
    return match.group(1).lower() if match else ""


def _boot_option_port_hint(display_name: str) -> str:
    match = re.search(r"(slot\s+\S+\s+port\s+\d+)", display_name or "", re.IGNORECASE)
    if match:
        return match.group(1)
    return ""


def _match_port(option: dict[str, str], port: str) -> bool:
    needle = port.strip().lower()
    if not needle:
        return False
    normalized = _normalize_mac(needle)
    if normalized and normalized == _normalize_mac(option.get("mac", "")):
        return True
    display = (option.get("display_name") or "").lower()
    return needle in display


def _result_entry(
    *,
    reference: str,
    display_name: str,
    device_path: str,
    kind: str,
    mac: str,
    port_hint: str,
    structured_boot_string: str = "",
    correlatable_id: str = "",
) -> dict[str, str]:
    return {
        "reference": reference,
        "display_name": display_name,
        "device_path": device_path,
        "kind": kind,
        "mac": mac,
        "port_hint": port_hint,
        "structured_boot_string": structured_boot_string,
        "correlatable_id": correlatable_id,
    }


def _hpe_boot_source_entry(source: dict[str, Any]) -> dict[str, str]:
    display_name = source.get("BootString") or "—"
    device_path = source.get("UEFIDevicePath") or source.get("CorrelatableID") or ""
    return _result_entry(
        reference=(
            f"Boot{source.get('BootOptionNumber')}"
            if source.get("BootOptionNumber")
            else source.get("StructuredBootString") or ""
        ),
        display_name=display_name,
        device_path=device_path,
        kind=_boot_option_kind(source.get("UEFIDevicePath") or "", display_name),
        mac=_boot_option_mac(source.get("UEFIDevicePath") or ""),
        port_hint=_boot_option_port_hint(display_name),
        structured_boot_string=source.get("StructuredBootString") or "",
        correlatable_id=source.get("CorrelatableID") or "",
    )




async def _boot_options(client: ILOClient) -> tuple[dict[str, Any], list[dict[str, str]]]:
    system = await client.get(await client.get_system_uri())
    boot = system.get("Boot") or {}
    options: list[dict[str, str]] = []

    options_uri = (boot.get("BootOptions") or {}).get("@odata.id")
    if options_uri:
        collection = await client.get(options_uri)
        for member in collection.get("Members", []):
            uri = member.get("@odata.id")
            if not uri:
                continue
            option = await client.get(uri)
            display_name = option.get("DisplayName") or option.get("Name") or "—"
            device_path = option.get("UefiDevicePath") or ""
            options.append(
                _result_entry(
                    reference=option.get("BootOptionReference") or option.get("Id") or "",
                    display_name=display_name,
                    device_path=device_path,
                    kind=_boot_option_kind(device_path, display_name),
                    mac=_boot_option_mac(device_path),
                    port_hint=_boot_option_port_hint(display_name),
                )
            )
    return system, options


async def _hpe_boot_settings(client: ILOClient) -> tuple[dict[str, Any], dict[str, Any]] | None:
    system = await client.get(await client.get_system_uri())
    bios_uri = (system.get("Bios") or {}).get("@odata.id")
    if not bios_uri:
        return None
    bios = await client.get(bios_uri)
    boot_uri = ((((bios.get("Oem") or {}).get("Hpe") or {}).get("Links") or {}).get("Boot") or {}).get("@odata.id")
    if not boot_uri:
        return None
    boot_resource = await client.get(boot_uri)
    settings_uri = (((boot_resource.get("@Redfish.Settings") or {}).get("SettingsObject") or {}).get("@odata.id"))
    if not settings_uri:
        return None
    settings_resource = await client.get(settings_uri)
    return boot_resource, settings_resource


async def fetch_boot_order(client: ILOClient) -> dict[str, Any]:
    system, options = await _boot_options(client)
    boot = system.get("Boot") or {}
    order = boot.get("BootOrder") or []
    by_ref = {option["reference"]: option for option in options}
    ordered: list[dict[str, str]] = []
    pxe_ipv4: list[dict[str, str]] = []
    desired: list[dict[str, str]] = []

    for index, reference in enumerate(order, start=1):
        option = by_ref.get(reference, {})
        ordered.append({
            "position": index,
            "reference": reference,
            "display_name": option.get("display_name") or reference,
            "kind": option.get("kind") or "Other",
            "device_path": option.get("device_path") or "",
            "mac": option.get("mac") or "",
            "port_hint": option.get("port_hint") or "",
            "structured_boot_string": option.get("structured_boot_string") or "",
            "correlatable_id": option.get("correlatable_id") or "",
        })

    hpe_boot = await _hpe_boot_settings(client)
    persistent_order: list[str] = []
    if hpe_boot:
        boot_resource, settings_resource = hpe_boot
        sources = [_hpe_boot_source_entry(source) for source in boot_resource.get("BootSources") or []]
        persistent_order = settings_resource.get("PersistentBootConfigOrder") or []

        pxe_ipv4 = [
            source for source in sources if source["kind"] == "PXE IPv4"
        ]

        desired = []
        for device in settings_resource.get("DesiredBootDevices") or []:
            corr = (device.get("CorrelatableID") or "").strip()
            if not corr:
                continue
            match = next((entry for entry in pxe_ipv4 if entry.get("correlatable_id") == corr), None)
            desired.append(match or {
                "reference": "",
                "display_name": corr,
                "device_path": corr,
                "kind": "Desired Boot Device",
                "mac": "",
                "port_hint": "",
                "structured_boot_string": "",
                "correlatable_id": corr,
            })
    else:
        pxe_ipv4 = [option for option in options if option["kind"] == "PXE IPv4"]

    return {
        "mode": boot.get("BootSourceOverrideMode") or "—",
        "override_enabled": boot.get("BootSourceOverrideEnabled") or "—",
        "override_target": boot.get("BootSourceOverrideTarget") or "—",
        "uefi_target": boot.get("UefiTargetBootSourceOverride") or "—",
        "order": ordered,
        "pxe_ipv4": pxe_ipv4,
        "desired_boot_devices": desired,
        "persistent_order": persistent_order,
    }


async def set_one_time_pxe(
    client: ILOClient,
    *,
    port: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Set a one-time PXE IPv4 boot override using the standard Redfish UefiTarget mechanism.

    Uses BootSourceOverrideTarget=UefiTarget with the exact UEFI device path for PXE IPv4
    (e.g. .../MAC(...)/IPv4(0.0.0.0) without /Uri()). This correctly distinguishes PXE from
    HTTP boot, which share the same NIC CorrelatableID in the HPE DesiredBootDevices API.
    """
    system_uri = await client.get_system_uri()
    system = await client.get(system_uri)
    boot = system.get("Boot") or {}

    # Verify UefiTarget is supported before proceeding
    allowed_targets = boot.get("BootSourceOverrideTarget@Redfish.AllowableValues") or []
    if "UefiTarget" not in allowed_targets:
        raise RuntimeError(
            "This server does not support UefiTarget boot override "
            f"(allowed: {', '.join(allowed_targets)})"
        )

    # Collect PXE IPv4 candidates from Redfish allowable UEFI paths first,
    # then enrich with HPE boot source display names for port matching.
    allowed_uefi = boot.get("UefiTargetBootSourceOverride@Redfish.AllowableValues") or []
    pxe_ipv4_paths = [
        p for p in allowed_uefi
        if _boot_option_kind(p, "") == "PXE IPv4"
    ]

    # Build candidate list, enriched with HPE boot source metadata where available
    pxe_candidates: list[dict[str, str]] = []
    hpe_boot = await _hpe_boot_settings(client)
    if hpe_boot:
        boot_resource, _ = hpe_boot
        hpe_sources = [_hpe_boot_source_entry(s) for s in boot_resource.get("BootSources") or []]
        hpe_by_path = {s["device_path"]: s for s in hpe_sources if s["kind"] == "PXE IPv4"}
        for path in pxe_ipv4_paths:
            pxe_candidates.append(hpe_by_path.get(path) or _result_entry(
                reference="", display_name=path, device_path=path,
                kind="PXE IPv4", mac=_boot_option_mac(path),
                port_hint=_boot_option_port_hint(path),
            ))
    else:
        _, options = await _boot_options(client)
        path_set = set(pxe_ipv4_paths)
        pxe_candidates = [o for o in options if o["kind"] == "PXE IPv4" and o["device_path"] in path_set]
        if not pxe_candidates:
            pxe_candidates = [
                _result_entry(
                    reference="", display_name=p, device_path=p,
                    kind="PXE IPv4", mac=_boot_option_mac(p), port_hint="",
                )
                for p in pxe_ipv4_paths
            ]

    if not pxe_candidates:
        raise RuntimeError("No PXE IPv4 boot targets found on this host")

    if port:
        matches = [c for c in pxe_candidates if _match_port(c, port)]
        if not matches:
            raise RuntimeError(f"No PXE IPv4 boot option matched port '{port}'")
        if len(matches) > 1:
            labels = ", ".join(m["display_name"] for m in matches)
            raise RuntimeError(f"Port '{port}' matched multiple PXE IPv4 boot options: {labels}")
        selected = matches[0]
    else:
        selected = pxe_candidates[0]

    payload: dict[str, Any] = {
        "Boot": {
            "BootSourceOverrideEnabled": "Once",
            "BootSourceOverrideTarget": "UefiTarget",
            "UefiTargetBootSourceOverride": selected["device_path"],
        }
    }

    if dry_run:
        return {
            "status": "dry-run",
            "url": system_uri,
            "payload": payload,
            "selected": selected,
            "mechanism": "redfish-boot-override",
        }

    await client.patch(system_uri, payload)
    return {
        "status": "accepted",
        "url": system_uri,
        "payload": payload,
        "selected": selected,
        "mechanism": "redfish-boot-override",
    }
