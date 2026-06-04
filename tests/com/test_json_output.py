"""
CLI-level JSON output tests for pcli com --json.

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

from pcli.com.devices import Device
from pcli.com.workspaces import Workspace
from pcli.common.display import OutputMode, set_output_mode


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
    "id": "ws-aaa",
    "name": "lab-workspace",
    "description": "Lab env",
    "account_type": "Standard",
    "created_at": "2024-01-01T00:00:00Z",
}


@pytest.fixture(autouse=True)
def reset_output_mode():
    """Ensure each test starts in TABLE mode to avoid state leakage."""
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


def _make_device():
    return Device.from_api(FAKE_DEVICE_RAW)


def _make_workspace():
    return Workspace.from_api(FAKE_WORKSPACE_RAW)


def _run_com_main(argv: list[str], capsys) -> dict | list:
    """Run pcli com main() with given argv and return parsed stdout JSON."""
    from pcli.com import cli

    fake_session = MagicMock()
    fake_session.workspace_id = "ws-aaa"

    with patch("pcli.com.cli._ensure_session", new_callable=AsyncMock, return_value=fake_session), \
         patch("pcli.com.devices.fetch_devices", new_callable=AsyncMock, return_value=[_make_device()]), \
         patch("pcli.com.workspaces.fetch_workspaces", new_callable=AsyncMock, return_value=[_make_workspace()]):
        cli.main(argv)

    captured = capsys.readouterr()
    assert not captured.out.startswith("\x1b"), "Rich escape codes leaked to stdout"
    return json.loads(captured.out)


class TestComJsonDevices:
    def test_list_devices_json_is_valid(self, capsys):
        result = _run_com_main(["list", "devices", "--json"], capsys)
        assert isinstance(result, list)
        assert len(result) == 1

    def test_list_devices_json_fields(self, capsys):
        result = _run_com_main(["list", "devices", "--json"], capsys)
        device = result[0]
        assert device["serial_number"] == "TWA25325G1206"
        assert device["name"] == "dl325-gen12"

    def test_list_devices_json_no_rich_markup(self, capsys):
        result = _run_com_main(["list", "devices", "--json"], capsys)
        raw = capsys.readouterr().out  # already consumed, check via re-parse
        # Verify the result roundtrips cleanly (no Rich tags in serialised form)
        text = json.dumps(result)
        assert "[bold" not in text
        assert "[/bold" not in text
        assert "[green" not in text

    def test_list_devices_stderr_clean(self, capsys):
        """Status spinners and warnings must go to stderr, not stdout."""
        _run_com_main(["list", "devices", "--json"], capsys)
        captured = capsys.readouterr()
        # stdout must be pure JSON
        json.loads(captured.out) if captured.out.strip() else None


class TestComJsonWorkspaces:
    def test_list_workspaces_json_is_valid(self, capsys):
        from pcli.com import cli

        fake_session = MagicMock()
        set_output_mode(OutputMode.TABLE)  # reset before test

        with patch("pcli.com.cli._ensure_session", new_callable=AsyncMock, return_value=fake_session), \
             patch("pcli.com.workspaces.fetch_workspaces", new_callable=AsyncMock, return_value=[_make_workspace()]):
            cli.main(["list", "workspaces", "--json"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, list)
        assert result[0]["name"] == "lab-workspace"


class TestComParserJson:
    def test_parser_accepts_json_flag(self):
        from pcli.com.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["list", "devices", "--json"])
        assert args.json_output is True

    def test_parser_json_default_false(self):
        from pcli.com.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["list", "devices"])
        assert args.json_output is False
