"""
hpeilo.firmware
~~~~~~~~~~~~~~~
Async firmware staging and flash operations via HPE iLO Redfish API.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from pcli.ilo.client import ILOClient, ServerDownOrUnreachableError, ilo_session


async def _update_service(client: ILOClient) -> dict[str, Any]:
    return await client.get(await client.get_update_service_uri())


async def _oem_actions(client: ILOClient) -> dict[str, Any]:
    svc = await _update_service(client)
    actions = svc.get("Oem", {}).get("Hpe", {}).get("Actions")
    if actions:
        return actions
    try:
        return svc["Actions"]["Oem"]["Hpe"]
    except KeyError as exc:
        raise RuntimeError(f"UpdateService has no Oem/Hpe actions: {exc}") from exc


async def _expand_members(
    client: ILOClient,
    members: list[dict[str, Any]],
    *,
    ready_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    coros = [client.get(item["@odata.id"]) for item in members if "@odata.id" in item and not any(key in item for key in ready_keys)]
    expanded_iter = iter(await asyncio.gather(*coros)) if coros else iter(())
    result = []
    for item in members:
        if any(key in item for key in ready_keys):
            result.append(dict(item))
        elif "@odata.id" in item:
            result.append(dict(next(expanded_iter)))
    return result


async def stage_from_uri(
    client: ILOClient,
    firmware_url: str,
    *,
    update_target: bool = False,
    dry_run: bool = False,
) -> dict:
    """Stage firmware from a URI into the iLO repository.

    When update_target=True, iLO will also immediately flash and reset itself
    (iLO firmware only). When False, the file is staged for later task queue flash.
    """
    actions = await _oem_actions(client)
    target = actions.get("#HpeiLOUpdateServiceExt.AddFromUri", {}).get("target")
    if not target:
        raise RuntimeError("AddFromUri action not found — iLO may not support remote staging")

    filename = firmware_url.rsplit("/", 1)[-1]
    payload: dict = {"ImageURI": firmware_url, "UpdateRepository": True, "UpdateTarget": update_target}
    if update_target:
        # iLO requires TPMOverrideFlag when flashing directly (UpdateTarget=True)
        payload["TPMOverrideFlag"] = True
    if dry_run:
        return {"dry_run": True, "target": target, "payload": payload}

    # Only skip re-staging when NOT doing direct flash — for direct flash we always
    # need to POST so iLO triggers the flash even if the file is already in the repo.
    if not update_target:
        existing = await get_component_repository(client)
        if any(component.get("Filename") == filename for component in existing):
            return {"already_staged": True, "Filename": filename}

    try:
        result = await client.post(target, payload)
    except RuntimeError as exc:
        # iLO returns this when a flash is already in progress (e.g. from a prior POST).
        # The flash is running — treat it as success and let the caller wait for reset.
        if "FirmwareFlashAlreadyInProgress" in str(exc):
            return {"flash_already_in_progress": True, "Filename": filename}
        raise
    return result or {"status": "accepted", "Filename": filename}


async def stage_from_file(client: ILOClient, local_path: str | Path, *, dry_run: bool = False) -> dict:
    local_path = Path(local_path)
    filename = local_path.name
    if dry_run:
        return {"dry_run": True, "local_path": str(local_path), "filename": filename}

    svc = await _update_service(client)
    push_uri = svc.get("HttpPushUri")
    if not push_uri:
        raise RuntimeError("iLO does not expose HttpPushUri — cannot upload directly")

    existing = await get_component_repository(client)
    if any(component.get("Filename") == filename for component in existing):
        return {"already_staged": True, "Filename": filename}

    with local_path.open("rb") as firmware_image:
        resp = await client.request(
            "POST",
            push_uri,
            files={"firmwareimage": (filename, firmware_image, "application/octet-stream")},
            timeout=httpx.Timeout(timeout=600.0, connect=10.0),
        )
    return {"uploaded": True, "Filename": filename, "status": resp.status_code}


async def get_component_repository(client: ILOClient) -> list[dict]:
    svc = await _update_service(client)
    repo_uri = svc.get("Oem", {}).get("Hpe", {}).get("ComponentRepository", {}).get("@odata.id")
    repo_uri = repo_uri or "/redfish/v1/UpdateService/ComponentRepository/"
    members = (await client.get(repo_uri)).get("Members", [])
    return await _expand_members(client, members, ready_keys=("Filename", "Version", "Name"))


async def get_task_queue(client: ILOClient) -> list[dict]:
    members = (await client.get("/redfish/v1/UpdateService/UpdateTaskQueue/")).get("Members", [])
    return await _expand_members(client, members, ready_keys=("Filename", "State", "Name"))


async def add_to_task_queue(
    client: ILOClient,
    filename: str,
    *,
    tpm_override: bool = True,
    updatable_by: list[str] | None = None,
    dry_run: bool = False,
) -> dict:
    queue_uri = "/redfish/v1/UpdateService/UpdateTaskQueue/"
    if updatable_by is None:
        name_lower = filename.lower()
        if name_lower.startswith(("ilo", "ilo7", "ilo6")):
            updatable_by = ["Bmc"]
        else:
            updatable_by = ["Uefi"]

    payload = {
        "Name": f"Update-{filename}",
        "Filename": filename,
        "Command": "ApplyUpdate",
        "UpdatableBy": updatable_by,
        "TPMOverride": tpm_override,
    }
    if dry_run:
        return {"dry_run": True, "target": queue_uri, "payload": payload}

    existing_tasks = await get_task_queue(client)
    for task in existing_tasks:
        if task.get("Filename") == filename and task.get("State") in ("Pending", "InProgress"):
            return {"already_queued": True, "Filename": filename, "State": task.get("State")}

    result = await client.post(queue_uri, payload)
    return result or {"status": "accepted", "Filename": filename}


async def clear_task_queue(client: ILOClient, *, dry_run: bool = False) -> list[str]:
    entries = await get_task_queue(client)
    uris = [entry["@odata.id"] for entry in entries if "@odata.id" in entry]
    if dry_run:
        return uris

    deleted = []
    for uri in uris:
        status = await client.delete(uri)
        if status in (200, 204):
            deleted.append(uri)
        else:
            raise RuntimeError(f"clear_task_queue: DELETE {uri} returned HTTP {status}")
    return deleted


async def wait_for_stage(
    client: ILOClient,
    filename: str,
    *,
    timeout: int = 300,
    poll_interval: int = 10,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for component in await get_component_repository(client):
            if component.get("Filename") == filename or filename in component.get("Name", ""):
                return component
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"Component '{filename}' did not appear in the repository within {timeout}s")


async def wait_for_online(
    host: dict,
    *,
    offline_grace: int = 30,
    timeout: int = 600,
    poll_interval: int = 15,
) -> None:
    await asyncio.sleep(offline_grace)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with ilo_session(host) as client:
                system = await client.get(await client.get_system_uri())
                if system.get("PowerState") == "On":
                    return
        except (ServerDownOrUnreachableError, httpx.HTTPError, RuntimeError):
            pass
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"Server did not come back online within {timeout + offline_grace}s")


async def wait_for_ilo_reset(
    host: dict,
    *,
    offline_grace: int = 20,
    offline_timeout: int = 60,
    online_timeout: int = 180,
    poll_interval: int = 10,
) -> None:
    """Wait for iLO to go offline then come back online after a firmware flash.

    Sequence: sleep offline_grace → confirm offline → wait for online.
    This avoids the race where wait_for_online returns before the reset begins.
    """
    await asyncio.sleep(offline_grace)

    # Wait for iLO to go offline
    deadline = time.monotonic() + offline_timeout
    went_offline = False
    while time.monotonic() < deadline:
        try:
            async with ilo_session(host) as client:
                await client.get("/redfish/v1/")
        except (ServerDownOrUnreachableError, httpx.HTTPError, RuntimeError):
            went_offline = True
            break
        await asyncio.sleep(poll_interval)

    if not went_offline:
        # iLO didn't go offline — flash may have been immediate or failed silently
        # Still wait briefly and try to confirm version
        await asyncio.sleep(10)

    # Wait for iLO to come back online
    deadline = time.monotonic() + online_timeout
    while time.monotonic() < deadline:
        try:
            async with ilo_session(host) as client:
                await client.get("/redfish/v1/")
                return
        except (ServerDownOrUnreachableError, httpx.HTTPError, RuntimeError):
            pass
        await asyncio.sleep(poll_interval)
    raise TimeoutError(f"iLO did not come back online within {offline_grace + offline_timeout + online_timeout}s")
