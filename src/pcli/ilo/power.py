"""
pcli.ilo.power
~~~~~~~~~~~~~~
Server power and reset operations.
"""

from __future__ import annotations

from pcli.ilo.client import ILOClient

RESET_TYPES = frozenset(
    {"On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart", "Nmi", "PushPowerButton"}
)


async def reset_server(client: ILOClient, reset_type: str = "GracefulRestart") -> dict:
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

    await client.post(target_uri, {"ResetType": reset_type})
    return {"status": "accepted", "url": target_uri, "reset_type": reset_type}


async def power_on(client: ILOClient) -> dict:
    return await reset_server(client, reset_type="On")


async def graceful_shutdown(client: ILOClient) -> dict:
    return await reset_server(client, reset_type="GracefulShutdown")


async def force_off(client: ILOClient) -> dict:
    return await reset_server(client, reset_type="ForceOff")
