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


# ── embedded-ref cleanup ────────────────────────────────────────────────────

def test_clean_refs_collapses_embedded_json_blob():
    text = ('inconsistent with the logical interconnect group '
            '{"name":"LIG-VC100","uri":"/rest/logical-interconnect-groups/abc"}.')
    assert act.clean_refs(text) == (
        "inconsistent with the logical interconnect group LIG-VC100.")


def test_clean_refs_passes_plain_text_through():
    assert act.clean_refs("Stage firmware 80% completed") == "Stage firmware 80% completed"
    assert act.clean_refs("") == ""


def test_normalize_alert_cleans_embedded_refs():
    raw = {"description": 'x {"name":"LIG-VC100","uri":"/rest/y"} z',
           "alertState": "Active", "severity": "Warning",
           "created": "2026-07-11T06:00:00Z"}
    assert act.normalize_alert(raw)["name"] == "x LIG-VC100 z"


# ── progress / phase text ───────────────────────────────────────────────────

def test_normalize_task_captures_parent_and_latest_progress():
    raw = {
        "name": "Update firmware", "taskState": "Running", "percentComplete": 30,
        "taskStatus": "Update logical interconnect firmware",
        "parentTaskUri": "/rest/tasks/parent-1",
        "created": "2026-07-11T06:00:00Z",
        "progressUpdates": [
            {"id": 0, "statusUpdate": "Stage firmware started"},
            {"id": 1, "statusUpdate": "Stage firmware 80% completed"},
        ],
        "associatedResource": {"resourceName": "Enclosure-01, interconnect 3"},
    }
    row = act.normalize_task(raw)
    assert row["parent"] == "/rest/tasks/parent-1"
    assert row["progress"] == "Stage firmware 80% completed"
    # phase_text prefers the newest progress update over taskStatus.
    assert act.phase_text(row) == "Stage firmware 80% completed"


def test_phase_text_falls_back_to_status_without_progress():
    row = act.normalize_task({"taskStatus": "Update logical interconnect.",
                              "created": "2026-07-11T06:00:00Z"})
    assert act.phase_text(row) == "Update logical interconnect."


def test_latest_progress_ignores_blank_updates():
    raw = {"created": "2026-07-11T06:00:00Z",
           "progressUpdates": [{"statusUpdate": "real"}, {"statusUpdate": "  "}]}
    # newest non-blank wins
    assert act.normalize_task(raw)["progress"] == "real"


# ── top-level filtering (GUI feed parity) ───────────────────────────────────

def _subtask(name, created, parent, resource="LE01"):
    return {"name": name, "created": created, "taskState": "Running",
            "parentTaskUri": parent,
            "associatedResource": {"resourceName": resource}}


def test_merge_toplevel_only_drops_subtasks_by_default():
    tasks = [
        _task("parent op", "2026-07-11T06:00:00Z"),
        _subtask("child op", "2026-07-11T06:00:01Z", parent="/rest/tasks/p"),
    ]
    rows = act.merge_activity(tasks, [])
    assert [r["name"] for r in rows] == ["parent op"]


def test_merge_all_tasks_includes_subtasks():
    tasks = [
        _task("parent op", "2026-07-11T06:00:00Z"),
        _subtask("child op", "2026-07-11T06:00:01Z", parent="/rest/tasks/p"),
    ]
    rows = act.merge_activity(tasks, [], toplevel_only=False)
    assert {r["name"] for r in rows} == {"parent op", "child op"}


# ── subtask tree ────────────────────────────────────────────────────────────

class _TreeClient:
    """Serves a root task + children keyed by parentTaskUri filter."""

    def __init__(self, root, children_by_parent):
        self._root = root
        self._kids = children_by_parent

    async def get(self, uri, params=None):
        if params and "filter" in params:
            # filter="parentTaskUri='/rest/tasks/x'"
            filt = params["filter"]
            parent = filt.split("'")[1]
            return {"members": self._kids.get(parent, [])}
        # direct GET of a task uri
        if uri == self._root["uri"]:
            return self._root
        return {}


@pytest.mark.asyncio
async def test_fetch_task_tree_builds_nested_hierarchy():
    root = {"uri": "/rest/tasks/root", "name": "LE fw update", "taskState": "Running",
            "percentComplete": 0, "created": "2026-07-11T06:00:00Z",
            "associatedResource": {"resourceName": "LE01"}}
    child = {"uri": "/rest/tasks/c1", "name": "Update firmware", "taskState": "Running",
             "percentComplete": 27, "parentTaskUri": "/rest/tasks/root",
             "created": "2026-07-11T06:00:01Z",
             "associatedResource": {"resourceName": "LE01-LIG-VC100"}}
    grandchild = {"uri": "/rest/tasks/g1", "name": "Update firmware", "taskState": "Running",
                  "percentComplete": 30, "parentTaskUri": "/rest/tasks/c1",
                  "created": "2026-07-11T06:00:02Z",
                  "associatedResource": {"resourceName": "Enclosure-01, interconnect 3"}}
    client = _TreeClient(root, {
        "/rest/tasks/root": [child],
        "/rest/tasks/c1": [grandchild],
    })
    tree = await act.fetch_task_tree(client, "/rest/tasks/root")
    rows = act.flatten_tree(tree)
    assert [(d, r["name"], r["resource"]) for d, r in rows] == [
        (0, "LE fw update", "LE01"),
        (1, "Update firmware", "LE01-LIG-VC100"),
        (2, "Update firmware", "Enclosure-01, interconnect 3"),
    ]


def test_tree_is_terminal():
    assert act.tree_is_terminal(None) is True
    assert act.tree_is_terminal({"task": {"state": "Completed"}}) is True
    assert act.tree_is_terminal({"task": {"state": "Error"}}) is True
    assert act.tree_is_terminal({"task": {"state": "Running"}}) is False


@pytest.mark.asyncio
async def test_find_active_task_picks_running_toplevel():
    class _C:
        async def get(self, uri, params=None):
            return {"members": [
                {"name": "sub", "taskState": "Running", "parentTaskUri": "/rest/tasks/p",
                 "created": "2026-07-11T06:00:02Z",
                 "associatedResource": {"resourceName": "LE01"}},
                {"name": "LE fw update", "taskState": "Running", "parentTaskUri": "",
                 "created": "2026-07-11T06:00:01Z",
                 "associatedResource": {"resourceName": "LE01"}},
                {"name": "done op", "taskState": "Completed", "parentTaskUri": "",
                 "created": "2026-07-11T06:00:00Z",
                 "associatedResource": {"resourceName": "LE01"}},
            ]}
    row = await act.find_active_task(_C(), resource="LE01")
    assert row is not None and row["name"] == "LE fw update"


@pytest.mark.asyncio
async def test_find_task_token_matches_name_or_resource():
    """`--tree LE01` (a resource name) must match the same way `--tree Logical`
    (a task-name fragment) does — the feed shows both columns, so either the
    Name or the Resource text the operator sees should locate the operation."""
    class _C:
        async def get(self, uri, params=None):
            return {"members": [
                {"name": "Logical enclosure firmware update", "taskState": "Running",
                 "parentTaskUri": "", "created": "2026-07-11T06:00:01Z",
                 "associatedResource": {"resourceName": "LE01"}},
                {"name": "Background inventory collection", "taskState": "Error",
                 "parentTaskUri": "", "created": "2026-07-11T06:00:00Z",
                 "associatedResource": {"resourceName": "Enclosure-01, bay 7"}},
            ]}
    # token = resource name (what the user sees in the Resource column)
    by_resource = await act.find_task(_C(), token="LE01")
    assert by_resource is not None and by_resource["name"] == "Logical enclosure firmware update"
    # token = task-name fragment
    by_name = await act.find_task(_C(), token="Logical")
    assert by_name is not None and by_name["resource"] == "LE01"
    # non-matching token finds nothing
    assert await act.find_task(_C(), token="zzz-nope") is None


def test_format_elapsed_uses_now():
    from datetime import datetime, timezone
    start = "2026-07-11T06:00:00Z"
    now = datetime(2026, 7, 11, 6, 4, 5, tzinfo=timezone.utc)
    assert act.format_elapsed(start, now=now) == "4m5s"
