"""
hpeilo.client
~~~~~~~~~~~~~
Shared connection management and Redfish URI navigators.

Design principles
-----------------
* ``ilo_session`` is a context manager — login on enter, logout on exit even
  if the body raises.  Every module that needs a client uses it; nothing
  else calls RedfishClient directly.
* URI navigators (_get_*_uri) discover endpoints from the Redfish root instead
  of hardcoding "/redfish/v1/Systems/1".  This works on multi-node or
  non-standard chassis numbering without code changes.
* All navigators are module-private (leading underscore) because callers in
  inventory.py / power.py / firmware.py import them explicitly; they are not
  part of the package's public surface.
"""

import urllib3

from contextlib import contextmanager
from typing import Generator

from redfish import RedfishClient
from redfish.rest.v1 import ServerDownOrUnreachableError  # noqa: F401 — re-exported for callers

# Connect timeout: give up quickly on unreachable hosts so the CLI doesn't hang.
# Read timeout is longer to allow slow Redfish responses on busy iLOs.
_CONNECT_TIMEOUT = urllib3.util.Timeout(connect=10, read=60)
_RETRIES = urllib3.util.Retry(connect=1, read=2, redirect=2)


@contextmanager
def ilo_session(host: dict) -> Generator[RedfishClient, None, None]:
    """Connect to an iLO, yield an authenticated client, always logout.

    Usage::

        from pcli.ilo.client import ilo_session

        with ilo_session(host) as client:
            resp = client.get("/redfish/v1/Systems/1")

    Parameters
    ----------
    host:
        Dict with keys: url, username, password.

    Raises
    ------
    ServerDownOrUnreachableError
        Propagated to caller if the iLO is unreachable at login time.
    """
    client = RedfishClient(
        base_url=host["url"],
        username=host["username"],
        password=host["password"],
        timeout=_CONNECT_TIMEOUT,
        retries=_RETRIES,
    )
    client.login()
    try:
        yield client
    finally:
        # logout() runs even if the caller's body raises — no leaked sessions.
        client.logout()


# ---------------------------------------------------------------------------
# URI navigators — call these instead of hardcoding /redfish/v1/Systems/1
# ---------------------------------------------------------------------------

def get_system_uri(client: RedfishClient) -> str:
    """Return the URI of the first ComputerSystem (typically /Systems/1)."""
    root_uri = client.root.obj["Systems"]["@odata.id"]
    members = client.get(root_uri).obj.get("Members", [])
    if not members:
        raise RuntimeError("No Systems members found in Redfish root")
    return members[0]["@odata.id"]


def get_chassis_uri(client: RedfishClient) -> str:
    """Return the URI of the first Chassis (typically /Chassis/1)."""
    root_uri = client.root.obj["Chassis"]["@odata.id"]
    members = client.get(root_uri).obj.get("Members", [])
    if not members:
        raise RuntimeError("No Chassis members found in Redfish root")
    return members[0]["@odata.id"]


def get_manager_uri(client: RedfishClient) -> str:
    """Return the URI of the first Manager (the iLO manager resource)."""
    root_uri = client.root.obj["Managers"]["@odata.id"]
    members = client.get(root_uri).obj.get("Members", [])
    if not members:
        raise RuntimeError("No Managers members found in Redfish root")
    return members[0]["@odata.id"]


def get_update_service_uri(client: RedfishClient) -> str:
    """Return the URI of the UpdateService."""
    return client.root.obj["UpdateService"]["@odata.id"]


def get_firmware_inventory_uri(client: RedfishClient) -> str:
    """Return the URI of the FirmwareInventory collection."""
    update_uri = get_update_service_uri(client)
    return client.get(update_uri).obj["FirmwareInventory"]["@odata.id"]
