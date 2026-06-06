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
    import pcli.ilo.cli  # noqa: F401


def test_inventory_imports_cleanly():
    """Importing inventory must not raise."""
    import pcli.ilo.inventory  # noqa: F401


# ---------------------------------------------------------------------------
# Dispatch table completeness — every key must map to a callable
# ---------------------------------------------------------------------------

def test_fetch_dispatch_all_callable():
    from pcli.ilo.cli import _FETCH_DISPATCH
    for key, fn in _FETCH_DISPATCH.items():
        assert callable(fn), f"_FETCH_DISPATCH['{key}'] is not callable"


def test_raw_dispatch_all_callable():
    from pcli.ilo.cli import _RAW_DISPATCH
    for key, fn in _RAW_DISPATCH.items():
        assert callable(fn), f"_RAW_DISPATCH['{key}'] is not callable"


def test_fetch_and_raw_dispatch_same_keys():
    from pcli.ilo.cli import _FETCH_DISPATCH, _RAW_DISPATCH
    assert set(_FETCH_DISPATCH) == set(_RAW_DISPATCH), (
        "FETCH and RAW dispatch tables have different keys"
    )


# ---------------------------------------------------------------------------
# Printer functions exist and are callable
# ---------------------------------------------------------------------------

def test_all_printers_exist():
    """Every printer used in _run_show() must exist in cli."""
    import pcli.ilo.cli as cli
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

def test_parser_show_subcommands():
    """All 'list' subcommands must be recognised."""
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    for what in ["firmwares", "nic-host", "nic-ilo", "nic", "storage",
                 "cpu", "memory", "com", "full", "disk-map"]:
        args = parser.parse_args(["list", what])
        assert args.command == "list"
        assert args.what == what


def test_parser_show_host_filter():
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["list", "nic-host", "--host", "dl325-gen12"])
    assert args.host == "dl325-gen12"


def test_parser_show_raw_flag():
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["list", "nic", "--raw"])
    assert args.raw is True


def test_parser_upgrade_auto():
    """bare 'upgrade' with --host defaults to auto action."""
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["upgrade", "--host", "dl325-gen12"])
    assert args.command == "upgrade"
    assert args.upgrade_action == "auto"
    assert args.host == "dl325-gen12"


def test_parser_upgrade_dry_run_and_reboot():
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["upgrade", "--host", "srv1", "--dry-run", "--reboot"])
    assert args.dry_run is True
    assert args.reboot is True


def test_parser_upgrade_subcommands():
    """All upgrade subcommands must parse correctly."""
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    cases = [
        (["upgrade", "components", "--host", "srv1"],  "components"),
        (["upgrade", "queue",      "--host", "srv1"],  "queue"),
        (["upgrade", "stage", "--host", "srv1", "--url", "https://x/fw.fwpkg"], "stage"),
        (["upgrade", "flash", "--host", "srv1", "fw.fwpkg"], "flash"),
        (["upgrade", "clear",      "--host", "srv1"],  "clear"),
    ]
    for argv, expected_action in cases:
        args = parser.parse_args(argv)
        assert args.upgrade_action == expected_action, f"Expected {expected_action} for {argv}"


def test_parser_upgrade_dry_run_subcommand():
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["upgrade", "clear", "--host", "srv1", "--dry-run"])
    assert args.dry_run is True


def test_parser_init_command():
    from pcli.ilo.cli import _build_parser
    parser = _build_parser()
    args = parser.parse_args(["init"])
    assert args.command == "init"


@pytest.mark.asyncio
async def test_async_main_dispatches_init():
    from pcli.ilo import cli

    with patch("pcli.ilo.cli._run_init") as run_init:
        await cli._async_main(Namespace(command="init"))

    run_init.assert_called_once_with()


def test_run_init_creates_user_config(monkeypatch, tmp_path):
    from pcli.ilo import cli

    config_home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: config_home)

    with patch("rich.prompt.Confirm.ask", side_effect=[False]), \
         patch("pcli.ilo.cli._open_in_editor") as open_editor:
        cli._run_init()

    dest = config_home / ".config" / "pcli" / "hosts-ilo.ini"
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
    from pcli.ilo.cli import _manager_network_targets

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
