"""Tests for proliant.common.argparse_utils.SuggestingArgumentParser.

Covers:
  - Invalid-choice errors print the "did you mean" suggestion and the
    valid-choices list via Rich (not raw ANSI escape codes) and exit(2).
  - Rich markup produced from user-controlled/argparse-generated text is
    escaped so stray brackets in the base message can't be misinterpreted
    as (or break on) Rich markup tags.
  - No raw '\033[' escape bytes are ever written directly (regression
    guard for the legacy-Windows-console coloring bug).
"""
from __future__ import annotations

import argparse

import pytest

from proliant.common.argparse_utils import SuggestingArgumentParser


def _build_parser() -> SuggestingArgumentParser:
    parser = SuggestingArgumentParser(prog="proliant test")
    sub = parser.add_subparsers(dest="resource")
    sub.add_parser("servers")
    sub.add_parser("firmware")
    sub.add_parser("workspaces")
    return parser


class TestSuggestingArgumentParser:
    def test_invalid_choice_exits_2(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["bogus"])
        assert exc_info.value.code == 2

    def test_invalid_choice_lists_valid_choices(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["bogus"])
        captured = capsys.readouterr()
        assert "servers" in captured.err
        assert "firmware" in captured.err
        assert "workspaces" in captured.err

    def test_close_match_prints_did_you_mean(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["servrs"])  # 1-char typo, close to 'servers'
        captured = capsys.readouterr()
        assert "Did you mean" in captured.err
        assert "servers" in captured.err

    def test_no_close_match_omits_did_you_mean(self, capsys):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["zzzzzzzzzz"])
        captured = capsys.readouterr()
        assert "Did you mean" not in captured.err

    def test_no_raw_ansi_escape_bytes_leak_when_not_a_terminal(self, capsys):
        """Regression guard: hand-written '\\033[33m' codes used to be
        written directly to stderr regardless of whether it was a real
        terminal. Rich auto-disables color on a non-tty stream (like
        pytest's captured stderr), so no raw escape bytes should appear."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["bogus"])
        captured = capsys.readouterr()
        assert "\033[" not in captured.err

    def test_stray_brackets_in_message_do_not_crash_or_leak_as_markup(self, capsys):
        """A required-argument style error (no 'invalid choice' match) can
        contain argparse's own bracketed usage syntax -- must be escaped,
        not interpreted as Rich markup tags."""
        parser = SuggestingArgumentParser(prog="proliant test")
        parser.add_argument("--foo", required=True)
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args([])
        assert exc_info.value.code == 2
        captured = capsys.readouterr()
        assert "--foo" in captured.err
