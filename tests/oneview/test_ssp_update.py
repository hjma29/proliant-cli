"""Tests for the SSP firmware-baseline rollout helpers + orchestration.

The write/reboot half can only run against a live appliance, so it's exercised
here with a fake client that records PATCH/PUT calls and replays task polling —
covering baseline selection, target resolution, plan building, payload shapes,
task handling, and the plan-vs-execute / ordering / failure branches.
"""

from __future__ import annotations

import re

import pytest

from proliant.oneview import ssp_update
from proliant.oneview.ssp_update import (
    INSTALL_TYPES,
    LE_SCOPE_SHARED,
    baseline_id,
    build_le_firmware_patch,
    build_plan,
    build_profile_firmware_put,
    diagnose_nonredundant_uplinks,
    find_le_by_name,
    hardware_enclosure_map,
    is_task_done,
    is_task_failed,
    normalize_le,
    normalize_profile,
    normalize_task,
    poll_task,
    profiles_under_le,
    run_ssp_apply,
    same_baseline,
    select_baseline,
    service_pack_baselines,
)
from proliant.oneview.ssp_update import (
    _normalize_block_decision,
    _uplink_redundancy_note,
    _uplink_set_names_from_reason,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

RAW_DRIVERS = [
    {"uri": "/rest/firmware-drivers/SSP_2023_05", "name": "HPE Synergy Service Pack",
     "baselineShortName": "SPP SY-2023.05.01", "version": "SY-2023.05.01",
     "bundleType": "ServicePack", "releaseDate": "2023-05-01T00:00:00.0Z"},
    {"uri": "/rest/firmware-drivers/SSP_2026_01", "name": "HPE Synergy Service Pack",
     "baselineShortName": "SPP SY-2026.01.02", "version": "SY-2026.01.02",
     "bundleType": "ServicePack", "releaseDate": "2026-03-05T18:06:27.876Z"},
    {"uri": "/rest/firmware-drivers/HOTFIX", "name": "Some Hotfix",
     "version": "1.0", "bundleType": "Hotfix", "releaseDate": "2025-01-01T00:00:00.0Z"},
]

LE_RAW = {
    "name": "LE01",
    "uri": "/rest/logical-enclosures/le-1",
    "eTag": "2026-07-10T05:35:47.088Z",
    "status": "OK",
    "firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"},
    "enclosureUris": ["/rest/enclosures/enc-1"],
}

PROFILE_RAW = {
    "name": "ocp-single-node",
    "uri": "/rest/server-profiles/sp-1",
    "serverHardwareUri": "/rest/server-hardware/uuid-1",
    "eTag": "1733277281713/180",
    "firmware": {
        "firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05",
        "manageFirmware": True,
        "forceInstallFirmware": False,
        "firmwareInstallType": "FirmwareOnlyOfflineMode",
        "firmwareActivationType": "Immediate",
        "installationPolicy": "LowerThanBaseline",
    },
}

RAW_HARDWARE = [
    {"uri": "/rest/server-hardware/uuid-1", "locationUri": "/rest/enclosures/enc-1"},
    {"uri": "/rest/server-hardware/uuid-2", "locationUri": "/rest/enclosures/enc-2"},
]


def _newest():
    return service_pack_baselines(RAW_DRIVERS)[0]


# ── baseline selection ────────────────────────────────────────────────────────

def test_service_pack_baselines_filters_and_orders_newest_first():
    packs = service_pack_baselines(RAW_DRIVERS)
    assert [b["version"] for b in packs] == ["SY-2026.01.02", "SY-2023.05.01"]
    assert all("hotfix" not in b["bundle_type"].lower() for b in packs)


def test_baseline_id_and_same_baseline():
    assert baseline_id("/rest/firmware-drivers/SSP_2026_01/") == "ssp_2026_01"
    assert same_baseline("/rest/firmware-drivers/SSP_2026_01",
                         "/rest/firmware-drivers/ssp_2026_01/")
    assert not same_baseline("", "")
    assert not same_baseline("/rest/firmware-drivers/A", "/rest/firmware-drivers/B")


def test_select_baseline_default_is_newest():
    assert select_baseline(service_pack_baselines(RAW_DRIVERS), None)["version"] == "SY-2026.01.02"


@pytest.mark.parametrize("query", ["SY-2023.05.01", "ssp_2023_05", "2023.05"])
def test_select_baseline_by_query(query):
    b = select_baseline(service_pack_baselines(RAW_DRIVERS), query)
    assert b is not None and b["version"] == "SY-2023.05.01"


def test_select_baseline_no_match_returns_none():
    assert select_baseline(service_pack_baselines(RAW_DRIVERS), "nope") is None
    assert select_baseline([], None) is None


# ── target normalization + resolution ─────────────────────────────────────────

def test_normalize_le_and_profile():
    le = normalize_le(LE_RAW)
    assert le["name"] == "LE01" and le["current_baseline_uri"].endswith("SSP_2023_05")
    assert le["enclosure_uris"] == ["/rest/enclosures/enc-1"]
    p = normalize_profile(PROFILE_RAW)
    assert p["manage_firmware"] is True
    assert p["server_hardware_uri"].endswith("uuid-1")
    assert p["install_type"] == "FirmwareOnlyOfflineMode"


def test_find_le_by_name_case_insensitive():
    les = [normalize_le(LE_RAW)]
    assert find_le_by_name(les, "le01")["name"] == "LE01"
    assert find_le_by_name(les, "missing") is None


def test_hardware_enclosure_map():
    m = hardware_enclosure_map(RAW_HARDWARE)
    assert m["/rest/server-hardware/uuid-1"] == "/rest/enclosures/enc-1"
    assert m["/rest/server-hardware/uuid-2"] == "/rest/enclosures/enc-2"


def test_profiles_under_le_matches_by_hardware_enclosure():
    le = normalize_le(LE_RAW)  # enclosureUris = ["/rest/enclosures/enc-1"]
    profiles = [normalize_profile(PROFILE_RAW)]  # server-hardware/uuid-1 -> enc-1
    hw_map = hardware_enclosure_map(RAW_HARDWARE)
    assert profiles_under_le(le, profiles, hw_map) == profiles
    # a profile whose hardware sits in a different enclosure is excluded
    other = dict(PROFILE_RAW, serverHardwareUri="/rest/server-hardware/uuid-2")
    assert profiles_under_le(le, [normalize_profile(other)], hw_map) == []


def test_profiles_under_le_no_enclosures_returns_empty():
    le = dict(normalize_le(LE_RAW), enclosure_uris=[])
    assert profiles_under_le(le, [normalize_profile(PROFILE_RAW)], {}) == []


# ── plan building ─────────────────────────────────────────────────────────────

def test_build_plan_marks_changes():
    target = _newest()
    plan = build_plan(target, [normalize_le(LE_RAW)], [normalize_profile(PROFILE_RAW)])
    assert plan["changes"] == 2
    assert plan["logical_enclosures"][0]["will_change"] is True
    assert plan["server_profiles"][0]["will_change"] is True


def test_build_plan_up_to_date_when_same_baseline():
    same = service_pack_baselines(RAW_DRIVERS)[1]  # SSP_2023_05 == current
    plan = build_plan(same, [normalize_le(LE_RAW)], [normalize_profile(PROFILE_RAW)])
    assert plan["changes"] == 0
    assert plan["logical_enclosures"][0]["will_change"] is False


def test_build_plan_unmanaged_profile_always_changes():
    raw = {**PROFILE_RAW, "firmware": {"manageFirmware": False}}
    plan = build_plan(service_pack_baselines(RAW_DRIVERS)[1], [], [normalize_profile(raw)])
    assert plan["server_profiles"][0]["will_change"] is True
    assert "managed firmware" in plan["server_profiles"][0]["detail"]


def test_build_plan_compat_absent_without_appliance_version():
    plan = build_plan(_newest(), [normalize_le(LE_RAW)], [])
    assert plan["compat"] is None


def test_build_plan_attaches_compat_when_appliance_version_given():
    plan = build_plan(_newest(), [normalize_le(LE_RAW)], [], appliance_version="10.00.00-0507518")
    assert plan["compat"]["status"] == "recommended"
    assert plan["compat"]["appliance_track"] == "10.0"
    assert plan["compat"]["source_url"].startswith("https://support.hpe.com/")


# ── OneView ↔ SSP compatibility ───────────────────────────────────────────────

@pytest.mark.parametrize("version, track", [
    ("10.00.00-0507518", "10.0"),
    ("9.20.00-0500184", "9.2"),
    ("10.10.00", "10.1"),
    ("11.30.00-1", "11.3"),
    ("10.20.00", "10.2"),
    ("", ""),
    ("garbage", ""),
])
def test_oneview_track(version, track):
    from proliant.oneview.ssp_update import oneview_track
    assert oneview_track(version) == track


def test_ssp_release_strips_prefix():
    from proliant.oneview.ssp_update import ssp_release
    assert ssp_release({"version": "SY-2026.01.02"}) == "2026.01.02"
    assert ssp_release({"version": "2025.10.02"}) == "2025.10.02"
    assert ssp_release({}) == ""


def test_compat_note_recommended_for_10_0():
    from proliant.oneview.ssp_update import compat_note
    note = compat_note("10.00.00-0507518", {"version": "SY-2026.01.02"})
    assert note["status"] == "recommended"
    assert note["recommended"] == "2026.01.02"
    assert "recommended" in note["message"]


def test_compat_note_resolves_recommended_release_date():
    from proliant.oneview.ssp_update import compat_note
    baselines = service_pack_baselines(RAW_DRIVERS)  # normalized (release_date)
    note = compat_note("10.00.00-0507518", {"version": "SY-2025.07.03"}, baselines)
    # recommended 2026.01.02 is registered in RAW_DRIVERS (dated 2026-03-05)
    assert note["recommended"] == "2026.01.02"
    assert note["recommended_release_date"] == "2026-03-05"


def test_compat_note_recommended_date_blank_when_not_registered():
    from proliant.oneview.ssp_update import compat_note
    only_old = [d for d in RAW_DRIVERS if "2026" not in (d.get("version") or "")]
    note = compat_note("10.00.00-0507518", {"version": "SY-2023.05.01"},
                       service_pack_baselines(only_old))
    assert note["recommended"] == "2026.01.02"
    assert note["recommended_release_date"] == ""


def test_compat_note_supported_but_not_recommended():
    from proliant.oneview.ssp_update import compat_note
    note = compat_note("10.00.00-0507518", {"version": "SY-2025.07.03"})
    assert note["status"] == "supported"
    assert "recommended is 2026.01.02" in note["message"]


def test_compat_note_family_wildcard_match():
    from proliant.oneview.ssp_update import compat_note
    # 10.0 lists 2025.05.01 concretely, but 10.1 lists the 2025.05.xx family
    note = compat_note("10.10.00", {"version": "SY-2025.05.07"})
    assert note["status"] == "supported"


def test_compat_note_unsupported_when_ssp_too_new_for_track():
    from proliant.oneview.ssp_update import compat_note
    note = compat_note("10.00.00-0507518", {"version": "SY-2026.04.01"})
    assert note["status"] == "unsupported"
    assert "not listed" in note["message"]


def test_compat_note_unknown_track():
    from proliant.oneview.ssp_update import compat_note
    note = compat_note("8.50.00", {"version": "SY-2026.01.02"})
    assert note["status"] == "unknown"
    assert note["recommended"] is None


def test_compat_matrix_matches_ssp_compat_order_and_content():
    from proliant.oneview.ssp_update import SSP_COMPAT, compat_matrix
    rows = compat_matrix()
    assert [r["track"] for r in rows] == list(SSP_COMPAT.keys())
    row = next(r for r in rows if r["track"] == "11.3")
    assert row["recommended"] == "2026.04.01"
    assert row["supported"] == ["2026.01.02", "2025.10.02", "2025.07.03", "2025.05.xx"]
    # tracks with no additional supported entries return an empty list, not None
    row94 = next(r for r in rows if r["track"] == "9.4")
    assert row94["supported"] == []


# ── payload construction ──────────────────────────────────────────────────────

def test_build_le_firmware_patch_shape():
    patch = build_le_firmware_patch("/rest/firmware-drivers/SSP_2026_01", scope=LE_SCOPE_SHARED, force=True)
    assert patch[0]["op"] == "replace" and patch[0]["path"] == "/firmware"
    val = patch[0]["value"]
    assert val["firmwareBaselineUri"].endswith("SSP_2026_01")
    assert val["firmwareUpdateOn"] == "SharedInfrastructureOnly"
    assert val["forceInstallFirmware"] is True
    assert val["logicalInterconnectUpdateMode"] == "Orchestrated"


def test_build_le_firmware_patch_parallel_activation_mode():
    """update_mode="Parallel" (mirrors -InterconnectActivationMode Parallel)
    must flow straight through to the wire -- this is the only mode that
    flashes a non-redundant Logical Interconnect regardless of validation."""
    patch = build_le_firmware_patch(
        "/rest/firmware-drivers/SSP_2026_01", scope=LE_SCOPE_SHARED, force=True,
        update_mode="Parallel",
    )
    assert patch[0]["value"]["logicalInterconnectUpdateMode"] == "Parallel"


def test_build_profile_firmware_put_preserves_and_overrides():
    body = build_profile_firmware_put(
        PROFILE_RAW, "/rest/firmware-drivers/SSP_2026_01",
        install_type=INSTALL_TYPES["firmware-and-drivers"], force=True,
    )
    fw = body["firmware"]
    assert fw["firmwareBaselineUri"].endswith("SSP_2026_01")
    assert fw["manageFirmware"] is True
    assert fw["firmwareInstallType"] == "FirmwareAndOSDrivers"
    assert fw["forceInstallFirmware"] is True
    # untouched fields survive; original object not mutated
    assert fw["installationPolicy"] == "LowerThanBaseline"
    assert PROFILE_RAW["firmware"]["firmwareBaselineUri"].endswith("SSP_2023_05")


def test_build_profile_firmware_put_keeps_install_type_when_not_overridden():
    body = build_profile_firmware_put(PROFILE_RAW, "/rest/firmware-drivers/SSP_2026_01")
    assert body["firmware"]["firmwareInstallType"] == "FirmwareOnlyOfflineMode"


# ── task handling ─────────────────────────────────────────────────────────────

def test_normalize_task_and_state_predicates():
    t = normalize_task({"uri": "/rest/tasks/1", "taskState": "Completed",
                        "percentComplete": 100, "associatedResource": {"resourceName": "LE01"}})
    assert t["percent"] == 100.0 and t["resource"] == "LE01"
    assert is_task_done(t) and not is_task_failed(t)
    assert is_task_failed(normalize_task({"taskState": "Error"}))
    assert not is_task_done(normalize_task({"taskState": "Running"}))


def test_normalize_task_extracts_latest_stage_from_progress_updates():
    t = normalize_task({
        "uri": "/rest/tasks/1", "taskState": "Running", "percentComplete": 40,
        "progressUpdates": [
            {"id": 0, "statusUpdate": "Monitor"},
            {"id": 1, "statusUpdate": "Update firmware"},
            {"id": 2, "statusUpdate": "   "},  # blank — should be skipped
        ],
    })
    assert t["stage"] == "Update firmware"
    # No progressUpdates → empty stage, never raises.
    assert normalize_task({"taskState": "Running"})["stage"] == ""


def test_normalize_task_cleans_embedded_refs_from_stage():
    """OneView embeds raw {"name":...,"uri":...} JSON blobs inline in some
    progress text (the GUI renders each as a link) -- verified live, this
    showed up in the CLI's own progress bar as a literal
    'Applying server hardware settings to {"name":"Enclosure-01, bay 6",
    "uri":"/rest/server-hardware/..."}.' instead of the plain resource name.
    normalize_task must collapse it the same way _clean_embedded_refs does
    for task-block-reason text."""
    t = normalize_task({
        "uri": "/rest/tasks/1", "taskState": "Running",
        "progressUpdates": [{
            "id": 0,
            "statusUpdate": 'Applying server hardware settings to '
                            '{"name":"Enclosure-01, bay 6","uri":"/rest/server-hardware/x"}.',
        }],
    })
    assert t["stage"] == "Applying server hardware settings to Enclosure-01, bay 6."


@pytest.mark.asyncio
async def test_await_task_treats_non_task_uri_as_synchronous_final():
    from proliant.oneview.ssp_update import _await_task

    class _NoGet:
        async def get(self, uri, params=None):  # pragma: no cover - must not be called
            raise AssertionError("synchronous op must not be polled")

    # Body has the mutated resource's own (non-task) uri → no polling.
    task = await _await_task(
        _NoGet(), {"uri": "/rest/server-profiles/sp-1"},
        emit=lambda k, d: None, sleeper=_noop_sleep, interval_s=1, timeout_s=10,
    )
    assert task["state"] == "Completed"


@pytest.mark.asyncio
async def test_await_task_polls_and_emits_initial_tick_for_task_uri():
    from proliant.oneview.ssp_update import _await_task

    class _C:
        def __init__(self):
            self.n = 0

        async def get(self, uri, params=None):
            self.n += 1
            state = "Completed" if self.n >= 2 else "Running"
            return {"uri": uri, "taskState": state, "percentComplete": self.n * 50}

    seen: list = []
    task = await _await_task(
        _C(), {"uri": "/rest/tasks/le-task", "taskState": "Running", "percentComplete": 0},
        emit=lambda k, d: seen.append((k, d.get("percent"))),
        sleeper=_noop_sleep, interval_s=1, timeout_s=100,
    )
    assert is_task_done(task)
    # First tick reflects the accepted task (0%), before any poll GET.
    assert seen[0] == ("task-progress", 0.0)


# ── fake client + orchestration ──────────────────────────────────────────────

class FakeClient:
    """Async-context OneViewClient stand-in for the apply flow."""

    def __init__(
        self, *, task_state="Completed", profile=None,
        le_get_response=None, children_tasks=None, blocked_until_forced=False,
        always_blocked=False,
        own_task_errors=None, raw_lis=None, li_firmware=None, task_children=None,
    ):
        self.task_state = task_state
        self._profile = profile or PROFILE_RAW
        self._le_get_response = le_get_response
        self._children_tasks = children_tasks or []
        self._own_task_errors = own_task_errors
        self._raw_lis = raw_lis or []
        self._li_firmware = li_firmware or {}
        # Optional parentTaskUri -> [child task dict] map, so a test can build a
        # multi-level task tree and prove _task_block_reason descends past the
        # direct children to a grandchild's taskErrors. When None, every
        # parentTaskUri filter returns the flat _children_tasks list (legacy).
        self._task_children = task_children
        # When True: the first PATCH always comes back "Warning" with the
        # baseline unchanged (simulating OneView's validation guard); a
        # second PATCH (retried with force, i.e. validation bypassed) comes
        # back "Completed" with the baseline actually moved -- lets tests
        # exercise the interactive retry-after-confirm path end to end.
        self._blocked_until_forced = blocked_until_forced
        # When True: EVERY patch (even one retried with force + validation
        # bypassed) comes back "Warning" with the baseline unchanged -- the
        # real-world single-legged-fabric case where forcing an Orchestrated
        # update still can't apply. Lets tests prove blocked_forced is set.
        self._always_blocked = always_blocked
        self._patch_count = 0
        self.calls: list[tuple] = []
        self.patch_headers: list = []
        self.patch_bodies: list = []
        self.put_bodies: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_all(self, uri, **extra_params):
        self.calls.append(("get_all", uri))
        if uri == "/rest/logical-interconnects":
            return self._raw_lis
        return []

    async def get(self, uri, params=None):
        self.calls.append(("get", uri))
        if uri.endswith("/firmware") and uri in self._li_firmware:
            return self._li_firmware[uri]
        if uri == "/rest/tasks":
            if self._task_children is not None and params:
                m = re.search(r"parentTaskUri='([^']*)'", params.get("filter", ""))
                if m:
                    return {"members": self._task_children.get(m.group(1), [])}
            return {"members": self._children_tasks}
        if "/rest/tasks/" in uri:
            if self._always_blocked:
                state = "Warning"
            elif self._blocked_until_forced:
                state = "Warning" if self._patch_count <= 1 else "Completed"
            else:
                state = self.task_state
            resp = {"uri": uri, "taskState": state, "taskStatus": state, "percentComplete": 100}
            if self._own_task_errors is not None:
                resp["taskErrors"] = self._own_task_errors
            return resp
        if uri.startswith("/rest/server-profiles/"):
            return dict(self._profile, uri=uri)
        if self._always_blocked:
            # Baseline never moves, no matter how many times we (force-)patch.
            return self._le_get_response or {}
        if self._blocked_until_forced:
            if self._patch_count <= 1:
                return self._le_get_response or {}
            applied_uri = self.patch_bodies[-1][0]["value"]["firmwareBaselineUri"]
            return {"firmware": {"firmwareBaselineUri": applied_uri}}
        if self._le_get_response is not None:
            return self._le_get_response
        return {}

    async def patch(self, uri, body, headers=None):
        self._patch_count += 1
        self.calls.append(("patch", uri))
        self.patch_bodies.append(body)
        self.patch_headers.append(headers)
        return {"uri": "/rest/tasks/le-task", "taskState": "Running", "percentComplete": 0}

    async def put(self, uri, body):
        self.calls.append(("put", uri))
        self.put_bodies.append(body)
        return {"uri": "/rest/tasks/sp-task", "taskState": "Running", "percentComplete": 0}


# One interconnect pair for uplink set "/rest/uplink-sets/pv": Bay 3 leg linked at
# 10G, Bay 6 leg down — the exact "one leg down, not redundant" shape that blocks
# an Orchestrated update.
_IC_PAIR_ONE_LEG_DOWN = [
    {
        "uri": "/rest/interconnects/ic3", "enclosureName": "Enclosure-01",
        "interconnectLocation": {"locationEntries": [
            {"type": "Enclosure", "value": "Enclosure-01"}, {"type": "Bay", "value": "3"}]},
        "ports": [{
            "portType": "Uplink", "portName": "Q1:1", "portStatus": "Linked",
            "operationalSpeed": "Speed10G",
            "associatedUplinkSetUri": "/rest/uplink-sets/pv"}],
    },
    {
        "uri": "/rest/interconnects/ic6", "enclosureName": "Enclosure-01",
        "interconnectLocation": {"locationEntries": [
            {"type": "Enclosure", "value": "Enclosure-01"}, {"type": "Bay", "value": "6"}]},
        "ports": [{
            "portType": "Uplink", "portName": "Q1:1", "portStatus": "Unlinked",
            "operationalSpeed": "Auto",
            "associatedUplinkSetUri": "/rest/uplink-sets/pv"}],
    },
]


class UplinkDiagClient(FakeClient):
    """FakeClient that also serves /rest/uplink-sets + /rest/interconnects so the
    non-redundant-uplink diagnostic has data to work with."""

    def __init__(self, *, uplink_sets, interconnects, **kw):
        super().__init__(**kw)
        self._uplink_sets = uplink_sets
        self._interconnects = interconnects

    async def get_all(self, uri, **extra_params):
        self.calls.append(("get_all", uri))
        if uri == "/rest/uplink-sets":
            return self._uplink_sets
        if uri == "/rest/interconnects":
            return self._interconnects
        if uri == "/rest/logical-interconnects":
            return self._raw_lis
        return []


async def _noop_sleep(_s):
    return None


@pytest.mark.asyncio
async def test_poll_task_runs_to_completion():
    class _C:
        def __init__(self):
            self.n = 0

        async def get(self, uri, params=None):
            if uri != "/rest/tasks/x":
                return {}  # the deepest-active-descendant lookup: no children
            self.n += 1
            state = "Completed" if self.n >= 2 else "Running"
            return {"uri": uri, "taskState": state, "percentComplete": self.n * 50}

    seen = []
    t = await poll_task(_C(), "/rest/tasks/x", emit=lambda k, d: seen.append(d["percent"]),
                        sleeper=_noop_sleep, interval_s=1, timeout_s=100)
    assert is_task_done(t)
    assert seen[-1] == 100


@pytest.mark.asyncio
async def test_poll_task_retries_transient_connection_error():
    """A transient blip (OneViewError) mid-poll must not abort the rollout --
    OneView keeps running the task, so the poll recovers and completes."""
    from proliant.oneview.client import OneViewError

    class _C:
        def __init__(self):
            self.calls = 0

        async def get(self, uri, params=None):
            if uri != "/rest/tasks/x":
                return {}  # descendant lookup: no children
            self.calls += 1
            if self.calls == 2:
                raise OneViewError("Cannot reach OneView appliance: timed out")
            if self.calls >= 3:
                return {"uri": uri, "taskState": "Completed", "percentComplete": 100}
            return {"uri": uri, "taskState": "Running", "percentComplete": 10}

    events = []
    t = await poll_task(_C(), "/rest/tasks/x",
                        emit=lambda k, d: events.append(d),
                        sleeper=_noop_sleep, interval_s=1, timeout_s=100)
    assert is_task_done(t)
    # A "Reconnecting" tick was surfaced during the blip, then it recovered.
    assert any(e.get("state") == "Reconnecting" for e in events)
    assert events[-1].get("percent") == 100


@pytest.mark.asyncio
async def test_poll_task_gives_up_after_sustained_disconnect():
    """Sustained loss of contact beyond the grace window re-raises rather than
    hanging forever."""
    from proliant.oneview.client import OneViewError

    class _C:
        async def get(self, uri, params=None):
            raise OneViewError("Cannot reach OneView appliance: down")

    with pytest.raises(OneViewError):
        await poll_task(_C(), "/rest/tasks/x", emit=lambda k, d: None,
                        sleeper=_noop_sleep, interval_s=10, timeout_s=1000,
                        reconnect_grace_s=30)


class _ChildTaskClient:
    """Simulates OneView's nested task tree: querying ``/rest/tasks`` with a
    ``parentTaskUri`` filter returns children whose own ``uri`` maps into
    ``tasks_by_parent`` for the *next* level down, mirroring how the GUI's
    task tree nests ("Logical enclosure firmware update" -> "Update
    enclosure firmware" -> "Update frame link module firmware")."""

    def __init__(self, root_task: dict, tasks_by_parent: dict[str, list[dict]]):
        self._root = root_task
        self._by_parent = tasks_by_parent

    async def get(self, uri, params=None):
        if uri == "/rest/tasks":
            parent = (params or {}).get("filter", "")
            for parent_uri, children in self._by_parent.items():
                if parent_uri in parent:
                    return {"members": children}
            return {"members": []}
        if uri == self._root["uri"]:
            return self._root
        return {}


@pytest.mark.asyncio
async def test_deepest_active_descendant_finds_nested_running_child():
    """Reproduces the live GUI screenshot: the root task's own percent is
    stuck at 0 while a grandchild task ("Update frame link module firmware")
    is the one actually carrying the real phase text + percent."""
    root = {"uri": "/rest/tasks/root", "taskState": "Running", "percentComplete": 0}
    child = {
        "uri": "/rest/tasks/child", "taskState": "Running", "percentComplete": 10,
        "progressUpdates": [{"statusUpdate": "Update enclosure firmware."}],
    }
    grandchild = {
        "uri": "/rest/tasks/grandchild", "taskState": "Running", "percentComplete": 30,
        "progressUpdates": [{"statusUpdate": "Update frame link module firmware (force install)."}],
        "associatedResource": {"resourceName": "Enclosure-01"},
    }
    client = _ChildTaskClient(root, {
        "/rest/tasks/root": [child],
        "/rest/tasks/child": [grandchild],
    })
    deepest = await ssp_update._deepest_active_descendant(client, normalize_task(root))
    assert deepest is not None
    assert deepest["stage"] == "Update frame link module firmware (force install)."
    assert deepest["percent"] == 30
    assert deepest["resource"] == "Enclosure-01"


@pytest.mark.asyncio
async def test_deepest_active_descendant_none_when_no_children():
    client = _ChildTaskClient({"uri": "/rest/tasks/root"}, {})
    deepest = await ssp_update._deepest_active_descendant(
        client, normalize_task({"uri": "/rest/tasks/root"}),
    )
    assert deepest is None


@pytest.mark.asyncio
async def test_deepest_active_descendant_ignores_stale_completed_grandchildren():
    """Reproduces a live false '100%  Running' the CLI showed for several
    minutes: root 'Update' (Running), its child 'Apply profile' (Running,
    own stage 'Stage component 4/5...', own percent 0) has two grandchildren
    that already finished ('Power on', 'Generate install set', both
    Completed at 100%). Picking 'whichever grandchild was most recently
    touched' as a fallback grabbed a finished, stale grandchild and overlaid
    its 100% onto the still-running rollout. The deepest *active* node is
    'Apply profile' itself -- there's nothing live below it -- so that's
    what must be returned, not a completed leaf."""
    root = {"uri": "/rest/tasks/root", "taskState": "Running", "percentComplete": 50}
    child = {
        "uri": "/rest/tasks/child", "taskState": "Running", "percentComplete": 0,
        "progressUpdates": [{"statusUpdate": "Stage component 4/5 - firmware.fwpkg"}],
    }
    grandchild_1 = {
        "uri": "/rest/tasks/gc1", "taskState": "Completed", "percentComplete": 100,
        "progressUpdates": [{"statusUpdate": "Successfully powered on server."}],
        "modified": "2026-07-14T03:22:08.632Z",
    }
    grandchild_2 = {
        "uri": "/rest/tasks/gc2", "taskState": "Completed", "percentComplete": 100,
        "progressUpdates": [{"statusUpdate": "Generating install set for the server."}],
        "modified": "2026-07-14T03:26:13.167Z",  # most recently touched -- must NOT win
    }
    client = _ChildTaskClient(root, {
        "/rest/tasks/root": [child],
        "/rest/tasks/child": [grandchild_1, grandchild_2],
    })
    deepest = await ssp_update._deepest_active_descendant(client, normalize_task(root))
    assert deepest is not None
    assert deepest["stage"] == "Stage component 4/5 - firmware.fwpkg"
    assert deepest["percent"] == 0
    assert deepest["uri"] == "/rest/tasks/child"


@pytest.mark.asyncio
async def test_enrich_with_active_descendant_overlays_display_fields_only():
    root = {"uri": "/rest/tasks/root", "taskState": "Running", "percentComplete": 0, "name": "Root"}
    grandchild = {
        "uri": "/rest/tasks/grandchild", "taskState": "Running", "percentComplete": 30,
        "progressUpdates": [{"statusUpdate": "Update frame link module firmware."}],
    }
    client = _ChildTaskClient(root, {
        "/rest/tasks/root": [{"uri": "/rest/tasks/child", "taskState": "Running", "percentComplete": 10}],
        "/rest/tasks/child": [grandchild],
    })
    task = normalize_task(root)
    enriched = await ssp_update._enrich_with_active_descendant(client, task)
    assert enriched["percent"] == 30
    assert enriched["stage"] == "Update frame link module firmware."
    # root's own identity fields are untouched -- only display fields overlaid
    assert enriched["uri"] == task["uri"]
    assert enriched["state"] == task["state"]
    assert enriched["name"] == "Root"


@pytest.mark.asyncio
async def test_enrich_with_active_descendant_never_raises_on_error():
    class _Boom:
        async def get(self, uri, params=None):
            raise RuntimeError("boom")

    task = normalize_task({"uri": "/rest/tasks/root", "taskState": "Running"})
    enriched = await ssp_update._enrich_with_active_descendant(_Boom(), task)
    assert enriched == task


@pytest.mark.asyncio
async def test_poll_task_emits_enriched_display_but_returns_root_state():
    """The bar should show the deepest descendant's phase text/percent on
    each in-progress tick, but the final returned task (used for
    done/failed detection) reflects only the root task."""
    root_running = {"uri": "/rest/tasks/root", "taskState": "Running", "percentComplete": 0}
    root_done = {"uri": "/rest/tasks/root", "taskState": "Completed", "percentComplete": 100}
    grandchild = {
        "uri": "/rest/tasks/grandchild", "taskState": "Running", "percentComplete": 45,
        "progressUpdates": [{"statusUpdate": "Update frame link module firmware."}],
    }

    class _C:
        def __init__(self):
            self.n = 0

        async def get(self, uri, params=None):
            if uri == "/rest/tasks/root":
                self.n += 1
                return root_running if self.n == 1 else root_done
            if uri == "/rest/tasks":
                parent = (params or {}).get("filter", "")
                if "root" in parent:
                    return {"members": [
                        {"uri": "/rest/tasks/child", "taskState": "Running", "percentComplete": 10},
                    ]}
                if "child" in parent:
                    return {"members": [grandchild]}
            return {}

    seen = []
    t = await poll_task(
        _C(), "/rest/tasks/root", emit=lambda k, d: seen.append(d),
        sleeper=_noop_sleep, interval_s=1, timeout_s=100,
    )
    assert is_task_done(t)
    assert t["percent"] == 100  # final returned task: root's own, unenriched
    # the in-progress tick was enriched with the descendant's own phase/percent
    assert seen[0]["percent"] == 45
    assert seen[0]["stage"] == "Update frame link module firmware."
    assert seen[-1]["percent"] == 100  # terminal tick: root's own, unenriched


@pytest.mark.asyncio
async def test_run_plan_only_never_writes_only_reads():
    """A plan-only run (execute=False) now does open the client -- it needs
    to cross-check each LE's real installed firmware (see below) -- but it
    must never PATCH/PUT anything."""
    fake = FakeClient()
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[normalize_profile(PROFILE_RAW)],
        execute=False,
    )
    assert res["status"] == "planned"
    assert res["plan"]["changes"] == 2
    assert not any(m in ("patch", "put") for m, _ in fake.calls)


@pytest.mark.asyncio
async def test_plan_flags_le_as_not_up_to_date_when_actually_not_installed():
    """Reproduces the live bug: the LE's own target pointer
    (``firmware.firmwareBaselineUri``) already says the new baseline, but
    every Logical Interconnect under it still reports the *old* SPP as
    actually installed (blocked/never finished propagating) -- the plan must
    not claim "up to date" in that case, and the top-level "changes" count
    must reflect the correction too."""
    newest = _newest()
    le_already_targeted = dict(LE_RAW, firmware={"firmwareBaselineUri": newest["uri"]})
    fake = FakeClient(
        raw_lis=[{"uri": "/rest/logical-interconnects/li-1", "enclosureUris": ["/rest/enclosures/enc-1"]}],
        li_firmware={
            "/rest/logical-interconnects/li-1/firmware": {
                "sppUri": "/rest/firmware-drivers/SSP_2023_05",  # still the OLD spp actually installed
            },
        },
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=newest,
        le_targets=[normalize_le(le_already_targeted)], profile_targets=[],
        execute=False,
    )
    le_plan = res["plan"]["logical_enclosures"][0]
    assert le_plan["will_change"] is True
    assert le_plan["detail"] == "target baseline set but not yet installed"
    assert res["plan"]["changes"] == 1


@pytest.mark.asyncio
async def test_plan_trusts_le_when_actual_installed_matches_target():
    newest = _newest()
    le_already_targeted = dict(LE_RAW, firmware={"firmwareBaselineUri": newest["uri"]})
    fake = FakeClient(
        raw_lis=[{"uri": "/rest/logical-interconnects/li-1", "enclosureUris": ["/rest/enclosures/enc-1"]}],
        li_firmware={"/rest/logical-interconnects/li-1/firmware": {"sppUri": newest["uri"]}},
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=newest,
        le_targets=[normalize_le(le_already_targeted)], profile_targets=[],
        execute=False,
    )
    le_plan = res["plan"]["logical_enclosures"][0]
    assert le_plan["will_change"] is False
    assert le_plan["detail"] == "up to date"


@pytest.mark.asyncio
async def test_plan_falls_back_to_target_only_when_no_lis_resolved():
    """No Logical Interconnects could be matched under the LE (e.g. this
    appliance/test doesn't model them) -- must not regress; falls back to
    the plain target-vs-target comparison exactly as before."""
    newest = _newest()
    le_already_targeted = dict(LE_RAW, firmware={"firmwareBaselineUri": newest["uri"]})
    fake = FakeClient()  # no raw_lis configured
    res = await run_ssp_apply(
        lambda: fake, baseline=newest,
        le_targets=[normalize_le(le_already_targeted)], profile_targets=[],
        execute=False,
    )
    le_plan = res["plan"]["logical_enclosures"][0]
    assert le_plan["will_change"] is False
    assert le_plan["detail"] == "up to date"


@pytest.mark.asyncio
async def test_run_execute_applies_le_then_profile_in_order():
    fake = FakeClient()
    events: list[str] = []
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[normalize_profile(PROFILE_RAW)],
        execute=True, confirm=lambda plan: True,
        on_event=lambda k, d: events.append(k), sleeper=_noop_sleep,
    )
    assert res["status"] == "applied"
    assert [r["kind"] for r in res["results"]] == ["logical-enclosure", "server-profile"]
    # shared-infra PATCH happens before compute PUT
    methods = [m for m, _ in fake.calls]
    assert methods.index("patch") < methods.index("put")
    # LE PATCH carries the required If-Match header
    assert fake.patch_headers[0] == {"If-Match": "*"}
    # profile PUT retargets the baseline
    assert fake.put_bodies[0]["firmware"]["firmwareBaselineUri"].endswith("SSP_2026_01")
    assert "plan" in events


@pytest.mark.asyncio
async def test_run_execute_confirm_false_aborts():
    fake = FakeClient()
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: False, sleeper=_noop_sleep,
    )
    assert res["status"] == "aborted"
    # Only the plan's read-only cross-check ran -- no PATCH/PUT before confirm.
    assert not any(m in ("patch", "put") for m, _ in fake.calls)


@pytest.mark.asyncio
async def test_run_execute_nothing_to_do_when_current():
    same = service_pack_baselines(RAW_DRIVERS)[1]  # already assigned everywhere
    res = await run_ssp_apply(
        lambda: FakeClient(), baseline=same,
        le_targets=[normalize_le(LE_RAW)], profile_targets=[normalize_profile(PROFILE_RAW)],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert res["status"] == "nothing-to-do"


@pytest.mark.asyncio
async def test_run_execute_reports_failure_and_stops():
    fake = FakeClient(task_state="Error")
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[normalize_profile(PROFILE_RAW)],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert res["status"] == "failed"
    # failed on the first (shared-infra) target; compute never attempted
    assert [r["kind"] for r in res["results"]] == ["logical-enclosure"]
    assert "put" not in [m for m, _ in fake.calls]


@pytest.mark.asyncio
async def test_run_execute_failed_surfaces_task_error_reason():
    """A failed firmware task must carry OneView's own actionable error (e.g.
    SERVER_NOT_POWERED_OFF_FOR_LE_FIRMWARE_UPDATE) so the CLI can show it
    instead of a useless 'check the UI' -- the reason + recommended action
    live in the task's taskErrors."""
    fake = FakeClient(
        task_state="Error",
        own_task_errors=[{
            "errorCode": "SERVER_NOT_POWERED_OFF_FOR_LE_FIRMWARE_UPDATE",
            "message": "The following servers: Enclosure-01, bay 1 are currently "
                       "powered on. Firmware update cannot be initiated until the "
                       "listed servers within the logical enclosure are powered off.",
            "recommendedActions": ["Power off the listed servers and retry the operation."],
        }],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert res["status"] == "failed"
    last = res["results"][-1]
    assert "powered on" in last["failed_reason"]
    assert "Power off the listed servers" in last["failed_resolution"]
    """--force must also skip OneView's non-disruptive-fabric guard, not just
    forceInstallFirmware -- otherwise a non-redundant fabric silently blocks
    the update no matter what --force was documented to do."""
    fake = FakeClient()
    await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep, force=True,
    )
    assert fake.patch_bodies[0][0]["value"]["validateIfLIFirmwareUpdateIsNonDisruptive"] is False


@pytest.mark.asyncio
async def test_run_execute_without_force_keeps_nondisruptive_validation():
    fake = FakeClient()
    await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert fake.patch_bodies[0][0]["value"]["validateIfLIFirmwareUpdateIsNonDisruptive"] is True


@pytest.mark.asyncio
async def test_run_execute_warning_with_unchanged_baseline_reports_blocked():
    """OneView can report a LE firmware task as 'Warning' / 100% complete
    while never actually applying anything (e.g. a non-redundant-fabric
    guard silently no-ops the update). Must not be reported as 'applied'."""
    fake = FakeClient(
        task_state="Warning",
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [
            {"details": "Non-redundant fabric; update would disrupt connectivity."}
        ]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[normalize_profile(PROFILE_RAW)],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert res["status"] == "blocked"
    assert "Non-redundant fabric" in res["results"][-1]["blocked_reason"]
    # LE step stopped the run -- compute (profile) phase never attempted
    assert [r["kind"] for r in res["results"]] == ["logical-enclosure"]
    assert "put" not in [m for m, _ in fake.calls]


@pytest.mark.asyncio
async def test_run_execute_warning_blocked_even_when_le_pointer_already_matches_target():
    """Reproduces the live false positive: OneView moves the LE's own target
    pointer (firmware.firmwareBaselineUri) to the new baseline as soon as
    the update is *requested*, regardless of whether it actually applied --
    so comparing only that pointer after a 'Warning' task always looks like
    success. The apply flow must cross-check the LIs' actually installed SPP
    (like the plan-phase fix does) instead of trusting the LE's own pointer."""
    newest = _newest()
    fake = FakeClient(
        task_state="Warning",
        le_get_response={"firmware": {"firmwareBaselineUri": newest["uri"]}},  # already "moved"
        children_tasks=[{"taskErrors": [
            {"details": "Non-redundant fabric; update would disrupt connectivity."}
        ]}],
        raw_lis=[{"uri": "/rest/logical-interconnects/li-1", "enclosureUris": ["/rest/enclosures/enc-1"]}],
        li_firmware={
            "/rest/logical-interconnects/li-1/firmware": {
                "sppUri": "/rest/firmware-drivers/SSP_2023_05",  # actually still the OLD spp
            },
        },
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=newest,
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert res["status"] == "blocked"
    assert "Non-redundant fabric" in res["results"][-1]["blocked_reason"]


@pytest.mark.asyncio
async def test_run_execute_warning_with_baseline_changed_still_applied():
    """A genuine 'completed with a minor warning' task (baseline did move)
    must still report success, not be misclassified as blocked."""
    fake = FakeClient(
        task_state="Warning",
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2026_01"}},
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert res["status"] == "applied"


@pytest.mark.asyncio
async def test_run_execute_completed_but_actually_unchanged_reports_unverified():
    """OneView reporting a plain 'Completed' task (not 'Warning') isn't
    unconditionally trustworthy either -- verified live, a
    --activation-mode parallel apply reported 'Completed' after only ~7s
    while the real interconnect stage+activate cycle underneath kept running
    for several more minutes. If the LI-actual cross-check still disagrees
    even after repolling for the full verify_timeout_s window, this must
    surface as 'unverified' rather than a blind 'SSP apply complete'."""
    newest = _newest()
    fake = FakeClient(
        task_state="Completed",
        raw_lis=[{"uri": "/rest/logical-interconnects/li-1", "enclosureUris": ["/rest/enclosures/enc-1"]}],
        li_firmware={
            "/rest/logical-interconnects/li-1/firmware": {
                "sppUri": "/rest/firmware-drivers/SSP_2023_05",  # still the OLD spp actually installed
            },
        },
    )
    sleeps: list[float] = []

    async def _tracking_sleep(s):
        sleeps.append(s)

    res = await run_ssp_apply(
        lambda: fake, baseline=newest,
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_tracking_sleep,
        poll_interval_s=5, verify_timeout_s=15,
    )
    assert res["status"] == "unverified"
    assert "did not reflect it" in res["results"][-1]["unverified_reason"]
    # repolled every poll_interval_s up to verify_timeout_s before giving up
    assert sleeps == [5, 5, 5]


@pytest.mark.asyncio
async def test_run_execute_completed_verified_after_a_few_repolls():
    """The common real-world case this bug fix targets: OneView's LE task
    reports 'Completed' immediately, but the LI's actual installed firmware
    only catches up a couple of repolls later -- this must resolve to a
    real 'applied' success, not 'unverified', once it does."""
    newest = _newest()
    calls = {"n": 0}

    class _SlowToConvergeClient(FakeClient):
        async def get(self, uri, params=None):  # noqa: D401 - test shim
            if uri == "/rest/logical-interconnects/li-1/firmware":
                calls["n"] += 1
                # First two checks still see the old SPP; third sees the new one.
                spp = "/rest/firmware-drivers/SSP_2023_05" if calls["n"] < 3 else newest["uri"]
                return {"sppUri": spp}
            return await super().get(uri, params=params)

    fake = _SlowToConvergeClient(
        task_state="Completed",
        raw_lis=[{"uri": "/rest/logical-interconnects/li-1", "enclosureUris": ["/rest/enclosures/enc-1"]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=newest,
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        poll_interval_s=5, verify_timeout_s=60,
    )
    assert res["status"] == "applied"
    assert calls["n"] >= 3


@pytest.mark.asyncio
async def test_run_execute_completed_with_actual_baseline_matching_is_applied():
    """Sanity check for the new 'Completed' cross-check: when the LI-actual
    baseline genuinely does match the target, still reports 'applied' with
    no unnecessary grace-period sleep."""
    newest = _newest()
    fake = FakeClient(
        task_state="Completed",
        raw_lis=[{"uri": "/rest/logical-interconnects/li-1", "enclosureUris": ["/rest/enclosures/enc-1"]}],
        li_firmware={"/rest/logical-interconnects/li-1/firmware": {"sppUri": newest["uri"]}},
    )
    sleeps: list[float] = []

    async def _tracking_sleep(s):
        sleeps.append(s)

    res = await run_ssp_apply(
        lambda: fake, baseline=newest,
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_tracking_sleep,
    )
    assert res["status"] == "applied"
    assert sleeps == []


@pytest.mark.asyncio
async def test_run_execute_blocked_then_confirmed_retries_without_forcing():
    """Mirrors the OneView GUI's own validation-warning modal ("Review the
    warnings... click OK to proceed"): when on_validation_blocked confirms,
    retry the *same* target once with only the non-disruptive validation guard
    cleared -- NOT with forceInstallFirmware. Proceeding through the warning is
    a separate decision from force-reinstalling, exactly as in the GUI."""
    fake = FakeClient(
        blocked_until_forced=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [
            {"details": "Non-redundant fabric; update would disrupt connectivity."}
        ]}],
    )
    seen = []

    def on_validation_blocked(info):
        seen.append(info)
        return True  # operator confirms "proceed anyway"

    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        on_validation_blocked=on_validation_blocked,
    )
    assert res["status"] == "applied"
    assert len(seen) == 1
    assert seen[0]["kind"] == "logical-enclosure"
    assert "Non-redundant fabric" in seen[0]["reason"]
    # first attempt kept validation on, retry bypassed it
    assert fake.patch_bodies[0][0]["value"]["validateIfLIFirmwareUpdateIsNonDisruptive"] is True
    assert fake.patch_bodies[1][0]["value"]["validateIfLIFirmwareUpdateIsNonDisruptive"] is False
    # ...but proceeding through the warning must NOT escalate to force-install
    # -- force is only ever set when the operator explicitly passes --force.
    assert fake.patch_bodies[0][0]["value"]["forceInstallFirmware"] is False
    assert fake.patch_bodies[1][0]["value"]["forceInstallFirmware"] is False


@pytest.mark.asyncio
async def test_run_execute_blocked_force_decision_escalates_to_force_install():
    """On-the-spot "B" choice: when on_validation_blocked returns "force", the
    same target is retried with BOTH the non-disruptive guard cleared AND
    forceInstallFirmware on -- the disruptive path that gets a genuinely
    non-redundant fabric to update (matching accepting the GUI's warning)."""
    fake = FakeClient(
        blocked_until_forced=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [
            {"details": "Non-redundant fabric; update would disrupt connectivity."}
        ]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        on_validation_blocked=lambda info: "force",
    )
    assert res["status"] == "applied"
    # first attempt: guard on, no force; retry: guard cleared AND force on
    assert fake.patch_bodies[0][0]["value"]["validateIfLIFirmwareUpdateIsNonDisruptive"] is True
    assert fake.patch_bodies[0][0]["value"]["forceInstallFirmware"] is False
    assert fake.patch_bodies[1][0]["value"]["validateIfLIFirmwareUpdateIsNonDisruptive"] is False
    assert fake.patch_bodies[1][0]["value"]["forceInstallFirmware"] is True


@pytest.mark.asyncio
async def test_run_execute_blocked_forced_but_still_blocked_sets_blocked_forced():
    """Real single-legged-fabric case: the operator chooses "force" (B), but an
    Orchestrated update still can't apply a fabric with only one live leg, so it
    stays blocked. The result must be flagged blocked_forced=True so the CLI can
    stop telling the operator to "force it" (which they already did) and instead
    steer them to fix redundancy or switch to Parallel mode."""
    fake = FakeClient(
        always_blocked=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [
            {"details": "Non-redundant fabric; update would disrupt connectivity."}
        ]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        on_validation_blocked=lambda info: "force",
    )
    assert res["status"] == "blocked"
    assert res["results"][-1]["blocked_forced"] is True
    # force was actually attempted on the retry (guard cleared + force on) before
    # OneView refused again
    assert fake.patch_bodies[-1][0]["value"]["validateIfLIFirmwareUpdateIsNonDisruptive"] is False
    assert fake.patch_bodies[-1][0]["value"]["forceInstallFirmware"] is True
    """A blocked result carries the non-redundant-uplink diagnostic so the CLI
    can show exactly which uplink set / leg lost redundancy."""
    fake = UplinkDiagClient(
        uplink_sets=[
            {"uri": "/rest/uplink-sets/pv", "name": "pvlan-uplinkset", "status": "Warning",
             "logicalInterconnectUri": "/rest/logical-interconnects/li1"},
        ],
        interconnects=_IC_PAIR_ONE_LEG_DOWN,
        raw_lis=[{"uri": "/rest/logical-interconnects/li1", "name": "LE01-LIG-VC100"}],
        blocked_until_forced=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [{"details":
            "does not have redundant connectivity ... uplink sets: pvlan-uplinkset"}]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        on_validation_blocked=lambda info: "abort",
    )
    assert res["status"] == "blocked"
    ups = res["results"][-1]["blocked_uplinks"]
    assert ups and ups[0]["name"] == "pvlan-uplinkset"
    assert ups[0]["li_name"] == "LE01-LIG-VC100"
    assert "Bay 6" in ups[0]["note"]  # names the down leg
    # operator aborted at the warning -> force was never attempted
    assert res["results"][-1]["blocked_forced"] is False


@pytest.mark.asyncio
async def test_run_execute_blocked_declined_stays_blocked_no_retry():
    """If the operator declines the validation-warning prompt, stop after a
    single attempt -- do not silently retry with force."""
    fake = FakeClient(
        blocked_until_forced=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [{"details": "reason"}]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        on_validation_blocked=lambda info: False,
    )
    assert res["status"] == "blocked"
    assert len(fake.patch_bodies) == 1


@pytest.mark.asyncio
async def test_run_execute_blocked_without_callback_no_retry():
    """No on_validation_blocked callback provided (e.g. --json / scripted
    mode) -- behaves exactly like the flag-only design: blocked, no retry,
    no prompt attempted."""
    fake = FakeClient(
        blocked_until_forced=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [{"details": "reason"}]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert res["status"] == "blocked"
    assert len(fake.patch_bodies) == 1


@pytest.mark.asyncio
async def test_run_execute_defaults_to_orchestrated_activation_mode():
    fake = FakeClient()
    await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
    )
    assert fake.patch_bodies[0][0]["value"]["logicalInterconnectUpdateMode"] == "Orchestrated"


@pytest.mark.asyncio
async def test_run_execute_passes_parallel_activation_mode_to_le_patch():
    """--activation-mode parallel (interconnect_activation_mode="Parallel")
    must reach the actual LE PATCH body, not just be accepted and ignored."""
    fake = FakeClient()
    await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        interconnect_activation_mode="Parallel",
    )
    assert fake.patch_bodies[0][0]["value"]["logicalInterconnectUpdateMode"] == "Parallel"


@pytest.mark.asyncio
async def test_run_execute_blocked_resolution_has_no_injected_hardware_advice():
    """A blocked logical-enclosure update must surface ONLY OneView's own
    reason + recommended actions -- never fabricated 'use --activation-mode
    parallel' / 'power off the servers' guidance. Proceeding through the same
    warning (as the GUI does) can complete an Orchestrated update on a
    partially non-redundant fabric, so prescribing Parallel-with-outage as the
    only path was wrong and is no longer injected."""
    fake = FakeClient(
        blocked_until_forced=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [{
            "details": "Non-redundant fabric.",
            "recommendedActions": ["Configure redundant connectivity for the uplink set."],
        }]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        on_validation_blocked=lambda info: False,
    )
    assert res["status"] == "blocked"
    resolution = res["results"][-1]["blocked_resolution"] or ""
    # OneView's own recommended action is shown verbatim...
    assert "Configure redundant connectivity" in resolution
    # ...and nothing prescriptive is fabricated on top of it.
    assert "--activation-mode parallel" not in resolution
    assert "power off" not in resolution.lower()


@pytest.mark.asyncio
async def test_run_execute_blocked_on_decline_never_force_installs():
    """Declining the validation-warning prompt stops after a single attempt
    and never silently escalates to forceInstallFirmware."""
    fake = FakeClient(
        blocked_until_forced=True,
        le_get_response={"firmware": {"firmwareBaselineUri": "/rest/firmware-drivers/SSP_2023_05"}},
        children_tasks=[{"taskErrors": [{"details": "Non-redundant fabric."}]}],
    )
    res = await run_ssp_apply(
        lambda: fake, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[],
        execute=True, confirm=lambda plan: True, sleeper=_noop_sleep,
        on_validation_blocked=lambda info: False,
    )
    assert res["status"] == "blocked"
    assert len(fake.patch_bodies) == 1
    assert fake.patch_bodies[0][0]["value"]["forceInstallFirmware"] is False


# ── _task_block_reason / _clean_embedded_refs (matches OneView's own GUI text) ─

def test_clean_embedded_refs_collapses_json_blob_to_name():
    text = (
        'Logical interconnect(s) {"name":"LE01-LIG-VC100","uri":"/rest/logical-interconnects/x"} '
        "do not have redundant connectivity."
    )
    assert ssp_update._clean_embedded_refs(text) == (
        "Logical interconnect(s) LE01-LIG-VC100 do not have redundant connectivity."
    )


def test_clean_embedded_refs_collapses_multiple_blobs_in_a_list():
    text = (
        "connectivity disruption on the following server profile(s): "
        '{"name":"aci-Mapped-host1","uri":"/rest/server-profiles/a"},'
        '{"name":"aci-FM-host1","uri":"/rest/server-profiles/b"}.'
    )
    assert ssp_update._clean_embedded_refs(text) == (
        "connectivity disruption on the following server profile(s): "
        "aci-Mapped-host1,aci-FM-host1."
    )


def test_clean_embedded_refs_leaves_plain_text_alone():
    assert ssp_update._clean_embedded_refs("no braces here") == "no braces here"


@pytest.mark.asyncio
async def test_task_block_reason_prefers_the_task_s_own_errors_over_children():
    # Reproduces the real OneView shape found live: the LE-level task itself
    # carries taskErrors (details = the real GUI warning body, message = the
    # generic "Review the warnings..." banner, recommendedActions = the
    # "Resolution:" steps) -- this must win over any child task, and the
    # embedded {"name":...} ref blobs must collapse to plain names.
    fake = FakeClient(
        own_task_errors=[{
            "message": "Review the warnings. If these conditions are acceptable, click OK to proceed.",
            "details": (
                'Logical interconnect(s) {"name":"LE01-LIG-VC100","uri":"/rest/x"} do not have '
                "redundant connectivity for one or more uplink sets and/or downlink connections "
                "configured. Updating the firmware will disrupt the network connectivity."
            ),
            "recommendedActions": [
                "Ensure that the following items have been addressed. \n"
                "- Redundancy is available for all the logical interconnect uplink sets. \n"
                "Alternatively, continuing firmware update may result in connectivity disruption "
                'on the following server profile(s): {"name":"aci-FM-host1","uri":"/rest/y"}.'
            ],
        }],
        children_tasks=[{"taskErrors": [{"details": "should not be used -- own task errors win"}]}],
    )
    reason = await ssp_update._task_block_reason(fake, "/rest/tasks/le-task")
    assert reason["warning"] == (
        "Logical interconnect(s) LE01-LIG-VC100 do not have redundant connectivity for one or "
        "more uplink sets and/or downlink connections configured. Updating the firmware will "
        "disrupt the network connectivity."
    )
    assert "Redundancy is available for all the logical interconnect uplink sets." in reason["resolution"]
    assert "aci-FM-host1" in reason["resolution"]
    assert "should not be used" not in reason["warning"]


@pytest.mark.asyncio
async def test_task_block_reason_falls_back_to_children_when_task_has_no_errors_of_its_own():
    fake = FakeClient(children_tasks=[{"taskErrors": [{"details": "child-level reason"}]}])
    reason = await ssp_update._task_block_reason(fake, "/rest/tasks/le-task")
    assert reason["warning"] == "child-level reason"
    assert reason["resolution"] == ""


@pytest.mark.asyncio
async def test_task_block_reason_falls_back_to_message_when_details_is_null():
    # Some task shapes leave "details" null and put the real body in "message"
    # instead (seen live on a child task) -- must not silently drop it.
    fake = FakeClient(children_tasks=[{"taskErrors": [
        {"details": None, "message": "the real warning body"},
    ]}])
    reason = await ssp_update._task_block_reason(fake, "/rest/tasks/le-task")
    assert reason["warning"] == "the real warning body"


@pytest.mark.asyncio
async def test_task_block_reason_descends_to_grandchild_task_errors():
    # Reproduces the real live tree: "Logical enclosure firmware update"
    # (no errors) -> "Update firmware" (per-LI, no errors) -> "Update firmware"
    # (the VALIDATION_FAILED_FOR_LOGICAL_INTERCONNECT at depth 2). The reason
    # must be pulled from the grandchild, not left blank as it was when only
    # direct children were checked.
    fake = FakeClient(task_children={
        "/rest/tasks/le-task": [{"uri": "/rest/tasks/li", "taskErrors": []}],
        "/rest/tasks/li": [{
            "uri": "/rest/tasks/li-validate",
            "taskErrors": [{
                "errorCode": "VALIDATION_FAILED_FOR_LOGICAL_INTERCONNECT",
                "message": "The logical interconnect does not have redundant "
                           "connectivity configured for one or more uplink sets: "
                           '{"name":"pvlan-uplinkset","uri":"/rest/x"}.',
                "recommendedActions": ["Ensure redundancy is available for all "
                                       "the logical interconnect uplink sets."],
            }],
        }],
    })
    reason = await ssp_update._task_block_reason(fake, "/rest/tasks/le-task")
    assert "does not have redundant connectivity" in reason["warning"]
    assert "pvlan-uplinkset" in reason["warning"]  # embedded ref blob collapsed to name
    assert "Ensure redundancy is available" in reason["resolution"]


# ── non-redundant uplink diagnostic ───────────────────────────────────────────

def test_uplink_set_names_from_reason_extracts_named_sets():
    r = ("The logical interconnect does not have redundant connectivity "
         "configured for one or more uplink sets: pvlan-uplinkset that will "
         "disrupt network connectivity.")
    assert _uplink_set_names_from_reason(r) == ["pvlan-uplinkset"]


def test_uplink_set_names_from_reason_handles_comma_list():
    r = "uplink sets: pvlan-uplinkset, mgmt-uplinkset."
    assert _uplink_set_names_from_reason(r) == ["pvlan-uplinkset", "mgmt-uplinkset"]


def test_uplink_set_names_from_reason_no_match_returns_empty():
    assert _uplink_set_names_from_reason("some unrelated warning text") == []


def test_uplink_redundancy_note_flags_down_leg():
    legs = [
        {"location": "Enclosure-01 Bay 3", "port": "Q1:1", "state": "Linked", "speed": "10"},
        {"location": "Enclosure-01 Bay 6", "port": "Q1:1", "state": "Unlinked", "speed": "unknown"},
    ]
    note = _uplink_redundancy_note(legs)
    assert "Bay 6" in note and "Bay 3" in note
    assert "restore redundancy" in note.lower()


def test_uplink_redundancy_note_flags_speed_mismatch():
    legs = [
        {"location": "Enclosure-01 Bay 3", "port": "Q1:1", "state": "Linked", "speed": "25"},
        {"location": "Enclosure-01 Bay 6", "port": "Q1:1", "state": "Linked", "speed": "10"},
    ]
    note = _uplink_redundancy_note(legs)
    assert "different speeds" in note
    assert "25G" in note and "10G" in note


def test_uplink_redundancy_note_no_legs():
    assert "no live uplink ports" in _uplink_redundancy_note([])


def test_normalize_block_decision_maps_all_forms():
    assert _normalize_block_decision(True) == "proceed"      # legacy bool
    assert _normalize_block_decision(False) == "abort"
    assert _normalize_block_decision(None) == "abort"
    assert _normalize_block_decision("force") == "force"
    assert _normalize_block_decision("PROCEED") == "proceed"
    assert _normalize_block_decision("nonsense") == "abort"


@pytest.mark.asyncio
async def test_diagnose_nonredundant_uplinks_uses_hint_and_finds_down_leg():
    fake = UplinkDiagClient(
        uplink_sets=[
            {"uri": "/rest/uplink-sets/pv", "name": "pvlan-uplinkset", "status": "Warning",
             "logicalInterconnectUri": "/rest/logical-interconnects/li1"},
            {"uri": "/rest/uplink-sets/ok", "name": "mgmt-uplinkset", "status": "OK",
             "logicalInterconnectUri": "/rest/logical-interconnects/li1"},
        ],
        interconnects=_IC_PAIR_ONE_LEG_DOWN,
        raw_lis=[{"uri": "/rest/logical-interconnects/li1", "name": "LE01-LIG-VC100"}],
    )
    out = await diagnose_nonredundant_uplinks(fake, ["pvlan-uplinkset"])
    assert len(out) == 1
    f = out[0]
    assert f["name"] == "pvlan-uplinkset"
    assert f["li_name"] == "LE01-LIG-VC100"
    assert {leg["location"] for leg in f["legs"]} == {"Enclosure-01 Bay 3", "Enclosure-01 Bay 6"}
    assert "Bay 6" in f["note"]


@pytest.mark.asyncio
async def test_diagnose_nonredundant_uplinks_falls_back_to_unhealthy_status():
    # No hint names -> pick every set OneView itself marks unhealthy (status != OK).
    fake = UplinkDiagClient(
        uplink_sets=[
            {"uri": "/rest/uplink-sets/pv", "name": "pvlan-uplinkset", "status": "Warning",
             "logicalInterconnectUri": "/rest/logical-interconnects/li1"},
            {"uri": "/rest/uplink-sets/ok", "name": "mgmt-uplinkset", "status": "OK"},
        ],
        interconnects=_IC_PAIR_ONE_LEG_DOWN,
        raw_lis=[{"uri": "/rest/logical-interconnects/li1", "name": "LE01-LIG-VC100"}],
    )
    out = await diagnose_nonredundant_uplinks(fake, [])
    assert [f["name"] for f in out] == ["pvlan-uplinkset"]


@pytest.mark.asyncio
async def test_diagnose_nonredundant_uplinks_empty_when_no_data():
    # A plain FakeClient serves no uplink-sets -> diagnostic returns [] (never raises).
    assert await diagnose_nonredundant_uplinks(FakeClient(), ["pvlan-uplinkset"]) == []


