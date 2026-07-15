"""
proliant.common.completers
~~~~~~~~~~~~~~~~~~~~~~
Shared argcomplete helpers for tab completion across all proliant modules.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from argcomplete.completers import FilesCompleter

from proliant.common import cache_dir

_COMPLETION_CACHE_TTL = 20.0  # seconds


def file_completion():
    """Argcomplete completer for arguments that intentionally accept file paths."""
    return FilesCompleter()


def suppress_file_completion():
    """Argcomplete completer for free-form values that should not list files.

    Deliberately does NOT use argcomplete's own SuppressCompleter: argcomplete
    treats a SuppressCompleter as "hide this option from '--<TAB>' flag-name
    completion entirely", not just "don't complete its value" (see
    argcomplete.finders.ArgcompleteFinder._get_option_completions, which skips
    any action whose completer is a SuppressCompleter that suppresses). That
    made every flag using this helper invisible to tab completion outright
    (e.g. `--concurrency` never appeared even in `--<TAB>` listings). A plain
    completer that just returns no candidates achieves the intended effect --
    no file-path fallback for the value -- without hiding the flag itself.
    """
    return lambda **kwargs: []


def cached_names(cache_key: str, fetch_fn: Callable[[], list[str]],
                  ttl: float = _COMPLETION_CACHE_TTL) -> list[str]:
    """Return a list of names for tab completion, cached briefly on disk.

    Tab completion re-invokes the whole CLI process on every keystroke, so a
    completer that makes a live API call (an OneView/iLO login handshake, a
    COM API fetch) pays that full network round trip again on every single
    TAB press while the user is mid-command -- often a couple of seconds
    each time. Cache the *unfiltered* name list under a small JSON file for
    `ttl` seconds so repeated TAB presses in the same typing burst are
    instant; results still refresh automatically shortly after, so newly
    added/removed objects show up within `ttl` seconds of the next attempt.

    `fetch_fn` must return the full list of candidate names -- do not
    pre-filter by the current prefix, since a later (or shorter, if the user
    backspaces) prefix during the same cache window still needs access to
    the complete list.
    """
    cache_file = cache_dir() / "completions" / f"{cache_key}.json"
    try:
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - data.get("ts", 0) < ttl:
                return data.get("names", [])
    except Exception:
        pass  # corrupt/unreadable cache -- fall through to a fresh fetch

    names = fetch_fn()

    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"ts": time.time(), "names": names}), encoding="utf-8"
        )
    except Exception:
        pass  # completion must never fail because the cache couldn't be written

    return names


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
