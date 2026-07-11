"""
proliant.oneview.ssp_update
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
SSP (HPE Synergy Service Pack) firmware **baseline** rollout across managed
hardware, driven by HPE OneView / Synergy Composer. This is the *second* half
of a Synergy software release, applied only **after** the appliance software
itself has been upgraded (see ``appliance_update.py``):

  1. Appliance software upgrade (``update.bin``) — ``oneview update appliance run``.
  2. SSP hardware firmware baseline (this module) — ``oneview update enclosure``:
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

import json
import re
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── REST endpoints ────────────────────────────────────────────────────────────

LE_URI = "/rest/logical-enclosures"
PROFILES_URI = "/rest/server-profiles"
FW_DRIVERS_URI = "/rest/firmware-drivers"
HARDWARE_URI = "/rest/server-hardware"


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


def recommended_release_date(rec_release: str, baselines: list[dict] | None) -> str:
    """Release date (``YYYY-MM-DD``) of the recommended SSP, if it's registered.

    Best-effort: the recommended SSP may not be imported on this appliance, in
    which case there's no local date to show (returns ``""``).
    """
    for b in baselines or []:
        if ssp_release(b) == rec_release:
            return (b.get("release_date") or "")[:10]
    return ""


def compat_note(
    appliance_version: str, baseline: dict, baselines: list[dict] | None = None
) -> dict[str, Any]:
    """Classify *baseline* against *appliance_version* using the HPE matrix.

    ``status`` is one of ``recommended`` / ``supported`` / ``unsupported`` /
    ``unknown`` (no published data for that OneView track). Always includes the
    source URL + snapshot date so the plan can cite it. When *baselines* (the
    registered SSPs) is given, the recommended SSP's release date is resolved
    best-effort for display.
    """
    track = oneview_track(appliance_version)
    ssp = ssp_release(baseline)
    note: dict[str, Any] = {
        "appliance_version": appliance_version,
        "appliance_track": track,
        "ssp": ssp,
        "recommended": None,
        "recommended_release_date": "",
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
    note["recommended_release_date"] = recommended_release_date(recommended, baselines)
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


def compat_matrix() -> list[dict[str, Any]]:
    """The full HPE Synergy Software Releases matrix, ready for display.

    One row per OneView "track" in :data:`SSP_COMPAT`, in the same order
    the table is defined (HPE's current Recommended Milestone first). See
    `proliant oneview release`.
    """
    return [
        {"track": track, "recommended": entry["recommended"],
         "supported": list(entry.get("supported", []))}
        for track, entry in SSP_COMPAT.items()
    ]


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
        "enclosure_uris": list(le.get("enclosureUris") or []),
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


def hardware_enclosure_map(raw_hardware: list[dict]) -> dict[str, str]:
    """Map each ``/rest/server-hardware`` member's own uri to its enclosure's uri.

    Same field fallback as ``oneview.enclosures._server_enclosure_uri``: Synergy
    compute modules carry their enclosure under ``locationUri`` (occasionally
    ``enclosureUri``/``serverGroupUri`` on older payload shapes).
    """
    out = {}
    for hw in raw_hardware:
        uri = hw.get("uri", "")
        if uri:
            out[uri] = hw.get("locationUri") or hw.get("enclosureUri") or hw.get("serverGroupUri") or ""
    return out


def find_le_by_name(les: list[dict], name: str) -> dict[str, Any] | None:
    """Case-insensitive exact-name lookup among normalized logical enclosures."""
    q = (name or "").strip().lower()
    for le in les:
        if (le.get("name", "") or "").lower() == q:
            return le
    return None


def profiles_under_le(
    le: dict, profiles: list[dict], hw_enclosure_map: dict[str, str]
) -> list[dict[str, Any]]:
    """Server profiles whose compute module lives in one of *le*'s enclosures.

    Mirrors the OneView GUI's "Update firmware for: Shared infrastructure and
    profiles" scope, which auto-selects every profile in the logical
    enclosure's frames rather than requiring them to be named individually.
    """
    encs = set(le.get("enclosure_uris") or [])
    if not encs:
        return []
    return [
        p for p in profiles
        if hw_enclosure_map.get(p.get("server_hardware_uri", ""), "") in encs
    ]


# ── plan building ──────────────────────────────────────────────────────────────

def build_plan(
    baseline: dict, le_targets: list[dict], profile_targets: list[dict],
    *, appliance_version: str = "", baselines: list[dict] | None = None,
) -> dict[str, Any]:
    """A non-destructive summary of what an apply *would* change.

    Each target is annotated with whether the requested baseline differs from
    what it currently has (``will_change``) plus a short human ``detail``. When
    *appliance_version* is given, a source-backed ``compat`` note pairs the
    running OneView version with the chosen SSP (see :func:`compat_note`);
    *baselines* lets it resolve the recommended SSP's release date.
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
        "compat": (
            compat_note(appliance_version, baseline, baselines) if appliance_version else None
        ),
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
    updates = d.get("progressUpdates") or []
    stage = ""
    for u in reversed(updates):
        text = (u or {}).get("statusUpdate", "") or ""
        if text.strip():
            stage = text.strip()
            break
    return {
        "uri": d.get("uri", "") or "",
        "name": d.get("name", "") or "",
        "state": d.get("taskState", "") or "",
        "status": d.get("taskStatus", "") or "",
        "percent": _percent(d.get("percentComplete")),
        "resource": (d.get("associatedResource") or {}).get("resourceName", "") or "",
        "stage": stage,
    }


def is_task_done(task: dict) -> bool:
    return (task.get("state") or "").lower() in _TASK_TERMINAL


def is_task_failed(task: dict) -> bool:
    return (task.get("state") or "").lower() in _TASK_FAILURE


def _resource_baseline_uri(resource: dict | None) -> str:
    return ((resource or {}).get("firmware") or {}).get("firmwareBaselineUri", "") or ""


async def _actual_le_baseline_uri(
    client: "OneViewClient", le: dict, raw_lis: list[dict],
) -> str | None:
    """The Logical Enclosure's *actually installed* shared-infra SSP baseline.

    ``le["current_baseline_uri"]`` (``firmware.firmwareBaselineUri``) only
    ever reflects the *target* -- OneView sets it as soon as an update is
    requested and never rolls it back if the rollout fails, is blocked, or
    never finishes propagating to hardware (see ``interconnects.py``'s
    ``_resolve_baseline_uri`` docstring). Verified live: after a rollout was
    blocked by a non-redundant-fabric guard, the LE's target field pointed at
    the new baseline while every Logical Interconnect under it still reported
    its *old* SPP as actually installed -- so a naive target-vs-target
    comparison wrongly reported the enclosure as already "up to date".

    Each Logical Interconnect's own ``/firmware`` sub-resource's ``sppUri``
    is the real, currently-applied SPP (the same source the GUI's own
    "Installed:" state reads from), so that -- not the LE's target pointer --
    is authoritative for "is this already applied".

    Returns ``None`` (meaning: fall back to the target-only comparison) if no
    Logical Interconnects can be resolved under this LE, if their installed
    SPP disagrees with one another, or if a lookup fails.
    """
    enc_uris = set(le.get("enclosure_uris") or [])
    if not enc_uris:
        return None
    matches = [li for li in raw_lis if enc_uris & set(li.get("enclosureUris") or [])]
    if not matches:
        return None
    spp_uris: set[str] = set()
    for li in matches:
        try:
            fw = await client.get(f"{li['uri']}/firmware") or {}
        except Exception:  # noqa: BLE001 - best-effort; caller falls back
            return None
        spp_uris.add(fw.get("sppUri", "") or "")
    if len(spp_uris) != 1:
        return None  # mixed or unknown across LIs -- don't guess, fall back
    return next(iter(spp_uris))


_EMBEDDED_REF_RE = re.compile(r'\{[^{}]*"name"\s*:\s*"[^"]*"[^{}]*\}')


def _clean_embedded_refs(text: str) -> str:
    """OneView embeds raw ``{"name":"X","uri":"..."}`` JSON blobs inline in
    some task messages (the GUI renders each as a clickable link) -- collapse
    each blob down to just its ``name`` so plain-text CLI output reads the
    same as the GUI, e.g. ``{"name":"LE01-LIG-VC100","uri":"/rest/..."}`` ->
    ``LE01-LIG-VC100``."""
    def _replace(m: re.Match) -> str:
        try:
            return json.loads(m.group(0)).get("name", m.group(0))
        except (json.JSONDecodeError, AttributeError):
            return m.group(0)
    return _EMBEDDED_REF_RE.sub(_replace, text)


async def _task_block_reason(client: "OneViewClient", task_uri: str) -> dict[str, str]:
    """Best-effort structured reason a ``Warning`` task actually changed nothing.

    OneView can report a task as ``Warning`` / 100% / "successful with
    warning" for a firmware update that it only validated and never actually
    applied (e.g. blocked by a non-redundant-connectivity guard). The full
    explanation -- exactly what the GUI's own "Review the warnings..." modal
    shows -- lives in that task's own ``taskErrors``: ``details`` (or
    ``message`` as a fallback, since OneView puts the real explanation in
    whichever field the parent/child task shape leaves free) is the warning
    body, and ``recommendedActions`` is the "Resolution:" steps. Some task
    shapes instead carry this on a child task (``parentTaskUri=<task_uri>``),
    checked as a fallback. Never raises: this is a diagnostic nicety layered
    on top of the baseline-mismatch check that actually decides pass/fail.
    """
    async def _errors_of(uri: str) -> list[dict]:
        try:
            return (await client.get(uri) or {}).get("taskErrors") or []
        except Exception:  # noqa: BLE001 - best-effort
            return []

    errs = await _errors_of(task_uri)
    if not errs:
        try:
            data = await client.get(
                "/rest/tasks", params={"filter": f"parentTaskUri='{task_uri}'", "count": 50},
            )
        except Exception:  # noqa: BLE001 - best-effort; caller still reports "blocked"
            data = {}
        for child in data.get("members", []) or []:
            errs.extend(child.get("taskErrors") or [])

    warnings: list[str] = []
    resolutions: list[str] = []
    for err in errs:
        body = (err.get("details") or err.get("message") or "").strip()
        if body:
            body = _clean_embedded_refs(body)
            if body not in warnings:
                warnings.append(body)
        for action in err.get("recommendedActions") or []:
            action = (action or "").strip()
            if action:
                action = _clean_embedded_refs(action)
                if action not in resolutions:
                    resolutions.append(action)

    return {"warning": "\n\n".join(warnings), "resolution": "\n\n".join(resolutions)}


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

    OneView returns the monitoring task URI for an async PATCH/PUT (HTTP 202)
    in the ``Location`` header, which the client promotes into the body's
    ``uri``. When a task URI is present we poll it to completion; otherwise the
    operation completed synchronously and the body is treated as final.
    """
    task = normalize_task(resp)
    if "/rest/tasks/" not in task["uri"]:
        return {**task, "state": task["state"] or "Completed"}
    # Surface an immediate first tick so the bar shows the task the moment it
    # is accepted, rather than waiting a full poll interval at 0%.
    emit("task-progress", task)
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
    on_validation_blocked: Callable[[dict], bool] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    sleeper: Callable[[float], Any] | None = None,
    poll_interval_s: float = 20.0,
    task_timeout_s: float = 90 * 60,
    appliance_version: str = "",
    baselines: list[dict] | None = None,
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
      ``blocked``        OneView validated the update and refused to apply it
                          (e.g. a non-redundant-fabric guard), and the operator
                          declined to proceed anyway -- the last ``results``
                          entry carries ``blocked_reason``/``blocked_resolution``.

    Mirrors the OneView GUI's own "Review the warnings. If these conditions
    are acceptable, click OK to proceed." flow: each target is first
    attempted with OneView's non-disruptive validation guard on (unless
    ``force`` is already set). If OneView blocks it -- reported as a
    ``Warning`` task whose target baseline never actually moved -- the real
    reason is surfaced via
    ``on_validation_blocked({"kind", "name", "reason", "resolution"})``, where
    ``reason`` is the same warning text and ``resolution`` the same
    "Resolution:" steps the GUI's own modal shows. Returning ``True`` retries
    that *same* target with the guard bypassed (no need to re-run the whole
    command with ``--force``); returning ``False`` (or passing no callback)
    stops with ``status == "blocked"``.
    """
    import asyncio

    sleeper = sleeper or asyncio.sleep
    emit = on_event or (lambda kind, data: None)
    baseline_uri = baseline.get("uri", "")

    async with client_factory() as client:
        plan = build_plan(
            baseline, le_targets, profile_targets,
            appliance_version=appliance_version, baselines=baselines,
        )

        # The LE's own target pointer can say "up to date" even when the SSP
        # was never actually installed on its interconnects (blocked, still
        # in progress, or reverted) -- cross-check against what's actually
        # running before trusting a "no change needed" verdict.
        try:
            raw_lis = await client.get_all("/rest/logical-interconnects")
        except Exception:  # noqa: BLE001 - best-effort; falls back to plan as-is
            raw_lis = []
        for le_plan, le in zip(plan["logical_enclosures"], le_targets):
            if le_plan["will_change"]:
                continue
            actual = await _actual_le_baseline_uri(client, le, raw_lis)
            if actual is not None and not same_baseline(actual, baseline_uri):
                le_plan["will_change"] = True
                le_plan["detail"] = "target baseline set but not yet installed"
        plan["changes"] = sum(
            1 for x in plan["logical_enclosures"] + plan["server_profiles"] if x["will_change"]
        )

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

        async def _apply_with_retry(
            kind: str, name: str, resource_uri: str, do_request: Callable[[bool], Any],
        ) -> tuple[str | None, dict[str, Any]]:
            """Run *do_request(force_flag)*, polling to completion.

            On a "Warning" task whose target baseline never actually moved,
            offers ``on_validation_blocked`` a chance to retry with the guard
            bypassed -- exactly once, since a second block means forcing
            didn't help. Returns ``(status, task)`` where *status* is
            ``None`` (success), ``"failed"``, or ``"blocked"``.
            """
            force_this = force
            already_offered = False
            while True:
                resp = await do_request(force_this)
                task = await _await_task(
                    client, resp, emit, sleeper, poll_interval_s, task_timeout_s
                )
                if is_task_failed(task):
                    return "failed", task
                if (task.get("state") or "").lower() == "warning":
                    current = await client.get(resource_uri)
                    if _resource_baseline_uri(current) != baseline_uri:
                        reason = await _task_block_reason(client, task["uri"])
                        if (
                            not force_this and not already_offered
                            and on_validation_blocked is not None
                            and on_validation_blocked({
                                "kind": kind, "name": name,
                                "reason": reason["warning"], "resolution": reason["resolution"],
                            })
                        ):
                            force_this = True
                            already_offered = True
                            emit("applying", {"kind": kind, "name": name, "retry": True})
                            continue
                        task["blocked_reason"] = reason["warning"]
                        task["blocked_resolution"] = reason["resolution"]
                        return "blocked", task
                return None, task

        # (a) shared infrastructure first
        for le in changing_les:
            emit("applying", {"kind": "logical-enclosure", "name": le["name"]})

            async def _le_request(force_flag: bool, le=le) -> dict[str, Any]:
                # --force also bypasses OneView's non-disruptive validation
                # guard -- otherwise a non-redundant fabric makes OneView
                # silently no-op the update rather than apply it.
                patch = build_le_firmware_patch(
                    baseline_uri, scope=scope, force=force_flag,
                    validate_nondisruptive=not force_flag,
                )
                return await client.patch(le["uri"], patch, headers={"If-Match": "*"})

            status, task = await _apply_with_retry(
                "logical-enclosure", le["name"], le["uri"], _le_request
            )
            results.append({"kind": "logical-enclosure", "name": le["name"], **task})
            emit("applied", results[-1])
            if status is not None:
                return {"status": status, "plan": plan, "results": results}

        # (b) compute (server profiles)
        for prof in changing_profs:
            emit("applying", {"kind": "server-profile", "name": prof["name"]})

            async def _profile_request(force_flag: bool, prof=prof) -> dict[str, Any]:
                full = await client.get(prof["uri"])
                body = build_profile_firmware_put(
                    full, baseline_uri, install_type=install_type, force=force_flag
                )
                return await client.put(prof["uri"], body)

            status, task = await _apply_with_retry(
                "server-profile", prof["name"], prof["uri"], _profile_request
            )
            results.append({"kind": "server-profile", "name": prof["name"], **task})
            emit("applied", results[-1])
            if status is not None:
                return {"status": status, "plan": plan, "results": results}

    return {"status": "applied", "plan": plan, "results": results}


async def fetch_apply_targets(client: "OneViewClient") -> dict[str, Any]:
    """Fetch + normalize everything the apply flow needs (baselines + targets)."""
    raw_drivers = await client.get_all(FW_DRIVERS_URI)
    les = await client.get_all(LE_URI)
    profiles = await client.get_all(PROFILES_URI)
    raw_hardware = await client.get_all(HARDWARE_URI)
    try:
        ver = await client.get("/rest/appliance/nodeinfo/version")
        appliance_version = (ver or {}).get("softwareVersion", "")
    except Exception:  # noqa: BLE001 — version is advisory; never fail the whole apply on it
        appliance_version = ""
    return {
        "baselines": service_pack_baselines(raw_drivers),
        "logical_enclosures": [normalize_le(le) for le in les],
        "server_profiles": [normalize_profile(p) for p in profiles],
        "hardware_enclosure_map": hardware_enclosure_map(raw_hardware),
        "appliance_version": appliance_version,
    }
