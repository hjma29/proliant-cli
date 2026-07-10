"""Tests for the SSP firmware-baseline rollout helpers + orchestration.

The write/reboot half can only run against a live appliance, so it's exercised
here with a fake client that records PATCH/PUT calls and replays task polling —
covering baseline selection, target resolution, plan building, payload shapes,
task handling, and the plan-vs-execute / ordering / failure branches.
"""

from __future__ import annotations

import pytest

from proliant.oneview.ssp_update import (
    INSTALL_TYPES,
    LE_SCOPE_SHARED,
    baseline_id,
    build_le_firmware_patch,
    build_plan,
    build_profile_firmware_put,
    is_task_done,
    is_task_failed,
    normalize_le,
    normalize_profile,
    normalize_task,
    poll_task,
    resolve_targets,
    run_ssp_apply,
    same_baseline,
    select_baseline,
    service_pack_baselines,
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
    p = normalize_profile(PROFILE_RAW)
    assert p["manage_firmware"] is True
    assert p["server_hardware_uri"].endswith("uuid-1")
    assert p["install_type"] == "FirmwareOnlyOfflineMode"


def test_resolve_targets_by_name_all_and_empty():
    items = [normalize_le(LE_RAW)]
    assert resolve_targets(items, ["le01"], False)[0]["name"] == "LE01"  # case-insensitive
    assert resolve_targets(items, None, True) == items
    assert resolve_targets(items, None, False) == []
    assert resolve_targets(items, ["missing"], False) == []


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


# ── payload construction ──────────────────────────────────────────────────────

def test_build_le_firmware_patch_shape():
    patch = build_le_firmware_patch("/rest/firmware-drivers/SSP_2026_01", scope=LE_SCOPE_SHARED, force=True)
    assert patch[0]["op"] == "replace" and patch[0]["path"] == "/firmware"
    val = patch[0]["value"]
    assert val["firmwareBaselineUri"].endswith("SSP_2026_01")
    assert val["firmwareUpdateOn"] == "SharedInfrastructureOnly"
    assert val["forceInstallFirmware"] is True
    assert val["logicalInterconnectUpdateMode"] == "Orchestrated"


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
        le_get_response=None, children_tasks=None,
    ):
        self.task_state = task_state
        self._profile = profile or PROFILE_RAW
        self._le_get_response = le_get_response
        self._children_tasks = children_tasks or []
        self.calls: list[tuple] = []
        self.patch_headers: list = []
        self.patch_bodies: list = []
        self.put_bodies: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, uri, params=None):
        self.calls.append(("get", uri))
        if uri == "/rest/tasks":
            return {"members": self._children_tasks}
        if "/rest/tasks/" in uri:
            return {"uri": uri, "taskState": self.task_state,
                    "taskStatus": self.task_state, "percentComplete": 100}
        if uri.startswith("/rest/server-profiles/"):
            return dict(self._profile, uri=uri)
        if self._le_get_response is not None:
            return self._le_get_response
        return {}

    async def patch(self, uri, body, headers=None):
        self.calls.append(("patch", uri))
        self.patch_bodies.append(body)
        self.patch_headers.append(headers)
        return {"uri": "/rest/tasks/le-task", "taskState": "Running", "percentComplete": 0}

    async def put(self, uri, body):
        self.calls.append(("put", uri))
        self.put_bodies.append(body)
        return {"uri": "/rest/tasks/sp-task", "taskState": "Running", "percentComplete": 0}


async def _noop_sleep(_s):
    return None


@pytest.mark.asyncio
async def test_poll_task_runs_to_completion():
    class _C:
        def __init__(self):
            self.n = 0

        async def get(self, uri, params=None):
            self.n += 1
            state = "Completed" if self.n >= 2 else "Running"
            return {"uri": uri, "taskState": state, "percentComplete": self.n * 50}

    seen = []
    t = await poll_task(_C(), "/rest/tasks/x", emit=lambda k, d: seen.append(d["percent"]),
                        sleeper=_noop_sleep, interval_s=1, timeout_s=100)
    assert is_task_done(t)
    assert seen[-1] == 100


@pytest.mark.asyncio
async def test_run_plan_only_does_not_touch_client():
    def factory():
        raise AssertionError("client_factory must not be called for a plan")

    res = await run_ssp_apply(
        factory, baseline=_newest(),
        le_targets=[normalize_le(LE_RAW)], profile_targets=[normalize_profile(PROFILE_RAW)],
        execute=False,
    )
    assert res["status"] == "planned"
    assert res["plan"]["changes"] == 2


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
    assert fake.calls == []


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
async def test_run_execute_force_bypasses_nondisruptive_validation():
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
