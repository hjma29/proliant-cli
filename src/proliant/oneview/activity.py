"""proliant.oneview.activity — the OneView GUI "Activity" feed.

The OneView GUI's *Activity* page is a single reverse-chronological feed that
merges two REST resources:

* ``/rest/tasks``  — named operations OneView performed ("Logical enclosure
  firmware update", "Update firmware", "Refresh", "Background inventory
  collection", ...). States: Completed / Warning / Error / Running /
  Terminated. Owner = who initiated it (Administrator / System).
* ``/rest/alerts`` — health/condition notifications ("A frame link module has
  restarted.", "Interconnect discovery is complete", ...). States: Active /
  Locked / Cleared, each with a severity (OK / Warning / Critical).

This module normalizes both into one common shape, merges them, and sorts by
creation time descending so ``proliant oneview activity`` reproduces what the
user sees on the GUI Activity page (and, crucially, surfaces the real error a
firmware task failed with instead of leaving them guessing).

Everything here is pure except :func:`fetch_activity`, which performs the two
GETs -- so the normalize/merge/format logic is unit-testable without a live
appliance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

TASKS_URI = "/rest/tasks"
ALERTS_URI = "/rest/alerts"


# ── time helpers ────────────────────────────────────────────────────────────

def parse_iso(ts: str | None) -> datetime | None:
    """Parse OneView's ISO-8601 timestamps (e.g. ``2026-07-11T06:26:39.479Z``).

    Returns ``None`` for empty/unparseable input so sorting/formatting can
    degrade gracefully rather than raising on odd data.
    """
    if not ts:
        return None
    raw = ts.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_duration(start: str | None, end: str | None) -> str:
    """Human "9m4s"-style duration between two ISO timestamps (GUI style).

    Empty string when either end is missing/unparseable or non-positive.
    """
    a, b = parse_iso(start), parse_iso(end)
    if a is None or b is None:
        return ""
    secs = int((b - a).total_seconds())
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{secs}s" if secs else f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins}m" if mins else f"{hours}h"


def format_local(ts: str | None) -> str:
    """ISO timestamp -> local "YYYY-MM-DD HH:MM:SS" for display."""
    dt = parse_iso(ts)
    if dt is None:
        return ts or ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


# ── normalization ───────────────────────────────────────────────────────────

def _resource_name(item: dict) -> str:
    ar = item.get("associatedResource") or {}
    return (ar.get("resourceName") or "").strip()


def normalize_task(t: dict) -> dict[str, Any]:
    """Normalize a ``/rest/tasks`` member into the common activity shape."""
    created = t.get("created") or ""
    modified = t.get("modified") or ""
    return {
        "kind": "task",
        "name": (t.get("name") or "").strip(),
        "resource": _resource_name(t),
        "created": created,
        "modified": modified,
        "state": (t.get("taskState") or "").strip(),
        "severity": "",
        "status": (t.get("taskStatus") or "").strip(),
        "percent": t.get("percentComplete"),
        "owner": (t.get("owner") or "").strip(),
        "duration": format_duration(created, modified),
        "uri": t.get("uri") or "",
    }


def normalize_alert(a: dict) -> dict[str, Any]:
    """Normalize a ``/rest/alerts`` member into the common activity shape."""
    created = a.get("created") or ""
    modified = a.get("modified") or ""
    return {
        "kind": "alert",
        "name": (a.get("description") or "").strip(),
        "resource": _resource_name(a),
        "created": created,
        "modified": modified,
        "state": (a.get("alertState") or "").strip(),
        "severity": (a.get("severity") or "").strip(),
        "status": (a.get("correctiveAction") or "").strip(),
        "percent": None,
        "owner": (a.get("assignedToUser") or "").strip(),
        "duration": "",
        "uri": a.get("uri") or "",
    }


# ── merge / filter ──────────────────────────────────────────────────────────

def merge_activity(
    tasks: list[dict],
    alerts: list[dict],
    *,
    limit: int | None = None,
    resource: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize + merge tasks and alerts, newest first.

    *resource* is a case-insensitive substring match on the associated
    resource name; *state* is a case-insensitive exact match on the row's
    state (task ``taskState`` or alert ``alertState``). *limit* caps the
    number of rows returned *after* filtering.
    """
    rows = [normalize_task(t) for t in tasks] + [normalize_alert(a) for a in alerts]

    if resource:
        needle = resource.lower()
        rows = [r for r in rows if needle in r["resource"].lower()]
    if state:
        want = state.lower()
        rows = [r for r in rows if r["state"].lower() == want]

    rows.sort(key=lambda r: parse_iso(r["created"]) or datetime.min.replace(tzinfo=timezone.utc),
              reverse=True)
    if limit is not None:
        rows = rows[:limit]
    return rows


# ── I/O ─────────────────────────────────────────────────────────────────────

async def fetch_activity(
    client: "Any",
    *,
    limit: int = 20,
    include_tasks: bool = True,
    include_alerts: bool = True,
    resource: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch the merged activity feed from a connected :class:`OneViewClient`.

    Pulls the newest ``limit`` of each resource server-side (sorted by
    creation descending) so a busy appliance doesn't page through thousands of
    historical rows, then merges/filters/caps client-side.
    """
    # Over-fetch a little per source so the client-side merge still has enough
    # rows to fill *limit* after interleaving + filtering.
    per_source = max(limit * 2, limit + 10)
    params = {"count": per_source, "sort": "created:descending"}

    tasks: list[dict] = []
    alerts: list[dict] = []
    if include_tasks:
        resp = await client.get(TASKS_URI, params=params)
        tasks = (resp or {}).get("members") or []
    if include_alerts:
        resp = await client.get(ALERTS_URI, params=params)
        alerts = (resp or {}).get("members") or []

    return merge_activity(tasks, alerts, limit=limit, resource=resource, state=state)
