"""
proliant.oneview.ssp_update
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
SSP (HPE Synergy Service Pack) firmware **baseline** rollout across managed
hardware, driven by HPE OneView / Synergy Composer. This is the *second* half
of a Synergy software release, applied only **after** the appliance software
itself has been upgraded (see ``appliance_update.py``):

  1. Appliance software upgrade (``update.bin``) — ``oneview upgrade run``.
  2. SSP hardware firmware baseline (this module) — ``oneview firmware apply``:
     bring frames / interconnects / compute modules in line with a chosen SSP.

OneView orchestrates the redundant, per-component flashing itself; the operator
only chooses the baseline, the scope, and confirms. Two scopes are exposed,
applied in HPE's recommended order:

  (a) **Shared infrastructure** — the Logical Enclosure firmware update (frame
      link modules + interconnects / Virtual Connect). Composer updates the
      redundant interconnect pair one side at a time (``Orchestrated`` mode) so
      the fabric stays up.

        PATCH /rest/logical-enclosures/{id}   (If-Match: *)
          body: [{"op":"replace","path":"/firmware","value":{
                   "firmwareBaselineUri": <uri>,
                   "firmwareUpdateOn": "SharedInfrastructureOnly",
                   "forceInstallFirmware": bool,
                   "validateIfLIFirmwareUpdateIsNonDisruptive": bool,
                   "logicalInterconnectUpdateMode": "Orchestrated",
                   "updateFirmwareOnUnmanagedInterconnect": bool}}]

  (b) **Compute** — each Server Profile's managed-firmware baseline (compute
      module iLO / BIOS / drivers). Fetch the profile, set its ``firmware``
      block to the new baseline, and PUT it back; the compute module reboots.

        GET /rest/server-profiles/{id}  ->  modify .firmware  ->  PUT it back

Both operations return an OneView **task** (``/rest/tasks/{id}``) whose
``percentComplete`` / ``taskState`` drive a live progress bar.

Payload shapes were reverse-engineered from HPE's own ``oneview-python`` SDK
(``examples/logical_enclosures.py``, ``examples/server_hardware_firmware_update.py``)
and cross-checked live against a Synergy Composer2. The write/reboot half can
only be exercised against a live appliance, so callers must gate real execution
behind an explicit confirmation; the default is a non-destructive **plan**.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── REST endpoints ────────────────────────────────────────────────────────────

LE_URI = "/rest/logical-enclosures"
PROFILES_URI = "/rest/server-profiles"
FW_DRIVERS_URI = "/rest/firmware-drivers"


# ── firmwareUpdateOn scope (logical-enclosure firmware directive) ──────────────

LE_SCOPE_ENCLOSURE = "EnclosureOnly"
LE_SCOPE_SHARED = "SharedInfrastructureOnly"
LE_SCOPE_SHARED_AND_PROFILES = "SharedInfrastructureAndServerProfiles"

# CLI-friendly install-type token -> OneView server-profile firmwareInstallType.
INSTALL_TYPES = {
    "firmware-only": "FirmwareOnly",
    "firmware-and-drivers": "FirmwareAndOSDrivers",
    "firmware-offline": "FirmwareOnlyOfflineMode",
}

# OneView task states (lower-cased) that count as terminal / success / failure.
_TASK_SUCCESS = {"completed", "warning"}
_TASK_FAILURE = {"error", "killed", "terminated", "interrupted", "timeout"}
_TASK_TERMINAL = _TASK_SUCCESS | _TASK_FAILURE


# ── baseline selection ─────────────────────────────────────────────────────────

def service_pack_baselines(raw_drivers: list[dict]) -> list[dict[str, Any]]:
    """Registered SSP/SPP (``ServicePack``) baselines, **newest first**."""
    from proliant.oneview.upgrade import _sort_by_release_date, normalize_baselines

    packs = [
        b for b in normalize_baselines(raw_drivers)
        if "servicepack" in (b.get("bundle_type") or "").lower()
    ]
    return list(reversed(_sort_by_release_date(packs)))


def baseline_id(uri: str) -> str:
    """Normalize a firmware-driver URI to its comparable short id (case-folded)."""
    return (uri or "").split("?")[0].rstrip("/").rsplit("/", 1)[-1].lower()


def same_baseline(a: str, b: str) -> bool:
    """True if two baseline URIs refer to the same (non-empty) driver."""
    ida = baseline_id(a)
    return bool(ida) and ida == baseline_id(b)


def select_baseline(baselines: list[dict], query: str | None) -> dict[str, Any] | None:
    """Pick a baseline by *query* (version / short name / uri id / name).

    With no query, returns the newest (``baselines`` is newest-first). Exact
    matches on version / name / uri-id win over substring matches so e.g.
    ``SY-2026.01.02`` doesn't get shadowed by an older partial hit.
    """
    if not baselines:
        return None
    if not query:
        return baselines[0]
    q = query.strip().lower()
    for b in baselines:
        tail = baseline_id(b.get("uri", ""))
        if q in {(b.get("version") or "").lower(), (b.get("name") or "").lower(), tail}:
            return b
    for b in baselines:
        hay = f"{b.get('name','')} {b.get('version','')} {b.get('uri','')}".lower()
        if q in hay:
            return b
    return None


# ── target normalization / resolution ──────────────────────────────────────────

def normalize_le(le: dict) -> dict[str, Any]:
    """Compact a ``/rest/logical-enclosures`` member for planning/apply."""
    fw = le.get("firmware") or {}
    return {
        "name": le.get("name", ""),
        "uri": le.get("uri", ""),
        "etag": le.get("eTag", "") or "",
        "current_baseline_uri": fw.get("firmwareBaselineUri", "") or "",
        "status": le.get("status", "") or "",
    }


def normalize_profile(p: dict) -> dict[str, Any]:
    """Compact a ``/rest/server-profiles`` member for planning/apply."""
    fw = p.get("firmware") or {}
    return {
        "name": p.get("name", ""),
        "uri": p.get("uri", ""),
        "server_hardware_uri": p.get("serverHardwareUri", "") or "",
        "manage_firmware": bool(fw.get("manageFirmware")),
        "current_baseline_uri": fw.get("firmwareBaselineUri", "") or "",
        "install_type": fw.get("firmwareInstallType", "") or "",
    }


def resolve_targets(
    items: list[dict], names: list[str] | None, all_flag: bool
) -> list[dict[str, Any]]:
    """Select targets by case-insensitive ``name`` match, or all when *all_flag*."""
    if all_flag:
        return list(items)
    if not names:
        return []
    wanted = {n.strip().lower() for n in names if n.strip()}
    return [it for it in items if (it.get("name", "") or "").lower() in wanted]


# ── plan building ──────────────────────────────────────────────────────────────

def build_plan(
    baseline: dict, le_targets: list[dict], profile_targets: list[dict]
) -> dict[str, Any]:
    """A non-destructive summary of what an apply *would* change.

    Each target is annotated with whether the requested baseline differs from
    what it currently has (``will_change``) plus a short human ``detail``.
    """
    target_uri = baseline.get("uri", "")

    le_plans: list[dict[str, Any]] = []
    for le in le_targets:
        changed = not same_baseline(le["current_baseline_uri"], target_uri)
        le_plans.append({
            "kind": "logical-enclosure",
            "name": le["name"],
            "uri": le["uri"],
            "current_baseline_uri": le["current_baseline_uri"],
            "will_change": changed,
            "detail": "up to date" if not changed else "shared infra (interconnects + frame)",
        })

    prof_plans: list[dict[str, Any]] = []
    for p in profile_targets:
        differs = not same_baseline(p["current_baseline_uri"], target_uri)
        will_change = differs or not p["manage_firmware"]
        if not p["manage_firmware"]:
            detail = "enable managed firmware"
        elif differs:
            detail = "compute (iLO / BIOS / drivers)"
        else:
            detail = "up to date"
        prof_plans.append({
            "kind": "server-profile",
            "name": p["name"],
            "uri": p["uri"],
            "server_hardware_uri": p["server_hardware_uri"],
            "manage_firmware": p["manage_firmware"],
            "current_baseline_uri": p["current_baseline_uri"],
            "will_change": will_change,
            "detail": detail,
        })

    changes = sum(1 for x in le_plans + prof_plans if x["will_change"])
    return {
        "baseline": baseline,
        "logical_enclosures": le_plans,
        "server_profiles": prof_plans,
        "changes": changes,
    }


# ── write payload construction (pure) ──────────────────────────────────────────

def build_le_firmware_patch(
    baseline_uri: str,
    *,
    scope: str = LE_SCOPE_SHARED,
    force: bool = False,
    update_mode: str = "Orchestrated",
    validate_nondisruptive: bool = True,
    update_unmanaged: bool = False,
) -> list[dict[str, Any]]:
    """JSON-patch body for a logical-enclosure firmware update.

    ``Orchestrated`` interconnect update mode flashes one side of each redundant
    interconnect pair at a time (fabric stays up); ``validate_nondisruptive``
    asks Composer to confirm the update won't drop the fabric before starting.
    """
    return [{
        "op": "replace",
        "path": "/firmware",
        "value": {
            "firmwareBaselineUri": baseline_uri,
            "firmwareUpdateOn": scope,
            "forceInstallFirmware": bool(force),
            "validateIfLIFirmwareUpdateIsNonDisruptive": bool(validate_nondisruptive),
            "logicalInterconnectUpdateMode": update_mode,
            "updateFirmwareOnUnmanagedInterconnect": bool(update_unmanaged),
        },
    }]


def build_profile_firmware_put(
    profile_full: dict,
    baseline_uri: str,
    *,
    install_type: str | None = None,
    force: bool | None = None,
    activation: str | None = None,
) -> dict[str, Any]:
    """Copy of *profile_full* with its ``firmware`` block retargeted to *baseline_uri*.

    The full profile resource is preserved and only the firmware block is
    changed (baseline + managed-firmware on, plus optional install-type /
    force / activation overrides) so OneView's full-replace PUT is safe.
    """
    body = dict(profile_full)
    fw = dict(body.get("firmware") or {})
    fw["manageFirmware"] = True
    fw["firmwareBaselineUri"] = baseline_uri
    if install_type is not None:
        fw["firmwareInstallType"] = install_type
    if force is not None:
        fw["forceInstallFirmware"] = bool(force)
    if activation is not None:
        fw["firmwareActivationType"] = activation
    body["firmware"] = fw
    return body


# ── task normalization / polling ───────────────────────────────────────────────

def _percent(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_task(data: dict | None) -> dict[str, Any]:
    """Compact an OneView task resource (or an async op's task body)."""
    d = data or {}
    return {
        "uri": d.get("uri", "") or "",
        "name": d.get("name", "") or "",
        "state": d.get("taskState", "") or "",
        "status": d.get("taskStatus", "") or "",
        "percent": _percent(d.get("percentComplete")),
        "resource": (d.get("associatedResource") or {}).get("resourceName", "") or "",
    }


def is_task_done(task: dict) -> bool:
    return (task.get("state") or "").lower() in _TASK_TERMINAL


def is_task_failed(task: dict) -> bool:
    return (task.get("state") or "").lower() in _TASK_FAILURE


async def poll_task(
    client: "OneViewClient",
    task_uri: str,
    *,
    emit: Callable[[str, dict], None],
    sleeper: Callable[[float], Any],
    interval_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    """Poll a task to a terminal state, emitting ``task-progress`` each tick."""
    waited = 0.0
    while True:
        task = normalize_task(await client.get(task_uri))
        emit("task-progress", task)
        if is_task_done(task):
            return task
        if waited >= timeout_s:
            return {**task, "state": "Timeout", "timed_out": True}
        await sleeper(interval_s)
        waited += interval_s


async def _await_task(
    client: "OneViewClient",
    resp: dict,
    emit: Callable[[str, dict], None],
    sleeper: Callable[[float], Any],
    interval_s: float,
    timeout_s: float,
) -> dict[str, Any]:
    """Resolve an async op response to a terminal task.

    OneView returns the task resource as the body of an async PATCH/PUT (HTTP
    202); if a URI is present we poll it, otherwise the operation completed
    synchronously and the body is treated as final.
    """
    task = normalize_task(resp)
    if not task["uri"]:
        return {**task, "state": task["state"] or "Completed"}
    return await poll_task(
        client, task["uri"], emit=emit, sleeper=sleeper,
        interval_s=interval_s, timeout_s=timeout_s,
    )


# ── orchestrator ───────────────────────────────────────────────────────────────

async def run_ssp_apply(
    client_factory: Callable[[], Any],
    *,
    baseline: dict,
    le_targets: list[dict],
    profile_targets: list[dict],
    scope: str = LE_SCOPE_SHARED,
    install_type: str | None = None,
    force: bool = False,
    execute: bool = False,
    confirm: Callable[[dict], bool] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    sleeper: Callable[[float], Any] | None = None,
    poll_interval_s: float = 20.0,
    task_timeout_s: float = 90 * 60,
) -> dict[str, Any]:
    """Plan (and optionally apply) an SSP firmware baseline rollout.

    Shared infrastructure (logical enclosures) is applied before compute
    (server profiles), matching HPE's recommended order. Returns a result dict
    whose ``status`` is one of:

      ``planned``        execute=False — the plan, nothing changed
      ``nothing-to-do``  execute=True but every target already matches
      ``aborted``        execute=True but the confirm callback returned False
      ``applied``        all targeted updates finished successfully
      ``failed``         a task reported failure (``results`` shows how far it got)
    """
    import asyncio

    sleeper = sleeper or asyncio.sleep
    emit = on_event or (lambda kind, data: None)

    plan = build_plan(baseline, le_targets, profile_targets)
    emit("plan", plan)

    if not execute:
        return {"status": "planned", "plan": plan}

    changing_les = [le for le in plan["logical_enclosures"] if le["will_change"] or force]
    changing_profs = [p for p in plan["server_profiles"] if p["will_change"] or force]
    if not changing_les and not changing_profs:
        return {"status": "nothing-to-do", "plan": plan}

    if confirm is not None and not confirm(plan):
        return {"status": "aborted", "plan": plan}

    results: list[dict[str, Any]] = []
    baseline_uri = baseline.get("uri", "")

    async with client_factory() as client:
        # (a) shared infrastructure first
        for le in changing_les:
            emit("applying", {"kind": "logical-enclosure", "name": le["name"]})
            patch = build_le_firmware_patch(baseline_uri, scope=scope, force=force)
            resp = await client.patch(le["uri"], patch, headers={"If-Match": "*"})
            task = await _await_task(
                client, resp, emit, sleeper, poll_interval_s, task_timeout_s
            )
            results.append({"kind": "logical-enclosure", "name": le["name"], **task})
            emit("applied", results[-1])
            if is_task_failed(task):
                return {"status": "failed", "plan": plan, "results": results}

        # (b) compute (server profiles)
        for prof in changing_profs:
            emit("applying", {"kind": "server-profile", "name": prof["name"]})
            full = await client.get(prof["uri"])
            body = build_profile_firmware_put(
                full, baseline_uri, install_type=install_type, force=force
            )
            resp = await client.put(prof["uri"], body)
            task = await _await_task(
                client, resp, emit, sleeper, poll_interval_s, task_timeout_s
            )
            results.append({"kind": "server-profile", "name": prof["name"], **task})
            emit("applied", results[-1])
            if is_task_failed(task):
                return {"status": "failed", "plan": plan, "results": results}

    return {"status": "applied", "plan": plan, "results": results}


async def fetch_apply_targets(client: "OneViewClient") -> dict[str, Any]:
    """Fetch + normalize everything the apply flow needs (baselines + targets)."""
    raw_drivers = await client.get_all(FW_DRIVERS_URI)
    les = await client.get_all(LE_URI)
    profiles = await client.get_all(PROFILES_URI)
    return {
        "baselines": service_pack_baselines(raw_drivers),
        "logical_enclosures": [normalize_le(le) for le in les],
        "server_profiles": [normalize_profile(p) for p in profiles],
    }
