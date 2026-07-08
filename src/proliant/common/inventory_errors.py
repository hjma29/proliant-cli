"""
proliant.common.inventory_errors
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Turns a raw configparser.Error into an actionable, user-facing message.

Used by ilo/config.py, oneview/config.py, and setup/wizard.py so a broken
inventory.ini (a duplicate key, a line outside any [section], bad
indentation, ...) always produces the same clear explanation -- with a link
to a working example -- instead of a raw Python traceback, no matter which
of those three places happens to parse the file first.
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path

SAMPLE_INVENTORY_URL = "https://github.com/hjma29/proliant-cli/blob/main/sample-inventory.ini"


def format_inventory_parse_error(exc: configparser.Error, path: Path) -> str:
    """Build a friendly, multi-line explanation for a malformed inventory.ini."""
    line_hint = ""
    m = re.search(r"\[line (\d+)\]", str(exc))
    if m:
        line_hint = f" (line {m.group(1)})"

    return (
        f"{path} is not in the right format{line_hint}:\n"
        f"  {exc}\n\n"
        f"  Common causes: a key=value line outside any [section], or the\n"
        f"  same key repeated twice within one section.\n\n"
        f"  See a working example: {SAMPLE_INVENTORY_URL}"
    )
