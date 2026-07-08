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
from pathlib import Path

SAMPLE_INVENTORY_URL = "https://github.com/hjma29/proliant-cli/blob/main/sample-inventory.ini"


def format_inventory_parse_error(exc: configparser.Error, path: Path) -> str:
    """Build a short, non-alarming explanation for a malformed inventory.ini.

    Deliberately omits the raw exception text and line number -- those read
    as scary low-level detail to non-technical users and aren't actionable
    on their own. Pointing at a working sample file is enough to fix it.
    """
    return (
        f"{path} is not in the right format.\n\n"
        f"  See a working example: {SAMPLE_INVENTORY_URL}"
    )
