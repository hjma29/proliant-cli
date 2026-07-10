"""Tests for the OneView appliance-describe model (appliance_info)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from proliant.oneview.appliance_info import (
    build_appliance_info,
    fetch_appliance_info,
    fmt_duration,
    fmt_fw_date,
    fmt_timestamp,
    fmt_uptime,
    normalize_last_update,
    normalize_nodes,
)


# ── live-shaped fixtures (captured from a real Synergy Composer2) ─────────────

HA_NODES = {
    "members": [
        {
            "role": "Standby",
            "version": "10.00.00-0507518",
            "location": {"bay": 1, "description": "Enclosure-01, appliance bay 1",
                         "enclosure": {"resourceName": "Enclosure-01"}},
            "synchronizationPercentComplete": 100,
            "appIpv4Addr": "10.16.43.101",
            "state": "OK", "status": "OK",
            "name": "Enclosure-01, appliance bay 1",
            "modelNumber": "Synergy Composer2",
        },
        {
            "role": "Active",
            "version": "10.00.00-0507518",
            "location": {"bay": 2, "description": "Enclosure-01, appliance bay 2",
                         "enclosure": {"resourceName": "Enclosure-01"}},
            "synchronizationPercentComplete": 100,
            "appIpv4Addr": "10.16.43.102",
            "state": "OK", "status": "OK",
            "name": "Enclosure-01, appliance bay 2",
            "modelNumber": "Synergy Composer2",
        },
    ],
}

STATUS = {
    "memory": 64, "memoryUnits": "GB",
    "activeUptime": {"hours": 0, "minutes": 33, "days": 0},
    "activeStartTime": "2026-07-10T05:23:33Z",
    "standbyUptime": {"hours": 0, "minutes": 9, "days": 0},
    "standbyStartTime": "2026-07-10T05:46:36Z",
}

VERSION = {
    "softwareVersion": "10.00.00-0507518",
    "modelNumber": "Synergy Composer2",
    "date": "2025-04-21T21:53:36+0000",
    "serialNumber": "CNX23304PC",
    "family": "Synergy Composer",
}

TASKS = {
    "members": [{
        "name": "Update appliance", "taskState": "Completed", "owner": "Administrator",
        "created": "2026-07-10T05:14:42.305Z", "modified": "2026-07-10T05:53:52.885Z",
    }],
}


# ── formatting helpers ────────────────────────────────────────────────────────

def test_fmt_timestamp_matches_oneview_style():
    dt = datetime(2026, 7, 9, 22, 23, 33, tzinfo=timezone(timedelta(hours=-7)))
    assert fmt_timestamp(dt) == "7/9/26 10:23:33 pm (UTC -0700)"


def test_fmt_timestamp_am_and_none():
    dt = datetime(2026, 1, 5, 9, 4, 7, tzinfo=timezone.utc)
    assert fmt_timestamp(dt) == "1/5/26 9:04:07 am (UTC +0000)"
    assert fmt_timestamp(None) == "—"


@pytest.mark.parametrize("obj,expected", [
    ({"days": 0, "hours": 0, "minutes": 33}, "33 minutes"),
    ({"days": 0, "hours": 0, "minutes": 1}, "1 minute"),
    ({"days": 1, "hours": 2, "minutes": 0}, "1 day 2 hours"),
    ({"days": 0, "hours": 0, "minutes": 0}, "0 minutes"),
    ({}, "0 minutes"),
])
def test_fmt_uptime(obj, expected):
    assert fmt_uptime(obj) == expected


def test_fmt_fw_date():
    assert fmt_fw_date("2025-04-21T21:53:36+0000") == "Apr 21, 2025"
    assert fmt_fw_date("") == ""


def test_fmt_duration():
    assert fmt_duration("2026-07-10T05:14:42.305Z", "2026-07-10T05:53:52.885Z") == "39m10s"
    assert fmt_duration("2026-07-10T05:00:00Z", "2026-07-10T06:05:10Z") == "1h05m10s"
    assert fmt_duration("2026-07-10T05:00:00Z", "2026-07-10T05:00:08Z") == "8s"
    assert fmt_duration("bad", "worse") == ""


# ── normalization ─────────────────────────────────────────────────────────────

def test_normalize_nodes_orders_active_first_and_merges_timing():
    nodes = normalize_nodes(HA_NODES, STATUS)
    assert [n["role"] for n in nodes] == ["Active", "Standby"]
    active = nodes[0]
    assert active["name"] == "Enclosure-01, appliance bay 2"
    assert active["bay"] == 2
    assert active["model"] == "Synergy Composer2"
    assert active["ilo_address"] == "not set"
    assert active["start_time"] == "2026-07-10T05:23:33Z"
    assert active["uptime"] == {"hours": 0, "minutes": 33, "days": 0}


def test_normalize_last_update():
    lu = normalize_last_update(TASKS)
    assert lu["state"] == "Completed"
    assert lu["owner"] == "Administrator"
    assert lu["duration"] == "39m10s"


def test_normalize_last_update_empty():
    assert normalize_last_update({"members": []}) is None
    assert normalize_last_update(None) is None


def test_build_appliance_info():
    info = build_appliance_info(HA_NODES, STATUS, VERSION, TASKS)
    assert info["model"] == "Synergy Composer2"
    assert info["memory"] == "64 GB"
    assert info["connected"] is True
    assert info["firmware"]["version"] == "10.00.00-0507518"
    assert len(info["nodes"]) == 2
    assert info["nodes"][0]["role"] == "Active"
    assert info["last_update"]["duration"] == "39m10s"


def test_build_appliance_info_single_node_not_connected():
    single = {"members": [HA_NODES["members"][1]]}  # just the active node
    info = build_appliance_info(single, STATUS, VERSION, None)
    assert info["connected"] is False
    assert info["last_update"] is None


def test_connected_false_when_out_of_sync():
    payload = {"members": [
        {**HA_NODES["members"][0], "synchronizationPercentComplete": 42},
        HA_NODES["members"][1],
    ]}
    info = build_appliance_info(payload, STATUS, VERSION, None)
    assert info["connected"] is False


# ── fetch (fake client) ───────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, *, tasks_raises=False):
        self.tasks_raises = tasks_raises
        self.calls: list[str] = []

    async def get(self, uri, params=None):
        self.calls.append(uri)
        if uri == "/rest/appliance/ha-nodes":
            return HA_NODES
        if uri == "/rest/appliance/nodeinfo/status":
            return STATUS
        if uri == "/rest/appliance/nodeinfo/version":
            return VERSION
        if uri == "/rest/tasks":
            if self.tasks_raises:
                raise RuntimeError("tasks endpoint unavailable")
            return TASKS
        return {}


@pytest.mark.asyncio
async def test_fetch_appliance_info_assembles_model():
    info = await fetch_appliance_info(_FakeClient())
    assert info["model"] == "Synergy Composer2"
    assert info["connected"] is True
    assert info["last_update"]["duration"] == "39m10s"


@pytest.mark.asyncio
async def test_fetch_tolerates_missing_task_banner():
    info = await fetch_appliance_info(_FakeClient(tasks_raises=True))
    assert info["last_update"] is None
    # core info still present even without the optional banner
    assert info["firmware"]["version"] == "10.00.00-0507518"
