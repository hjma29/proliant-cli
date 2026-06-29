"""
proliant.ilo.bios
~~~~~~~~~~~~~
BIOS settings inspection and modification for HPE iLO servers.
"""

from __future__ import annotations

import re
from typing import Any

from proliant.ilo.client import ILOClient


WORKLOAD_PROFILES = [
    "GeneralThroughputCompute",
    "GeneralPeakFrequencyCompute",
    "LowLatency",
    "DecisionSupport",
    "GraphicProcessing",
    "TransactionalApplicationProcessing",
    "MissionCritical",
    "Custom",
]

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
    ("Virtualization", [
        ("ProcAmdIoVt",             "AMD-Vi / IOMMU (VT-d)"),
        ("AmdDmarSupport",          "DMAR (strict DMA isolation)"),
        ("Sriov",                   "SR-IOV"),
        ("AmdSecureNestedPaging",   "AMD SNP (secure nested paging)"),
        ("AmdSecureMemoryEncryption", "AMD SME"),
    ]),
    ("Security", [
        ("TpmState",                "TPM State"),
        ("SecureBootStatus",        "Secure Boot"),
    ]),
    ("Serial Console", [
        ("VirtualSerialPort",       "Virtual Serial Port"),
        ("SerialConsolePort",       "Serial Console Port"),
        ("SerialConsoleBaudRate",   "Baud Rate"),
        ("SerialConsoleEmulation",  "Emulation"),
        ("EmsConsole",              "EMS Console"),
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


SERIAL_CONSOLE_PORTS = ["Auto", "Disabled", "Physical", "Virtual"]
SERIAL_CONSOLE_BAUD_RATES = ["BaudRate9600", "BaudRate19200", "BaudRate38400", "BaudRate57600", "BaudRate115200"]
SERIAL_CONSOLE_EMULATIONS = ["Vt100", "Ansi", "Vt100Plus", "VtUtf8"]
EMS_CONSOLE_VALUES = ["Disabled", "Physical", "Virtual"]
VIRTUAL_SERIAL_PORT_VALUES = ["Com1Irq4", "Com2Irq3", "Disabled"]


async def set_serial_console(client: ILOClient, port: str | None = None,
                              ems: str | None = None,
                              vsp: str | None = None) -> None:
    """PATCH serial console settings to BIOS pending settings."""
    attrs: dict[str, str] = {}
    if port is not None:
        attrs["SerialConsolePort"] = port
    if ems is not None:
        attrs["EmsConsole"] = ems
    if vsp is not None:
        attrs["VirtualSerialPort"] = vsp
    if not attrs:
        raise ValueError("No settings specified")
    resp = await client.patch(
        "/redfish/v1/systems/1/bios/settings/",
        {"Attributes": attrs},
    )
    msg_id = (
        resp.get("error", {})
        .get("@Message.ExtendedInfo", [{}])[0]
        .get("MessageId", "")
    )
    if "Success" not in msg_id and "SystemResetRequired" not in msg_id:
        raise RuntimeError(f"Unexpected iLO response: {msg_id}")


async def set_workload_profile(client: ILOClient, profile: str) -> str:
    """PATCH WorkloadProfile to BIOS pending settings. Returns confirmation message."""
    resp = await client.patch(
        "/redfish/v1/systems/1/bios/settings/",
        {"Attributes": {"WorkloadProfile": profile}},
    )
    msg_id = (
        resp.get("error", {})
        .get("@Message.ExtendedInfo", [{}])[0]
        .get("MessageId", "")
    )
    if "Success" not in msg_id and "SystemResetRequired" not in msg_id:
        raise RuntimeError(f"Unexpected iLO response: {msg_id}")
    return profile


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
        lines.append(f"  {'-' * (width_label + 22)}")
        for key, label in rows:
            if key in attrs:
                lines.append(f"  {label:<{width_label}}  {attrs[key]}")

    lines.append("")
    return lines
