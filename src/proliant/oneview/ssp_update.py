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


def find_profile_by_name(profiles: list[dict], name: str) -> dict[str, Any] | None:
    """Case-insensitive exact-name lookup among normalized server profiles."""
    q = (name or "").strip().lower()
    for p in profiles:
        if (p.get("name", "") or "").lower() == q:
            return p
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


def _progress_log(data: dict) -> list[str]:
    """Every non-blank ``progressUpdates`` entry, oldest first, refs cleaned.

    Mirrors ``activity.py``'s ``_progress_log`` -- this module keeps its own
    copy of ``normalize_task`` (it predates ``activity.py`` and drives the
    live single-line progress bar during ``--execute``, not the ``activity``
    feed), so the same "GUI shows the full scrolling log, we only kept the
    latest line" gap needed the same fix here independently.
    """
    updates = data.get("progressUpdates") or []
    out = []
    for u in updates:
        text = (u or {}).get("statusUpdate", "") or ""
        if text.strip():
            out.append(_clean_embedded_refs(text.strip()))
    return out


def normalize_task(data: dict | None) -> dict[str, Any]:
    """Compact an OneView task resource (or an async op's task body)."""
    d = data or {}
    log = _progress_log(d)
    stage = log[-1] if log else ""
    # OneView's plain `percentComplete` stays a flat 0 for the entire
    # duration of some task types -- confirmed live on a server-profile
    # firmware "Apply profile" task: `percentComplete` sat at 0 the whole
    # ~12 minutes it ran, while `computedPercentComplete` climbed (e.g. 18)
    # as each of its `totalSteps` actually completed. That's the same
    # step-weighted progress the GUI's own bar reads, so prefer it -- falling
    # back to the plain field for task shapes (or older OneView versions)
    # that only ever populate that one.
    percent = _percent(d.get("computedPercentComplete"))
    if percent is None:
        percent = _percent(d.get("percentComplete"))
    total_steps = d.get("totalSteps")
    completed_steps = d.get("completedSteps")
    has_steps = isinstance(total_steps, int) and total_steps > 0
    return {
        "uri": d.get("uri", "") or "",
        "name": d.get("name", "") or "",
        "state": d.get("taskState", "") or "",
        "status": d.get("taskStatus", "") or "",
        "percent": percent,
        "resource": (d.get("associatedResource") or {}).get("resourceName", "") or "",
        "stage": stage,
        "progress_log": log,
        "completed_steps": completed_steps if has_steps and isinstance(completed_steps, int) else None,
        "total_steps": total_steps if has_steps else None,
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


async def _child_tasks(client: "OneViewClient", parent_task_uri: str) -> list[dict]:
    """Direct children of *parent_task_uri* (``parentTaskUri`` filter). Never
    raises -- returns ``[]`` on any error, since every caller treats this as a
    best-effort enrichment, not something to fail the whole operation over."""
    try:
        data = await client.get(
            "/rest/tasks", params={"filter": f"parentTaskUri='{parent_task_uri}'", "count": 50},
        )
    except Exception:  # noqa: BLE001 - best-effort
        return []
    return data.get("members", []) or []


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
    shapes instead carry this on a *descendant* task, not the polled one: e.g.
    "Logical enclosure firmware update" -> "Update firmware" (per-LI) ->
    "Update firmware" (the actual ``VALIDATION_FAILED_FOR_LOGICAL_INTERCONNECT``
    at depth 2). The whole subtree is walked breadth-first (bounded depth) so
    that message surfaces instead of leaving the operator with a blank reason.
    Never raises: this is a diagnostic nicety layered on top of the
    baseline-mismatch check that actually decides pass/fail.
    """
    async def _errors_of(uri: str) -> list[dict]:
        try:
            return (await client.get(uri) or {}).get("taskErrors") or []
        except Exception:  # noqa: BLE001 - best-effort
            return []

    errs = await _errors_of(task_uri)
    if not errs:
        # Descend the task tree level by level, stopping at the first level
        # that yields any taskErrors. The trees OneView builds here are shallow
        # (3-4 levels); the depth cap and the ``seen`` set keep this bounded and
        # cycle-proof even if the API ever returns a task as its own ancestor.
        frontier = [task_uri]
        seen = {task_uri}
        depth = 0
        while frontier and not errs and depth < 4:
            depth += 1
            next_frontier: list[str] = []
            for parent_uri in frontier:
                for child in await _child_tasks(client, parent_uri):
                    errs.extend(child.get("taskErrors") or [])
                    child_uri = child.get("uri")
                    if child_uri and child_uri not in seen:
                        seen.add(child_uri)
                        next_frontier.append(child_uri)
            frontier = next_frontier

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


# ── non-redundant uplink diagnostic (the "why" behind a blocked update) ─────────

# OneView's non-redundant-fabric warning names the offending set(s) after an
# explicit "uplink set(s):" — e.g. "...for one or more uplink sets: pvlan-uplinkset".
_UPLINK_SET_NAMES_RE = re.compile(r"uplink[\s-]?sets?\s*:\s*([^.\n]+)", re.IGNORECASE)
# ...trim any trailing prose OneView appends after the name list.
_UPLINK_NAME_STOP_RE = re.compile(
    r"\s+(?:that|which|is|are|will|would|does|do|has|have|because|so|and)\b",
    re.IGNORECASE,
)


def _uplink_set_names_from_reason(reason: str) -> list[str]:
    """Pull the uplink-set name(s) OneView named in a non-redundant-fabric warning.

    Lets the diagnostic zoom straight in on exactly what OneView flagged rather
    than scanning every set. Best-effort: returns ``[]`` if the phrasing doesn't
    match, in which case the caller falls back to every set OneView itself marks
    unhealthy.
    """
    names: list[str] = []
    for m in _UPLINK_SET_NAMES_RE.finditer(reason or ""):
        tail = _UPLINK_NAME_STOP_RE.split(m.group(1), maxsplit=1)[0]
        for part in re.split(r"[,;]", tail):
            part = part.strip().strip(".")
            if part and part not in names:
                names.append(part)
    return names


def _uplink_redundancy_note(legs: list[dict[str, str]]) -> str:
    """One-line "why it's not redundant / what to fix" from an uplink set's legs."""
    if not legs:
        return (
            "OneView flags this uplink set as not redundant, but no live uplink "
            "ports were found for it — check that its uplink ports are cabled on "
            "both interconnects."
        )
    linked = [leg for leg in legs if (leg.get("state") or "").lower() == "linked"]
    linked_locs = {leg["location"] for leg in linked}
    down = [leg for leg in legs if (leg.get("state") or "").lower() != "linked"]
    if len(linked_locs) < 2:
        if down and linked_locs:
            d = down[0]
            return (
                f"the leg on {next(iter(linked_locs))} is up, but the leg on "
                f"{d['location']} ({d.get('port', '?')}) is {d.get('state') or 'down'} "
                "with no negotiated speed — with only one live leg the set has no "
                "redundant path, so an Orchestrated update can't keep the fabric up. "
                "Bring that uplink up (check the cable / switch port / port speed) to "
                "restore redundancy."
            )
        return (
            "all live uplink legs land on a single interconnect — there is no "
            "redundant leg on the paired interconnect."
        )
    speeds = {leg["speed"] for leg in linked if leg.get("speed") not in ("", "unknown")}
    if len(speeds) > 1:
        detail = ", ".join(
            f"{leg['location']} {leg.get('port', '')}={leg['speed']}G" for leg in linked
        )
        return (
            f"the redundant legs negotiated different speeds ({detail}); a speed "
            "mismatch between the two sides can drop redundancy. Set both uplink "
            "ports to the same speed."
        )
    return (
        "OneView reports this uplink set as degraded even though both legs appear "
        "linked — check the OneView Activity log / alerts for the specific reason "
        "before forcing the update."
    )


async def diagnose_nonredundant_uplinks(
    client: "OneViewClient", hint_names: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Explain *why* an uplink set isn't redundant, for the block/abort UX.

    OneView's own warning only *names* the offending uplink set(s); operators
    then have to dig through the interconnect port pages to see which physical
    leg is down or mis-negotiated. This does that dig for them, read-only:

      * pick the uplink set(s) OneView flagged (``hint_names`` parsed from the
        warning) or, failing that, every set OneView itself marks unhealthy
        (``status`` other than OK/Unknown);
      * for each, list its live uplink legs across the interconnect pair
        (location, port, link state, negotiated speed) from ``/rest/interconnects``;
      * summarise the redundancy problem with a concrete "what to fix" note.

    Never raises — a diagnostic layered on top of OneView's own verdict. Returns
    ``[]`` on any error or if nothing looks wrong.
    """
    from proliant.oneview.interconnects import parse_port_speed

    try:
        raw_sets = await client.get_all("/rest/uplink-sets")
        raw_ics = await client.get_all("/rest/interconnects")
    except Exception:  # noqa: BLE001 - best-effort diagnostic
        return []
    if not raw_sets:
        return []
    try:
        raw_lis = await client.get_all("/rest/logical-interconnects")
    except Exception:  # noqa: BLE001
        raw_lis = []
    li_names = {li.get("uri", ""): li.get("name", "") for li in raw_lis}

    hint_lower = {n.lower() for n in (hint_names or [])}
    if hint_lower:
        sets = [u for u in raw_sets if (u.get("name") or "").lower() in hint_lower]
    else:
        sets = [
            u for u in raw_sets if (u.get("status") or "OK") not in ("OK", "Unknown", "")
        ]
    if not sets:
        return []

    # interconnect uri -> "Enclosure-01 Bay 3", and its raw ports
    ic_label: dict[str, str] = {}
    ic_ports: dict[str, list[dict]] = {}
    for ic in raw_ics:
        entries = (ic.get("interconnectLocation") or {}).get("locationEntries", [])
        loc = {e.get("type"): e.get("value") for e in entries}
        encl = ic.get("enclosureName") or loc.get("Enclosure") or ""
        bay = loc.get("Bay", "")
        ic_uri = ic.get("uri", "")
        ic_label[ic_uri] = (
            f"{encl} Bay {bay}".strip() if bay else (encl or ic.get("name", ""))
        )
        ic_ports[ic_uri] = ic.get("ports") or []

    findings: list[dict[str, Any]] = []
    for u in sets:
        uri = u.get("uri", "")
        legs: list[dict[str, str]] = []
        for ic_uri, ports in ic_ports.items():
            for p in ports:
                if p.get("portType") != "Uplink":
                    continue
                if (p.get("associatedUplinkSetUri") or "") != uri:
                    continue
                legs.append({
                    "location": ic_label.get(ic_uri, ic_uri),
                    "port": p.get("portName", ""),
                    "state": p.get("portStatus", "") or "",
                    "speed": parse_port_speed(p.get("operationalSpeed")),
                })
        findings.append({
            "name": u.get("name", ""),
            "status": u.get("status", "") or "",
            "li_name": li_names.get(u.get("logicalInterconnectUri", ""), ""),
            "legs": legs,
            "note": _uplink_redundancy_note(legs),
        })
    return findings


def _normalize_block_decision(result: Any) -> str:
    """Map an ``on_validation_blocked`` return into a retry decision.

    Legacy callbacks returned a bool — ``True`` meaning "proceed through the
    warning" (clear the non-disruptive guard only). The A/B menu now returns an
    explicit string: ``"abort"`` (stop, blocked), ``"proceed"`` (clear the guard,
    matching the GUI's plain "OK to proceed"), or ``"force"`` (also
    force-reinstall — the disruptive path that gets a genuinely non-redundant
    fabric to update, as the GUI does when you accept the disruption).
    """
    if result is True:
        return "proceed"
    if isinstance(result, str):
        r = result.strip().lower()
        if r in ("abort", "proceed", "force"):
            return r
    return "abort"


async def _deepest_active_descendant(
    client: "OneViewClient", root: dict, *, max_depth: int = 5,
) -> dict[str, Any] | None:
    """Find the most granular currently-active step under *root*'s task.

    OneView's own multi-phase rollouts nest tasks -- e.g. the GUI's own task
    tree shows "Logical enclosure firmware update" -> "Update enclosure
    firmware" (per-enclosure) -> "Update frame link module firmware" -- and
    the real, customer-meaningful phase text and per-phase percent usually
    live on a *descendant* task while the root task's own
    ``percentComplete``/``progressUpdates`` stay flat at 0% for the whole
    multi-minute rollout. Descends the ``parentTaskUri`` chain picking
    whichever child is still ``Running`` at each level, so the CLI's single
    progress bar can show "Update frame link module firmware  30%" instead of
    sitting at a motionless "0%" the entire time.

    Only ever descends into a level's *Running* child, never a Completed one
    -- confirmed live: once the truly active step (e.g. "Apply profile",
    still Running, "Stage component 4/5...") has only already-finished
    children beneath it (e.g. "Power on", "Generate install set", both long
    Completed at 100%), picking "whichever's most recently touched" as a
    fallback previously grabbed that stale, finished grandchild and overlaid
    its 100% onto the whole rollout's progress bar -- showing a contradictory
    "100%  Running" for several more minutes while real work continued
    underneath. Stopping at the deepest level with a genuinely active child
    keeps the display honest (0% while staging, not a false 100%).

    Returns ``None`` if there's no descendant with anything more informative
    than the root already has (so callers can just keep showing the root's
    own state). Never raises -- display-only enrichment, not load-bearing for
    success/failure detection.
    """
    node_uri = root.get("uri", "")
    best: dict[str, Any] | None = None
    depth = 0
    while node_uri and depth < max_depth:
        children = await _child_tasks(client, node_uri)
        running = [c for c in children if (c.get("taskState") or "").lower() == "running"]
        if not running:
            # Nothing actually in flight at this level -- any leftover child
            # is a finished, stale snapshot (see docstring). Stop here and
            # keep the deepest *active* node found so far (possibly None).
            break
        chosen = normalize_task(
            max(running, key=lambda c: c.get("modified") or c.get("created") or "")
        )
        if not chosen["stage"] and chosen["percent"] is None:
            break  # this level has nothing more informative to show
        best = chosen
        node_uri = chosen["uri"]
        depth += 1
    return best


async def _enrich_with_active_descendant(client: "OneViewClient", task: dict) -> dict:
    """Overlay the deepest active descendant's stage/resource/percent/step
    count onto *task* for display, e.g. so the bar shows "Update frame link
    module firmware  30%" sourced from a child task instead of the root
    task's own flat "0%". Leaves *task*'s own ``state``/``uri`` untouched --
    those still drive done/failed detection off the authoritative root task."""
    try:
        deepest = await _deepest_active_descendant(client, task)
    except Exception:  # noqa: BLE001 - best-effort, display-only
        deepest = None
    if deepest is None:
        return task
    merged = dict(task)
    if deepest.get("stage"):
        merged["stage"] = deepest["stage"]
    if deepest.get("resource"):
        merged["resource"] = deepest["resource"]
    if deepest.get("percent") is not None:
        merged["percent"] = deepest["percent"]
    if deepest.get("total_steps") is not None:
        merged["completed_steps"] = deepest.get("completed_steps")
        merged["total_steps"] = deepest.get("total_steps")
    # The descendant actually doing the work (e.g. "Apply profile") is the
    # one that accumulates a rich progressUpdates log -- e.g. every "Stage
    # component N/6"/"Install component N/6" line, matching the GUI's own
    # expanded Activity view. Propagate its full log (not just "stage",
    # the latest line) so the live bar can print the same scrolling detail.
    if deepest.get("progress_log"):
        merged["progress_log"] = deepest["progress_log"]
    return merged


async def poll_task(
    client: "OneViewClient",
    task_uri: str,
    *,
    emit: Callable[[str, dict], None],
    sleeper: Callable[[float], Any],
    interval_s: float,
    timeout_s: float,
    reconnect_grace_s: float = 300.0,
) -> dict[str, Any]:
    """Poll a task to a terminal state, emitting ``task-progress`` each tick.

    Each emitted tick is enriched with the deepest active descendant task's
    own stage/percent (see ``_enrich_with_active_descendant``) so the display
    reflects OneView's real current phase (e.g. "Update frame link module
    firmware  30%") instead of the root task's own flat 0% for the whole
    rollout -- but done/failed detection below always uses the un-enriched
    root task, since a child task finishing early doesn't mean the rollout as
    a whole is done.

    Tolerates transient loss of contact with the appliance. A firmware rollout
    is exactly when brief management-network blips are most likely (interconnect
    modules and frame link modules reboot as they flash), and OneView keeps
    running the task server-side regardless of whether the CLI can reach it at
    that instant -- so a single failed poll must not abort the whole monitored
    update. On a connection error the poll is retried (surfacing a
    "Reconnecting…" tick) until contact is regained; only sustained failure for
    longer than ``reconnect_grace_s`` (or the overall ``timeout_s``) gives up
    and re-raises.
    """
    from proliant.oneview.client import OneViewError

    waited = 0.0
    unreachable_for = 0.0
    while True:
        try:
            raw = await client.get(task_uri)
        except OneViewError as exc:
            # Transient blip -- retry rather than aborting the rollout the
            # appliance is still running. Give up only after sustained loss.
            unreachable_for += interval_s
            if unreachable_for > reconnect_grace_s or waited >= timeout_s:
                raise
            emit("task-progress", {
                "state": "Reconnecting",
                "stage": "Lost contact with the appliance, retrying…",
                "percent": None,
                "resource": "",
            })
            await sleeper(interval_s)
            waited += interval_s
            continue
        unreachable_for = 0.0
        task = normalize_task(raw)
        display = task if is_task_done(task) else await _enrich_with_active_descendant(client, task)
        emit("task-progress", display)
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
    interconnect_activation_mode: str = "Orchestrated",
    execute: bool = False,
    confirm: Callable[[dict], bool] | None = None,
    on_validation_blocked: Callable[[dict], Any] | None = None,
    on_event: Callable[[str, dict], None] | None = None,
    sleeper: Callable[[float], Any] | None = None,
    poll_interval_s: float = 20.0,
    task_timeout_s: float = 90 * 60,
    verify_timeout_s: float = 5 * 60,
    profile_concurrency: int = 1,
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
      ``failed``         a task reported failure. For shared infrastructure
                          this stops further logical enclosures (they share
                          fabric); for server profiles every targeted
                          profile is still attempted regardless (see
                          ``profile_concurrency`` below) -- ``results``
                          carries one entry per profile either way, so check
                          each entry for the full picture rather than
                          assuming the batch stopped here.
      ``blocked``        OneView validated the update and refused to apply it
                          (e.g. a non-redundant-fabric guard), and the operator
                          declined to proceed anyway -- the relevant ``results``
                          entry carries ``blocked_reason``/``blocked_resolution``
                          and a ``blocked_uplinks`` diagnostic naming exactly
                          which uplink set / leg lost redundancy.
      ``unverified``     OneView reported the task as plain "Completed", but
                          re-checking what's actually installed didn't confirm
                          it landed -- the relevant ``results`` entry carries
                          ``unverified_reason``. Unlike ``blocked``, this is
                          not auto-retried with force (no known reason force
                          would help); treat it as "verify manually" rather
                          than "definitely failed".

    Mirrors the OneView GUI's own "Review the warnings. If these conditions
    are acceptable, click OK to proceed." flow: each target is first
    attempted with OneView's non-disruptive validation guard on (unless
    ``force`` is already set). If OneView blocks it -- reported as a
    ``Warning`` task whose target baseline never actually moved -- the real
    reason is surfaced via ``on_validation_blocked({"kind", "name", "reason",
    "resolution", "uplinks"})``, where ``reason``/``resolution`` are the same
    warning text + "Resolution:" steps the GUI's own modal shows and
    ``uplinks`` is a diagnostic naming exactly which uplink set / leg lost
    redundancy. The callback returns the operator's on-the-spot decision:

      ``"proceed"`` (or legacy ``True``)  retry the same target with only the
                    non-disruptive guard cleared -- the GUI's plain "OK to
                    proceed" (``forceInstallFirmware`` untouched);
      ``"force"``   retry with the guard cleared *and* ``forceInstallFirmware``
                    on -- the disruptive path that gets a genuinely
                    non-redundant fabric to update (a brief outage on those
                    uplinks / any server profiles riding them);
      ``"abort"``   (or ``False``/no callback)  stop with ``status ==
                    "blocked"`` and let the operator fix the fabric first.

    Either retry is offered exactly once -- a second block means it didn't help.

    ``interconnect_activation_mode`` mirrors the OneView SDK's own
    ``-InterconnectActivationMode``: ``"Orchestrated"`` (default) flashes one
    side of each redundant Logical Interconnect pair at a time so the fabric
    stays up. If an uplink set isn't redundant, OneView raises its
    non-disruptive validation *warning*; proceeding through it clears the guard,
    and forcing through it (disruptive) additionally re-installs -- exactly the
    A/B choice the GUI offers. ``"Parallel"`` flashes every interconnect at once
    regardless of redundancy, at the cost of a real network outage during the
    update, and OneView requires the affected compute modules to be powered off
    first.

    ``profile_concurrency`` controls how many server-profile firmware PUTs run
    at once (default 1 -- fully sequential). Researched against HPE's own
    tools before adding this: there is no OneView bulk/batch firmware API for
    server profiles at all -- the PowerShell library, Python SDK, and Ansible
    collection all serialize one ``PUT /rest/server-profiles/{id}`` at a time,
    and even OneView's own ``SharedInfrastructureAndServerProfiles`` LE-cascade
    documents placing each hypervisor into maintenance mode *serially*.
    Firmware only actually lands on hardware one step at a time regardless
    (iSUT/RBSU processes one server), so concurrency here doesn't make the
    real work faster -- it only overlaps the wall-clock wait across multiple
    *independent* servers. OneView publishes no concurrent-task ceiling, so
    keep this modest for large fleets rather than firing everything at once.
    Profiles are processed in waves of up to ``profile_concurrency``: each
    wave's requests are submitted together and all polled to completion
    before the next wave starts (so a failure surfaces after its wave
    finishes, not mid-wave -- the remaining *already-launched* servers in
    that wave are not abandoned partway through a reboot cycle).

    Unlike the shared-infrastructure loop above (which stops at the first
    logical enclosure that fails/blocks, since they share fabric), a
    failed/blocked/unverified *profile* does NOT stop the remaining waves --
    confirmed live: one server's drive firmware hanging and failing has no
    bearing on whether an unrelated server profile in the same batch can
    still update safely, so every targeted profile is always attempted and
    ``results`` carries an entry for each one. The returned ``status`` still
    reflects whichever issue was seen *first* (so a straightforward `applied`
    vs "something needs attention" check at the call site keeps working),
    but check every entry in ``results`` for the full per-profile picture --
    a non-``applied`` status here does not mean the whole batch stopped.
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

        # build_plan()'s own dicts don't carry enclosure_uris (only name/uri/
        # current_baseline_uri/etc) -- look the original target back up by
        # uri so the actual-installed cross-check below has what it needs.
        le_targets_by_uri = {le["uri"]: le for le in le_targets}

        if confirm is not None and not confirm(plan):
            return {"status": "aborted", "plan": plan}

        results: list[dict[str, Any]] = []

        async def _apply_with_retry(
            kind: str, name: str, resource_uri: str, do_request: Callable[[bool], Any],
            actual_checker: Callable[[], Any] | None = None,
            emit: Callable[[str, dict], Any] = emit,
        ) -> tuple[str | None, dict[str, Any]]:
            """Run *do_request(force_flag)*, polling to completion.

            On a "Warning" task, verify the target baseline actually moved
            before trusting it as a real success. *actual_checker*, when
            given, re-checks what's really installed (e.g. via each Logical
            Interconnect's own firmware sub-resource for a logical enclosure)
            rather than the resource's own target-baseline pointer, since
            OneView sets that pointer as soon as an update is *requested* and
            never rolls it back if the update is blocked or fails -- so for
            a logical enclosure the naive pointer comparison always reports
            "changed" right after the request, masking a real block. Falls
            back to the plain pointer comparison if no checker is given or it
            can't determine an answer (e.g. server profiles, whose firmware
            pointer does reflect the real state).

            A plain "Completed" task (when *actual_checker* is given) also
            gets cross-checked -- verified live, a ``--activation-mode
            parallel`` apply reported "Completed" after only ~7s while the
            real interconnect stage+activate cycle underneath continued
            running for several more minutes (confirmed by re-issuing the
            same request by hand: it converged to a real, verified firmware
            change after ~4 minutes). So a single-instant disagreement isn't
            trusted -- ``actual_checker`` is repolled every *poll_interval_s*
            for up to *verify_timeout_s* to give OneView's asynchronous
            LI-firmware activation time to finish, and only reported as an
            honest ``"unverified"`` result if it still disagrees once that
            window elapses.

            Offers ``on_validation_blocked`` a chance to retry with the guard
            bypassed -- exactly once, since a second block means forcing
            didn't help. Returns ``(status, task)`` where *status* is
            ``None`` (success), ``"failed"``, ``"blocked"``, or
            ``"unverified"``.
            """
            force_this = force
            # "Proceed through the non-disruptive validation warning" is a
            # SEPARATE decision from "force-reinstall the firmware": the OneView
            # GUI's own "Review the warnings... click OK to proceed" only clears
            # validateIfLIFirmwareUpdateIsNonDisruptive -- it does NOT set
            # forceInstallFirmware. --force turns on both guards up front; the
            # interactive proceed below turns on only the validation bypass and
            # leaves forceInstallFirmware exactly as the operator chose.
            bypass_validation = force
            already_offered = False
            while True:
                resp = await do_request(force_this, bypass_validation)
                task = await _await_task(
                    client, resp, emit, sleeper, poll_interval_s, task_timeout_s
                )
                if is_task_failed(task):
                    # Surface OneView's own error reason (e.g.
                    # SERVER_NOT_POWERED_OFF_FOR_LE_FIRMWARE_UPDATE) instead of
                    # leaving the CLI to say a useless "check the UI" -- the
                    # actionable message + recommended action live in the
                    # task's (or a child task's) taskErrors, the same source
                    # the Warning/blocked path already reads.
                    reason = await _task_block_reason(client, task["uri"])
                    if reason["warning"]:
                        task["failed_reason"] = reason["warning"]
                    if reason["resolution"]:
                        task["failed_resolution"] = reason["resolution"]
                    return "failed", task
                if (task.get("state") or "").lower() == "warning":
                    # A "Warning" task at 100% does NOT mean the firmware was
                    # actually applied -- OneView uses this same state/percent
                    # combo for a genuine "succeeded with a minor note" *and*
                    # for "refused to apply due to a validation guard". Make
                    # that visible instead of leaving the bar frozen looking
                    # like a clean, confident finish while we go check which
                    # one this was.
                    emit("task-progress", {
                        **task, "stage": "Checking whether the update actually applied…",
                    })
                    changed = None
                    if actual_checker is not None:
                        actual = await actual_checker()
                        if actual is not None:
                            changed = same_baseline(actual, baseline_uri)
                    if changed is None:
                        current = await client.get(resource_uri)
                        changed = _resource_baseline_uri(current) == baseline_uri
                    if not changed:
                        reason = await _task_block_reason(client, task["uri"])
                        # Dig out *why* the fabric isn't redundant (which uplink
                        # set, which leg is down / mis-negotiated) so the operator
                        # can decide on the spot: fix it, or force through.
                        uplinks = await diagnose_nonredundant_uplinks(
                            client, _uplink_set_names_from_reason(reason["warning"])
                        )
                        decision = "abort"
                        if (
                            not bypass_validation and not already_offered
                            and on_validation_blocked is not None
                        ):
                            decision = _normalize_block_decision(on_validation_blocked({
                                "kind": kind, "name": name,
                                "reason": reason["warning"],
                                "resolution": reason["resolution"],
                                "uplinks": uplinks,
                            }))
                        if decision in ("proceed", "force"):
                            # "proceed" clears only the non-disruptive validation
                            # guard (the GUI's plain "OK to proceed"); "force" also
                            # force-reinstalls -- the disruptive path that actually
                            # gets a non-redundant fabric to update, as the GUI
                            # does when you accept the disruption. Offered exactly
                            # once: a second block means it didn't help.
                            bypass_validation = True
                            if decision == "force":
                                force_this = True
                            already_offered = True
                            emit("applying", {"kind": kind, "name": name, "retry": True})
                            continue
                        # Surface OneView's own reason + recommended actions
                        # verbatim (the same text the GUI's warning modal shows),
                        # plus the concrete non-redundant-uplink diagnostic so the
                        # operator sees exactly which leg to fix.
                        task["blocked_reason"] = reason["warning"]
                        task["blocked_resolution"] = reason["resolution"]
                        task["blocked_uplinks"] = uplinks
                        # ``bypass_validation`` is True here only if force/proceed
                        # was already in effect (either --force up front or the
                        # operator picked B/proceed and we retried once). If it's
                        # still blocked with the guard bypassed, forcing did NOT
                        # help -- an Orchestrated update simply can't flash a
                        # single-legged fabric, so telling the operator to "force
                        # it" again is a dead end. The CLI uses this to steer them
                        # to the real fix (restore redundancy) or Parallel mode.
                        task["blocked_forced"] = bool(bypass_validation)
                        return "blocked", task
                elif actual_checker is not None:
                    # Don't trust a plain "Completed" report unconditionally
                    # either -- OneView's LE-level task returns as soon as it
                    # hands the update off to the LI, well before the real
                    # interconnect stage+activate cycle underneath finishes
                    # (confirmed live: "Completed" after ~7s, but the actual
                    # firmware change didn't land for ~4 more minutes). Keep
                    # repolling the real installed state for up to
                    # verify_timeout_s (unlike the "Warning" branch above,
                    # this isn't a validation guard to force through -- it's
                    # just OneView still finishing the work asynchronously).
                    emit("task-progress", {
                        **task, "stage": "Verifying the update actually applied…",
                    })
                    actual = await actual_checker()
                    changed = same_baseline(actual, baseline_uri) if actual is not None else None
                    waited = 0.0
                    while changed is False and waited < verify_timeout_s:
                        await sleeper(poll_interval_s)
                        waited += poll_interval_s
                        emit("task-progress", {
                            **task,
                            "stage": "Verifying the update actually applied "
                                     "(OneView is still activating)…",
                        })
                        actual = await actual_checker()
                        changed = same_baseline(actual, baseline_uri) if actual is not None else None
                    if changed is False:
                        task["unverified_reason"] = (
                            "OneView reported this update as completed, but the actual "
                            "installed baseline did not reflect it when re-checked "
                            "immediately afterward."
                        )
                        return "unverified", task
                return None, task

        # (a) shared infrastructure first
        for le in changing_les:
            emit("applying", {"kind": "logical-enclosure", "name": le["name"]})

            async def _le_request(force_flag: bool, bypass_validation: bool, le=le) -> dict[str, Any]:
                # forceInstallFirmware (re-flash even if already at the target)
                # and validateIfLIFirmwareUpdateIsNonDisruptive (OneView's
                # non-redundant-fabric guard) are INDEPENDENT knobs -- passed
                # separately so "proceed through the validation warning" matches
                # the GUI exactly: guard off, force untouched.
                patch = build_le_firmware_patch(
                    baseline_uri, scope=scope, force=force_flag,
                    validate_nondisruptive=not bypass_validation,
                    update_mode=interconnect_activation_mode,
                )
                return await client.patch(le["uri"], patch, headers={"If-Match": "*"})

            async def _le_actual_checker(le=le) -> str | None:
                target = le_targets_by_uri.get(le["uri"], le)
                return await _actual_le_baseline_uri(client, target, raw_lis)

            status, task = await _apply_with_retry(
                "logical-enclosure", le["name"], le["uri"], _le_request,
                actual_checker=_le_actual_checker,
            )
            # kind/name always identify the *requested target*, not whatever
            # (possibly empty, in tests) "name" the task body itself carries --
            # merge task fields first so kind/name can't be silently
            # overwritten by them. "outcome" records this target's own
            # per-entry result ("applied"/"failed"/"blocked"/"unverified") so
            # callers can tell exactly which entries had trouble without
            # guessing from the overall (batch-wide) status or assuming the
            # last entry in ``results`` is the relevant one.
            results.append({
                **task, "kind": "logical-enclosure", "name": le["name"],
                "outcome": status or "applied",
            })
            emit("applied", results[-1])
            if status is not None:
                return {"status": status, "plan": plan, "results": results}

        # (b) compute (server profiles) -- processed in waves of up to
        # profile_concurrency at once (default 1, see docstring above for why
        # sequential is the researched-and-matched-to-HPE default).
        async def _run_one_profile(prof: dict) -> tuple[str | None, dict[str, Any]]:
            # Tag every event this profile's processing emits with its own
            # name via "target", so a caller running multiple profiles
            # concurrently can route "task-progress" ticks back to the right
            # one (e.g. a multi-row progress display). The underlying OneView
            # task body has no such correlation -- its own "name"/"resource"
            # fields describe the task, not which profile requested it.
            def _tagged_emit(evt_kind: str, payload: dict) -> None:
                emit(evt_kind, {**payload, "target": prof["name"]})

            _tagged_emit("applying", {"kind": "server-profile", "name": prof["name"]})

            async def _profile_request(force_flag: bool, bypass_validation: bool, prof=prof) -> dict[str, Any]:
                # Server-profile firmware has no non-disruptive-validation guard
                # to bypass (that's a Logical-Interconnect concept); accept
                # bypass_validation for a uniform do_request signature, unused.
                full = await client.get(prof["uri"])
                body = build_profile_firmware_put(
                    full, baseline_uri, install_type=install_type, force=force_flag
                )
                return await client.put(prof["uri"], body)

            status, task = await _apply_with_retry(
                "server-profile", prof["name"], prof["uri"], _profile_request,
                emit=_tagged_emit,
            )
            # kind/name always identify the *requested target*; merge task
            # fields first so they can't be silently overwritten by whatever
            # "name" the task body itself carries (see the LE loop above for
            # the same fix). "outcome" records this profile's own per-entry
            # result -- important now that a failed/blocked/unverified profile
            # no longer stops later profiles from being attempted (see the
            # wave loop below), so ``results`` can hold a mix of outcomes and
            # callers must inspect each entry rather than assume the last one
            # (or the batch-wide ``status``) describes every entry.
            entry = {**task, "kind": "server-profile", "name": prof["name"], "outcome": status or "applied"}
            _tagged_emit("applied", entry)
            return status, entry

        wave_size = max(1, int(profile_concurrency))
        overall_status: str | None = None
        i = 0
        while i < len(changing_profs):
            wave = changing_profs[i:i + wave_size]
            # asyncio.gather preserves result order matching *wave*'s order
            # regardless of which profile's request actually finishes first,
            # so `results` stays deterministic (profile order) even when
            # multiple servers are updating concurrently.
            wave_results = await asyncio.gather(*(_run_one_profile(p) for p in wave))
            for status, entry in wave_results:
                results.append(entry)
                if status is not None and overall_status is None:
                    overall_status = status
            # A failure/block/unverified profile does NOT stop the remaining
            # waves -- each profile targets an independent physical server,
            # so one server's firmware trouble has no bearing on whether an
            # unrelated one can still update safely (confirmed live: a
            # server-profile update batch stopped dead after the first
            # profile's drive firmware failed, silently never attempting the
            # other 5 unrelated servers in the same run). Every targeted
            # profile is always attempted; the loop only remembers the first
            # non-``applied`` status it saw, to return once all waves finish.
            i += wave_size

    if overall_status is not None:
        return {"status": overall_status, "plan": plan, "results": results}
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
