"""
proliant.common.display
~~~~~~~~~~~~~~~~~~~
Shared Rich display helpers used across all proliant modules.

Provides:
  - A single shared Console instance (stderr-aware for --json mode)
  - Table factory with consistent proliant styling
  - Shared table printers (memory report, raw JSON)
  - Output mode helper (table vs json)
"""

from __future__ import annotations

import json
import sys
import threading
from enum import Enum
from typing import Any

from rich.console import Console
from rich.table import Table
from rich import box


class OutputMode(Enum):
    TABLE = "table"
    JSON = "json"


# Thread-local storage for output mode and console instance.
# Using threading.local() means each thread (test worker, parallel task)
# has its own independent output state without cross-thread pollution.
_tls = threading.local()

_utf8_stdio_done = False


def ensure_utf8_stdio() -> None:
    """Force stdout/stderr to UTF-8 so Rich glyphs never crash on Windows.

    When output is piped/redirected on Windows (a non-TTY), Rich falls back to
    the legacy console renderer which encodes with the OS code page (cp1252 on
    most systems). Glyphs used throughout the UI — ``↔``, ``✓``, ``⚠``, ``•``,
    box-drawing, etc. — have no cp1252 mapping and raise ``UnicodeEncodeError``,
    crashing the command mid-render. Reconfiguring the streams to UTF-8 with
    ``errors="replace"`` makes redirected output robust regardless of the
    console code page. Idempotent and best-effort — never raises (pytest's
    capture streams, for instance, don't support ``reconfigure``)."""
    global _utf8_stdio_done
    if _utf8_stdio_done:
        return
    _utf8_stdio_done = True
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # already detached / unsupported
            pass


def set_output_mode(mode: OutputMode) -> None:
    """Set per-thread output mode. Call early in CLI main() based on --json flag."""
    _tls.mode = mode
    _tls.console = None  # force re-creation on next get_console() call


def get_output_mode() -> OutputMode:
    return getattr(_tls, "mode", OutputMode.TABLE)


def get_console() -> Console:
    """Return the thread-local Console instance.

    In JSON mode, console writes to stderr so stdout is reserved for data.
    """
    if getattr(_tls, "console", None) is None:
        ensure_utf8_stdio()
        if get_output_mode() == OutputMode.JSON:
            _tls.console = Console(stderr=True)
        else:
            _tls.console = Console()
    return _tls.console


def make_table(
    title: str,
    *columns: tuple[str, dict[str, Any]],
    box_style=box.ROUNDED,
    **kwargs,
) -> Table:
    """Create a Rich Table with consistent proliant styling.

    Args:
        title: Table title
        columns: Tuples of (header_name, column_kwargs)
        box_style: Rich box style (default: ROUNDED)
        **kwargs: Additional Table kwargs

    Example::

        t = make_table("Servers", ("Name", {"no_wrap": True}), ("Model", {}))
    """
    defaults = {
        "show_header": True,
        "header_style": "bold cyan",
    }
    defaults.update(kwargs)
    t = Table(title=title, box=box_style, **defaults)
    for name, opts in columns:
        t.add_column(name, **opts)
    return t


def print_json(data: Any) -> None:
    """Print structured data as JSON to stdout (for piping)."""
    print(json.dumps(data, indent=2, default=str))


def print_memory_report(rows: list[dict], source: str = "") -> None:
    """Shared memory part-number breakdown table (used by ilo, com, oneview).

    Args:
        rows: Output from aggregate_by_part_number()
        source: Optional label like "iLO", "COM", "OneView"
    """
    c = get_console()

    if get_output_mode() == OutputMode.JSON:
        print_json(rows)
        return

    total_dimms = sum(r["count"] for r in rows)
    total_tb = sum(r["count"] * r["capacity_gb"] for r in rows) / 1024

    title = f"Memory Part-Number Breakdown  ({total_dimms} DIMMs  /  {total_tb:.1f} TB total)"
    if source:
        title = f"{source}: {title}"

    table = make_table(
        title,
        ("HPE Part Number", {"min_width": 14, "no_wrap": True}),
        ("Vendor",          {"min_width": 12, "no_wrap": True}),
        ("Capacity",        {"justify": "right", "no_wrap": True}),
        ("Type",            {"no_wrap": True}),
        ("Speed",           {"justify": "right", "no_wrap": True}),
        ("Count",           {"justify": "right", "no_wrap": True, "style": "bold"}),
        ("Total",           {"justify": "right", "no_wrap": True}),
        ("Servers",         {"min_width": 20, "no_wrap": False, "style": "dim"}),
    )

    for r in rows:
        cap = f"{r['capacity_gb']} GB" if r["capacity_gb"] else "—"
        speed = f"{r['speed_mts']} MT/s" if r["speed_mts"] else "—"
        total_cap_gb = r["count"] * r["capacity_gb"]
        total_cap = f"{total_cap_gb} GB" if total_cap_gb < 1024 else f"{total_cap_gb/1024:.1f} TB"
        servers_str = (
            ", ".join(sorted(r["servers"])) if isinstance(r.get("servers"), (set, list))
            else str(len(r.get("servers", [])))
        )
        table.add_row(
            r["hpe_pn"], r["vendor"], cap, r["type"], speed,
            str(r["count"]), total_cap, servers_str,
        )

    c.print(table)
