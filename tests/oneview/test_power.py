from __future__ import annotations

import pytest

from proliant.oneview.power import run_power_action


ENC_URI = "/rest/enclosures/enc1"
SERVER_URI = "/rest/server-hardware/sh1"
PROFILE_URI = "/rest/server-profiles/sp1"


class FakeClient:
    def __init__(self, collections: dict[str, list[dict]], singles: dict[str, dict] | None = None):
        self._collections = collections
        self._singles = singles or {}
        self.puts: list[tuple[str, dict]] = []

    async def get_all(self, uri: str) -> list[dict]:
        return self._collections.get(uri, [])

    async def get(self, uri: str, params=None) -> dict:
        return self._singles.get(uri, {})

    async def put(self, uri: str, body: dict) -> dict:
        self.puts.append((uri, body))
        return {"uri": "/rest/tasks/power-task"}


def _server(**overrides) -> dict:
    data = {
        "name": "Enclosure-01, bay 6",
        "uri": SERVER_URI,
        "locationUri": ENC_URI,
        "position": 6,
        "powerState": "On",
    }
    data.update(overrides)
    return data


def _enclosure(**overrides) -> dict:
    data = {
        "name": "Enclosure-01",
        "uri": ENC_URI,
        "managerBays": [{"bayNumber": 1}, {"bayNumber": 2}],
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_shutdown_profile_uses_server_hardware_momentary_press():
    client = FakeClient(
        collections={
            "/rest/server-profiles": [{
                "name": "ocp-host-1",
                "uri": PROFILE_URI,
                "serverHardwareUri": SERVER_URI,
            }],
        },
        singles={SERVER_URI: _server()},
    )

    result = await run_power_action(client, "shutdown", "profile", name="ocp-host-1")

    assert client.puts == [
        (f"{SERVER_URI}/powerState", {"powerState": "Off", "powerControl": "MomentaryPress"})
    ]
    assert result["target_type"] == "profile"
    assert result["target"] == "ocp-host-1"
    assert result["task_uri"] == "/rest/tasks/power-task"


@pytest.mark.asyncio
async def test_force_off_server_by_location_uses_press_and_hold():
    client = FakeClient(
        collections={
            "/rest/enclosures": [_enclosure()],
            "/rest/server-hardware": [_server()],
        },
    )

    result = await run_power_action(client, "off", "server", enclosure="Enclosure-01", bay=6)

    assert client.puts == [
        (f"{SERVER_URI}/powerState", {"powerState": "Off", "powerControl": "PressAndHold"})
    ]
    assert result["method"] == "server-hardware powerState"


@pytest.mark.asyncio
async def test_shutdown_interconnect_is_not_supported():
    client = FakeClient(collections={})

    with pytest.raises(ValueError, match="does not expose 'shutdown' for interconnect"):
        await run_power_action(client, "shutdown", "interconnect", name="Enclosure-01, interconnect 6")


@pytest.mark.asyncio
async def test_cycle_action_is_not_supported_use_efuse_instead():
    """cycle/reset were removed from power in favor of 'proliant oneview efuse'."""
    client = FakeClient(collections={})

    with pytest.raises(ValueError, match="Use 'proliant oneview efuse' for a hard power-cycle"):
        await run_power_action(client, "cycle", "server", name="Enclosure-01, bay 6")


def test_parser_power_shutdown_profile_parses():
    from proliant.oneview.cli import _build_parser, _cmd_power

    parser = _build_parser()
    args = parser.parse_args(["power", "shutdown", "profile", "ocp-host-1", "--dry-run"])

    assert args.func is _cmd_power
    assert args.power_action == "shutdown"
    assert args.power_target_type == "profile"
    assert args.name == "ocp-host-1"
    assert args.dry_run is True


def test_parser_power_server_by_location_parses():
    from proliant.oneview.cli import _build_parser, _cmd_power

    parser = _build_parser()
    args = parser.parse_args(["power", "on", "server", "--enclosure", "Enclosure-01", "--bay", "6"])

    assert args.func is _cmd_power
    assert args.power_target_type == "server"
    assert args.enclosure == "Enclosure-01"
    assert args.bay == 6


def test_parser_power_has_no_yes_flag():
    """power never needs --yes since it's graceful-only; --yes is efuse-only."""
    from proliant.oneview.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["power", "on", "server", "Enclosure-01, bay 6", "--yes"])


def test_parser_power_cycle_action_rejected():
    """cycle/reset are no longer valid power actions; use 'oneview efuse' instead."""
    from proliant.oneview.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["power", "cycle", "flm", "Enclosure-01", "1", "--yes"])


def test_parser_power_has_no_interconnect_or_flm_targets():
    """power only exposes server/profile targets; interconnect/flm are efuse-only."""
    from proliant.oneview.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["power", "on", "flm", "Enclosure-01", "1"])
