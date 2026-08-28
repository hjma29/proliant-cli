"""
proliant.oneview.network_adapters
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Host NIC inventory from HPE OneView -- storage's sibling for physical NICs.

(Named network_adapters.py, not network.py, to avoid clashing with
proliant.oneview.network -- the *logical* fabric module for ethernet
networks/network sets/uplink sets, an unrelated OneView concept from the
per-server physical NIC hardware this module inventories.)

Key endpoint:
  GET /rest/server-hardware/{id}/networkAdapters  → list of Redfish
      `NetworkAdapter` resources for that server, proxying the same
      schema iLO/COM expose (see proliant.common.network_report). Port
      MAC/link/speed are inlined directly on the hardware/appliance
      generations probed so far (`Ports[].Ethernet.AssociatedMACAddresses`
      + `CurrentSpeedGbps` + `LinkStatus`) — but this module also
      resolves MAC via a linked `NetworkDeviceFunction` sibling (already
      inlined on the adapter, matched by `AssignablePhysicalPorts`/
      `AssignablePhysicalNetworkPorts`) as a defensive fallback for older
      combinations that don't inline it on the port itself.

LLDP neighbor data isn't inlined on the port's `Ethernet` block like it is
on iLO/COM — OneView buries it in vendor-specific `Oem.Hpe.LldpData`
(different key names, e.g. `ChassisID` not `ChassisId`), which the shared
classifier doesn't parse, so `network describe` won't show an LLDP
neighbor for OneView-managed servers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from proliant.common.network_report import (
    build_port_dict,
    classify_network_adapter,
    summarize_network_for_list,
)
from proliant.oneview.client import OneViewError

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient

__all__ = [
    "fetch_network_report_data",
    "fetch_network_list_row",
    "summarize_network_for_list",
]


def _adapter_location(adapter: dict) -> str:
    for ctrl in adapter.get("Controllers") or []:
        label = ((ctrl.get("Location") or {}).get("PartLocation") or {}).get("ServiceLabel")
        if label:
            return label
    return "N/A"


def _adapter_firmware(adapter: dict) -> str:
    for ctrl in adapter.get("Controllers") or []:
        fw = ctrl.get("FirmwarePackageVersion")
        if fw:
            return fw
    return "N/A"


def _ndf_mac_by_port_id(adapter: dict) -> dict[str, str]:
    """Map port Id -> MAC, resolved from the adapter's already-inlined
    NetworkDeviceFunctions -- fallback for hardware/appliance combinations
    whose Ports don't inline Ethernet.AssociatedMACAddresses directly."""
    mac_by_port: dict[str, str] = {}
    for ndf in adapter.get("NetworkDeviceFunctions") or []:
        eth = ndf.get("Ethernet") or {}
        mac = eth.get("PermanentMACAddress") or eth.get("MACAddress")
        if not mac:
            continue
        links = (ndf.get("AssignablePhysicalPorts") or []) + (ndf.get("AssignablePhysicalNetworkPorts") or [])
        for link in links:
            port_id = (link.get("@odata.id") or "").rstrip("/").rsplit("/", 1)[-1]
            if port_id:
                mac_by_port.setdefault(port_id, mac.lower())
    return mac_by_port


def _resolved_ports(adapter: dict) -> list[dict]:
    ports = adapter.get("Ports") or []
    if not ports:
        return []
    if any((p.get("Ethernet") or {}).get("AssociatedMACAddresses") for p in ports):
        return [build_port_dict(p) for p in ports]

    mac_by_port = _ndf_mac_by_port_id(adapter)
    return [build_port_dict(p, mac=mac_by_port.get(str(p.get("Id") or ""))) for p in ports]


async def fetch_network_report_data(client: "OneViewClient", server_uri: str) -> list[dict]:
    """Return per-adapter, per-port host NIC inventory for one
    OneView-managed server, in the same shape as
    proliant.ilo.inventory.fetch_network_report_data:
      [{"adapter": ..., "model": ..., "part_number": ..., "location": ...,
        "firmware": ..., "ports": [...]}]
    """
    try:
        payload = await client.get(f"{server_uri}/networkAdapters")
    except OneViewError:
        return []

    result: list[dict] = []
    for adapter in (payload or {}).get("data") or []:
        entry = classify_network_adapter(
            adapter_id=adapter.get("Id") or "",
            name=adapter.get("Name") or "",
            model=adapter.get("Model") or "",
            part_number=adapter.get("PartNumber") or "N/A",
            location=_adapter_location(adapter),
            firmware=_adapter_firmware(adapter),
            ports=_resolved_ports(adapter),
        )
        if entry:
            result.append(entry)
    return result


async def fetch_network_list_row(client: "OneViewClient", server: dict) -> list[tuple[str, str]]:
    """Per-server row for `proliant oneview network list`:
    Location | Port | MAC | Link Status.
    """
    report = await fetch_network_report_data(client, server["uri"])
    summary = summarize_network_for_list(report)
    return [
        ("Location",     summary["location"]),
        ("Port",         summary["port"]),
        ("MAC",          summary["mac"]),
        ("Link Status",  summary["link_status"]),
    ]
