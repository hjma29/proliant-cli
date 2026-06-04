"""
pcli.common.completers
~~~~~~~~~~~~~~~~~~~~~~
Shared argcomplete helpers for tab completion across all pcli modules.
"""

from __future__ import annotations


def comma_sep_completer(choices: tuple | list):
    """Argcomplete completer for comma-separated field lists.

    Handles partial completion of the last segment after a comma::

        --fields name,ser<TAB>  →  --fields name,serial

    Usage::

        arg.completer = comma_sep_completer(("name", "serial", "model"))
    """
    def completer(prefix: str, **kwargs):
        if "," in prefix:
            before, current = prefix.rsplit(",", 1)
            before += ","
        else:
            before, current = "", prefix
        return [before + c for c in choices if c.lower().startswith(current.lower())]
    return completer
