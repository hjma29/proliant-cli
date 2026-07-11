"""Tests for the SSP firmware-baseline rollout helpers + orchestration.

The write/reboot half can only run against a live appliance, so it's exercised
here with a fake client that records PATCH/PUT calls and replays task polling —
covering baseline selection, target resolution, plan building, payload shapes,
task handling, and the plan-vs-execute / ordering / failure branches.
"""

from __future__ import annotations

import pytest

from proliant.oneview import ssp_update
from proliant.oneview.ssp_update import (
    INSTALL_TYPES,
    LE_SCOPE_SHARED,
    baseline_id,
    build_le_firmware_patch,
    build_plan,
    build_profile_firmware_put,
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
        own_task_errors=None, raw_lis=None, li_firmware=None,
    ):
        self.task_state = task_state
        self._profile = profile or PROFILE_RAW
        self._le_get_response = le_get_response
        self._children_tasks = children_tasks or []
        self._own_task_errors = own_task_errors
        self._raw_lis = raw_lis or []
        self._li_firmware = li_firmware or {}
        # When True: the first PATCH always comes back "Warning" with the
        # baseline unchanged (simulating OneView's validation guard); a
        # second PATCH (retried with force, i.e. validation bypassed) comes
        # back "Completed" with the baseline actually moved -- lets tests
        # exercise the interactive retry-after-confirm path end to end.
        self._blocked_until_forced = blocked_until_forced
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
            return {"members": self._children_tasks}
        if "/rest/tasks/" in uri:
            if self._blocked_until_forced:
                state = "Warning" if self._patch_count <= 1 else "Completed"
            else:
                state = self.task_state
            resp = {"uri": uri, "taskState": state, "taskStatus": state, "percentComplete": 100}
            if self._own_task_errors is not None:
                resp["taskErrors"] = self._own_task_errors
            return resp
        if uri.startswith("/rest/server-profiles/"):
            return dict(self._profile, uri=uri)
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
    --activation-mode parallel apply reported 'Completed' after only ~12s,
    fast enough to raise doubt. If the LI-actual cross-check disagrees even
    after a brief grace re-check, this must surface as 'unverified' rather
    than a blind 'SSP apply complete'."""
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
    )
    assert res["status"] == "unverified"
    assert "did not reflect it" in res["results"][-1]["unverified_reason"]
    # gave OneView's own state one brief grace period before deciding
    assert sleeps == [5]


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
async def test_run_execute_blocked_then_confirmed_retries_with_force():
    """Mirrors the OneView GUI's own validation-warning modal ("Review the
    warnings... click OK to proceed"): when on_validation_blocked confirms,
    retry the *same* target once with force -- no need to abort and
    re-invoke the whole command with --force."""
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
async def test_run_execute_blocked_hint_suggests_parallel_mode_when_still_orchestrated():
    """When a logical-enclosure target is blocked and the caller is still on
    the default Orchestrated activation mode, the blocked_resolution must
    point the operator at --activation-mode parallel -- otherwise retrying
    with --force alone (which this same suite proves is a no-op against a
    non-redundant fabric) looks like the only option."""
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
    assert "--activation-mode parallel" in res["results"][-1]["blocked_resolution"]


@pytest.mark.asyncio
async def test_run_execute_blocked_no_parallel_hint_when_already_parallel():
    """If the caller already retried with --activation-mode parallel and it
    still gets blocked (e.g. some other validation issue), don't suggest
    switching to the mode that's already in effect."""
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
        interconnect_activation_mode="Parallel",
    )
    assert res["status"] == "blocked"
    assert "--activation-mode parallel" not in res["results"][-1]["blocked_resolution"]


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

