"""CLI-level tests that `--concurrency` reaches `run_ssp_apply()` correctly.

Companion to `test_update_enclosure_scope.py` -- proves the `--concurrency N`
flag (default 1, fully sequential) is threaded through to
`run_ssp_apply(profile_concurrency=...)` and that an invalid value (<1) is
rejected before any client call is made, rather than silently passed through
to run_ssp_apply().
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
    def __init__(self):
        self.printed: list[str] = []

    def print(self, *a, **k):
        self.printed.append(" ".join(str(x) for x in a))

    def status(self, *_a, **_k):
        return _NullStatus()

    def input(self, *_a, **_k):  # pragma: no cover - not expected in plan mode
        raise AssertionError("no interactive input expected in plan-only mode")


class _FakeCM:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


_LE = {
    "name": "LE01", "uri": "/rest/logical-enclosures/le-1",
    "enclosure_uris": ["/rest/enclosures/enc-1"],
    "current_baseline_uri": "/rest/firmware-drivers/OLD",
}
_BASELINE = {
    "uri": "/rest/firmware-drivers/NEW", "name": "HPE Synergy Service Pack",
    "version": "SY-2025.10.01", "releaseDate": "2025-09-26T00:00:00.0Z",
}
_PROFILE = {
    "name": "aci-FM-host1", "uri": "/rest/server-profiles/p-1",
    "server_hardware_uri": "/rest/server-hardware/sh-1",
    "manage_firmware": True, "current_baseline_uri": "/rest/firmware-drivers/OLD",
}
_DATA = {
    "logical_enclosures": [_LE],
    "baselines": [_BASELINE],
    "server_profiles": [_PROFILE],
    "hardware_enclosure_map": {"/rest/server-hardware/sh-1": "/rest/enclosures/enc-1"},
    "appliance_version": "10.00.00-0000000",
}


def _make_args(**overrides) -> argparse.Namespace:
    base = dict(
        name="LE01", baseline=None, scope="shared-infra-and-profiles",
        install_type=None, execute=False, activation_mode=None,
        force=False, yes=False, json_output=False, concurrency=1,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_default_concurrency_is_one():
    fake_run = AsyncMock(return_value={"status": "planned", "plan": {}})
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=_FakeConsole()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._async_update_enclosure(_make_args())

    assert fake_run.await_args.kwargs["profile_concurrency"] == 1


@pytest.mark.asyncio
async def test_concurrency_flag_reaches_run_ssp_apply():
    fake_run = AsyncMock(return_value={"status": "planned", "plan": {}})
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=_FakeConsole()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._async_update_enclosure(_make_args(concurrency=4))

    assert fake_run.await_args.kwargs["profile_concurrency"] == 4


@pytest.mark.asyncio
async def test_concurrency_zero_is_rejected_before_any_apply_call():
    fake_run = AsyncMock(return_value={"status": "planned", "plan": {}})
    console = _FakeConsole()
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=console), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._async_update_enclosure(_make_args(concurrency=0))

    fake_run.assert_not_awaited()
    assert any("--concurrency must be at least 1" in line for line in console.printed)


@pytest.mark.asyncio
async def test_concurrency_negative_is_rejected():
    fake_run = AsyncMock(return_value={"status": "planned", "plan": {}})
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=_FakeConsole()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._async_update_enclosure(_make_args(concurrency=-1))

    fake_run.assert_not_awaited()
