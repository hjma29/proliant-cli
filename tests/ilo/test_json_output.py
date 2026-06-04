"""
CLI-level JSON output tests for pcli ilo --json.

Tests that --json emits clean, parseable stdout JSON and that
the parser correctly wires up json_output.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pcli.common.display import OutputMode, set_output_mode


FAKE_HOST = {"name": "dl325-gen12", "host": "192.168.1.10", "username": "admin", "password": "pass"}


@pytest.fixture(autouse=True)
def reset_output_mode():
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


class TestIloParserJson:
    def test_parser_json_flag_on_list(self):
        from pcli.ilo.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "list", "ilo"])
        assert args.json_output is True

    def test_parser_json_default_false(self):
        from pcli.ilo.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["list", "ilo"])
        assert args.json_output is False

    def test_parser_json_flag_list_firmwares(self):
        from pcli.ilo.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "list", "firmwares"])
        assert args.json_output is True


class TestIloJsonOutput:
    def test_list_ilo_json_is_valid(self, capsys):
        from pcli.ilo import cli

        fake_ilo_data = [("iLO Version", "iLO 5 v2.78")]
        fake_results = [(FAKE_HOST["name"], None, fake_ilo_data)]

        with patch("pcli.ilo.cli._load_hosts_or_exit", return_value=[FAKE_HOST]), \
             patch("pcli.ilo.cli._run_parallel_async", new_callable=AsyncMock, return_value=fake_results):
            cli.main(["--json", "list", "ilo", "--host", "dl325-gen12"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, list)
        assert result[0]["Server"] == "dl325-gen12"

    def test_list_firmwares_json_is_valid(self, capsys):
        from pcli.ilo import cli

        fake_fw_data = [("System ROM", "U46 v2.82"), ("iLO 5", "2.78")]
        fake_results = [(FAKE_HOST["name"], None, fake_fw_data)]

        with patch("pcli.ilo.cli._load_hosts_or_exit", return_value=[FAKE_HOST]), \
             patch("pcli.ilo.cli._run_parallel_async", new_callable=AsyncMock, return_value=fake_results):
            cli.main(["--json", "list", "firmwares", "--host", "dl325-gen12"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, list)
        assert result[0]["Server"] == "dl325-gen12"
        assert result[0]["System ROM"] == "U46 v2.82"

    def test_list_network_json_includes_location(self, capsys):
        from pcli.ilo import cli

        fake_network_data = [
            {
                "Name": "Broadcom P225p NetXtreme-E Dual-port 10Gb/25Gb Ethernet PCIe Adapter - NIC",
                "Version": "235.1.164.14",
                "Location": "PCIE Slot 6",
            }
        ]
        fake_results = [(FAKE_HOST["name"], None, fake_network_data)]

        with patch("pcli.ilo.cli._load_hosts_or_exit", return_value=[FAKE_HOST]), \
             patch("pcli.ilo.cli._run_parallel_async", new_callable=AsyncMock, return_value=fake_results):
            cli.main(["--json", "list", "network", "--host", "dl325-gen12"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result == [
            {
                "Server": "dl325-gen12",
                "Name": "Broadcom P225p NetXtreme-E Dual-port 10Gb/25Gb Ethernet PCIe Adapter - NIC",
                "Version": "235.1.164.14",
                "Location": "PCIE Slot 6",
            }
        ]

    def test_json_error_host_included(self, capsys):
        """Errors for a host are serialised as {"Server": ..., "error": ...}."""
        from pcli.ilo import cli

        fake_results = [(FAKE_HOST["name"], "connection refused", [])]

        with patch("pcli.ilo.cli._load_hosts_or_exit", return_value=[FAKE_HOST]), \
             patch("pcli.ilo.cli._run_parallel_async", new_callable=AsyncMock, return_value=fake_results):
            cli.main(["--json", "list", "ilo", "--host", "dl325-gen12"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result[0]["error"] == "connection refused"

    def test_json_no_rich_markup_in_stdout(self, capsys):
        from pcli.ilo import cli

        fake_results = [(FAKE_HOST["name"], None, [("iLO Version", "iLO 5 v2.78")])]

        with patch("pcli.ilo.cli._load_hosts_or_exit", return_value=[FAKE_HOST]), \
             patch("pcli.ilo.cli._run_parallel_async", new_callable=AsyncMock, return_value=fake_results):
            cli.main(["--json", "list", "ilo", "--host", "dl325-gen12"])

        captured = capsys.readouterr()
        assert "[bold" not in captured.out
        assert "[green" not in captured.out
        assert "\x1b[" not in captured.out
