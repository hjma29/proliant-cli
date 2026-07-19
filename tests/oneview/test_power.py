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


def test_parser_power_profile_target_has_no_yes_flag():
    """--yes is only meaningful for the server target's --all bulk operation."""
    from proliant.oneview.cli import _build_parser

    parser = _build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["power", "on", "profile", "ocp-host-1", "--yes"])


def test_parser_power_server_yes_flag_parses_but_not_required():
    """--yes is accepted on the server target (to confirm --all) but a
    single-target power command still doesn't require it."""
    from proliant.oneview.cli import _build_parser, _cmd_power

    parser = _build_parser()
    args = parser.parse_args(["power", "on", "server", "Enclosure-01, bay 6", "--yes"])

    assert args.func is _cmd_power
    assert args.yes is True
    assert args.all is False


def test_parser_power_server_all_flag_parses():
    from proliant.oneview.cli import _build_parser, _cmd_power

    parser = _build_parser()
    args = parser.parse_args(["power", "on", "server", "--all", "--yes"])

    assert args.func is _cmd_power
    assert args.power_target_type == "server"
    assert args.all is True
    assert args.yes is True


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


def _power_all_args(**overrides) -> "argparse.Namespace":
    import argparse

    base = dict(
        power_action="on", power_target_type="server", name=None, enclosure=None,
        bay=None, all=True, yes=False, dry_run=False, json_output=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


@pytest.mark.asyncio
async def test_power_all_requires_yes_or_dry_run():
    from proliant.oneview import cli as cli_module

    with pytest.raises(ValueError, match="bulk operation"):
        await cli_module._cmd_power_all(_power_all_args())


@pytest.mark.asyncio
async def test_power_all_rejects_name_or_bay():
    from proliant.oneview import cli as cli_module

    with pytest.raises(ValueError, match="cannot be combined"):
        await cli_module._cmd_power_all(_power_all_args(name="Enclosure-01, bay 6", yes=True))

    with pytest.raises(ValueError, match="cannot be combined"):
        await cli_module._cmd_power_all(_power_all_args(bay=6, yes=True))


class _FakeCM:
    def __init__(self, client):
        self._client = client

    async def __aenter__(self):
        return self._client

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_power_all_dry_run_targets_every_server():
    from unittest.mock import patch

    from proliant.oneview import cli as cli_module

    client = FakeClient(collections={
        "/rest/server-hardware": [
            _server(name="Enclosure-01, bay 6", uri=SERVER_URI),
            _server(name="Enclosure-01, bay 7", uri="/rest/server-hardware/sh2"),
        ],
    })

    with patch.object(cli_module, "_load_client", return_value=_FakeCM(client)), \
         patch.object(cli_module, "print_json") as fake_print_json:
        await cli_module._cmd_power_all(_power_all_args(dry_run=True))

    assert client.puts == []
    results = fake_print_json.call_args[0][0]
    assert len(results) == 2
    assert all(r["status"] == "dry-run" for r in results)


@pytest.mark.asyncio
async def test_power_all_scoped_to_one_enclosure():
    from unittest.mock import patch

    from proliant.oneview import cli as cli_module

    other_enc_uri = "/rest/enclosures/enc2"
    client = FakeClient(collections={
        "/rest/enclosures": [_enclosure(), _enclosure(name="Enclosure-02", uri=other_enc_uri)],
        "/rest/server-hardware": [
            _server(name="Enclosure-01, bay 6", uri=SERVER_URI, locationUri=ENC_URI),
            _server(name="Enclosure-02, bay 1", uri="/rest/server-hardware/sh2",
                    locationUri=other_enc_uri, position=1),
        ],
    })

    with patch.object(cli_module, "_load_client", return_value=_FakeCM(client)), \
         patch.object(cli_module, "print_json") as fake_print_json:
        await cli_module._cmd_power_all(_power_all_args(enclosure="Enclosure-01", yes=True))

    assert client.puts == [(f"{SERVER_URI}/powerState", {"powerState": "On"})]
    results = fake_print_json.call_args[0][0]
    assert len(results) == 1
    assert results[0]["target"] == "Enclosure-01, bay 6"
