"""
pcli.ilo.bios
~~~~~~~~~~~~~
BIOS settings inspection for HPE iLO servers.
"""

from __future__ import annotations

import re
from typing import Any

from pcli.ilo.client import ILOClient


# Keys to display, organized by section: (attribute_key, display_label)
_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Boot", [
        ("BootOrderPolicy",         "Boot Order Policy"),
        ("SecureBootStatus",        "Secure Boot"),
        ("UsbBoot",                 "USB Boot"),
        ("HttpSupport",             "HTTP Boot Support"),
        ("F11BootMenu",             "F11 Boot Menu"),
    ]),
    ("Network Boot", [
        ("PreBootNetwork",          "Pre-Boot Network"),
        ("PrebootNetworkEnvPolicy", "Pre-Boot Network Env"),
        ("NetworkBootRetry",        "Network Boot Retry"),
        ("NetworkBootRetryCount",   "Network Boot Retry Count"),
        ("EmbeddedIpxe",            "Embedded iPXE"),
        ("IpxeBootOrder",           "iPXE Boot Order"),
        ("IpxeScriptAutoStart",     "iPXE Script Auto-Start"),
    ]),
    ("NIC Boot Ports", []),   # populated dynamically
    ("Performance", [
        ("WorkloadProfile",         "Workload Profile"),
        ("ProcSMT",                 "CPU SMT (Hyper-Threading)"),
        ("ProcAMDBoost",            "AMD CPU Boost"),
        ("ThermalConfig",           "Thermal Config"),
        ("AutoPowerOn",             "Auto Power-On"),
        ("WakeOnLan",               "Wake on LAN"),
    ]),
    ("Security", [
        ("TpmState",                "TPM State"),
        ("AmdSecureMemoryEncryption", "AMD SME"),
        ("AmdSecureNestedPaging",   "AMD SNP"),
        ("Sriov",                   "SR-IOV"),
    ]),
]


def _nic_boot_keys(attrs: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (key, label) pairs for all SlotXNicBootY attributes found."""
    rows = []
    for k in sorted(attrs.keys()):
        m = re.match(r"Slot(\d+)NicBoot(\d+)$", k)
        if m:
            slot, port = m.group(1), m.group(2)
            rows.append((k, f"Slot {slot} Port {port}"))
    return rows


async def fetch_bios(client: ILOClient, pending: bool = False) -> dict[str, Any]:
    path = "/redfish/v1/systems/1/bios/settings/" if pending else "/redfish/v1/systems/1/bios/"
    data = await client.get(path)
    return data.get("Attributes", {})


def format_bios(attrs: dict[str, Any], host: str, pending: bool = False) -> list[str]:
    """Return formatted lines for bios show output."""
    lines = []
    label_suffix = " (pending — takes effect after reboot)" if pending else ""
    lines.append(f"\n  BIOS Settings{label_suffix}  [{host}]")

    sections = list(_SECTIONS)
    nic_rows = _nic_boot_keys(attrs)
    sections = [
        (title, nic_rows if title == "NIC Boot Ports" else rows)
        for title, rows in sections
    ]

    for title, rows in sections:
        if not rows:
            continue
        has_any = any(k in attrs for k, _ in rows)
        if not has_any:
            continue
        width_label = max(len(label) for _, label in rows)
        lines.append(f"\n  {title}")
        lines.append(f"  {'─' * (width_label + 22)}")
        for key, label in rows:
            if key in attrs:
                lines.append(f"  {label:<{width_label}}  {attrs[key]}")

    lines.append("")
    return lines
