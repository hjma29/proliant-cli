"""
proliant.oneview.upgrade
~~~~~~~~~~~~~~~~~~~~~~~~~~
Read-only OneView appliance upgrade *readiness* assessment plus safe disk
cleanup of unused firmware baselines (Phase 1).

Design principle — NOTHING here touches the data plane or flashes hardware:

  * Everything in the ``readiness`` path is a plain GET (version, health,
    alerts, backups, logical-interconnect consistency, firmware baselines).
  * The only write operation is ``delete_baseline()`` — a ``DELETE`` of an
    *unused* firmware baseline from the appliance repository. That reclaims
    disk only; OneView refuses to delete a baseline that is assigned to a
    logical enclosure, logical interconnect, or server profile, so a running
    enclosure can never be affected. Cleanup is gated behind an explicit
    ``--yes`` in the CLI and defaults to a dry-run preview.

Key endpoints (all verified against a live Synergy Composer2, API v7000):
  GET  /rest/appliance/nodeinfo/version   → software version, model, platform
  GET  /rest/appliance/health-status      → MEMORY / CPU / DISK availability
  GET  /rest/alerts?filter=alertState='Active'
  GET  /rest/backups                      → last backup state + timestamp
  GET  /rest/firmware-drivers             → uploaded SPP / SSP baselines
  GET  /rest/repositories                 → repositoryType + total/available space (KiB)
  GET  /rest/logical-enclosures           → assigned firmware baseline
  GET  /rest/logical-interconnects        → consistency + assigned baseline
  GET  /rest/server-profiles              → assigned firmware baseline
  DELETE /rest/firmware-drivers/{id}      → remove an unused baseline

Note on external repositories: a firmware-drivers member whose ``locations``
map points at a ``FirmwareExternalRepo`` (Firmware Bundles > External
Repositories in the UI) is only a *reference* into that external SPP source —
its ``bundleSize`` does not represent bytes stored on the appliance, and
OneView unconditionally refuses ``DELETE`` for it (HTTP 400 "...exists only
in the external repository..."). ``classify_baselines()`` excludes these from
``prunable``/``reclaimable_gb`` and reports them separately as
``external_unused`` so cleanup never promises disk it cannot reclaim.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── Synergy Composer upgrade-path milestone chain ────────────────────────────
# Source: HPE Synergy Software Releases "Upgrade Paths" table
#   https://support.hpe.com/docs/display/public/synergy-sw-release/Upgrade_Table.html
# '*' releases in HPE's table are mandatory milestones that must be reached
# before continuing to a later release. Each entry is (major, minor, revision,
# label). Revision distinguishes patch milestones like 8.30.01 vs 8.60.02.
# NOTE: exact terminal patch levels evolve; always confirm the current hop
# against HPE's live table before executing an actual update.
_MILESTONES: list[tuple[int, int, int, str]] = [
    (6, 0, 0, "6.0"),
    (6, 3, 0, "6.3"),
    (7, 0, 0, "7.0"),
    (8, 0, 0, "8.0"),
    (8, 30, 1, "8.30.01"),
    (8, 60, 2, "8.60.02"),
    (9, 10, 1, "9.10.01"),
    (10, 0, 0, "10.0"),
    (11, 1, 0, "11.01"),
    (11, 3, 0, "11.3"),
]

_UPGRADE_TABLE_URL = (
    "https://support.hpe.com/docs/display/public/synergy-sw-release/Upgrade_Table.html"
)

# Readiness thresholds for appliance free disk space (GB). A OneView update
# image plus SPP/SSP staging needs meaningful headroom; be conservative.
_DISK_FAIL_GB = 10.0
_DISK_WARN_GB = 40.0

# Backup considered stale (and a fresh one recommended) beyond this age.
_BACKUP_STALE_DAYS = 7


# ── version parsing / upgrade path ───────────────────────────────────────────

def parse_version(software_version: str) -> tuple[int, int, int]:
    """Parse a OneView software version string into (major, minor, revision).

    Accepts forms like '9.20.00-0500184', '9.20.00', '9.20', '11.01'.
    Missing components default to 0. Non-numeric input yields (0, 0, 0).
    """
    if not software_version:
        return (0, 0, 0)
    head = re.split(r"[-+ ]", software_version.strip(), maxsplit=1)[0]
    parts = head.split(".")
    nums: list[int] = []
    for p in parts[:3]:
        m = re.match(r"\d+", p)
        nums.append(int(m.group()) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def compute_upgrade_path(software_version: str) -> dict[str, Any]:
    """Compute the supported Synergy Composer upgrade path from a version.

    Returns a dict with:
      current            normalized 'major.minor.revision' label
      recommended_next   next milestone label to hop to (or None if latest)
      path_to_latest     list of milestone labels from next hop to latest
      latest             latest milestone label known to this table
      hops_to_latest     number of hops to reach latest
      at_latest          True if already at/above the latest milestone
      note / source_url  guidance + citation
    """
    cur = parse_version(software_version)
    latest = _MILESTONES[-1]
    remaining = [m for m in _MILESTONES if (m[0], m[1], m[2]) > cur]

    path = [m[3] for m in remaining]
    at_latest = not remaining
    return {
        "current": f"{cur[0]}.{cur[1]:02d}.{cur[2]:02d}",
        "recommended_next": path[0] if path else None,
        "path_to_latest": path,
        "latest": latest[3],
        "hops_to_latest": len(path),
        "at_latest": at_latest,
        "note": (
            "Already at or beyond the latest milestone in this table."
            if at_latest else
            "HPE gates Synergy Composer upgrades through mandatory milestone "
            "releases; confirm the exact terminal patch level against HPE's live "
            "upgrade table before proceeding. Newer Synergy Software Releases "
            "(SSP firmware) typically require a newer OneView — upgrade OneView "
            "before reassigning a newer SSP baseline."
        ),
        "source_url": _UPGRADE_TABLE_URL,
    }


# ── size / health parsing ────────────────────────────────────────────────────

_SIZE_RE = re.compile(r"([\d.]+)\s*([KMGT]?B)?", re.IGNORECASE)
_UNIT_TO_GB = {
    "B": 1 / (1024 ** 3),
    "KB": 1 / (1024 ** 2),
    "MB": 1 / 1024,
    "GB": 1.0,
    "TB": 1024.0,
}


def parse_size_to_gb(text: str) -> float | None:
    """Parse a size string like '18.27 GB' or '4095 MB' into GB (float)."""
    if not text:
        return None
    m = _SIZE_RE.match(str(text).strip())
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    return round(value * _UNIT_TO_GB.get(unit, _UNIT_TO_GB["B"]), 2)


def parse_health(members: list[dict]) -> dict[str, dict[str, Any]]:
    """Normalize /rest/appliance/health-status members keyed by resource type."""
    out: dict[str, dict[str, Any]] = {}
    for m in members or []:
        rtype = str(m.get("resourceType", "")).upper()
        if not rtype:
            continue
        out[rtype] = {
            "severity": m.get("severity", ""),
            "available": m.get("available", ""),
            "capacity": m.get("capacity", ""),
            "message": m.get("statusMessage", ""),
            "available_gb": parse_size_to_gb(m.get("available", "")),
        }
    return out


def summarize_alerts(members: list[dict]) -> dict[str, int]:
    """Count active alerts by severity."""
    critical = warning = other = 0
    for a in members or []:
        sev = str(a.get("severity", "")).lower()
        if sev == "critical":
            critical += 1
        elif sev == "warning":
            warning += 1
        else:
            other += 1
    return {
        "critical": critical,
        "warning": warning,
        "other": other,
        "total": critical + warning + other,
    }


# ── firmware baselines / usage ───────────────────────────────────────────────

def normalize_baselines(members: list[dict]) -> list[dict[str, Any]]:
    """Normalize /rest/firmware-drivers members into compact baseline dicts."""
    out: list[dict[str, Any]] = []
    for m in members or []:
        size = m.get("bundleSize") or 0
        out.append({
            "uri": m.get("uri", ""),
            "name": m.get("baselineShortName") or m.get("name", ""),
            "version": m.get("version", ""),
            "bundle_type": m.get("bundleType", ""),
            "release_date": m.get("releaseDate", ""),
            "size_bytes": int(size) if isinstance(size, (int, float)) else 0,
            "state": m.get("state", ""),
            # dict of "/rest/repositories/{uuid}" -> repo display name; only
            # populated when the bundle is a reference into a Firmware Bundles
            # repository (internal or external) rather than a direct upload.
            "locations": m.get("locations") or {},
        })
    return out


def normalize_repositories(members: list[dict]) -> list[dict[str, Any]]:
    """Normalize /rest/repositories members into compact dicts.

    NOTE — unit gotcha: ``totalSpace``/``availableSpace`` on this endpoint
    are reported in **KiB**, not bytes (unlike ``bundleSize`` on
    ``/rest/firmware-drivers``, which *is* bytes — see ``normalize_baselines``).
    Confirmed against a live Synergy Composer2 GUI: Internal repo
    65,011,712 KiB / 1024**2 = exactly 62.00 GB, matching the GUI's "62.00 GB".
    """
    out: list[dict[str, Any]] = []
    for r in members or []:
        total_kb = r.get("totalSpace") or 0
        avail_kb = r.get("availableSpace") or 0
        out.append({
            "uri": r.get("uri", ""),
            "name": r.get("name", ""),
            "repository_type": r.get("repositoryType", ""),
            "total_gb": round(total_kb / (1024 ** 2), 2) if isinstance(total_kb, (int, float)) else 0.0,
            "available_gb": round(avail_kb / (1024 ** 2), 2) if isinstance(avail_kb, (int, float)) else 0.0,
            "state": r.get("state", ""),
            "url": r.get("repositoryUrl", ""),
        })
    return out


def _is_external_baseline(locations: dict[str, str], repo_types: dict[str, str] | None) -> bool:
    """True if a baseline is only hosted in an external repo (not deletable).

    HPE OneView always rejects ``DELETE`` for a firmware baseline whose
    ``locations`` map resolves to a repository with
    ``repositoryType: FirmwareExternalRepo`` (HTTP 400 "...exists only in the
    external repository..."), and that baseline's reported ``bundleSize`` does
    not represent reclaimable appliance disk at all.

    ``repo_types`` maps repository URI -> repositoryType (from
    ``/rest/repositories``). If it's ``None`` (couldn't be fetched), or a
    baseline's specific repository URI is missing from it, conservatively
    treat any populated ``locations`` as external — better to under-report
    reclaimable space than promise disk that can never actually be freed.
    """
    if not locations:
        return False
    if repo_types is None:
        return True
    for uri in locations:
        repo_type = repo_types.get(uri)
        if repo_type is None or "external" in repo_type.lower():
            return True
    return False


def _collect_referenced_uris(
    logical_enclosures: list[dict],
    logical_interconnects: list[dict],
    server_profiles: list[dict],
    baselines: list[dict],
) -> set[str]:
    """Build the set of firmware-baseline URIs that are in use / depended on.

    A baseline is "in use" if referenced by a logical enclosure, logical
    interconnect, or server profile firmware assignment — or if another
    baseline lists it as a parent bundle (custom SPP built on a base SPP).
    """
    refs: set[str] = set()

    def _add(uri: object) -> None:
        if isinstance(uri, str) and uri:
            # normalize trailing query/fragment just in case
            refs.add(uri.split("?")[0])

    for le in logical_enclosures or []:
        _add((le.get("firmware") or {}).get("firmwareBaselineUri"))
    for li in logical_interconnects or []:
        _add((li.get("firmware") or {}).get("firmwareBaselineUri"))
    for sp in server_profiles or []:
        _add((sp.get("firmware") or {}).get("firmwareBaselineUri"))
    # Custom/child baselines that depend on a parent bundle.
    for b in baselines or []:
        parent = b.get("parentBundle")
        if isinstance(parent, dict):
            _add(parent.get("uri"))
        elif isinstance(parent, str):
            _add(parent)
    return refs


def _sort_by_release_date(baselines: list[dict]) -> list[dict]:
    """Sort baselines oldest -> newest by release_date.

    Entries with a missing/unparseable release date sort last (their
    position relative to each other is otherwise stable) since there's no
    chronological info to place them by.
    """
    return sorted(
        baselines,
        key=lambda b: (_parse_iso(b.get("release_date", "")) is None,
                       _parse_iso(b.get("release_date", "")) or datetime.min.replace(tzinfo=timezone.utc)),
    )


def classify_baselines(
    baselines: list[dict],
    logical_enclosures: list[dict],
    logical_interconnects: list[dict],
    server_profiles: list[dict],
    raw_members: list[dict] | None = None,
    repo_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Split baselines into in-use, prunable (old & unused), and retained-newer.

    A baseline is safe to prune only if it is BOTH unused AND not newer than
    the newest currently-assigned baseline. Unused baselines that are *newer*
    than what's assigned are almost certainly intended upgrade targets, so they
    are surfaced separately as ``retained_newer`` and never offered for deletion.

    Unused baselines that only exist in an external Firmware Bundles
    repository (see ``_is_external_baseline``) are never deletable via the
    OneView API and their size doesn't reflect appliance disk usage — these
    are excluded from ``prunable``/``reclaimable_gb`` entirely and reported
    separately as ``external_unused``, regardless of their release date.

    ``raw_members`` (optional) is the untouched firmware-drivers payload used
    only to detect parentBundle dependencies. ``repo_types`` (optional) maps
    repository URI -> repositoryType from ``/rest/repositories``.
    Returns keys: in_use, prunable, retained_newer, external_unused,
    reclaimable_bytes, reclaimable_gb, cutoff_date.
    """
    refs = _collect_referenced_uris(
        logical_enclosures, logical_interconnects, server_profiles,
        raw_members if raw_members is not None else [],
    )

    in_use, unused = [], []
    for b in baselines:
        uri = (b.get("uri") or "").split("?")[0]
        entry = {**b, "in_use": uri in refs}
        (in_use if uri in refs else unused).append(entry)

    # Split out baselines that only live in an external repository — OneView
    # refuses to delete these and their bundleSize isn't reclaimable appliance
    # disk, so they must never enter the prunable/reclaimable_gb calculation.
    unused_internal, external_unused = [], []
    for b in unused:
        if _is_external_baseline(b.get("locations") or {}, repo_types):
            external_unused.append(b)
        else:
            unused_internal.append(b)

    # Newest release date among assigned baselines is the pruning cutoff.
    assigned_dates = [d for d in (_parse_iso(b.get("release_date", "")) for b in in_use) if d]
    cutoff = max(assigned_dates) if assigned_dates else None

    prunable, retained_newer = [], []
    for b in unused_internal:
        released = _parse_iso(b.get("release_date", ""))
        if cutoff is not None and released is not None and released > cutoff:
            retained_newer.append(b)
        else:
            prunable.append(b)

    # Safety net: if nothing is assigned (no cutoff), keep the single newest
    # unused baseline as a retained upgrade candidate rather than pruning all.
    if cutoff is None and prunable:
        newest = max(
            prunable,
            key=lambda b: _parse_iso(b.get("release_date", "")) or datetime.min.replace(tzinfo=timezone.utc),
        )
        if _parse_iso(newest.get("release_date", "")):
            prunable.remove(newest)
            retained_newer.append(newest)

    reclaimable = sum(b["size_bytes"] for b in prunable)
    in_use = _sort_by_release_date(in_use)
    prunable = _sort_by_release_date(prunable)
    retained_newer = _sort_by_release_date(retained_newer)
    external_unused = _sort_by_release_date(external_unused)
    return {
        "in_use": in_use,
        "prunable": prunable,
        "retained_newer": retained_newer,
        "external_unused": external_unused,
        "reclaimable_bytes": reclaimable,
        "reclaimable_gb": round(reclaimable / (1024 ** 3), 2),
        "cutoff_date": cutoff.isoformat() if cutoff else "",
    }


# ── readiness assessment (pure) ──────────────────────────────────────────────

def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        t = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def assess_readiness(data: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Build the readiness report (checks + overall verdict) from gathered data.

    ``data`` keys: version(dict), upgrade_path(dict), health(dict),
    alerts(dict summary), backup(dict), logical_interconnects(list of
    {name,status,consistency}), interconnect_count(int),
    baseline_summary(dict from classify_baselines).

    Each check is {name, status (PASS/WARN/FAIL/INFO), detail}.
    Verdict = worst of PASS/WARN/FAIL across non-INFO checks.
    """
    now = now or datetime.now(timezone.utc)
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    # Upgrade path (informational)
    up = data.get("upgrade_path", {})
    if up.get("at_latest"):
        add("Upgrade path", "INFO",
            f"At/above latest milestone ({up.get('latest', '?')}).")
    else:
        nxt = up.get("recommended_next")
        path = " -> ".join(up.get("path_to_latest", [])) or "?"
        add("Upgrade path", "INFO",
            f"Next hop: {nxt}. Full path to latest: {path} "
            f"({up.get('hops_to_latest', 0)} hop(s)).")

    # Disk
    disk = (data.get("health") or {}).get("DISK", {})
    free = disk.get("available_gb")
    if free is None:
        add("Appliance disk space", "WARN", "Could not read appliance free disk.")
    elif free < _DISK_FAIL_GB:
        add("Appliance disk space", "FAIL",
            f"Only {free:.1f} GB free — too low for an update. Free space first.")
    elif free < _DISK_WARN_GB:
        add("Appliance disk space", "WARN",
            f"{free:.1f} GB free — tight. Consider pruning unused baselines "
            f"('proliant oneview upgrade cleanup').")
    else:
        add("Appliance disk space", "PASS", f"{free:.1f} GB free.")

    # Memory / CPU
    for res in ("MEMORY", "CPU"):
        h = (data.get("health") or {}).get(res)
        if not h:
            continue
        sev = str(h.get("severity", "")).upper()
        status = "PASS" if sev in ("", "INFO", "OK") else ("WARN" if sev == "WARNING" else "FAIL")
        add(f"Appliance {res.lower()}", status,
            f"{h.get('message', '')} (avail {h.get('available', '?')} / {h.get('capacity', '?')}).".strip())

    # Active alerts
    al = data.get("alerts", {})
    crit, warn = al.get("critical", 0), al.get("warning", 0)
    if crit:
        add("Active alerts", "FAIL",
            f"{crit} critical, {warn} warning active alert(s) — resolve criticals before updating.")
    elif warn:
        add("Active alerts", "WARN", f"{warn} warning active alert(s) — review before updating.")
    else:
        add("Active alerts", "PASS", "No active critical/warning alerts.")

    # Backup freshness
    bk = data.get("backup", {})
    created = _parse_iso(bk.get("created", ""))
    state = str(bk.get("state", "")).lower()
    if not created:
        add("Appliance backup", "WARN", "No recent appliance backup found — take one before updating.")
    else:
        age_days = (now - created).total_seconds() / 86400
        if state and state not in ("succeeded", "normal", ""):
            add("Appliance backup", "WARN", f"Last backup state '{bk.get('state')}' — verify a good backup exists.")
        elif age_days > _BACKUP_STALE_DAYS:
            add("Appliance backup", "WARN",
                f"Last backup is {age_days:.0f} days old — take a fresh one before updating.")
        else:
            add("Appliance backup", "PASS", f"Recent backup ({age_days:.0f} day(s) old).")

    # Logical interconnect consistency
    lis = data.get("logical_interconnects", [])
    inconsistent = [li for li in lis if str(li.get("consistency", "")).upper() not in ("CONSISTENT", "")]
    li_warn = [li for li in lis if str(li.get("status", "")).lower() in ("warning", "critical")]
    if inconsistent:
        names = ", ".join(li.get("name", "?") for li in inconsistent)
        add("Logical interconnect consistency", "WARN",
            f"Not consistent: {names} — reconcile with the LIG before firmware changes.")
    elif li_warn:
        names = ", ".join(li.get("name", "?") for li in li_warn)
        add("Logical interconnect consistency", "WARN", f"Non-OK status: {names}.")
    elif lis:
        add("Logical interconnect consistency", "PASS", f"All {len(lis)} logical interconnect(s) consistent.")

    # Interconnect redundancy (enables non-disruptive orchestrated firmware flash)
    ic_count = data.get("interconnect_count", 0)
    if ic_count >= 2:
        add("Interconnect redundancy", "PASS",
            f"{ic_count} interconnects — redundant; orchestrated (non-disruptive) firmware activation possible.")
    elif ic_count == 1:
        add("Interconnect redundancy", "WARN",
            "Single interconnect — a firmware flash would be service-affecting (no redundancy).")

    # Stale baselines (informational, actionable via cleanup)
    bs = data.get("baseline_summary", {})
    prunable = bs.get("prunable", [])
    if prunable:
        add("Reclaimable firmware baselines", "INFO",
            f"{len(prunable)} old unused baseline(s) using {bs.get('reclaimable_gb', 0):.1f} GB — "
            f"reclaim with 'proliant oneview upgrade cleanup'.")

    # Unused baselines that only exist in an external repository — never
    # deletable via OneView, shown separately so the count above never
    # includes disk that can't actually be reclaimed this way.
    external_unused = bs.get("external_unused", [])
    if external_unused:
        repo_names = sorted({
            loc for b in external_unused for loc in (b.get("locations") or {}).values()
        })
        repo_note = ", ".join(repo_names) if repo_names else "an external repository"
        add("External-repository baselines", "INFO",
            f"{len(external_unused)} unused baseline(s) exist only in {repo_note} — "
            f"not deletable via OneView; remove them from the source repository "
            f"directly if no longer needed.")

    # Verdict = worst non-INFO status
    order = {"PASS": 0, "WARN": 1, "FAIL": 2}
    verdict = "PASS"
    for c in checks:
        if c["status"] in order and order[c["status"]] > order[verdict]:
            verdict = c["status"]
    return {"checks": checks, "verdict": verdict}


# ── async fetchers ───────────────────────────────────────────────────────────

async def fetch_appliance_version(client: "OneViewClient") -> dict[str, Any]:
    data = await client.get("/rest/appliance/nodeinfo/version")
    return {
        "software_version": data.get("softwareVersion", ""),
        "model": data.get("modelNumber", ""),
        "family": data.get("family", ""),
        "platform_type": data.get("platformType", ""),
        "serial": data.get("serialNumber", ""),
        "date": data.get("date", ""),
    }


async def fetch_health(client: "OneViewClient") -> dict[str, dict[str, Any]]:
    data = await client.get("/rest/appliance/health-status")
    return parse_health(data.get("members", []))


async def fetch_active_alerts(client: "OneViewClient") -> dict[str, int]:
    try:
        members = await client.get_all("/rest/alerts", filter="alertState='Active'")
    except Exception:  # noqa: BLE001 — alerts are advisory; never fail readiness on this
        members = []
    return summarize_alerts(members)


async def fetch_last_backup(client: "OneViewClient") -> dict[str, Any]:
    try:
        data = await client.get("/rest/backups")
    except Exception:  # noqa: BLE001
        return {}
    # /rest/backups is a single DTO on Synergy Composer.
    return {
        "state": data.get("backupState", data.get("status", "")),
        "created": data.get("created", ""),
    }


async def fetch_logical_interconnects(client: "OneViewClient") -> list[dict[str, Any]]:
    members = await client.get_all("/rest/logical-interconnects")
    return [
        {
            "name": m.get("name", ""),
            "status": m.get("status", ""),
            "consistency": m.get("consistencyStatus", ""),
            "firmware": m.get("firmware", {}),
        }
        for m in members
    ]


async def fetch_repositories(client: "OneViewClient") -> list[dict[str, Any]]:
    """Fetch and normalize /rest/repositories (best-effort, [] on failure)."""
    try:
        raw = await client.get_all("/rest/repositories")
    except Exception:  # noqa: BLE001 — optional; callers handle an empty list
        return []
    return normalize_repositories(raw)


async def fetch_repository_types(client: "OneViewClient") -> dict[str, str]:
    """URI -> repositoryType map from /rest/repositories (best-effort).

    Used to tell an internal (appliance-hosted) firmware repository apart
    from an external one (``FirmwareExternalRepo``) so
    ``classify_baselines()`` never offers external-only baselines for
    deletion. Returns ``{}`` (not ``None``) if the fetch fails — callers
    should still pass an empty dict through so ``_is_external_baseline``'s
    "unknown repo -> assume external" fallback applies per-URI rather than
    reverting to the coarser "any locations -> external" behavior.
    """
    repos = await fetch_repositories(client)
    return {r["uri"]: r["repository_type"] for r in repos if r["uri"]}


async def gather_readiness(client: "OneViewClient") -> dict[str, Any]:
    """Fetch everything and build the full readiness report (all read-only)."""
    version = await fetch_appliance_version(client)
    upgrade_path = compute_upgrade_path(version["software_version"])
    health = await fetch_health(client)
    alerts = await fetch_active_alerts(client)
    backup = await fetch_last_backup(client)
    logical_interconnects = await fetch_logical_interconnects(client)
    interconnects = await client.get_all("/rest/interconnects")

    raw_baselines = await client.get_all("/rest/firmware-drivers")
    logical_enclosures = await client.get_all("/rest/logical-enclosures")
    server_profiles = await client.get_all("/rest/server-profiles")
    repo_types = await fetch_repository_types(client)
    baseline_summary = classify_baselines(
        normalize_baselines(raw_baselines),
        logical_enclosures,
        logical_interconnects,
        server_profiles,
        raw_members=raw_baselines,
        repo_types=repo_types,
    )

    report = assess_readiness({
        "version": version,
        "upgrade_path": upgrade_path,
        "health": health,
        "alerts": alerts,
        "backup": backup,
        "logical_interconnects": logical_interconnects,
        "interconnect_count": len(interconnects),
        "baseline_summary": baseline_summary,
    })

    return {
        "appliance": version,
        "upgrade_path": upgrade_path,
        "health": health,
        "alerts": alerts,
        "backup": backup,
        "verdict": report["verdict"],
        "checks": report["checks"],
        "stale_baselines": {
            "count": len(baseline_summary["prunable"]),
            "reclaimable_gb": baseline_summary["reclaimable_gb"],
            "retained_newer": len(baseline_summary["retained_newer"]),
            "items": baseline_summary["prunable"],
            "external_unused": len(baseline_summary["external_unused"]),
        },
    }


async def gather_stale_baselines(client: "OneViewClient") -> dict[str, Any]:
    """Return in-use vs stale firmware baselines for the cleanup command."""
    raw_baselines = await client.get_all("/rest/firmware-drivers")
    logical_interconnects = await client.get_all("/rest/logical-interconnects")
    logical_enclosures = await client.get_all("/rest/logical-enclosures")
    server_profiles = await client.get_all("/rest/server-profiles")
    repo_types = await fetch_repository_types(client)
    return classify_baselines(
        normalize_baselines(raw_baselines),
        logical_enclosures,
        logical_interconnects,
        server_profiles,
        raw_members=raw_baselines,
        repo_types=repo_types,
    )


async def delete_baseline(client: "OneViewClient", uri: str) -> None:
    """DELETE a single firmware baseline (repository only, never flashes hardware)."""
    await client.delete(uri)
