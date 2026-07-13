"""
proliant.oneview.power
~~~~~~~~~~~~~~~~~~~~~~
OneView-managed graceful power operations: on / off / shutdown, issued
against OneView's own server-hardware ``powerState`` resource (``PUT
/rest/server-hardware/{id}/powerState``). This is a OneView appliance API
call, not a direct Redfish call to the blade's iLO: for OneView-managed
Synergy hardware, OneView owns and rotates the local iLO admin account, so
this CLI has no separate Redfish credentials to talk to the BMC directly.
OneView translates the request into the equivalent Redfish/IPMI action
against the management processor internally.

For a hard power-cycle (Synergy bay eFuse), see ``proliant.oneview.efuse``
/ ``proliant oneview efuse`` -- that uses a different mechanism (PATCHing
the enclosure's ``bayPowerState``) and is intentionally not exposed here.
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


SERVER_POWER_ACTIONS: dict[str, tuple[str, str | None, str]] = {
    "on": ("On", None, "power on"),
    "off": ("Off", "PressAndHold", "force power off"),
    "shutdown": ("Off", "MomentaryPress", "gracefully shut down"),
}

__all__ = [
    "SERVER_POWER_ACTIONS",
    "set_server_power_state",
    "run_power_action",
]


async def set_server_power_state(
    client: "OneViewClient",
    server: dict[str, Any],
    *,
    action: str,
    target_type: str,
    target_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_action = action.lower()
    if normalized_action not in SERVER_POWER_ACTIONS:
        raise ValueError(f"Unsupported server power action '{action}'")

    power_state, power_control, label = SERVER_POWER_ACTIONS[normalized_action]
    uri = str(server.get("uri") or "")
    if not uri:
        raise ValueError("Selected server hardware has no OneView URI")

    payload: dict[str, Any] = {"powerState": power_state}
    if power_control:
        payload["powerControl"] = power_control

    url = f"{uri.rstrip('/')}/powerState"
    if dry_run:
        body: dict[str, Any] = {}
        status = "dry-run"
    else:
        body = await client.put(url, payload)
        status = "accepted"

    return {
        "status": status,
        "action": normalized_action,
        "action_label": label,
        "method": "server-hardware powerState",
        "target_type": target_type,
        "target": target_name or server.get("name", ""),
        "server": target_summary(server),
        "url": url,
        "payload": payload,
        "task_uri": body.get("uri", ""),
        "task": body,
    }


async def run_power_action(
    client: "OneViewClient",
    action: str,
    target_type: str,
    *,
    name: str | None = None,
    enclosure: str | None = None,
    bay: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    normalized_action = action.lower()
    normalized_target = target_type.lower()

    if normalized_action not in SERVER_POWER_ACTIONS:
        raise ValueError(
            f"Unsupported OneView power action '{action}'. "
            "Use 'proliant oneview efuse' for a hard power-cycle."
        )

    if normalized_target == "server":
        server = await resolve_server_target(client, name=name, enclosure=enclosure, bay=bay)
        return await set_server_power_state(
            client, server, action=normalized_action, target_type="server", dry_run=dry_run
        )
    if normalized_target == "profile":
        profile_name = name or ""
        _profile, server = await get_profile_server(client, profile_name)
        return await set_server_power_state(
            client,
            server,
            action=normalized_action,
            target_type="profile",
            target_name=profile_name,
            dry_run=dry_run,
        )

    raise ValueError(
        f"OneView does not expose '{normalized_action}' for {normalized_target}. "
        "Use 'proliant oneview efuse' for a hard power-cycle."
    )
