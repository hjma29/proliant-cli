"""
CLI smoke tests — catch missing functions, broken dispatch tables,
and argument parser regressions without needing a live iLO.
"""

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import smoke test — catches NameError on any module-level reference
# ---------------------------------------------------------------------------

def test_cli_imports_cleanly():
    """Importing cli must not raise NameError or AttributeError."""
    import proliant.ilo.cli  # noqa: F401


def test_inventory_imports_cleanly():
    """Importing inventory must not raise."""
    import proliant.ilo.inventory  # noqa: F401


# ---------------------------------------------------------------------------
# Dispatch table completeness — every key must map to a callable
# ---------------------------------------------------------------------------

def test_fetch_dispatch_all_callable():
    from proliant.ilo.cli import _FETCH_DISPATCH
    for key, fn in _FETCH_DISPATCH.items():
        assert callable(fn), f"_FETCH_DISPATCH['{key}'] is not callable"


def test_raw_dispatch_all_callable():
    from proliant.ilo.cli import _RAW_DISPATCH
    for key, fn in _RAW_DISPATCH.items():
        assert callable(fn), f"_RAW_DISPATCH['{key}'] is not callable"


def test_fetch_and_raw_dispatch_same_keys():
    from proliant.ilo.cli import _FETCH_DISPATCH, _RAW_DISPATCH
    assert set(_FETCH_DISPATCH) == set(_RAW_DISPATCH), (
        "FETCH and RAW dispatch tables have different keys"
    )


# ---------------------------------------------------------------------------
# Printer functions exist and are callable
# ---------------------------------------------------------------------------

def test_all_printers_exist():
    """Every printer used in _run_show() must exist in cli."""
    import proliant.ilo.cli as cli
    expected = [
        "print_full_table",
        "print_fleet_table",
        "print_network_table",
        "print_nic_ilo_table",
        "print_disk_map_table",
        "_print_component_table",
        "_print_raw_table",
    ]
    for name in expected:
        fn = getattr(cli, name, None)
        assert fn is not None, f"cli.{name} is missing"
        assert callable(fn), f"cli.{name} is not callable"


# ---------------------------------------------------------------------------
# Argument parser — subcommand structure
# ---------------------------------------------------------------------------

def test_parser_list_resources():
    """All list-capable inventory resources must be recognised."""
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    for what in ["firmwares", "nic-host", "nic-ilo", "nic", "storage",
                 "cpu", "memory", "com", "full", "disk-map", "serial", "update-method", "license"]:
        resource = "firmware" if what == "firmwares" else what
        args = parser.parse_args([resource, "list"])
        assert args.command == "list"
        assert args.what == what


def test_parser_show_host_filter():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["nic-host", "list", "dl325-gen12"])
    assert args.host == "dl325-gen12"


def test_parser_show_raw_flag():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["nic", "list", "--raw"])
    assert args.raw is True


def test_parser_upgrade_auto():
    """firmware upgrade uses the auto-upgrade action."""
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["firmware", "upgrade", "dl325-gen12"])
    assert args.command == "upgrade"
    assert args.upgrade_action == "auto"
    assert args.host == "dl325-gen12"


def test_parser_upgrade_dry_run_and_reboot():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["firmware", "upgrade", "srv1", "--dry-run", "--reboot"])
    assert args.dry_run is True
    assert args.reboot is True


def test_parser_upgrade_subcommands():
    """All firmware maintenance subcommands must parse correctly."""
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    cases = [
        (["firmware", "components", "srv1"],  "components"),
        (["firmware", "queue", "srv1"],  "queue"),
        (["firmware", "stage", "srv1", "--url", "https://x/fw.fwpkg"], "stage"),
        (["firmware", "flash", "srv1", "fw.fwpkg"], "flash"),
        (["firmware", "clear", "srv1"],  "clear"),
    ]
    for argv, expected_action in cases:
        args = parser.parse_args(argv)
        assert args.upgrade_action == expected_action, f"Expected {expected_action} for {argv}"


def test_parser_upgrade_dry_run_subcommand():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["firmware", "clear", "srv1", "--dry-run"])
    assert args.dry_run is True


def test_parser_power_reset_defaults():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["power", "reset", "dl325-gen12"])
    assert args.command == "power"
    assert args.power_action == "reset"
    assert args.host == "dl325-gen12"
    assert args.reset_type == "GracefulRestart"


def test_parser_power_reset_explicit_type_and_dry_run():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args([
        "power", "reset", "srv1", "--reset-type", "ForceRestart", "--dry-run",
    ])
    assert args.reset_type == "ForceRestart"
    assert args.dry_run is True


def test_parser_power_other_subcommands():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    for action in ("on", "off", "shutdown"):
        args = parser.parse_args(["power", action, "srv1"])
        assert args.command == "power"
        assert args.power_action == action


def test_parser_boot_show():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["boot", "describe", "dl325-gen12"])
    assert args.command == "boot"
    assert args.boot_action == "show"
    assert args.host == "dl325-gen12"


def test_parser_boot_pxe_specific_port_dry_run():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args([
        "boot", "set", "pxe", "srv1", "--port", "Slot 1 Port 1", "--dry-run",
    ])
    assert args.command == "boot"
    assert args.boot_action == "pxe"
    assert args.port == "Slot 1 Port 1"
    assert args.dry_run is True


def test_parser_bios_set_workload_profile():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args([
        "bios", "set", "workload-profile", "srv1", "LowLatency",
    ])
    assert args.command == "bios"
    assert args.bios_action == "set"
    assert args.bios_set_action == "workload-profile"
    assert args.host == "srv1"
    assert args.profile == "LowLatency"


def test_parser_init_command():
    from proliant.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["init"])
    assert args.command == "init"


@pytest.mark.asyncio
async def test_async_main_dispatches_init():
    from proliant.ilo import cli

    with patch("proliant.ilo.cli._run_init") as run_init:
        await cli._async_main(Namespace(command="init"))

    run_init.assert_called_once_with()


@pytest.mark.asyncio
async def test_async_main_dispatches_power():
    from proliant.ilo import cli

    args = Namespace(command="power", power_action="reset", host="srv1", reset_type="GracefulRestart", dry_run=False)
    with patch("proliant.ilo.cli._run_power") as run_power:
        await cli._async_main(args)

    run_power.assert_called_once_with(args)


@pytest.mark.asyncio
async def test_async_main_dispatches_boot():
    from proliant.ilo import cli

    args = Namespace(command="boot", boot_action="show", host="srv1")
    with patch("proliant.ilo.cli._run_boot") as run_boot:
        await cli._async_main(args)

    run_boot.assert_called_once_with(args)


def test_run_init_creates_user_config(monkeypatch, tmp_path):
    from proliant.ilo import cli

    monkeypatch.setattr("proliant.common.config_dir", lambda: tmp_path)

    with patch("rich.prompt.Confirm.ask", side_effect=[False]), \
         patch("proliant.ilo.cli._open_in_editor") as open_editor:
        cli._run_init()

    dest = tmp_path / "inventory.ini"
    assert dest.exists()
    assert "[defaults]" in dest.read_text()
    open_editor.assert_not_called()


class FakeILOClient:
    def __init__(self, responses):
        self.responses = responses

    async def get_manager_uri(self):
        return "/redfish/v1/Managers/1"

    async def get(self, uri):
        return self.responses[uri]


@pytest.mark.asyncio
async def test_manager_network_targets_discovers_interface_and_reset_action():
    from proliant.ilo.cli import _manager_network_targets

    client = FakeILOClient({
        "/redfish/v1/Managers/1": {
            "EthernetInterfaces": {"@odata.id": "/redfish/v1/Managers/1/EthernetInterfaces"},
            "Actions": {
                "#Manager.Reset": {"target": "/redfish/v1/Managers/1/Actions/Manager.Reset"}
            },
        },
        "/redfish/v1/Managers/1/EthernetInterfaces": {
            "Members": [{"@odata.id": "/redfish/v1/Managers/1/EthernetInterfaces/1"}]
        },
    })

    interface_uri, reset_target = await _manager_network_targets(client)

    assert interface_uri == "/redfish/v1/Managers/1/EthernetInterfaces/1"
    assert reset_target == "/redfish/v1/Managers/1/Actions/Manager.Reset"
