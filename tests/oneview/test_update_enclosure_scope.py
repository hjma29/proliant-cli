"""CLI-level test that `--scope shared-infra-and-profiles` actually reaches
OneView as ``SharedInfrastructureAndServerProfiles`` on the wire.

Regression test: `_async_update_enclosure()` used to hardcode
``scope=LE_SCOPE_SHARED`` when calling `run_ssp_apply()` no matter what
`--scope` the operator passed -- the CLI's own *plan* preview correctly
listed every server profile as a target (it's computed from a separate
`profiles_under_le()` call), but the actual logical-enclosure firmware PATCH
always told OneView `"firmwareUpdateOn": "SharedInfrastructureOnly"`, so
`--scope shared-infra-and-profiles` silently behaved exactly like the
default `shared-infra` scope.
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
    def print(self, *a, **k):
        pass

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
        force=False, yes=False, json_output=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_scope_shared_infra_and_profiles_reaches_run_ssp_apply():
    fake_run = AsyncMock(return_value={"status": "planned", "plan": {}})
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=_FakeConsole()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._async_update_enclosure(_make_args(scope="shared-infra-and-profiles"))

    assert fake_run.await_args is not None
    kwargs = fake_run.await_args.kwargs
    assert kwargs["scope"] == ssp_update.LE_SCOPE_SHARED_AND_PROFILES
    # The plan must still carry every profile under the LE as a target.
    assert [p["uri"] for p in kwargs["profile_targets"]] == [_PROFILE["uri"]]


@pytest.mark.asyncio
async def test_scope_shared_infra_only_does_not_use_profiles_scope():
    fake_run = AsyncMock(return_value={"status": "planned", "plan": {}})
    with patch.object(cli, "_load_client", return_value=_FakeCM()), \
         patch.object(cli, "get_console", return_value=_FakeConsole()), \
         patch.object(ssp_update, "fetch_apply_targets", AsyncMock(return_value=_DATA)), \
         patch.object(ssp_update, "run_ssp_apply", fake_run):
        await cli._async_update_enclosure(_make_args(scope="shared-infra"))

    kwargs = fake_run.await_args.kwargs
    assert kwargs["scope"] == ssp_update.LE_SCOPE_SHARED
    assert kwargs["profile_targets"] == []
