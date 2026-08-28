"""
proliant.com.network
~~~~~~~~~~~~~~~~~~~~~
Host NIC inventory via HPE Compute Ops Management -- storage's sibling for
physical NICs.

Key endpoint:
  GET /compute-ops-mgmt/v1/servers/{id}/inventory  →  the "networkAdapter"
      section's "data" list is the exact same Redfish `NetworkAdapter`
      schema iLO exposes directly, with Ports (MAC, link status, speed,
      LLDP neighbor) already fully inlined (no follow-up @odata.id
      fetches needed, just like the "storage" section). Because COM
      already collected/cached this from each server's own iLO, this
      works for ANY COM-managed server -- no direct iLO network
      reachability or per-server credentials required, only an
      authenticated `proliant com login` session.

See proliant.common.network_report for the shared per-adapter/per-port
classification/summarization that `ilo`, `oneview` and `com` all share.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from proliant.common.network_report import (
    build_port_dict,
    classify_network_adapter,
    summarize_network_for_list,
)

if TYPE_CHECKING:
    from proliant.com.client import COMClient
    from proliant.com.auth import COMSession

__all__ = [
    "fetch_network_report_data",
    "fetch_network_list_row",
    "run_network_list",
    "run_network_describe",
    "summarize_network_for_list",
]


def _adapter_firmware(adapter: dict) -> str:
    for ctrl in adapter.get("Controllers") or []:
        fw = ctrl.get("FirmwarePackageVersion")
        if fw:
            return fw
    return "N/A"


def _adapter_location(adapter: dict) -> str:
    for ctrl in adapter.get("Controllers") or []:
        label = ((ctrl.get("Location") or {}).get("PartLocation") or {}).get("ServiceLabel")
        if label:
            return label
    loc = ((adapter.get("Location") or {}).get("PartLocation") or {}).get("ServiceLabel")
    return loc or "N/A"


async def fetch_network_report_data(client: "COMClient", server_id: str) -> list[dict]:
    """Return per-adapter, per-port host NIC inventory for one COM-managed
    server, in the same shape as
    proliant.ilo.inventory.fetch_network_report_data:
      [{"adapter": ..., "model": ..., "part_number": ..., "location": ...,
        "firmware": ..., "ports": [...]}]

    Returns [] on error, or if COM hasn't collected network inventory for
    this server yet (e.g. just added, or the iLO agent hasn't reported in).
    """
    from proliant.com.inventory import _servers_url

    try:
        inv = await client.get(_servers_url(client, f"/servers/{server_id}/inventory"))
    except Exception:
        return []

    entries = (inv.get("networkAdapter") or {}).get("data") or []
    result: list[dict] = []
    for adapter in entries:
        ports = [build_port_dict(p) for p in (adapter.get("Ports") or [])]
        entry = classify_network_adapter(
            adapter_id=adapter.get("Id") or "",
            name=adapter.get("Name") or "",
            model=adapter.get("Model") or "",
            part_number=adapter.get("PartNumber") or "N/A",
            location=_adapter_location(adapter),
            firmware=_adapter_firmware(adapter),
            ports=ports,
        )
        if entry:
            result.append(entry)
    return result


async def fetch_network_list_row(client: "COMClient", server: dict) -> list[tuple[str, str]]:
    """Per-server row for `proliant com network list`:
    Location | Port | MAC | Link Status.
    """
    report = await fetch_network_report_data(client, server["id"])
    summary = summarize_network_for_list(report)
    return [
        ("Location",     summary["location"]),
        ("Port",         summary["port"]),
        ("MAC",          summary["mac"]),
        ("Link Status",  summary["link_status"]),
    ]


async def run_network_list(session: "COMSession") -> None:
    """Fleet-wide host NIC summary, mirroring `proliant ilo network list` /
    `proliant oneview network list`.

    Renders via the shared plain-text print_network_list_table (same as
    `ilo`/`oneview` network list) so all three modules look identical.
    """
    from proliant.common.display import get_console, print_json, print_network_list_table, OutputMode, get_output_mode
    from proliant.com.client import COMClient
    from proliant.com.inventory import _get_com_servers

    async with COMClient(session) as client:
        with get_console().status("[dim]Fetching server list…[/dim]"):
            servers = await _get_com_servers(client)
        servers = sorted(servers, key=lambda s: s["name"].lower())
        with get_console().status("[dim]Fetching network details across fleet…[/dim]"):
            rows = await asyncio.gather(
                *[fetch_network_list_row(client, s) for s in servers],
                return_exceptions=True,
            )

    fallback_row = [
        ("Location", "—"),
        ("Port", "—"),
        ("MAC", "—"),
        ("Link Status", "—"),
    ]
    results = [
        (s["name"], None, fallback_row if isinstance(row, Exception) else row)
        for s, row in zip(servers, rows)
    ]

    if get_output_mode() == OutputMode.JSON:
        print_json([{"name": name, **dict(row)} for name, _err, row in results])
        return

    if not results:
        get_console().print("[yellow]No servers found in COM.[/yellow]")
        return

    print_network_list_table(results)


async def run_network_describe(session: "COMSession", target: str) -> None:
    """Show full per-adapter, per-port host NIC detail for one COM server:
    model, part number, location, firmware, MAC, link status, speed, LLDP
    neighbor -- mirroring `proliant ilo network describe` /
    `proliant oneview network describe`.
    """
    from proliant.common.display import get_console, print_json, print_network_report, OutputMode, get_output_mode
    from proliant.com.client import COMClient
    from proliant.com.describe import _find_server

    async with COMClient(session) as client:
        with get_console().status(f"[dim]Fetching server '{target}'…[/dim]"):
            r = await client.get(session.com_url("/servers"), params={"limit": 1000})
        server = _find_server(r.get("items", []), target)
        if not server:
            get_console().print(f"[red]Server '{target}' not found.[/red]")
            sys.exit(1)
        with get_console().status("[dim]Fetching network details…[/dim]"):
            report = await fetch_network_report_data(client, server["id"])

    if get_output_mode() == OutputMode.JSON:
        print_json(report)
        return

    console = get_console()
    console.print(f"[bold]{server.get('name', target)}[/bold]")
    print_network_report(console, report)
