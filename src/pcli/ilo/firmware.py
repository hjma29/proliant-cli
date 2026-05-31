"""
hpeilo.firmware
~~~~~~~~~~~~~~~
Firmware staging and flash operations via HPE iLO 7 Redfish API.

Flow for a remote URL update (e.g. from HPE SDR):
  1. stage_from_uri()           — downloads .fwpkg from URL into iLO component repository
  2. get_component_repository() — confirm component is staged and get its Filename
  3. add_to_task_queue()        — schedule component for flash (applied on next reboot)
  4. get_task_queue()           — monitor task status
  5. (reboot server)            — iLO applies the update during POST

Flow for direct file upload (air-gapped / iLO can't reach internet):
  1. stage_from_file()          — multipart POST upload direct to iLO HttpPushUri
  2. wait_for_stage()           — poll ComponentRepository until file appears
  3. add_to_task_queue() ...

Redfish reference (iLO 7):
  https://servermanagementportal.ext.hpe.com/docs/redfishservices/ilos/ilo7/ilo7_120/ilo7_update_service_resourcedefns120
"""

import time
from pathlib import Path

import urllib3

from redfish import RedfishClient

from pcli.ilo.client import get_update_service_uri


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _update_service(client: RedfishClient) -> dict:
    """Return the UpdateService resource dict."""
    return client.get(get_update_service_uri(client)).obj


def _oem_actions(client: RedfishClient) -> dict:
    """Return the Oem HPE actions dict from the UpdateService.

    iLO 6 (Gen10/Gen11) places them at  Actions → Oem → Hpe
    iLO 7 (Gen12)       places them at  Oem → Hpe → Actions
    We try both locations.
    """
    svc = _update_service(client)

    # iLO 7 / Gen12 path: Oem.Hpe.Actions
    actions = (
        svc.get("Oem", {})
           .get("Hpe", {})
           .get("Actions")
    )
    if actions:
        return actions

    # iLO 6 / Gen11 path: Actions.Oem.Hpe
    try:
        return svc["Actions"]["Oem"]["Hpe"]
    except KeyError as exc:
        raise RuntimeError(f"UpdateService has no Oem/Hpe actions: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def stage_from_uri(
    client: RedfishClient,
    firmware_url: str,
    *,
    dry_run: bool = False,
) -> dict:
    """Download a firmware package from a remote URL into the iLO component repository.

    This is a non-destructive staging step — nothing is flashed until
    ``add_to_task_queue`` is called and the server reboots.

    Parameters
    ----------
    client:
        Authenticated RedfishClient from ``ilo_session``.
    firmware_url:
        Public HTTPS URL of a ``.fwpkg`` file, e.g. from HPE SDR.
    dry_run:
        If True, return a dict describing what *would* be sent without
        actually posting to iLO.

    Returns
    -------
    dict
        Redfish response body, or a ``{"dry_run": true, ...}`` preview dict.

    Raises
    ------
    RuntimeError
        If the Redfish POST returns a non-2xx status.
    """
    actions = _oem_actions(client)
    target = actions.get(
        "#HpeiLOUpdateServiceExt.AddFromUri", {}
    ).get("target")
    if not target:
        raise RuntimeError(
            "AddFromUri action not found — iLO may not support remote staging"
        )

    filename = firmware_url.rsplit("/", 1)[-1]
    payload = {
        "ImageURI": firmware_url,
        "UpdateRepository": True,
        "UpdateTarget": False,
    }

    if dry_run:
        return {"dry_run": True, "target": target, "payload": payload}

    # Skip download if the file is already in the component repository
    existing = get_component_repository(client)
    if any(c.get("Filename") == filename for c in existing):
        return {"already_staged": True, "Filename": filename}

    resp = client.post(target, body=payload)
    if resp.status not in (200, 201, 202):
        raise RuntimeError(
            f"stage_from_uri failed — HTTP {resp.status}: {resp.ori}"
        )
    return resp.obj or {"status": resp.status}


def stage_from_file(
    client: RedfishClient,
    local_path: str | Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Upload a local .fwpkg file directly to iLO via HttpPushUri (multipart POST).

    Use this when iLO cannot reach the internet or a local HTTP server
    (air-gapped management network). The file is streamed from disk to iLO.

    Parameters
    ----------
    client:
        Authenticated RedfishClient.
    local_path:
        Path to the local .fwpkg file on disk.
    dry_run:
        If True, return a preview dict without uploading.

    Returns
    -------
    dict
        Redfish response body or dry-run preview.
    """
    local_path = Path(local_path)
    filename = local_path.name

    if dry_run:
        return {"dry_run": True, "local_path": str(local_path), "filename": filename}

    # Skip if already staged (inline repo check to avoid forward reference)
    svc = _update_service(client)
    push_uri = svc.get("HttpPushUri")
    if not push_uri:
        raise RuntimeError("iLO does not expose HttpPushUri — cannot upload directly")

    repo_uri = (
        svc.get("Oem", {}).get("Hpe", {}).get("ComponentRepository", {}).get("@odata.id")
        or "/redfish/v1/UpdateService/ComponentRepository/"
    )
    repo_resp = client.get(repo_uri)
    if repo_resp.status == 200:
        for m in repo_resp.obj.get("Members", []):
            existing_name = m.get("Filename") or client.get(m["@odata.id"]).obj.get("Filename")
            if existing_name == filename:
                return {"already_staged": True, "Filename": filename}

    base_url = client.base_url
    upload_url = f"{base_url}{push_uri}"

    file_data = local_path.read_bytes()
    http = urllib3.PoolManager(
        cert_reqs="CERT_NONE",
        timeout=urllib3.util.Timeout(connect=10, read=600),
    )
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    resp = http.request(
        "POST",
        upload_url,
        fields={"firmwareimage": (filename, file_data, "application/octet-stream")},
        headers={"X-Auth-Token": client.session_key},
    )
    if resp.status not in (200, 201, 202):
        raise RuntimeError(
            f"stage_from_file failed — HTTP {resp.status}: {resp.data[:200]}"
        )
    return {"uploaded": True, "Filename": filename, "status": resp.status}


def get_component_repository(client: RedfishClient) -> list[dict]:
    """Return all components currently staged in the iLO component repository.

    Each dict includes at minimum: ``Name``, ``Filename``, ``Version``,
    ``SizeBytes``, ``@odata.id``.

    Returns
    -------
    list[dict]
        One entry per staged component (may be empty).
    """
    svc = _update_service(client)
    repo_uri = (
        svc.get("Oem", {})
           .get("Hpe", {})
           .get("ComponentRepository", {})
           .get("@odata.id")
    )
    if not repo_uri:
        repo_uri = "/redfish/v1/UpdateService/ComponentRepository/"

    resp = client.get(repo_uri)
    if resp.status != 200:
        raise RuntimeError(
            f"get_component_repository failed — HTTP {resp.status}"
        )

    members = resp.obj.get("Members", [])

    # Gen12 (iLO 7) returns Members as stub objects containing only @odata.id.
    # Fetch each member individually to get Filename, Version, Name, etc.
    result = []
    for m in members:
        if "Filename" in m:
            result.append(m)           # already expanded (Gen11 / iLO 6)
        elif "@odata.id" in m:
            detail = client.get(m["@odata.id"]).obj
            result.append(dict(detail))
    return result


def get_task_queue(client: RedfishClient) -> list[dict]:
    """Return all entries in the firmware update task queue.

    Each dict includes: ``Name``, ``Filename``, ``State``, ``Result``,
    ``UpdatableBy``, ``Command``, ``@odata.id``.

    Returns
    -------
    list[dict]
        Current task queue (may be empty when idle).
    """
    resp = client.get("/redfish/v1/UpdateService/UpdateTaskQueue/")
    if resp.status != 200:
        raise RuntimeError(f"get_task_queue failed — HTTP {resp.status}")

    members = resp.obj.get("Members", [])

    # Gen12 (iLO 7) returns Members as stubs with only @odata.id — expand each.
    result = []
    for m in members:
        if "Filename" in m or "State" in m:
            result.append(m)           # already expanded (Gen11 / iLO 6)
        elif "@odata.id" in m:
            detail = client.get(m["@odata.id"]).obj
            result.append(dict(detail))
    return result


def add_to_task_queue(
    client: RedfishClient,
    filename: str,
    *,
    tpm_override: bool = True,
    updatable_by: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Schedule a staged component for flash on next reboot.

    The ``filename`` must match the ``Filename`` field returned by
    ``get_component_repository()``.

    Parameters
    ----------
    client:
        Authenticated RedfishClient from ``ilo_session``.
    filename:
        ``.fwpkg`` filename of the staged component (e.g. ``A66_1.40_01_09_2026.fwpkg``).
    tpm_override:
        Pass True to bypass TPM prompt on reboot (standard for automated flashing).
    updatable_by:
        Override the UpdatableBy list. If None (default), uses the value from the
        staged component's own metadata in the ComponentRepository. Passing all three
        values (Bmc, RuntimeAgent, Uefi) causes iLO 7 to split into two tasks, where
        the RuntimeAgent/Uefi task never fires without a SUM agent in the OS.
    dry_run:
        If True, return a preview dict without writing to iLO.

    Returns
    -------
    dict
        The created task queue entry, or a ``{"dry_run": true, ...}`` preview.

    Raises
    ------
    RuntimeError
        If the Redfish POST returns a non-2xx status.
    """
    queue_uri = "/redfish/v1/UpdateService/UpdateTaskQueue/"

    # Determine UpdatableBy based on component type (not component metadata).
    # HPE's own examples always use ["Uefi"] for reboot-applied updates.
    # Component metadata UpdatableBy is informational and misleading for this purpose:
    #   - ["Bmc"] task → BMC pre-processes, returns SystemResetRequired but does NOT flash ROM
    #   - ["Uefi"] task → UEFI applies the flash during next POST (correct for BIOS/components)
    if updatable_by is None:
        name_lower = filename.lower()
        if name_lower.startswith("ilo") or name_lower.startswith("ilo7") or name_lower.startswith("ilo6"):
            updatable_by = ["Bmc"]   # iLO: flashed immediately by BMC, no reboot
        else:
            updatable_by = ["Uefi"]  # BIOS, NIC, storage, etc.: applied at next POST

    payload = {
        "Name": f"Update-{filename}",
        "Filename": filename,
        "Command": "ApplyUpdate",
        "UpdatableBy": updatable_by,
        "TPMOverride": tpm_override,
    }

    if dry_run:
        return {"dry_run": True, "target": queue_uri, "payload": payload}

    # Skip if an active task for this filename already exists
    existing_tasks = get_task_queue(client)
    for t in existing_tasks:
        if t.get("Filename") == filename and t.get("State") in ("Pending", "InProgress"):
            return {"already_queued": True, "Filename": filename, "State": t.get("State")}

    resp = client.post(queue_uri, body=payload)
    if resp.status not in (200, 201, 202):
        raise RuntimeError(
            f"add_to_task_queue failed — HTTP {resp.status}: {resp.ori}"
        )
    return resp.obj or {"status": resp.status}


def clear_task_queue(client: RedfishClient, *, dry_run: bool = False) -> list[str]:
    """Delete all entries from the update task queue.

    Useful for cancelling a staged update before reboot, or clearing
    completed/failed entries from a previous run.

    Parameters
    ----------
    dry_run:
        If True, return the URIs that *would* be deleted without deleting.

    Returns
    -------
    list[str]
        URIs of entries that were deleted (or would be, if dry_run).
    """
    entries = get_task_queue(client)
    uris = [e["@odata.id"] for e in entries if "@odata.id" in e]

    if dry_run:
        return uris

    deleted = []
    for uri in uris:
        resp = client.delete(uri)
        if resp.status in (200, 204):
            deleted.append(uri)
        else:
            raise RuntimeError(
                f"clear_task_queue: DELETE {uri} returned HTTP {resp.status}"
            )
    return deleted


def wait_for_stage(
    client: RedfishClient,
    filename: str,
    *,
    timeout: int = 300,
    poll_interval: int = 10,
) -> dict:
    """Poll the component repository until ``filename`` appears (staging complete).

    Parameters
    ----------
    filename:
        The base filename to look for (e.g. ``A66_1.40_01_09_2026.fwpkg``).
    timeout:
        Max seconds to wait before raising TimeoutError.
    poll_interval:
        Seconds between polls.

    Returns
    -------
    dict
        The matching component entry from the repository.

    Raises
    ------
    TimeoutError
        If the component does not appear within ``timeout`` seconds.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for comp in get_component_repository(client):
            if comp.get("Filename") == filename or filename in comp.get("Name", ""):
                return comp
        time.sleep(poll_interval)

    raise TimeoutError(
        f"Component '{filename}' did not appear in the repository within {timeout}s"
    )


def wait_for_online(
    host: dict,
    *,
    offline_grace: int = 30,
    timeout: int = 600,
    poll_interval: int = 15,
) -> None:
    """Wait for the server to reboot and come back online.

    First waits ``offline_grace`` seconds (to let the server begin shutting down),
    then polls iLO until the server reports PowerState == "On".

    Parameters
    ----------
    host:
        Dict with keys: url, username, password (same as ilo_session).
    offline_grace:
        Seconds to wait before starting to poll — lets the OS begin shutdown.
    timeout:
        Max total seconds to wait after the grace period.
    poll_interval:
        Seconds between polls.

    Raises
    ------
    TimeoutError
        If the server does not come back online within ``timeout`` seconds.
    """
    from redfish.rest.v1 import ServerDownOrUnreachableError
    from pcli.ilo.client import ilo_session, get_system_uri

    time.sleep(offline_grace)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with ilo_session(host) as client:
                system = client.get(get_system_uri(client)).obj
                if system.get("PowerState") == "On":
                    return
        except (ServerDownOrUnreachableError, Exception):
            pass  # still offline or mid-reboot
        time.sleep(poll_interval)

    raise TimeoutError(f"Server did not come back online within {timeout + offline_grace}s")
