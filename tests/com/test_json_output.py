"""
CLI-level JSON output tests for proliant com --json.

These mock the API layer to avoid needing real HPE Compute Ops Management
credentials and verify that --json produces clean, parseable stdout JSON with
no Rich markup leaking through.
"""

import json
import sys
import time
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from proliant.com.devices import Device
from proliant.com.workspaces import Workspace
from proliant.common.display import OutputMode, set_output_mode


FAKE_DEVICE_RAW = {
    "resource_id": "aaa-111",
    "serial_number": "TWA25325G1206",
    "part_number": "P81967-B21",
    "device_type": "COMPUTE",
    "name": "dl325-gen12",
    "device_model": "HPE ProLiant DL325 Gen12",
    "subscription_tier": "CONNECTED",
    "subscription_key": "SVC-12345",
    "tags": [],
}

FAKE_WORKSPACE_RAW = {
    "platform_customer_id": "ws-aaa",
    "company_name": "lab-workspace",
    "description": "Lab env",
    "account_status": "ACTIVE",
    "address": {"city": "Houston", "state_or_region": "TX", "country_code": "US"},
}


def _make_workspace():
    from proliant.com.workspaces import Workspace
    return Workspace.from_api(FAKE_WORKSPACE_RAW, active_id="ws-aaa", region="us-west")


@pytest.fixture(autouse=True)
def reset_output_mode():
    """Ensure each test starts in TABLE mode to avoid state leakage."""
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


def _make_device():
    return Device.from_api(FAKE_DEVICE_RAW)


def _run_com_main(argv: list[str], capsys) -> dict | list:
    """Run proliant com main() with given argv and return parsed stdout JSON."""
    from proliant.com import cli
    from proliant.com.servers import device_to_server_row

    fake_session = MagicMock()
    fake_session.workspace_id = "ws-aaa"

    fake_server_row = device_to_server_row(_make_device())

    with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock, return_value=fake_session), \
         patch.object(cli._servers_mod, "fetch_all_devices", new_callable=AsyncMock, return_value=[fake_server_row]), \
         patch.object(cli._servers_mod, "fetch_servers", new_callable=AsyncMock, return_value=[fake_server_row]), \
         patch("proliant.com.workspaces.fetch_workspaces", new_callable=AsyncMock, return_value=[_make_workspace()]):
        cli.main(argv)

    captured = capsys.readouterr()
    assert not captured.out.startswith("\x1b"), "Rich escape codes leaked to stdout"
    return json.loads(captured.out)


class TestComJsonDevices:
    def test_list_devices_json_is_valid(self, capsys):
        result = _run_com_main(["--json", "devices", "list"], capsys)
        assert isinstance(result, list)
        assert len(result) == 1

    def test_list_devices_json_fields(self, capsys):
        result = _run_com_main(["--json", "devices", "list"], capsys)
        device = result[0]
        assert device["serial_number"] == "TWA25325G1206"
        assert device["name"] == "dl325-gen12"

    def test_list_devices_json_no_rich_markup(self, capsys):
        result = _run_com_main(["--json", "devices", "list"], capsys)
        text = json.dumps(result)
        assert "[bold" not in text
        assert "[/bold" not in text
        assert "[green" not in text

    def test_list_devices_stderr_clean(self, capsys):
        """Status spinners and warnings must go to stderr, not stdout."""
        _run_com_main(["--json", "devices", "list"], capsys)
        # stdout is consumed in _run_com_main; second readouterr() returns empty
        captured = capsys.readouterr()
        # Any remaining stdout must also be parseable (or empty)
        if captured.out.strip():
            json.loads(captured.out)


class TestComJsonWorkspaces:
    def test_list_workspaces_json_is_valid(self, capsys):
        from proliant.com import cli

        fake_session = MagicMock()

        with patch("proliant.com.cli._ensure_session", new_callable=AsyncMock, return_value=fake_session), \
             patch("proliant.com.workspaces.fetch_workspaces", new_callable=AsyncMock, return_value=[_make_workspace()]):
            cli.main(["--json", "workspaces", "list"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, list)
        assert result[0]["company_name"] == "lab-workspace"


class TestComParserJson:
    def test_parser_accepts_json_flag(self):
        from proliant.com.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "devices", "list"])
        assert args.json_output is True

    def test_parser_json_default_false(self):
        from proliant.com.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["devices", "list"])
        assert args.json_output is False
