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


# ── OneView ↔ SSP compatibility (advisory) ─────────────────────────────────────
#
# OneView and SSPs ship as matched HPE Synergy Software Releases; an SSP is only
# validated against specific appliance versions. The appliance's firmware-drivers
# API exposes NO minimum-OneView field, so this pairing can't be read live — it
# lives only in HPE's published matrix. The map below is a snapshot of that table
# (keyed by OneView "track" — e.g. 10.0), used to surface a source-backed note in
# the plan. It is advisory: always confirm against the SSP release notes.
#
#   Source: HPE Synergy Software Releases — Overview
#   https://support.hpe.com/docs/display/public/synergy-sw-release/index.html
#
# "xx" in a supported entry is a wildcard for the trailing component (a whole
# point-release family, e.g. 2025.05.xx). Tracks whose appliance version string
# is ambiguous to parse (11.01, 9.10.01) are intentionally omitted → "unknown".
SSP_COMPAT_SOURCE_URL = "https://support.hpe.com/docs/display/public/synergy-sw-release/index.html"
SSP_COMPAT_AS_OF = "2026-07"

SSP_COMPAT: dict[str, dict[str, Any]] = {
    "11.3": {"recommended": "2026.04.01",
             "supported": ["2026.01.02", "2025.10.02", "2025.07.03", "2025.05.xx"]},
    "11.2": {"recommended": "2026.04.01", "supported": ["2026.01.02"]},
    "11.1": {"recommended": "2026.04.01", "supported": ["2026.01.02", "2025.10.02"]},
    "10.2": {"recommended": "2025.10.02", "supported": ["2025.07.03", "2025.05.xx"]},
    "10.1": {"recommended": "2025.07.03", "supported": ["2025.05.xx"]},
    "10.0": {"recommended": "2026.01.02",
             "supported": ["2025.10.02", "2025.07.03", "2025.05.01"]},
    "9.4": {"recommended": "2025.05.01", "supported": []},
    "9.3": {"recommended": "2025.05.01", "supported": []},
    "9.2": {"recommended": "2024.11.02", "supported": []},
    "9.0": {"recommended": "2024.11.02", "supported": []},
}


def oneview_track(version: str) -> str:
    """Reduce a OneView ``softwareVersion`` to its HPE matrix "track" label.

    HPE zero-pads the minor field to two digits (``9.20``, ``10.00``, ``10.10``)
    while the release matrix labels drop the trailing zero (``9.2``, ``10.0``,
    ``10.1``). Examples::

        "10.00.00-0507518" -> "10.0"
        "9.20.00-0500184"  -> "9.2"
        "10.10.00"         -> "10.1"

    Returns ``""`` when *version* isn't a recognizable ``MAJOR.MINOR…`` string.
    """
    head = (version or "").strip().split("-", 1)[0].split(" ", 1)[0]
    parts = head.split(".")
    if not parts or not parts[0].isdigit():
        return ""
    major = int(parts[0])
    minor_field = parts[1] if len(parts) > 1 and parts[1].isdigit() else "0"
    n = int(minor_field)
    minor = n // 10 if n >= 10 and n % 10 == 0 else n
    return f"{major}.{minor}"


def ssp_release(baseline: dict) -> str:
    """The bare SSP release id from a baseline (``SY-2026.01.02`` -> ``2026.01.02``)."""
    v = (baseline.get("version") or "").strip()
    if "-" in v:
        v = v.split("-", 1)[1]
    return v


def _ssp_matches(ssp: str, listed: str) -> bool:
    """Compare an SSP release id to a matrix entry, treating ``xx`` as a wildcard."""
    if not ssp or not listed:
        return False
    if ssp == listed:
        return True
    a, b = ssp.split("."), listed.split(".")
    if len(a) != len(b):
        return False
    return all(x == y or y.lower() == "xx" for x, y in zip(a, b))


def compat_note(appliance_version: str, baseline: dict) -> dict[str, Any]:
    """Classify *baseline* against *appliance_version* using the HPE matrix.

    ``status`` is one of ``recommended`` / ``supported`` / ``unsupported`` /
    ``unknown`` (no published data for that OneView track). Always includes the
    source URL + snapshot date so the plan can cite it.
    """
    track = oneview_track(appliance_version)
    ssp = ssp_release(baseline)
    note: dict[str, Any] = {
        "appliance_version": appliance_version,
        "appliance_track": track,
        "ssp": ssp,
        "recommended": None,
        "supported": [],
        "source_url": SSP_COMPAT_SOURCE_URL,
        "as_of": SSP_COMPAT_AS_OF,
    }
    entry = SSP_COMPAT.get(track)
    if not entry:
        note["status"] = "unknown"
        note["message"] = (
            f"No published SSP compatibility data for OneView "
            f"{track or appliance_version or 'this appliance'}."
        )
        return note
    recommended = entry["recommended"]
    supported = list(entry.get("supported", []))
    note["recommended"] = recommended
    note["supported"] = supported
    if _ssp_matches(ssp, recommended):
        note["status"] = "recommended"
        note["message"] = f"SSP {ssp} is HPE's recommended baseline for OneView {track}."
    elif any(_ssp_matches(ssp, s) for s in supported):
        note["status"] = "supported"
        note["message"] = (
            f"SSP {ssp} is supported on OneView {track} "
            f"(recommended is {recommended})."
        )
    else:
        allowed = ", ".join([recommended, *supported]) if supported else recommended
        note["status"] = "unsupported"
        note["message"] = (
            f"SSP {ssp} is not listed for OneView {track} — HPE lists: {allowed}."
        )
    return note


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
    baseline: dict, le_targets: list[dict], profile_targets: list[dict],
    *, appliance_version: str = "",
) -> dict[str, Any]:
    """A non-destructive summary of what an apply *would* change.

    Each target is annotated with whether the requested baseline differs from
    what it currently has (``will_change``) plus a short human ``detail``. When
    *appliance_version* is given, a source-backed ``compat`` note pairs the
    running OneView version with the chosen SSP (see :func:`compat_note`).
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
        "compat": compat_note(appliance_version, baseline) if appliance_version else None,
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
    appliance_version: str = "",
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

    plan = build_plan(baseline, le_targets, profile_targets, appliance_version=appliance_version)
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
    try:
        ver = await client.get("/rest/appliance/nodeinfo/version")
        appliance_version = (ver or {}).get("softwareVersion", "")
    except Exception:  # noqa: BLE001 — version is advisory; never fail the whole apply on it
        appliance_version = ""
    return {
        "baselines": service_pack_baselines(raw_drivers),
        "logical_enclosures": [normalize_le(le) for le in les],
        "server_profiles": [normalize_profile(p) for p in profiles],
        "appliance_version": appliance_version,
    }
