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
        ("Vendor P/N",      {"min_width": 14, "no_wrap": True, "style": "dim"}),
        ("Vendor",          {"min_width": 12, "no_wrap": True}),
        ("Capacity",        {"justify": "right", "no_wrap": True}),
        ("Type",            {"no_wrap": True}),
        ("Speed",           {"justify": "right", "no_wrap": True}),
        ("Count",           {"justify": "right", "no_wrap": True, "style": "bold"}),
        ("Total",           {"justify": "right", "no_wrap": True}),
        ("Status",          {"no_wrap": False}),
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
        attention = r.get("attention_statuses")
        if attention:
            status_str = (
                f"[bold red]⚠ {', '.join(sorted(attention))}[/bold red]\n"
                f"[dim]({', '.join(sorted(r.get('attention_servers', ())))})[/dim]"
            )
        else:
            status_str = "[dim]OK[/dim]"
        table.add_row(
            r["hpe_pn"], r.get("vendor_pn") or "—", r["vendor"], cap, r["type"], speed,
            str(r["count"]), total_cap, status_str, servers_str,
        )

    c.print(table)


def health_style(v: str | None) -> str:
    """Rich-styled health status string (green OK / yellow Warning / red
    Critical), used across ilo/oneview describe output."""
    styles = {"OK": "green", "Warning": "yellow", "Critical": "red"}
    s = styles.get(v or "", "")
    return f"[{s}]{v}[/{s}]" if s else (v or "—")


def print_storage_report(console: Console, storage_report: list[dict]) -> None:
    """Render the per-controller / per-disk storage report.

    Shared by `proliant ilo storage describe`/`servers describe` and
    `proliant oneview storage describe`/`servers describe` — both feed it
    the same report shape produced by
    proliant.common.storage_report.classify_storage_resource:
      [{"controller": ..., "firmware": ..., "attach_mode": ..., "drives": [...]}]
    """
    if not storage_report:
        console.print("[dim]No storage controllers/drives found.[/dim]")
        return

    total_disks = sum(len(ctrl["drives"]) for ctrl in storage_report)
    ctrl_count = sum(1 for c in storage_report if c["attach_mode"] == "RAID Controller")
    console.print(
        f"[bold]Storage[/bold]   "
        f"[dim]{ctrl_count} controller(s), {total_disks} disk(s)[/dim]"
    )
    for ctrl in storage_report:
        is_raid = ctrl["attach_mode"] == "RAID Controller"
        if is_raid:
            console.print(
                f"  [bold]{ctrl['controller']}[/bold]  "
                f"[dim]fw {ctrl['firmware']}[/dim]  "
                f"[green]{ctrl['attach_mode']}[/green]  "
                f"[dim]({len(ctrl['drives'])} disk(s))[/dim]"
            )
        else:
            # Direct-attached: there is no real controller to name — the
            # embedded Storage resource's Controllers entry (if any)
            # describes the drive's own controller chip, not a RAID/HBA
            # product, so it's intentionally omitted here.
            console.print(
                f"  [yellow]{ctrl['attach_mode']}[/yellow]  "
                f"[dim]({len(ctrl['drives'])} disk(s))[/dim]"
            )
        disk_t = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        disk_t.add_column("Disk ID", no_wrap=True)
        disk_t.add_column("Bay", no_wrap=True)
        disk_t.add_column("Capacity", justify="right")
        disk_t.add_column("Media", no_wrap=True)
        disk_t.add_column("Protocol", no_wrap=True)
        disk_t.add_column("Model", style="dim")
        disk_t.add_column("Serial", style="dim")
        disk_t.add_column("Firmware", style="dim")
        disk_t.add_column("Health")
        for d in ctrl["drives"]:
            cap_str = f"{d['capacity_gb']} GB" if d["capacity_gb"] else "—"
            disk_t.add_row(
                str(d["id"]), d["bay"], cap_str, d["media_type"], d["protocol"],
                d["model"], d["serial"], d["firmware"], health_style(d["health"]),
            )
        console.print(disk_t)


def print_storage_list_table(results: list[tuple[str, str | None, list[tuple[str, str]]]]) -> None:
    """Plain fixed-width `storage list` table shared by `ilo`, `oneview`,
    and `com` -- Server | Storage Controller | RAID Attached |
    Direct Attached. Deliberately plain text (no Rich box/title)
    to match the original `proliant ilo storage list` style and keep the
    three modules' output visually identical.

    A cell value may contain embedded '\\n' when a server has more than
    one storage controller (proliant.common.storage_report.summarize_
    storage_for_list emits one line per controller) -- those are rendered
    as aligned continuation lines under the same server row.
    """
    keys = ("Storage Controller", "RAID Attached", "Direct Attached")
    server_data: dict[str, dict[str, str]] = {}
    errors: dict[str, str] = {}
    for host_name, error, rows in results:
        if error:
            server_data[host_name] = {k: "ERROR" for k in keys}
            errors[host_name] = error
        else:
            server_data[host_name] = dict(rows)

    if not server_data:
        print("No servers found.")
        return

    srv_w = max(len("Server"), max(len(n) for n in server_data))
    col_w: dict[str, int] = {k: len(k) for k in keys}
    for vals in server_data.values():
        for key in keys:
            for line in vals.get(key, "N/A").split("\n"):
                col_w[key] = max(col_w[key], len(line))

    header = f"{'Server':<{srv_w}}" + "".join(f"   {key:<{col_w[key]}}" for key in keys)
    print(header)
    print("-" * len(header))
    for host_name in sorted(server_data):
        vals = server_data[host_name]
        col_lines = {key: vals.get(key, "N/A").split("\n") for key in keys}
        nlines = max(len(lines) for lines in col_lines.values())
        for i in range(nlines):
            name_field = host_name if i == 0 else ""
            row = f"{name_field:<{srv_w}}" + "".join(
                f"   {(col_lines[key][i] if i < len(col_lines[key]) else ''):<{col_w[key]}}"
                for key in keys
            )
            print(row)
        if host_name in errors:
            print(f"  {'':>{srv_w}}   {errors[host_name]}")

