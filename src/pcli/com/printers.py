"""
pcli.com.printers
~~~~~~~~~~~~~~~~~
Table and JSON printers for COM CLI commands.

Extracted from cli.py to keep the main CLI module focused on argument parsing
and command dispatch.
"""
from __future__ import annotations

from typing import Optional

from rich.table import Table
from rich import box

from pcli.common.completers import comma_sep_completer
from pcli.common.display import get_console, get_output_mode, OutputMode, print_json


# ---------------------------------------------------------------------------
# Device field registry
# ---------------------------------------------------------------------------

_DEVICE_FIELDS: dict = {
    "name":     ("Name",     "bold cyan", {"no_wrap": True, "ratio": 4},
                 lambda d, _u: d.display_name),
    "ilo-name": ("iLO Name", "cyan",      {"no_wrap": True, "ratio": 3},
                 lambda d, _u: d.raw.get("deviceName") or d.raw.get("secondaryName") or "—"),
    "type":     ("Type",     "dim",       {"no_wrap": True, "min_width": 7, "max_width": 8},
                 lambda d, _u: d.device_type),
    "model":    ("Model",    "white",     {"no_wrap": True, "ratio": 2},
                 lambda d, _u: d.model),
    "serial":   ("Serial",   "green",     {"no_wrap": True, "min_width": 13},
                 lambda d, _u: d.serial_number),
    "part":     ("Part #",   "dim",       {"no_wrap": True, "min_width": 11},
                 lambda d, _u: d.product_id or "—"),
    "service":  ("Service",  "yellow",    {"no_wrap": True, "ratio": 2},
                 lambda d, _u: d.service_name or "—"),
    "sub-key":  ("Sub Key",  "dim",       {"no_wrap": True, "min_width": 9, "max_width": 10},
                 lambda d, _u: (d.subscription_key[:8] + "…") if d.subscription_key else "—"),
    "location": ("Location", "dim",       {"no_wrap": True, "ratio": 2},
                 lambda d, _u: (d.raw.get("location") or {}).get("locationName") or "—"),
    "added":    ("Added",    "dim",       {"no_wrap": True, "min_width": 10},
                 lambda d, _u: (d.raw.get("createdAt") or "")[:10] or "—"),
    "updated":  ("Updated",  "dim",       {"no_wrap": True, "min_width": 10},
                 lambda d, _u: (d.raw.get("updatedAt") or "")[:10] or "—"),
    "added-by": ("Added By", "dim",       {"no_wrap": True, "ratio": 2},
                 lambda d, u: u.get(
                     ((d.raw.get("contact") or {}).get("workspaceUser") or {}).get("id", ""),
                     "—"
                 )),
}

_DEVICE_DEFAULT_FIELDS = ("name", "type", "model", "serial", "service", "sub-key")

DEVICE_FIELD_NAMES = tuple(_DEVICE_FIELDS.keys())


def make_field_completer(choices: tuple):
    """Argcomplete completer for comma-separated field lists like 'name,ser<TAB>'."""
    return comma_sep_completer(choices)


def parse_fields(fields_str: Optional[str], available: dict, defaults: tuple) -> list[str]:
    """Parse a comma-separated --fields string into a validated list of field keys."""
    if not fields_str:
        return list(defaults)
    keys = [f.strip().lower() for f in fields_str.split(",") if f.strip()]
    bad = [k for k in keys if k not in available]
    if bad:
        valid = ", ".join(available.keys())
        raise SystemExit(f"Unknown field(s): {', '.join(bad)}\nAvailable: {valid}")
    return keys


# ---------------------------------------------------------------------------
# Table printers
# ---------------------------------------------------------------------------

def print_devices_table(device_list: list, raw: bool = False,
                        fields: Optional[str] = None,
                        sort_by: Optional[str] = None,
                        user_cache: Optional[dict] = None) -> None:
    if raw or get_output_mode() == OutputMode.JSON:
        print_json([d.raw for d in device_list])
        return

    if not device_list:
        get_console().print("[yellow]No devices found.[/yellow]")
        return

    selected = parse_fields(fields, _DEVICE_FIELDS, _DEVICE_DEFAULT_FIELDS)
    uc = user_cache or {}

    # Sorting
    sort_key = (sort_by or "name").lower()
    if sort_key not in _DEVICE_FIELDS:
        raise SystemExit(f"Unknown sort field: {sort_key}\nAvailable: {', '.join(_DEVICE_FIELDS)}")
    sorted_list = sorted(device_list,
                         key=lambda d: _DEVICE_FIELDS[sort_key][3](d, uc).lower())

    table = Table(
        title=f"GreenLake Devices ({len(device_list)} total)",
        box=box.ROUNDED,
        show_lines=False,
        expand=True,
    )
    for key in selected:
        header, style, kwargs, _ = _DEVICE_FIELDS[key]
        table.add_column(header, style=style, **kwargs)

    for d in sorted_list:
        table.add_row(*[_DEVICE_FIELDS[key][3](d, uc) for key in selected])

    get_console().print(table)


def print_workspaces_table(workspace_list: list, raw: bool = False) -> None:
    if raw or get_output_mode() == OutputMode.JSON:
        print_json([w.raw for w in workspace_list])
        return

    if not workspace_list:
        get_console().print("[yellow]No workspaces found.[/yellow]")
        return

    table = Table(
        title=f"GreenLake Workspaces ({len(workspace_list)} total)",
        box=box.ROUNDED,
        show_lines=False,
    )
    table.add_column("",           style="bold green", no_wrap=True)  # active marker
    table.add_column("Name",       style="bold cyan",  no_wrap=True)
    table.add_column("ID",         style="dim")
    table.add_column("Region",     style="green")
    table.add_column("Status",     style="yellow")
    table.add_column("Location",   style="white")
    table.add_column("Description", style="dim")

    for w in workspace_list:
        status_color = "green" if w.status == "ACTIVE" else "yellow"
        table.add_row(
            "* " if w.active else "  ",
            w.name,
            w.id,
            w.region,
            f"[{status_color}]{w.status}[/{status_color}]",
            w.address,
            w.description or "—",
        )

    get_console().print(table)
    get_console().print("[dim]  * = active workspace[/dim]")


def print_bundles_table(bundle_list: list, raw: bool = False) -> None:
    if raw or get_output_mode() == OutputMode.JSON:
        print_json([b.raw for b in bundle_list])
        return

    if not bundle_list:
        get_console().print("[yellow]No bundles found.[/yellow]")
        return

    table = Table(
        title=f"COM SPP Firmware Bundles ({len(bundle_list)} shown)",
        box=box.ROUNDED,
        show_lines=False,
        expand=True,
    )
    table.add_column("Gen",     style="bold cyan",  no_wrap=True, min_width=8)
    table.add_column("Type",    style="dim",        no_wrap=True, min_width=6)
    table.add_column("Version", style="white",      no_wrap=True, min_width=14)
    table.add_column("Release", style="dim",        no_wrap=True, min_width=10)
    table.add_column("Active",  style="green",      no_wrap=True, min_width=6)
    table.add_column("Display Name", style="white", no_wrap=True, ratio=2)

    for b in bundle_list:
        active_str = "[green]✓[/green]" if b.is_active else "[dim]—[/dim]"
        table.add_row(
            b.generation,
            b.bundle_type,
            b.release_version,
            b.release_date,
            active_str,
            b.display_name,
        )

    get_console().print(table)
