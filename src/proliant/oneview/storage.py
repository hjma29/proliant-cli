"""
proliant.oneview.storage
~~~~~~~~~~~~~~~~~~~~~~~~~
Local (on-server) storage inventory from HPE OneView.

Key endpoint:
  GET /rest/server-hardware/{id}/localStorageV2  → list of Redfish
      `Storage` resources for that server (Controllers/StorageControllers
      and Drives already fully inlined — no follow-up @odata.id fetches
      needed, unlike iLO's direct Redfish API).

OneView proxies the exact same iLO Redfish `Storage` schema (Id prefixed
DE*/DA*, drive fields, etc.) used by `proliant ilo storage` — see
proliant.common.storage_report for the shared RAID-vs-direct-attach
classification and summarization these two modules share.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from proliant.common.storage_report import (
    classify_storage_resource,
    summarize_storage_for_list,
    summarize_storage_report,
)
from proliant.oneview.client import OneViewError

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient

__all__ = [
    "fetch_storage_report_data",
    "fetch_storage_list_row",
    "summarize_storage_report",
    "summarize_storage_for_list",
]


async def _fetch_local_storage_entries(client: "OneViewClient", server_uri: str) -> list[dict]:
    """Fetch a server's local storage entries, preferring localStorageV2
    (Redfish-schema, matches iLO) and falling back to the older
    localStorage sub-resource if V2 isn't available on this appliance/
    server generation.
    """
    for sub_resource in ("localStorageV2", "localStorage"):
        try:
            payload = await client.get(f"{server_uri}/{sub_resource}")
        except OneViewError:
            continue
        entries = (payload or {}).get("data") or []
        if entries:
            return entries
    return []


async def fetch_storage_report_data(client: "OneViewClient", server_uri: str) -> list[dict]:
    """Return per-controller storage + physical-drive detail for one
    OneView-managed server, in the same shape as
    proliant.ilo.inventory.fetch_storage_report_data:
      [{"controller": ..., "firmware": ..., "attach_mode": ..., "drives": [...]}]
    """
    result: list[dict] = []
    for storage in await _fetch_local_storage_entries(client, server_uri):
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


async def fetch_storage_list_row(client: "OneViewClient", server: dict) -> list[tuple[str, str]]:
    """Per-server row for `proliant oneview storage list`:
    Storage Controller | Disks Behind Controller | Disks Direct-Connected.
    """
    report = await fetch_storage_report_data(client, server["uri"])
    summary = summarize_storage_for_list(report)
    return [
        ("Storage Controller",      summary["controller"]),
        ("Disks Behind Controller", summary["behind"]),
        ("Disks Direct-Connected",  summary["direct"]),
    ]
