"""
proliant.ilo.power
~~~~~~~~~~~~~~
Server power and reset operations.
"""

from __future__ import annotations

from proliant.ilo.client import ILOClient

RESET_TYPES = frozenset(
    {"On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart", "Nmi", "PushPowerButton"}
)


async def reset_server(
    client: ILOClient,
    reset_type: str = "GracefulRestart",
    *,
    dry_run: bool = False,
) -> dict:
    """Send a reset action to the server."""
    if reset_type not in RESET_TYPES:
        raise ValueError(f"Invalid reset_type '{reset_type}'. Valid values: {sorted(RESET_TYPES)}")

    system = await client.get(await client.get_system_uri())
    target_uri = (
        system.get("Actions", {})
        .get("#ComputerSystem.Reset", {})
        .get("target")
    )
    if not target_uri:
        raise RuntimeError("ComputerSystem.Reset action not found on this system")

    payload = {"ResetType": reset_type}
    if dry_run:
        return {"status": "dry-run", "url": target_uri, "payload": payload, "reset_type": reset_type}

    await client.post(target_uri, payload)
    return {"status": "accepted", "url": target_uri, "reset_type": reset_type}


async def power_on(client: ILOClient, *, dry_run: bool = False) -> dict:
    return await reset_server(client, reset_type="On", dry_run=dry_run)


async def graceful_shutdown(client: ILOClient, *, dry_run: bool = False) -> dict:
    return await reset_server(client, reset_type="GracefulShutdown", dry_run=dry_run)


async def force_off(client: ILOClient, *, dry_run: bool = False) -> dict:
    return await reset_server(client, reset_type="ForceOff", dry_run=dry_run)
