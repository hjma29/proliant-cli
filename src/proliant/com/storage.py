"""
proliant.com.storage
~~~~~~~~~~~~~~~~~~~~~
RAID-vs-direct-attach storage inventory via HPE Compute Ops Management.

Key endpoint:
  GET /compute-ops-mgmt/v1/servers/{id}/inventory  →  the "storage"
      section's "data" list is the exact same Redfish `Storage` schema
      iLO exposes directly, with Controllers/StorageControllers and
      Drives already fully inlined (no follow-up @odata.id fetches
      needed, just like OneView's localStorageV2). Because COM already
      collected/cached this from each server's own iLO, this works for
      ANY COM-managed server -- no direct iLO network reachability or
      per-server credentials required, only an authenticated
      `proliant com login` session.

See proliant.common.storage_report for the shared RAID-vs-direct-attach
classification/summarization that `ilo`, `oneview` and `com` all share.
"""

from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING

from proliant.common.storage_report import (
    classify_storage_resource,
    summarize_storage_for_list,
    summarize_storage_report,
)

if TYPE_CHECKING:
    from proliant.com.client import COMClient
    from proliant.com.auth import COMSession

__all__ = [
    "fetch_storage_report_data",
    "fetch_storage_list_row",
    "run_storage_list",
    "run_storage_describe",
    "summarize_storage_report",
    "summarize_storage_for_list",
]


async def fetch_storage_report_data(client: "COMClient", server_id: str) -> list[dict]:
    """Return per-controller storage + physical-drive detail for one
    COM-managed server, in the same shape as
    proliant.ilo.inventory.fetch_storage_report_data:
      [{"controller": ..., "firmware": ..., "attach_mode": ..., "drives": [...]}]

    Returns [] on error, or if COM hasn't collected storage inventory for
    this server yet (e.g. just added, or the iLO agent hasn't reported in).
    """
    from proliant.com.inventory import _servers_url

    try:
        inv = await client.get(_servers_url(client, f"/servers/{server_id}/inventory"))
    except Exception:
        return []

    entries = (inv.get("storage") or {}).get("data") or []
    result: list[dict] = []
    for storage in entries:
        drives = storage.get("Drives") or []
        ctrl_entries = storage.get("Controllers") or storage.get("StorageControllers") or []
        entry = classify_storage_resource(
            storage_id=storage.get("Id") or "",
            storage_name=storage.get("Name") or "N/A",
            ctrl_entries=ctrl_entries,
            drives=drives,
        )
        if entry:
            result.append(entry)
    return result


async def fetch_storage_list_row(client: "COMClient", server: dict) -> list[tuple[str, str]]:
    """Per-server row for `proliant com storage list`:
    Storage Controller | RAID Attached | Direct Attached.
    """
    report = await fetch_storage_report_data(client, server["id"])
    summary = summarize_storage_for_list(report)
    return [
        ("Storage Controller",      summary["controller"]),
        ("RAID Attached", summary["behind"]),
        ("Direct Attached",  summary["direct"]),
    ]


async def run_storage_list(session: "COMSession") -> None:
    """Fleet-wide RAID-controller vs. direct-attached storage summary,
    mirroring `proliant ilo storage list` / `proliant oneview storage list`.

    Renders via the shared plain-text print_storage_list_table (same as
    `ilo`/`oneview` storage list) so all three modules look identical.
    """
    from proliant.common.display import get_console, print_json, print_storage_list_table, OutputMode, get_output_mode
    from proliant.com.client import COMClient
    from proliant.com.inventory import _get_com_servers

    async with COMClient(session) as client:
        with get_console().status("[dim]Fetching server list…[/dim]"):
            servers = await _get_com_servers(client)
        servers = sorted(servers, key=lambda s: s["name"].lower())
        with get_console().status("[dim]Fetching storage details across fleet…[/dim]"):
            rows = await asyncio.gather(
                *[fetch_storage_list_row(client, s) for s in servers],
                return_exceptions=True,
            )

    fallback_row = [
        ("Storage Controller", "—"),
        ("RAID Attached", "—"),
        ("Direct Attached", "—"),
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

    print_storage_list_table(results)


async def run_storage_describe(session: "COMSession", target: str) -> None:
    """Show full per-controller, per-disk storage detail for one COM
    server: RAID Controller vs. Direct/HBA classification, capacity,
    media, protocol, model, serial, firmware, health -- mirroring
    `proliant ilo storage describe` / `proliant oneview storage describe`.
    """
    from proliant.common.display import get_console, print_json, print_storage_report, OutputMode, get_output_mode
    from proliant.com.client import COMClient
    from proliant.com.describe import _find_server

    async with COMClient(session) as client:
        with get_console().status(f"[dim]Fetching server '{target}'…[/dim]"):
            r = await client.get(session.com_url("/servers"), params={"limit": 1000})
        server = _find_server(r.get("items", []), target)
        if not server:
            get_console().print(f"[red]Server '{target}' not found.[/red]")
            sys.exit(1)
        with get_console().status("[dim]Fetching storage details…[/dim]"):
            report = await fetch_storage_report_data(client, server["id"])

    if get_output_mode() == OutputMode.JSON:
        print_json(report)
        return

    console = get_console()
    console.print(f"[bold]{server.get('name', target)}[/bold]")
    print_storage_report(console, report)
