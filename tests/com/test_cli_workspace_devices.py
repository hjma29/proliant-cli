"""Tests for COM CLI workspace-switch discoverability and devices/servers scoping.

Covers:
  - 'proliant com workspaces use NAME' as a discoverable alias for the
    pre-existing (and still supported) 'proliant com workspace use NAME'.
  - 'servers list' no longer exposes --type (servers are always COMPUTE-only)
    while 'devices list' keeps it.
  - _cmd_show_devices forces device_type=COMPUTE for the 'servers' command.
  - _model_names_completer never raises and returns hyphenated slugs.
"""
from __future__ import annotations

import argparse
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proliant.com.cli import _build_parser


class TestWorkspacesUseAlias:
    def test_workspaces_use_parses(self):
        parser = _build_parser()
        args = parser.parse_args(["workspaces", "use", "hj-tes1"])
        assert args.command == "workspaces"
        assert args.what == "use"
        assert args.workspace == "hj-tes1"

    def test_workspace_use_still_works(self):
        """Backward-compatible singular alias must keep working."""
        parser = _build_parser()
        args = parser.parse_args(["workspace", "use", "hj-tes1"])
        assert args.command == "workspace"
        assert args.what == "use"
        assert args.workspace == "hj-tes1"

    def test_workspaces_list_still_works(self):
        parser = _build_parser()
        args = parser.parse_args(["workspaces", "list"])
        assert args.command == "workspaces"
        assert args.what == "list"

    @pytest.mark.asyncio
    async def test_workspaces_use_dispatches_to_switch_workspace(self):
        from proliant.com import cli

        args = argparse.Namespace(command="workspaces", what="use", workspace="hj-tes1")
        with patch("proliant.com.login.switch_workspace", new_callable=AsyncMock,
                   return_value="hj-tes1") as mock_switch:
            await cli._async_main(args)
        mock_switch.assert_awaited_once_with("hj-tes1")


class TestServersListTypeScoping:
    def test_servers_list_rejects_type_flag(self):
        """'servers list' no longer accepts --type — servers are always compute."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["servers", "list", "--type", "STORAGE"])

    def test_devices_list_still_accepts_type_flag(self):
        parser = _build_parser()
        args = parser.parse_args(["devices", "list", "--type", "STORAGE"])
        assert args.type == "STORAGE"

    @pytest.mark.asyncio
    async def test_show_devices_forces_compute_for_servers_command(self):
        from proliant.com import cli

        args = argparse.Namespace(
            command="servers", fields=None, sort_by=None, filter_text=None,
            filter_model=None,
        )
        fake_session = MagicMock()
        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock,
                   return_value=fake_session), \
             patch.object(cli._servers_mod, "fetch_servers", new_callable=AsyncMock,
                   return_value=[]) as mock_fetch, \
             patch("proliant.com.cli.print_devices_table") as mock_print:
            await cli._cmd_show_devices(args)

        mock_fetch.assert_awaited_once_with(fake_session)
        _, kwargs = mock_print.call_args
        assert kwargs["title"] == "GreenLake Servers"

    @pytest.mark.asyncio
    async def test_show_devices_respects_type_for_devices_command(self):
        from proliant.com import cli

        args = argparse.Namespace(
            command="devices", type="STORAGE", fields=None, sort_by=None,
            filter_text=None, filter_model=None,
        )
        fake_session = MagicMock()
        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock,
                   return_value=fake_session), \
             patch.object(cli._servers_mod, "fetch_all_devices", new_callable=AsyncMock,
                   return_value=[]) as mock_fetch, \
             patch("proliant.com.cli.print_devices_table") as mock_print:
            await cli._cmd_show_devices(args)

        mock_fetch.assert_awaited_once_with(fake_session, device_type="STORAGE")
        _, kwargs = mock_print.call_args
        assert kwargs["title"] == "GreenLake Devices"


class TestModelNamesCompleter:
    def test_returns_empty_list_when_not_logged_in(self):
        from proliant.com.cli import _model_names_completer

        with patch("proliant.com.auth.COMSession.load", side_effect=Exception("no session")):
            assert _model_names_completer("") == []

    def test_returns_hyphenated_slugs_matching_prefix(self, tmp_path, monkeypatch):
        from proliant.com import cli
        from proliant.com.devices import Device
        from proliant.common import completers as completers_mod

        monkeypatch.setattr(completers_mod, "cache_dir", lambda: tmp_path)

        def _fake_device(model: str) -> Device:
            return Device(
                id="x", serial_number="x", product_id="x", device_type="COMPUTE",
                display_name="x", model=model, service_name="", subscription_key=None,
                tags={}, raw={},
            )

        devices = [_fake_device("DL380 GEN11"), _fake_device("DL325 GEN12")]
        fake_session = MagicMock(region="us-west")
        with patch("proliant.com.auth.COMSession.load", return_value=fake_session), \
             patch("proliant.com.devices.fetch_devices", new_callable=AsyncMock,
                   return_value=devices):
            result = cli._model_names_completer("dl380")

        assert result == ["dl380-gen11"]


class TestServerTargetsCompleter:
    """'describe' still accepts serial/iLO hostname if typed manually, but
    tab completion should only *suggest* names -- see _server_targets_completer
    docstring in com/cli.py."""

    def test_returns_empty_list_when_not_logged_in(self):
        from proliant.com.cli import _server_targets_completer

        with patch("proliant.com.auth.COMSession.load", side_effect=Exception("no session")):
            assert _server_targets_completer("") == []

    def test_suggests_name_only_not_serial_or_ilo_hostname(self, tmp_path, monkeypatch):
        from proliant.com import cli
        from proliant.common import completers as completers_mod

        monkeypatch.setattr(completers_mod, "cache_dir", lambda: tmp_path)

        fake_data = {
            "items": [
                {
                    "name": "com-team13.hol.enablement.local",
                    "hardware": {
                        "serialNumber": "2M294600BJ",
                        "bmc": {"hostname": "ILO2M294600BJ.hol.enablement.local"},
                    },
                },
            ]
        }
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_data)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)
        fake_session = MagicMock(region="us-west")

        with patch("proliant.com.auth.COMSession.load", return_value=fake_session), \
             patch("proliant.com.cli.COMClient", return_value=fake_client):
            result = cli._server_targets_completer("")

        assert result == ["com-team13.hol.enablement.local"]

    def test_falls_back_to_serial_when_server_has_no_name(self, tmp_path, monkeypatch):
        from proliant.com import cli
        from proliant.common import completers as completers_mod

        monkeypatch.setattr(completers_mod, "cache_dir", lambda: tmp_path)

        fake_data = {
            "items": [
                {
                    "name": "",
                    "hardware": {
                        "serialNumber": "TWA25325G1206",
                        "bmc": {"hostname": ""},
                    },
                },
            ]
        }
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=fake_data)
        fake_client.__aenter__ = AsyncMock(return_value=fake_client)
        fake_client.__aexit__ = AsyncMock(return_value=False)
        fake_session = MagicMock(region="us-west")

        with patch("proliant.com.auth.COMSession.load", return_value=fake_session), \
             patch("proliant.com.cli.COMClient", return_value=fake_client):
            result = cli._server_targets_completer("")

        assert result == ["TWA25325G1206"]
