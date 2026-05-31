"""
hpeilo.power
~~~~~~~~~~~~
Server power / reset operations — all functions here POST to the iLO.

These are WRITE operations.  They are separated from inventory.py
deliberately so that read-only scripts can import inventory without
any risk of accidentally pulling in destructive code.

Redfish endpoint reference (iLO 7 v1.20):
  Systems resource definitions:
  https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo7/ilo7_120/ilo7_computer_system_resourcedefns120

  Reset action target:
    GET /Systems/1 → .Actions.#ComputerSystem.Reset.target
    POST {target}  → { "ResetType": "<value>" }

  Supported ResetType values (subset):
    GracefulRestart   – OS-controlled reboot (safe, slower)
    ForceRestart      – hard reset, no OS shutdown (fast, data risk)
    GracefulShutdown  – OS-controlled power-off
    ForceOff          – cut power immediately
    On                – power on from off state
"""

from redfish import RedfishClient

from pcli.ilo.client import get_system_uri

# Valid ResetType values accepted by HPE iLO
RESET_TYPES = frozenset(
    {"On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart", "Nmi", "PushPowerButton"}
)


def reset_server(client: RedfishClient, reset_type: str = "GracefulRestart") -> dict:
    """Send a reset action to the server.

    Parameters
    ----------
    client:
        An authenticated RedfishClient (from ``ilo_session``).
    reset_type:
        One of the RESET_TYPES values.  Defaults to GracefulRestart
        (OS-controlled reboot).  Use ForceRestart only when the OS
        is unresponsive.

    Returns
    -------
    dict
        The raw Redfish response dict (status + headers).

    Raises
    ------
    ValueError
        If reset_type is not a recognised value.
    RuntimeError
        If the Reset action target URI is not found on the system.
    """
    if reset_type not in RESET_TYPES:
        raise ValueError(f"Invalid reset_type '{reset_type}'. Valid values: {sorted(RESET_TYPES)}")

    system = client.get(get_system_uri(client)).obj
    actions = system.get("Actions", {})
    reset_action = actions.get("#ComputerSystem.Reset", {})
    target_uri = reset_action.get("target")
    if not target_uri:
        raise RuntimeError("ComputerSystem.Reset action not found on this system")

    resp = client.post(target_uri, body={"ResetType": reset_type})
    return {"status": resp.status, "url": target_uri, "reset_type": reset_type}


def power_on(client: RedfishClient) -> dict:
    """Power on a server that is currently off."""
    return reset_server(client, reset_type="On")


def graceful_shutdown(client: RedfishClient) -> dict:
    """Request an OS-controlled shutdown."""
    return reset_server(client, reset_type="GracefulShutdown")


def force_off(client: RedfishClient) -> dict:
    """Immediately cut power — use only when graceful shutdown is not possible."""
    return reset_server(client, reset_type="ForceOff")
