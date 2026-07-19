"""
proliant.oneview.refresh
~~~~~~~~~~~~~~~~~~~~~~~~
Non-disruptive server hardware refresh: PUT
``/rest/server-hardware/{id}/refreshState``, the same request OneView's own
GUI issues from the "Refresh" action on a server hardware or server profile
page. This re-polls the blade's iLO for current hardware/adapter inventory
and re-evaluates profile consistency -- it does not power-cycle the server
or change its profile assignment.

Useful after a transient management-plane interruption (e.g. an appliance
active/standby node swap during an appliance firmware update) leaves stale
Critical alerts like "Unable to read server adapter configuration data" or
a server profile marked NonCompliant, even though the underlying hardware
is healthy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from proliant.oneview.targets import (
    get_profile_server,
    resolve_server_target,
    target_summary,
)

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient

__all__ = [
    "refresh_server_hardware",
    "run_refresh_action",
]


async def refresh_server_hardware(
    client: "OneViewClient",
    server: dict[str, Any],
    *,
    target_type: str,
    target_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    uri = str(server.get("uri") or "")
    if not uri:
        raise ValueError("Selected server hardware has no OneView URI")

    payload: dict[str, Any] = {"refreshState": "RefreshPending"}
    url = f"{uri.rstrip('/')}/refreshState"
    if dry_run:
        body: dict[str, Any] = {}
        status = "dry-run"
    else:
        body = await client.put(url, payload)
        status = "accepted"

    return {
        "status": status,
        "action": "refresh",
        "action_label": "refresh",
        "method": "server-hardware refreshState",
        "target_type": target_type,
        "target": target_name or server.get("name", ""),
        "server": target_summary(server),
        "url": url,
        "payload": payload,
        "task_uri": body.get("uri", ""),
        "task": body,
    }


async def run_refresh_action(
    client: "OneViewClient",
    target_type: str,
    *,
    name: str | None = None,
    enclosure: str | None = None,
    bay: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_target = target_type.lower()

    if normalized_target == "server":
        server = await resolve_server_target(client, name=name, enclosure=enclosure, bay=bay)
        return await refresh_server_hardware(
            client, server, target_type="server", dry_run=dry_run
        )
    if normalized_target == "profile":
        profile_name = name or ""
        _profile, server = await get_profile_server(client, profile_name)
        return await refresh_server_hardware(
            client,
            server,
            target_type="profile",
            target_name=profile_name,
            dry_run=dry_run,
        )

    raise ValueError(
        f"OneView does not expose 'refresh' for {normalized_target}. "
        "Use 'server' or 'profile' as the target."
    )
