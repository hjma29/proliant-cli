"""Tests for `proliant oneview server-profiles update <NAME>`.

Targets a single named server profile's SSP firmware baseline directly,
without requiring (or touching) its logical enclosure's shared infrastructure
-- unlike `update enclosure --scope profiles-only`, which updates every
profile under a given LE at once. Mirrors test_update_enclosure_scope.py
(engine wiring) and test_profiles_reapply_cli.py (CLI status rendering).
"""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from proliant.common.display import OutputMode, set_output_mode
from proliant.oneview import cli, ssp_update


@pytest.fixture(autouse=True)
def reset_output_mode():
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


class _NullStatus:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConsole:
    def __init__(self, input_answer: str = ""):
        self.printed: list[str] = []
        self.input_answer = input_answer
        self.input_prompts: list[str] = []

    def print(self, *a, **k):
        self.printed.append(" ".join(str(x) for x in a))

    def status(self, *_a, **_k):
        return _NullStatus()

    def input(self, prompt="", **_k):
        self.input_prompts.append(prompt)
        return self.input_answer


class _FakeCM:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


_BASELINE = {
    "uri": "/rest/firmware-drivers/NEW", "name": "HPE Synergy Service Pack",
    "version": "SY-2025.10.01", "releaseDate": "2025-09-26T00:00:00.0Z",
}
_PROFILE = {
    "name": "aci-vc-LAG-host1", "uri": "/rest/server-profiles/p-1",
    "server_hardware_uri": "/rest/server-hardware/sh-1",
    "manage_firmware": True, "current_baseline_uri": "/rest/firmware-drivers/OLD",
}
_DATA = {
    "logical_enclosures": [],
    "baselines": [_BASELINE],
    "server_profiles": [_PROFILE],
    "hardware_enclosure_map": {},
    "appliance_version": "10.00.00-0000000",
}


def _make_args(**overrides) -> argparse.Namespace:
    base = dict(
        name="aci-vc-LAG-host1", baseline=None, install_type=None,
        force=False, yes=False, json_output=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_always_executes_targeting_just_the_named_profile_no_le():
    """There is no --execute flag -- the command always applies (gated by the
    type-to-confirm prompt below), and the engine call must never include a
    logical enclosure since this command is scoped to exactly one profile,
    unlike `update enclosure`."""
    fake_run = AsyncMock(return_value={"status": "aborted"})
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=_FakeConsole()), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._cmd_profiles_update(_make_args())

    assert fake_run.await_args is not None
    kwargs = fake_run.await_args.kwargs
    assert kwargs["le_targets"] == []
    assert [p["uri"] for p in kwargs["profile_targets"]] == [_PROFILE["uri"]]
    assert kwargs["execute"] is True
    assert kwargs["confirm"] is not None


@pytest.mark.asyncio
async def test_profile_not_found_prints_known_profiles():
    console = _FakeConsole()
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)):
        await cli._cmd_profiles_update(_make_args(name="does-not-exist"))

    assert any("not found" in line for line in console.printed)
    assert any("aci-vc-LAG-host1" in line for line in console.printed)


@pytest.mark.asyncio
async def test_baseline_not_found_prints_available():
    console = _FakeConsole()
    data = {**_DATA, "baselines": []}
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=data)):
        await cli._cmd_profiles_update(_make_args())

    assert any("No SSP/SPP baselines are registered" in line for line in console.printed)


@pytest.mark.asyncio
async def test_yes_flag_skips_confirm_prompt_and_applies():
    fake_run = AsyncMock(return_value={
        "status": "applied",
        "results": [{"kind": "server-profile", "name": "aci-vc-LAG-host1", "state": "Completed"}],
    })
    console = _FakeConsole()
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._cmd_profiles_update(_make_args(yes=True))

    assert console.input_prompts == []  # never prompted
    kwargs = fake_run.await_args.kwargs
    assert kwargs["execute"] is True
    assert kwargs["confirm"] is not None
    assert kwargs["confirm"]({}) is True  # --yes short-circuits confirm to True
    assert any("SSP apply complete" in line for line in console.printed)


@pytest.mark.asyncio
async def test_without_yes_confirm_accepts_y():
    seen = {}

    async def _capture(_factory, *, confirm, **_kw):
        seen["result"] = confirm({"compat": {}})
        return {"status": "aborted"}

    console = _FakeConsole(input_answer="y")
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", side_effect=_capture):
        await cli._cmd_profiles_update(_make_args(yes=False))

    assert seen["result"] is True
    assert len(console.input_prompts) == 1
    assert "y/N" in console.input_prompts[0]


@pytest.mark.asyncio
async def test_confirm_rejects_anything_but_y():
    seen = {}

    async def _capture(_factory, *, confirm, **_kw):
        seen["result"] = confirm({"compat": {}})
        return {"status": "aborted"}

    console = _FakeConsole(input_answer="wrong")
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", side_effect=_capture):
        await cli._cmd_profiles_update(_make_args(yes=False))

    assert seen["result"] is False


@pytest.mark.asyncio
async def test_failed_status_prints_reason_and_activity_hint():
    fake_run = AsyncMock(return_value={
        "status": "failed",
        "results": [{
            "kind": "server-profile", "name": "aci-vc-LAG-host1", "state": "Error",
            "status": "Error", "failed_reason": "boom", "failed_resolution": "fix it",
        }],
    })
    console = _FakeConsole()
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._cmd_profiles_update(_make_args(yes=True))

    assert any("SSP apply failed" in line for line in console.printed)
    assert any("boom" in line for line in console.printed)
    assert any("oneview activity" in line for line in console.printed)


@pytest.mark.asyncio
async def test_unverified_status_message():
    fake_run = AsyncMock(return_value={
        "status": "unverified",
        "results": [{
            "kind": "server-profile", "name": "aci-vc-LAG-host1",
            "unverified_reason": "still catching up",
        }],
    })
    console = _FakeConsole()
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._cmd_profiles_update(_make_args(yes=True))

    assert any("could not be verified" in line for line in console.printed)


@pytest.mark.asyncio
async def test_json_mode_never_prompts_and_prints_raw_result():
    fake_run = AsyncMock(return_value={"status": "applied", "results": []})
    console = _FakeConsole()
    printed = {}
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run), \
         patch.object(cli, "print_json", lambda data: printed.setdefault("data", data)):
        await cli._cmd_profiles_update(_make_args(yes=False, json_output=True))

    assert console.input_prompts == []
    assert printed["data"]["status"] == "applied"


def test_step_segment_renders_completed_over_total_steps():
    """Reproduces the live 'Apply profile' task getting stuck at '0%' with
    no other indication it's still working (see normalize_task's
    computedPercentComplete docstring) -- the step count is the same
    'still working, here's how far' detail the GUI's subtask log shows."""
    assert cli._step_segment({"completed_steps": 15, "total_steps": 24}) == "step 15/24"


def test_step_segment_defaults_completed_to_zero_when_missing():
    assert cli._step_segment({"total_steps": 24}) == "step 0/24"


def test_step_segment_blank_when_no_total_steps():
    assert cli._step_segment({}) == ""
    assert cli._step_segment({"total_steps": 0, "completed_steps": 0}) == ""
    assert cli._step_segment({"total_steps": None}) == ""


def test_step_segment_shows_plan_separately_when_completed_exceeds_total():
    # Live incident: OneView incremented `completedSteps` past `totalSteps`
    # (26 vs. a planned 24) -- apparently counting every progress-log entry,
    # including retried power-cycle attempts and the final failure entry,
    # without revising the original plan upward. A plain "26/24" fraction
    # reads like a CLI bug, so once completed overtakes the plan this shows
    # the stale plan separately instead of a bogus >100% ratio.
    assert cli._step_segment({"completed_steps": 26, "total_steps": 24}) == "step 26 (plan: 24)"
    assert cli._step_segment({"completed_steps": 24, "total_steps": 24}) == "step 24/24"


class _RecordingConsole:
    def __init__(self):
        self.lines: list[str] = []

    def print(self, text, **kwargs):
        self.lines.append(text)


def test_print_new_progress_log_lines_prints_only_unseen_lines():
    # GUI parity: each newly-appeared progress_log entry should print once,
    # above the live bar, the moment it shows up.
    console = _RecordingConsole()
    bars: dict = {}
    cli._print_new_progress_log_lines(
        console, bars, "log", {"progress_log": ["Stage component 1/6 - a.fwpkg"]}
    )
    assert console.lines == ["  [dim]Stage component 1/6 - a.fwpkg[/dim]"]

    # Next tick adds one more line -- only the new one should print again.
    cli._print_new_progress_log_lines(
        console, bars, "log",
        {"progress_log": ["Stage component 1/6 - a.fwpkg", "Stage component 2/6 - b.fwpkg"]},
    )
    assert console.lines == [
        "  [dim]Stage component 1/6 - a.fwpkg[/dim]",
        "  [dim]Stage component 2/6 - b.fwpkg[/dim]",
    ]


def test_print_new_progress_log_lines_does_not_dedupe_repeated_text():
    # Live incident: a retried "Power off server." legitimately appears twice
    # in progressUpdates. Dedup must be by position (how many lines already
    # printed), not by text, or the CLI would silently swallow the retry.
    console = _RecordingConsole()
    bars: dict = {}
    cli._print_new_progress_log_lines(console, bars, "log", {"progress_log": ["Power off server."]})
    cli._print_new_progress_log_lines(
        console, bars, "log", {"progress_log": ["Power off server.", "Power off server."]}
    )
    assert console.lines == ["  [dim]Power off server.[/dim]"] * 2


def test_print_new_progress_log_lines_prefixes_with_label_when_given():
    # update enclosure --concurrency runs several targets' bars at once --
    # their interleaved log lines need the target name so they stay
    # attributable once printed above the shared multi-row bar.
    console = _RecordingConsole()
    bars: dict = {}
    cli._print_new_progress_log_lines(
        console, bars, "log", {"progress_log": ["Install firmware."]}, label="aci-vc-tunnel-host2"
    )
    assert console.lines == ["  [dim]aci-vc-tunnel-host2:[/dim] [dim]Install firmware.[/dim]"]


def test_print_new_progress_log_lines_noop_when_no_new_lines():
    console = _RecordingConsole()
    bars = {"log": 2}
    cli._print_new_progress_log_lines(
        console, bars, "log", {"progress_log": ["a", "b"]}
    )
    assert console.lines == []
    assert bars["log"] == 2


def test_build_activity_tree_table_shows_full_progress_history():
    # GUI parity: the Activity page's expanded view for an "Apply profile"
    # task lists every "Stage component N/6" / "Install component N/6" line
    # as it happened, not just the latest one -- --tree must reproduce that
    # full ordered log instead of collapsing it to a single phase line.
    node = {
        "task": {
            "name": "Apply profile : test-host", "resource": "Enc-01 bay 2",
            "state": "Running", "percent": 99, "status": "", "progress": "",
            "progress_log": [
                "Stage component 1/6 - a.fwpkg",
                "Stage component 2/6 - b.fwpkg",
            ],
            "completed_steps": 26, "total_steps": 24,
            "created": "2026-07-11T06:00:00Z", "duration": "1h",
        },
        "children": [],
    }
    table, _subtitle = cli._build_activity_tree_table(node, {"name": "test"})
    name_cell = table.columns[0]._cells[0]
    assert "Stage component 1/6 - a.fwpkg" in name_cell
    assert "Stage component 2/6 - b.fwpkg" in name_cell
    # The step segment (with the completed>total plan note) is appended
    # after the full log, not instead of it.
    assert "step 26 (plan: 24)" in name_cell


def test_render_batch_result_reports_every_failed_profile_not_just_last():
    """Reproduces the live incident this fix addresses: a batch of profiles
    where the FIRST one failed but later ones still succeeded (continuing
    past a failed/blocked/unverified profile no longer stops the batch --
    see run_ssp_apply's profile wave loop). The renderer must not assume
    ``results[-1]`` is the problem -- it must find and report the actual
    failed entry even though it's first in the list, not last."""
    console = _FakeConsole()
    result = {
        "status": "failed",
        "results": [
            {
                "kind": "server-profile", "name": "ocp-single-node", "state": "Error",
                "status": "Error", "failed_reason": "Firmware update failed.",
                "failed_resolution": "Cold boot the server and retry.",
                "outcome": "failed",
            },
            {
                "kind": "server-profile", "name": "aci-vc-tunnel-host1",
                "state": "Completed", "outcome": "applied",
            },
            {
                "kind": "server-profile", "name": "aci-vc-tunnel-host2",
                "state": "Completed", "outcome": "applied",
            },
        ],
    }
    cli._render_ssp_apply_result(console, result, plan_message="")
    text = "\n".join(console.printed)
    assert "ocp-single-node" in text
    assert "Firmware update failed." in text
    assert "Cold boot the server and retry." in text
    # The two profiles that succeeded must NOT be reported as the failure,
    # and the summary must make clear the batch kept going past the failure.
    assert "2" in text and "3" in text  # "2 of 3 target(s) applied normally"


def test_render_batch_result_reports_multiple_failed_profiles():
    """When more than one profile in the same batch fails, each one gets its
    own failure detail -- not just the first or last."""
    console = _FakeConsole()
    result = {
        "status": "failed",
        "results": [
            {
                "kind": "server-profile", "name": "profile-a", "state": "Error",
                "failed_reason": "reason A", "outcome": "failed",
            },
            {
                "kind": "server-profile", "name": "profile-b",
                "state": "Completed", "outcome": "applied",
            },
            {
                "kind": "server-profile", "name": "profile-c", "state": "Error",
                "failed_reason": "reason C", "outcome": "failed",
            },
        ],
    }
    cli._render_ssp_apply_result(console, result, plan_message="")
    text = "\n".join(console.printed)
    assert "profile-a" in text and "reason A" in text
    assert "profile-c" in text and "reason C" in text


def test_render_single_target_result_unchanged_when_no_outcome_key():
    """Backward-compat: a result whose entries predate the "outcome" field
    (e.g. built by a caller/test that doesn't set it) still falls back to
    describing the last entry, same as before this fix."""
    console = _FakeConsole()
    result = {
        "status": "failed",
        "results": [{
            "kind": "server-profile", "name": "aci-vc-LAG-host1", "state": "Error",
            "status": "Error", "failed_reason": "boom", "failed_resolution": "fix it",
        }],
    }
    cli._render_ssp_apply_result(console, result, plan_message="")
    text = "\n".join(console.printed)
    assert "aci-vc-LAG-host1" in text
    assert "boom" in text
