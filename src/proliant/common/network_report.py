"""
proliant.common.network_report
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared host-NIC classification/formatting for `ilo`, `oneview`, and `com`
`network list` / `network describe`, mirroring proliant.common.storage_report's
RAID-vs-direct-attach split for disks.

All three modules expose the same Redfish `NetworkAdapter` resource (ilo
directly via Chassis/NetworkAdapters, oneview via its server-hardware
`networkAdapters` sub-resource proxy, com via its cached per-server
inventory mirror's `networkAdapter` key) -- but the exact port sub-schema
varies by generation:
  - Newer iLO 6/7 hosts (unified `Port` schema): `LinkStatus`/`LinkState` +
    `Ethernet.AssociatedMACAddresses` + `Ethernet.LLDPReceive` are all
    inlined directly on the port itself.
  - Older iLO 5 / Synergy Gen10 hosts (`NetworkPort` schema): the MAC
    address instead lives on a linked `NetworkDeviceFunction` sibling
    resource, and there's no inlined LLDP data.

Each module's own fetch function is responsible for resolving any
`@odata.id` links (iLO, OneView) or simply reading already-inlined data
(COM) into plain dicts *before* calling build_port_dict()/
classify_network_adapter() here -- this module does no network I/O, only
classification/formatting, exactly like storage_report.py.
"""

from __future__ import annotations

from typing import Any


def _display_link_status(status: str | None) -> str:
    """Normalize the many Redfish spellings of link state into one of a
    handful of short, human labels."""
    normalized = (status or "").replace("_", "").replace("-", "").strip().lower()
    if normalized == "linkup":
        return "Link Up"
    if normalized in ("nolink", "linkdown", "down"):
        return "No Link"
    if normalized == "disabled":
        return "Disabled"
    return status or "N/A"


def build_port_dict(
    port: dict[str, Any],
    *,
    mac: str | None = None,
    speed_gbps: float | None = None,
) -> dict[str, Any]:
    """Build one physical-port entry from an already-resolved Redfish
    `Port`/`NetworkPort` dict.

    `mac`/`speed_gbps` may be passed in explicitly when the caller already
    resolved them from a linked `NetworkDeviceFunction` (older Redfish
    schema, MAC/speed not inlined on the port) -- otherwise they're read
    straight off `port` (newer unified `Port` schema).
    """
    eth = port.get("Ethernet") or {}
    if mac is None:
        macs = eth.get("AssociatedMACAddresses") or []
        mac = str(macs[0]).lower() if macs else None

    link_status = (
        port.get("LinkStatus")
        or port.get("LinkState")
        or (port.get("Status") or {}).get("State")
    )

    if speed_gbps is None:
        speed_gbps = port.get("CurrentSpeedGbps")

    lldp = eth.get("LLDPReceive") or {}
    neighbor = None
    if lldp.get("ChassisId") or lldp.get("SystemDescription"):
        # SystemDescription is often a multi-line switch banner -- only the
        # first line is a useful "what device is this" label.
        neighbor_name = (lldp.get("SystemDescription") or "").splitlines()[0].strip()
        neighbor = {
            "chassis": neighbor_name or lldp.get("ChassisId") or "—",
            "port": lldp.get("PortId") or "—",
            "ip": lldp.get("ManagementAddressIPv4") or "—",
        }

    return {
        "port": str(port.get("PortId") or port.get("Id") or "N/A"),
        "mac": mac or "N/A",
        "link_status": _display_link_status(link_status),
        "speed_gbps": speed_gbps,
        "health": (port.get("Status") or {}).get("Health") or "—",
        "lldp_neighbor": neighbor,
    }


def classify_network_adapter(
    *,
    adapter_id: str,
    name: str,
    model: str,
    part_number: str,
    location: str,
    firmware: str,
    ports: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Classify one Redfish `NetworkAdapter` resource into a report entry.

    `ports` must already be a list of resolved dicts (see build_port_dict).
    Returns None if there are no ports to report (mirrors
    proliant.common.storage_report.classify_storage_resource).

    Returns: {"adapter": str, "model": str, "part_number": str,
              "location": str, "firmware": str, "ports": [...]}
    """
    if not ports:
        return None
    return {
        "id": adapter_id or "",
        "adapter": name or model or "N/A",
        "model": model or "N/A",
        "part_number": part_number or "N/A",
        "location": location or "N/A",
        "firmware": firmware or "N/A",
        "ports": ports,
    }


def summarize_network_for_list(report: list[dict]) -> dict[str, str]:
    """Compact fleet-wide columns for `network list`: Location | Port | MAC
    | Link Status -- one line per physical port. Unlike disks (grouped by
    capacity/type since a server can have dozens), NIC ports are few and
    each is physically distinct, so no aggregation is done here -- every
    port gets its own line, in sync across all four columns.
    """
    locations: list[str] = []
    ports: list[str] = []
    macs: list[str] = []
    links: list[str] = []
    for adapter in report:
        for p in adapter["ports"]:
            locations.append(adapter["location"])
            ports.append(p["port"])
            macs.append(p["mac"])
            links.append(p["link_status"])

    if not ports:
        return {"location": "—", "port": "—", "mac": "—", "link_status": "—"}

    return {
        "location": "\n".join(locations),
        "port": "\n".join(ports),
        "mac": "\n".join(macs),
        "link_status": "\n".join(links),
    }
