"""CLI-level tests for `proliant oneview server-profiles reapply`.

Mirrors test_update_enclosure_concurrency.py's style: patches the engine
function (`run_profile_reapply`) and proves the CLI wires `--yes`/confirm
correctly, prints the right message for each result status, and never
prompts interactively when --yes is given.
"""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, patch

import pytest

from proliant.common.display import OutputMode, set_output_mode
from proliant.oneview import cli, profile_reapply


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


def _make_args(**overrides) -> argparse.Namespace:
    base = dict(name="aci-vc-LAG-host1", yes=False, json_output=False)
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_yes_flag_skips_confirm_prompt_and_reapplies():
    fake_run = AsyncMock(return_value={
        "status": "applied", "profile": {"name": "aci-vc-LAG-host1"},
        "results": [{"kind": "server-profile", "name": "aci-vc-LAG-host1", "state": "Completed"}],
    })
    console = _FakeConsole()
    with patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(profile_reapply, "run_profile_reapply", fake_run):
        await cli._cmd_profiles_reapply(_make_args(yes=True))

    assert console.input_prompts == []  # never prompted
    assert fake_run.await_args.kwargs["name"] == "aci-vc-LAG-host1"
    assert any("Reapplied server profile" in line for line in console.printed)


@pytest.mark.asyncio
async def test_without_yes_prompts_and_calls_confirm_callback():
    fake_run = AsyncMock(return_value={"status": "aborted"})
    console = _FakeConsole(input_answer="aci-vc-LAG-host1")

    async def _capture_confirm(_factory, *, name, confirm, **_kw):
        # Exercise the CLI's confirm() callback the same way the real engine
        # would, proving it prompts and matches on typed profile name.
        confirm({"name": name})
        return {"status": "aborted"}

    with patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(profile_reapply, "run_profile_reapply", side_effect=_capture_confirm):
        await cli._cmd_profiles_reapply(_make_args(yes=False))

    assert len(console.input_prompts) == 1
    assert "aci-vc-LAG-host1" in console.input_prompts[0]


@pytest.mark.asyncio
async def test_confirm_rejects_mismatched_typed_name():
    console = _FakeConsole(input_answer="wrong-name")
    seen = {}

    async def _capture_confirm(_factory, *, name, confirm, **_kw):
        seen["result"] = confirm({"name": name})
        return {"status": "aborted"}

    with patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(profile_reapply, "run_profile_reapply", side_effect=_capture_confirm):
        await cli._cmd_profiles_reapply(_make_args(yes=False))

    assert seen["result"] is False


@pytest.mark.asyncio
async def test_not_found_prints_known_profiles():
    fake_run = AsyncMock(return_value={"status": "not-found", "query": "missing", "known": "a, b"})
    console = _FakeConsole()
    with patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(profile_reapply, "run_profile_reapply", fake_run):
        await cli._cmd_profiles_reapply(_make_args(name="missing", yes=True))

    assert any("not found" in line for line in console.printed)
    assert any("a, b" in line for line in console.printed)


@pytest.mark.asyncio
async def test_failed_status_prints_reason_and_activity_hint():
    fake_run = AsyncMock(return_value={
        "status": "failed", "profile": {"name": "aci-vc-LAG-host1"},
        "results": [{"kind": "server-profile", "name": "aci-vc-LAG-host1", "state": "Error", "status": "Error"}],
    })
    console = _FakeConsole()
    with patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(profile_reapply, "run_profile_reapply", fake_run):
        await cli._cmd_profiles_reapply(_make_args(yes=True))

    assert any("Reapply failed" in line for line in console.printed)
    assert any("oneview activity" in line for line in console.printed)


@pytest.mark.asyncio
async def test_timeout_status_prints_activity_hint():
    fake_run = AsyncMock(return_value={
        "status": "timeout", "profile": {"name": "aci-vc-LAG-host1"},
        "results": [{"kind": "server-profile", "name": "aci-vc-LAG-host1", "state": "Timeout"}],
    })
    console = _FakeConsole()
    with patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(profile_reapply, "run_profile_reapply", fake_run):
        await cli._cmd_profiles_reapply(_make_args(yes=True))

    assert any("did not finish within the timeout" in line for line in console.printed)


@pytest.mark.asyncio
async def test_json_mode_never_prompts_and_prints_raw_result():
    fake_run = AsyncMock(return_value={"status": "applied", "profile": {"name": "x"}, "results": []})
    console = _FakeConsole()
    printed = {}
    with patch.object(cli, "get_console", return_value=console), \
         patch.object(cli, "_oneview_client_factory", return_value=lambda: object()), \
         patch.object(profile_reapply, "run_profile_reapply", fake_run), \
         patch.object(cli, "print_json", lambda data: printed.setdefault("data", data)):
        await cli._cmd_profiles_reapply(_make_args(yes=False, json_output=True))

    assert console.input_prompts == []
    assert printed["data"]["status"] == "applied"
