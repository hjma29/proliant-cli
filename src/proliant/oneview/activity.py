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

import json
import re
from datetime import datetime, timezone
from typing import Any

TASKS_URI = "/rest/tasks"
ALERTS_URI = "/rest/alerts"

# States a task is in while still working (used to pick a live "watch" target).
_ACTIVE_TASK_STATES = {"running", "pending", "starting", "new", "initializing"}

# OneView embeds raw ``{"name":"X","uri":"..."}`` JSON blobs inline in some task
# and alert messages (the GUI renders each as a clickable link). Collapse each
# blob down to just its ``name`` so plain-text CLI output reads like the GUI.
_EMBEDDED_REF_RE = re.compile(r'\{[^{}]*"name"\s*:\s*"[^"]*"[^{}]*\}')


def clean_refs(text: str) -> str:
    """Replace inline ``{"name":"X","uri":...}`` JSON blobs with just ``X``."""
    if not text:
        return ""

    def _replace(m: "re.Match[str]") -> str:
        try:
            return json.loads(m.group(0)).get("name", m.group(0))
        except (json.JSONDecodeError, AttributeError):
            return m.group(0)

    return _EMBEDDED_REF_RE.sub(_replace, text).strip()


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


def format_elapsed(start: str | None, *, now: datetime | None = None) -> str:
    """Human "9m4s"-style elapsed time from *start* until now.

    Used for still-running tasks, whose ``modified`` timestamp stops advancing
    while their subtasks do the real work — so ``created``→``modified`` would
    understate how long the operation has actually been running. This matches
    the GUI's live running-duration counter.
    """
    a = parse_iso(start)
    if a is None:
        return ""
    ref = now or datetime.now(timezone.utc)
    secs = int((ref - a).total_seconds())
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{secs}s" if secs else f"{mins}m"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins}m" if mins else f"{hours}h"


# ── normalization ───────────────────────────────────────────────────────────

def _resource_name(item: dict) -> str:
    ar = item.get("associatedResource") or {}
    return (ar.get("resourceName") or "").strip()


def _latest_progress(t: dict) -> str:
    """The newest ``progressUpdates[].statusUpdate`` on a task.

    OneView's granular per-phase text (e.g. "Stage firmware 80% completed",
    "Initiating loading of images on the interconnect / reboot") lives here —
    it is what the GUI streams under each subtask, and it is richer than the
    task's own ``taskStatus`` for firmware rollouts.
    """
    for u in reversed(t.get("progressUpdates") or []):
        text = ((u or {}).get("statusUpdate") or "").strip()
        if text:
            return clean_refs(text)
    return ""


def normalize_task(t: dict) -> dict[str, Any]:
    """Normalize a ``/rest/tasks`` member into the common activity shape."""
    created = t.get("created") or ""
    modified = t.get("modified") or ""
    status = clean_refs((t.get("taskStatus") or "").strip())
    return {
        "kind": "task",
        "name": (t.get("name") or "").strip(),
        "resource": _resource_name(t),
        "created": created,
        "modified": modified,
        "state": (t.get("taskState") or "").strip(),
        "severity": "",
        "status": status,
        "progress": _latest_progress(t),
        "percent": t.get("percentComplete"),
        "owner": (t.get("owner") or "").strip(),
        "duration": format_duration(created, modified),
        "parent": (t.get("parentTaskUri") or "").strip(),
        "uri": t.get("uri") or "",
    }


def phase_text(row: dict) -> str:
    """Best human phase text for a task row: prefer the newest progress update
    (richest, GUI-style), fall back to ``taskStatus``."""
    return row.get("progress") or row.get("status") or ""


def normalize_alert(a: dict) -> dict[str, Any]:
    """Normalize a ``/rest/alerts`` member into the common activity shape."""
    created = a.get("created") or ""
    modified = a.get("modified") or ""
    return {
        "kind": "alert",
        "name": clean_refs((a.get("description") or "").strip()),
        "resource": _resource_name(a),
        "created": created,
        "modified": modified,
        "state": (a.get("alertState") or "").strip(),
        "severity": (a.get("severity") or "").strip(),
        "status": clean_refs((a.get("correctiveAction") or "").strip()),
        "progress": "",
        "percent": None,
        "owner": (a.get("assignedToUser") or "").strip(),
        "duration": "",
        "parent": "",
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
    toplevel_only: bool = True,
) -> list[dict[str, Any]]:
    """Normalize + merge tasks and alerts, newest first.

    *resource* is a case-insensitive substring match on the associated
    resource name; *state* is a case-insensitive exact match on the row's
    state (task ``taskState`` or alert ``alertState``). *limit* caps the
    number of rows returned *after* filtering.

    When *toplevel_only* (the default) subtasks — tasks that carry a
    ``parentTaskUri`` — are dropped, so the feed matches the OneView GUI
    Activity page, which lists only the top-level operation (e.g. a single
    "Logical enclosure firmware update") and hides its subtask tree behind an
    expander. Use ``activity --tree``/``--watch`` to see that subtask detail.
    """
    task_rows = [normalize_task(t) for t in tasks]
    if toplevel_only:
        task_rows = [r for r in task_rows if not r["parent"]]
    rows = task_rows + [normalize_alert(a) for a in alerts]

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
    toplevel_only: bool = True,
) -> list[dict[str, Any]]:
    """Fetch the merged activity feed from a connected :class:`OneViewClient`.

    Pulls the newest ``limit`` of each resource server-side (sorted by
    creation descending) so a busy appliance doesn't page through thousands of
    historical rows, then merges/filters/caps client-side.
    """
    # Over-fetch per source so the client-side merge still has enough rows to
    # fill *limit* after interleaving + filtering. When we drop subtasks
    # (toplevel_only) a firmware rollout alone can contribute a dozen subtask
    # rows we throw away, so pull a wider window.
    factor = 5 if toplevel_only else 2
    per_source = max(limit * factor, limit + 30)
    params = {"count": per_source, "sort": "created:descending"}

    tasks: list[dict] = []
    alerts: list[dict] = []
    if include_tasks:
        resp = await client.get(TASKS_URI, params=params)
        tasks = (resp or {}).get("members") or []
    if include_alerts:
        resp = await client.get(ALERTS_URI, params=params)
        alerts = (resp or {}).get("members") or []

    return merge_activity(
        tasks, alerts, limit=limit, resource=resource, state=state,
        toplevel_only=toplevel_only,
    )


# ── subtask tree (the GUI's expandable per-operation detail) ─────────────────

async def _direct_children(client: "Any", parent_uri: str, *, count: int = 60) -> list[dict]:
    """Direct child tasks of *parent_uri* via the ``parentTaskUri`` filter.

    Best-effort: returns ``[]`` on any error so tree-building never fails the
    whole command over one flaky sub-fetch.
    """
    if not parent_uri:
        return []
    try:
        data = await client.get(
            TASKS_URI,
            params={"filter": f"parentTaskUri='{parent_uri}'",
                    "count": count, "sort": "created:ascending"},
        )
    except Exception:  # noqa: BLE001 - best-effort enrichment
        return []
    return (data or {}).get("members") or []


async def fetch_task_tree(
    client: "Any", root_uri: str, *, max_depth: int = 6, max_children: int = 60,
) -> dict[str, Any] | None:
    """Fetch a task and its full subtask hierarchy.

    Returns a nested ``{"task": <normalized>, "children": [<node>, ...]}`` tree
    (or ``None`` if the root can't be read) that mirrors the OneView GUI's
    expandable Activity subtask view — e.g. "Logical enclosure firmware update"
    → "Update firmware (LE01)" → "Update firmware (LE01-LIG-VC100)" → per
    interconnect, each node carrying its own state/percent/phase text.
    """
    try:
        root_raw = await client.get(root_uri)
    except Exception:  # noqa: BLE001 - best-effort
        return None

    async def _build(raw: dict, depth: int) -> dict[str, Any]:
        node = {"task": normalize_task(raw), "children": []}
        if depth < max_depth:
            for child in await _direct_children(client, raw.get("uri") or "", count=max_children):
                node["children"].append(await _build(child, depth + 1))
        return node

    return await _build(root_raw, 0)


def flatten_tree(node: dict | None, depth: int = 0) -> list[tuple[int, dict[str, Any]]]:
    """Depth-first flatten of :func:`fetch_task_tree` into ``(depth, row)`` pairs,
    children ordered by creation time (matching the GUI's subtask order)."""
    if node is None:
        return []
    rows: list[tuple[int, dict[str, Any]]] = [(depth, node["task"])]
    for child in sorted(node.get("children", []),
                        key=lambda c: c["task"].get("created") or ""):
        rows.extend(flatten_tree(child, depth + 1))
    return rows


def tree_is_terminal(node: dict | None) -> bool:
    """True when the root task has reached a terminal state (so a ``--watch``
    loop can stop). Only the root matters — a finished subtask doesn't mean the
    whole rollout is done."""
    if node is None:
        return True
    state = (node["task"].get("state") or "").lower()
    return state in {"completed", "error", "warning", "terminated", "killed", "timeout"}


async def find_active_task(
    client: "Any", *, resource: str | None = None, name_contains: str | None = None,
    token: str | None = None, count: int = 500,
) -> dict[str, Any] | None:
    """Newest still-running top-level task, for ``activity --watch`` to follow.

    Filters to top-level tasks (no ``parentTaskUri``) that are in an active
    state, optionally narrowed by associated-resource substring and/or task
    name substring. ``token`` matches EITHER the task name or the resource
    (what the operator sees in the feed's Name / Resource columns).
    """
    for raw in await _recent_task_members(client, count):
        if (raw.get("parentTaskUri") or "").strip():
            continue
        if (raw.get("taskState") or "").lower() not in _ACTIVE_TASK_STATES:
            continue
        row = normalize_task(raw)
        if _task_row_matches(row, resource=resource, name_contains=name_contains, token=token):
            return row
    return None


async def find_task(
    client: "Any", *, resource: str | None = None, name_contains: str | None = None,
    token: str | None = None, count: int = 500,
) -> dict[str, Any] | None:
    """Newest top-level task matching the filters, running or not (for a
    one-shot ``activity --tree``). Falls back to the most recent top-level task
    when no filter is given. ``token`` matches EITHER the task name or the
    resource (what the operator sees in the feed's Name / Resource columns)."""
    for raw in await _recent_task_members(client, count):
        if (raw.get("parentTaskUri") or "").strip():
            continue
        row = normalize_task(raw)
        if _task_row_matches(row, resource=resource, name_contains=name_contains, token=token):
            return row
    return None


async def _recent_task_members(client: "Any", count: int) -> list[dict]:
    try:
        data = await client.get(
            TASKS_URI,
            params={"count": max(1, count), "sort": "created:descending"},
        )
    except Exception:  # noqa: BLE001 - best-effort
        return []
    return (data or {}).get("members") or []


def _task_row_matches(
    row: dict[str, Any], *,
    resource: str | None = None,
    name_contains: str | None = None,
    token: str | None = None,
) -> bool:
    if resource and resource.lower() not in row["resource"].lower():
        return False
    if name_contains and name_contains.lower() not in row["name"].lower():
        return False
    if token:
        tok = token.lower()
        if tok not in row["name"].lower() and tok not in row["resource"].lower():
            return False
    return True
