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
        force=False, execute=False, yes=False, json_output=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_plan_only_targets_just_the_named_profile_no_le():
    """The engine call must never include a logical enclosure -- this command
    is scoped to exactly one profile, unlike `update enclosure`."""
    fake_run = AsyncMock(return_value={"status": "planned", "plan": {}})
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=_FakeConsole()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._cmd_profiles_update(_make_args())

    assert fake_run.await_args is not None
    kwargs = fake_run.await_args.kwargs
    assert kwargs["le_targets"] == []
    assert [p["uri"] for p in kwargs["profile_targets"]] == [_PROFILE["uri"]]
    assert kwargs["execute"] is False
    assert kwargs["confirm"] is None


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
        await cli._cmd_profiles_update(_make_args(execute=True, yes=True))

    assert console.input_prompts == []  # never prompted
    kwargs = fake_run.await_args.kwargs
    assert kwargs["execute"] is True
    assert kwargs["confirm"] is not None
    assert kwargs["confirm"]({}) is True  # --yes short-circuits confirm to True
    assert any("SSP apply complete" in line for line in console.printed)


@pytest.mark.asyncio
async def test_without_yes_confirm_matches_typed_baseline_version():
    seen = {}

    async def _capture(_factory, *, confirm, **_kw):
        seen["result"] = confirm({"compat": {}})
        return {"status": "aborted"}

    console = _FakeConsole(input_answer="SY-2025.10.01")
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", side_effect=_capture):
        await cli._cmd_profiles_update(_make_args(execute=True, yes=False))

    assert seen["result"] is True
    assert len(console.input_prompts) == 1
    assert "SY-2025.10.01" in console.input_prompts[0]


@pytest.mark.asyncio
async def test_confirm_rejects_mismatched_typed_version():
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
        await cli._cmd_profiles_update(_make_args(execute=True, yes=False))

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
        await cli._cmd_profiles_update(_make_args(execute=True, yes=True))

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
        await cli._cmd_profiles_update(_make_args(execute=True, yes=True))

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
        await cli._cmd_profiles_update(_make_args(execute=True, yes=False, json_output=True))

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
