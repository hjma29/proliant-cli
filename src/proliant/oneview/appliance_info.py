"""
proliant.oneview.appliance_info
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Read-only "describe" model for an HPE Synergy Composer / OneView appliance —
the data behind the appliance's *Settings -> Appliance -> General* page.

It stitches together a few small read-only REST calls into one normalized dict
so the CLI can render a single, coherent appliance summary:

  GET /rest/appliance/ha-nodes            -> the active/standby Composer node pair
  GET /rest/appliance/nodeinfo/status     -> memory, per-role start time + uptime
  GET /rest/appliance/nodeinfo/version    -> firmware version + build date
  GET /rest/tasks?filter=name='Update…'   -> the most recent "Update appliance" task

The formatting helpers are pure functions (no I/O) so they're unit-tested
directly; ``fetch_appliance_info`` is the only coroutine and is exercised with a
fake client like the rest of the oneview suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── REST endpoints ────────────────────────────────────────────────────────────

HA_NODES_URI = "/rest/appliance/ha-nodes"
NODE_STATUS_URI = "/rest/appliance/nodeinfo/status"
NODE_VERSION_URI = "/rest/appliance/nodeinfo/version"
TASKS_URI = "/rest/tasks"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ── time / value formatting (pure) ────────────────────────────────────────────

def _parse_iso(raw: str) -> datetime | None:
    """Parse an ISO-8601 timestamp (tolerating a trailing ``Z``) as aware UTC."""
    if not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def fmt_timestamp(dt: datetime | None) -> str:
    """Format an aware datetime like OneView does: ``7/9/26 10:23:33 pm (UTC -0700)``.

    The datetime is formatted in *its own* timezone, so callers convert to the
    desired zone (usually local) before calling. A naive/None value yields ``—``.
    """
    if dt is None:
        return "—"
    hour12 = dt.hour % 12 or 12
    ampm = "am" if dt.hour < 12 else "pm"
    yy = dt.year % 100
    stamp = f"{dt.month}/{dt.day}/{yy:02d} {hour12}:{dt.minute:02d}:{dt.second:02d} {ampm}"
    offset = dt.strftime("%z")  # e.g. -0700, or '' if naive
    if offset:
        stamp += f" (UTC {offset})"
    return stamp


def fmt_uptime(obj: dict[str, Any] | None) -> str:
    """Render an uptime ``{days, hours, minutes}`` dict, e.g. ``33 minutes``."""
    obj = obj or {}
    days = int(obj.get("days") or 0)
    hours = int(obj.get("hours") or 0)
    minutes = int(obj.get("minutes") or 0)
    parts: list[str] = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not parts:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


def fmt_fw_date(raw: str) -> str:
    """Format a firmware build date as ``Apr 21, 2025`` (empty -> '')."""
    dt = _parse_iso(raw)
    if dt is None:
        return raw or ""
    return f"{_MONTHS[dt.month - 1]} {dt.day}, {dt.year}"


def fmt_duration(created: str, modified: str) -> str:
    """Elapsed time between two ISO timestamps, e.g. ``39m10s`` / ``1h05m10s``."""
    start, end = _parse_iso(created), _parse_iso(modified)
    if start is None or end is None:
        return ""
    total = int((end - start).total_seconds())
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


# ── normalization (pure) ──────────────────────────────────────────────────────

def _memory(status: dict[str, Any]) -> str:
    mem = status.get("memory")
    if mem is None:
        return "—"
    units = status.get("memoryUnits") or "GB"
    return f"{mem} {units}"


def normalize_nodes(
    ha_payload: dict[str, Any] | None,
    status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Normalize ``/rest/appliance/ha-nodes`` members into node summaries.

    Per-role start time + uptime from ``nodeinfo/status`` are merged in so each
    node carries its own timing. Nodes are ordered Active first, then by bay.
    """
    status = status or {}
    members = (ha_payload or {}).get("members", []) or []
    nodes: list[dict[str, Any]] = []
    for m in members:
        loc = m.get("location") or {}
        role = m.get("role") or ""
        key = role.lower()  # 'active' / 'standby'
        nodes.append({
            "role": role,
            "name": loc.get("description") or m.get("name") or "",
            "bay": loc.get("bay"),
            "enclosure": (loc.get("enclosure") or {}).get("resourceName") or "",
            "model": m.get("modelNumber") or "",
            "version": m.get("version") or "",
            "state": m.get("state") or m.get("status") or "",
            "sync_percent": m.get("synchronizationPercentComplete"),
            "app_ipv4": m.get("appIpv4Addr") or "",
            "ilo_address": "not set",  # Composer bays expose no iLO address
            "start_time": status.get(f"{key}StartTime") or "",
            "uptime": status.get(f"{key}Uptime") or {},
        })
    order = {"active": 0, "standby": 1}
    nodes.sort(key=lambda n: (order.get(n["role"].lower(), 9), n.get("bay") or 0))
    return nodes


def _is_connected(nodes: list[dict[str, Any]]) -> bool:
    """True when a redundant pair is present, healthy, and fully in sync."""
    if len(nodes) < 2:
        return False
    for n in nodes:
        if (n.get("state") or "").upper() != "OK":
            return False
        sync = n.get("sync_percent")
        if sync is not None and sync != 100:
            return False
    return True


def normalize_last_update(tasks_payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Normalize the most recent "Update appliance" task into a banner summary."""
    members = (tasks_payload or {}).get("members", []) or []
    if not members:
        return None
    t = members[0]
    return {
        "name": t.get("name") or "Update appliance",
        "state": t.get("taskState") or "",
        "owner": t.get("owner") or "",
        "duration": fmt_duration(t.get("created") or "", t.get("modified") or ""),
        "finished_raw": t.get("modified") or "",
    }


def build_appliance_info(
    ha_payload: dict[str, Any] | None,
    status: dict[str, Any] | None,
    version: dict[str, Any] | None,
    tasks_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the normalized appliance-describe model from the raw payloads."""
    status = status or {}
    version = version or {}
    nodes = normalize_nodes(ha_payload, status)
    return {
        "model": version.get("modelNumber") or (nodes[0]["model"] if nodes else ""),
        "family": version.get("family") or "",
        "serial_number": version.get("serialNumber") or "",
        "memory": _memory(status),
        "nodes": nodes,
        "connected": _is_connected(nodes),
        "firmware": {
            "version": version.get("softwareVersion") or "",
            "date_raw": version.get("date") or "",
        },
        "last_update": normalize_last_update(tasks_payload),
    }


# ── fetch (I/O) ───────────────────────────────────────────────────────────────

async def fetch_appliance_info(client: "OneViewClient") -> dict[str, Any]:
    """Fetch + normalize the appliance-describe model over the OneView REST API."""
    ha_payload = await client.get(HA_NODES_URI)
    status = await client.get(NODE_STATUS_URI)
    version = await client.get(NODE_VERSION_URI)
    tasks_payload: dict[str, Any] | None = None
    try:
        tasks_payload = await client.get(TASKS_URI, params={
            "filter": "name='Update appliance'",
            "sort": "created:descending",
            "count": 1,
        })
    except Exception:  # noqa: BLE001 — the update banner is best-effort/optional
        tasks_payload = None
    return build_appliance_info(ha_payload, status, version, tasks_payload)
