"""
CLI-level JSON output tests for pcli qs --json.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from pcli.common.display import OutputMode, set_output_mode
from pcli.qs.client import QSEntry


@pytest.fixture(autouse=True)
def reset_output_mode():
    set_output_mode(OutputMode.TABLE)
    yield
    set_output_mode(OutputMode.TABLE)


FAKE_ENTRY = QSEntry(
    doc_id="a00073551enw",
    title="HPE ProLiant DL380 Gen12 QuickSpecs",
    version="11",
    last_modified="05/13/2026 00:00:00.000",
)

FAKE_MARKDOWN = """# HPE ProLiant DL380 Gen12 QuickSpecs

## Summary of Changes

| Date | Version | Action | Description of Change |
|------|---------|--------|----------------------|
| April 2024 | 11 | Updated | Added Gen12 CPUs |
| Jan 2024 | 10 | Updated | Updated memory configs |

## Overview

Some overview text here.
"""

FAKE_SECTIONS = ["Summary of Changes", "Overview"]


class TestQsParserJson:
    def test_parser_accepts_json_flag(self):
        from pcli.qs.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "list", "--model", "dl380gen12"])
        assert args.json_output is True

    def test_parser_json_default_false(self):
        from pcli.qs.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["list", "--model", "dl380gen12"])
        assert args.json_output is False

    def test_parser_json_on_describe(self):
        from pcli.qs.cli import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--json", "describe", "a00073551enw"])
        assert args.json_output is True


class TestQsListJson:
    def test_list_json_is_valid(self, capsys):
        from pcli.qs import cli

        with patch("pcli.qs.cli.search_quickspecs", return_value=[FAKE_ENTRY]), \
             patch("pcli.qs.cli.fetch_quickspec_markdown", return_value=(FAKE_MARKDOWN, FAKE_SECTIONS)):
            cli.main(["--json", "list", "--model", "dl380gen12"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert isinstance(result, dict)
        assert result["doc_id"] == "a00073551enw"
        assert "revisions" in result

    def test_list_json_revisions_structure(self, capsys):
        from pcli.qs import cli

        with patch("pcli.qs.cli.search_quickspecs", return_value=[FAKE_ENTRY]), \
             patch("pcli.qs.cli.fetch_quickspec_markdown", return_value=(FAKE_MARKDOWN, FAKE_SECTIONS)):
            cli.main(["--json", "list", "--model", "dl380gen12"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        revisions = result["revisions"]
        assert isinstance(revisions, list)
        if revisions:
            rev = revisions[0]
            assert "date" in rev
            assert "version" in rev
            assert "action" in rev
            assert "description" in rev

    def test_list_json_no_rich_markup(self, capsys):
        from pcli.qs import cli

        with patch("pcli.qs.cli.search_quickspecs", return_value=[FAKE_ENTRY]), \
             patch("pcli.qs.cli.fetch_quickspec_markdown", return_value=(FAKE_MARKDOWN, FAKE_SECTIONS)):
            cli.main(["--json", "list", "--model", "dl380gen12"])

        captured = capsys.readouterr()
        assert "[bold" not in captured.out
        assert "\x1b[" not in captured.out


class TestQsDescribeJson:
    def test_describe_json_is_valid(self, capsys):
        from pcli.qs import cli

        with patch("pcli.qs.cli.fetch_quickspec_markdown", return_value=(FAKE_MARKDOWN, FAKE_SECTIONS)):
            cli.main(["--json", "describe", "a00073551enw"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert result["doc_id"] == "a00073551enw"
        assert "sections" in result
        assert isinstance(result["sections"], dict)

    def test_describe_json_sections_populated(self, capsys):
        from pcli.qs import cli

        with patch("pcli.qs.cli.fetch_quickspec_markdown", return_value=(FAKE_MARKDOWN, FAKE_SECTIONS)):
            cli.main(["--json", "describe", "a00073551enw"])

        captured = capsys.readouterr()
        result = json.loads(captured.out)
        assert "Summary of Changes" in result["sections"]
        assert "Overview" in result["sections"]
