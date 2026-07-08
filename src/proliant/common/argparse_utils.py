"""
proliant.common.argparse_utils
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared argparse helpers for the ilo/com/oneview sub-CLIs.

SuggestingArgumentParser renders its "invalid choice" errors through Rich
instead of hand-written ANSI escape codes (``"\033[33m...\033[0m"``).
Two earlier per-module copies of this class wrote raw escape sequences
directly to stderr, which has two problems a plain Console.print() doesn't:

  1. On Windows consoles with no VT/ANSI processing active (some legacy
     conhost configurations), raw escape codes render as nothing/garbled
     text instead of color. Rich detects this and falls back to the Win32
     console API (SetConsoleTextAttribute) for color, which is the same
     path already proven to work for every other colored message in the
     app (e.g. the setup wizard's "Setup complete!").
  2. Raw escape codes are written unconditionally, even when stderr is
     piped/redirected (not a real terminal) — leaking literal
     ``\x1b[33m`` bytes into logs/files. Rich auto-detects a non-tty
     stderr and disables color automatically.
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys

from rich.console import Console
from rich.markup import escape


class SuggestingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that suggests close matches on invalid choice errors."""

    def error(self, message: str) -> None:
        console = Console(stderr=True)
        suggestion = None
        match = re.search(r"(invalid choice: '[^']+') \(choose from ([^)]+)\)", message)
        if match:
            bad_part = match.group(1)
            choices_str = match.group(2)
            choices = [c.strip().strip("'") for c in choices_str.split(",")]
            close = difflib.get_close_matches(
                re.search(r"'([^']+)'", bad_part).group(1), choices, n=1, cutoff=0.6
            )
            if close:
                suggestion = close[0]
            colored_choices = ", ".join(f"[yellow]{escape(c)}[/yellow]" for c in choices)
            message = re.sub(
                r"\(choose from [^)]+\)",
                f"(choose from {colored_choices})",
                escape(message),
            )
        else:
            message = escape(message)
        if suggestion:
            console.print(f"\n[bold green]  Did you mean: '{escape(suggestion)}'?[/bold green]\n")
        self.print_usage(sys.stderr)
        console.print(f"{escape(self.prog)}: error: {message}")
        sys.exit(2)
