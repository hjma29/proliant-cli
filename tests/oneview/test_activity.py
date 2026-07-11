"""Unit tests for proliant.oneview.activity — the GUI Activity feed."""

from __future__ import annotations

import pytest

from proliant.oneview import activity as act


# ── time helpers ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ts,ok", [
    ("2026-07-11T06:26:39.479Z", True),
    ("2026-07-11T06:26:39Z", True),
    ("2026-07-11T06:26:39.479+00:00", True),
    ("", False),
    (None, False),
    ("not-a-date", False),
])
def test_parse_iso(ts, ok):
    result = act.parse_iso(ts)
    assert (result is not None) == ok


def test_parse_iso_is_timezone_aware():
    dt = act.parse_iso("2026-07-11T06:26:39.479Z")
    assert dt is not None and dt.tzinfo is not None


@pytest.mark.parametrize("start,end,expected", [
    ("2026-07-11T06:26:39Z", "2026-07-11T06:26:44Z", "5s"),
    ("2026-07-11T06:16:05Z", "2026-07-11T06:25:24Z", "9m19s"),
    ("2026-07-11T06:16:05Z", "2026-07-11T06:17:05Z", "1m"),
    ("2026-07-11T06:00:00Z", "2026-07-11T08:30:00Z", "2h30m"),
    ("2026-07-11T06:00:00Z", "2026-07-11T08:00:00Z", "2h"),
    ("2026-07-11T06:26:39Z", "", ""),
    ("2026-07-11T06:26:44Z", "2026-07-11T06:26:39Z", ""),   # negative -> blank
])
def test_format_duration(start, end, expected):
    assert act.format_duration(start, end) == expected


# ── normalization ───────────────────────────────────────────────────────────

def test_normalize_task():
    raw = {
        "name": "Logical enclosure firmware update",
        "taskState": "Error",
        "taskStatus": "Firmware update failed.",
        "percentComplete": 100,
        "created": "2026-07-11T06:26:39.479Z",
        "modified": "2026-07-11T06:26:44.739Z",
        "owner": "Administrator",
        "associatedResource": {"resourceName": "LE01"},
        "uri": "/rest/tasks/abc",
    }
    row = act.normalize_task(raw)
    assert row["kind"] == "task"
    assert row["name"] == "Logical enclosure firmware update"
    assert row["resource"] == "LE01"
    assert row["state"] == "Error"
    assert row["owner"] == "Administrator"
    assert row["duration"] == "5s"
    assert row["uri"] == "/rest/tasks/abc"


def test_normalize_alert():
    raw = {
        "description": "The frame link module in bay 2 is active.",
        "alertState": "Cleared",
        "severity": "OK",
        "created": "2026-07-11T04:49:00.491Z",
        "associatedResource": {"resourceName": "Enclosure-01"},
        "assignedToUser": None,
        "uri": "/rest/alerts/7124",
    }
    row = act.normalize_alert(raw)
    assert row["kind"] == "alert"
    assert row["name"] == "The frame link module in bay 2 is active."
    assert row["resource"] == "Enclosure-01"
    assert row["state"] == "Cleared"
    assert row["severity"] == "OK"
    assert row["owner"] == ""          # unassigned
    assert row["duration"] == ""       # alerts have no duration


def test_normalize_handles_missing_fields():
    assert act.normalize_task({})["name"] == ""
    assert act.normalize_alert({})["resource"] == ""


# ── merge / filter ──────────────────────────────────────────────────────────

def _task(name, created, resource="LE01", state="Completed"):
    return {"name": name, "created": created, "taskState": state,
            "associatedResource": {"resourceName": resource}}


def _alert(desc, created, resource="Enclosure-01", state="Cleared"):
    return {"description": desc, "created": created, "alertState": state,
            "associatedResource": {"resourceName": resource}}


def test_merge_sorts_newest_first_across_kinds():
    tasks = [_task("older task", "2026-07-11T06:00:00Z")]
    alerts = [_alert("newer alert", "2026-07-11T07:00:00Z")]
    rows = act.merge_activity(tasks, alerts)
    assert [r["name"] for r in rows] == ["newer alert", "older task"]


def test_merge_limit_applies_after_sort():
    tasks = [_task(f"t{i}", f"2026-07-11T0{i}:00:00Z") for i in range(5)]
    rows = act.merge_activity(tasks, [], limit=2)
    assert [r["name"] for r in rows] == ["t4", "t3"]


def test_merge_resource_filter_is_substring_ci():
    tasks = [_task("t1", "2026-07-11T06:00:00Z", resource="LE01"),
             _task("t2", "2026-07-11T06:01:00Z", resource="LE02")]
    rows = act.merge_activity(tasks, [], resource="le01")
    assert [r["name"] for r in rows] == ["t1"]


def test_merge_state_filter_is_exact_ci():
    tasks = [_task("ok", "2026-07-11T06:00:00Z", state="Completed"),
             _task("bad", "2026-07-11T06:01:00Z", state="Error")]
    rows = act.merge_activity(tasks, [], state="error")
    assert [r["name"] for r in rows] == ["bad"]


# ── fetch_activity I/O ──────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, tasks, alerts):
        self._tasks = tasks
        self._alerts = alerts
        self.calls: list[tuple] = []

    async def get(self, uri, params=None):
        self.calls.append((uri, params))
        if uri == act.TASKS_URI:
            return {"members": self._tasks}
        if uri == act.ALERTS_URI:
            return {"members": self._alerts}
        return {}


@pytest.mark.asyncio
async def test_fetch_activity_merges_both_sources():
    client = _FakeClient(
        tasks=[_task("t1", "2026-07-11T06:00:00Z")],
        alerts=[_alert("a1", "2026-07-11T07:00:00Z")],
    )
    rows = await act.fetch_activity(client, limit=10)
    assert [r["name"] for r in rows] == ["a1", "t1"]
    assert client.calls[0][0] == act.TASKS_URI
    assert client.calls[1][0] == act.ALERTS_URI


@pytest.mark.asyncio
async def test_fetch_activity_tasks_only_skips_alerts():
    client = _FakeClient(tasks=[_task("t1", "2026-07-11T06:00:00Z")], alerts=[])
    rows = await act.fetch_activity(client, include_alerts=False)
    assert [c[0] for c in client.calls] == [act.TASKS_URI]
    assert [r["kind"] for r in rows] == ["task"]


@pytest.mark.asyncio
async def test_fetch_activity_alerts_only_skips_tasks():
    client = _FakeClient(tasks=[], alerts=[_alert("a1", "2026-07-11T06:00:00Z")])
    rows = await act.fetch_activity(client, include_tasks=False)
    assert [c[0] for c in client.calls] == [act.ALERTS_URI]
    assert [r["kind"] for r in rows] == ["alert"]
