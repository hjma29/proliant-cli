"""
pcli.com.printers
~~~~~~~~~~~~~~~~~
Table and JSON printers for COM CLI commands.

Extracted from cli.py to keep the main CLI module focused on argument parsing
and command dispatch.
"""
from __future__ import annotations

import re
from typing import Optional

from rich.table import Table
from rich import box

from pcli.common.completers import comma_sep_completer
from pcli.common.display import get_console, get_output_mode, OutputMode, print_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TIER_MAP = {
    "STANDARD_PROLIANT":   "Standard-ProLiant",
    "ENHANCED_PROLIANT":   "Enhanced-ProLiant",
    "BASIC_PROLIANT":      "Basic-ProLiant",
    "FOUNDATION_PROLIANT": "Foundation-ProLiant",
    "FOUNDATION_STORAGE":  "Foundation-Storage",
}

_TIER_SHORT_MAP = {
    "STANDARD_PROLIANT":   "Std",
    "ENHANCED_PROLIANT":   "Enh",
    "BASIC_PROLIANT":      "Bas",
    "FOUNDATION_PROLIANT": "Fnd",
    "FOUNDATION_STORAGE":  "Fnd",
}

_REGION_MAP = {
    "us-west":    "US West",
    "us-east":    "US East",
    "eu-central": "EU Central",
    "ap-northeast": "AP Northeast",
    "ap-southeast": "AP Southeast",
}


def _fmt_tier(raw: dict) -> str:
    subs = raw.get("subscription") or []
    tier = (subs[0].get("tier") or "") if subs else ""
    return _TIER_MAP.get(tier) or (tier.replace("_", "-").title() if tier else "—")


def _fmt_tier_short(raw: dict) -> str:
    subs = raw.get("subscription") or []
    tier = (subs[0].get("tier") or "") if subs else ""
    return _TIER_SHORT_MAP.get(tier) or (tier.split("_")[0].title() if tier else "—")


def _fmt_region(raw: dict) -> str:
    region = raw.get("region") or ""
    return _REGION_MAP.get(region.lower(), region) or "—"


def _fmt_device_cell(d) -> str:
    """Serial (green bold primary) + iLO hostname (white secondary)."""
    hostname = d.raw.get("deviceName") or d.raw.get("secondaryName") or ""
    if hostname:
        return f"[bold green]{d.serial_number}[/bold green]\n{hostname}"
    return f"[bold green]{d.serial_number}[/bold green]"


_UNNAMED_OS = {"host is unnamed", "unnamed", "localhost"}
_COMPUTE_TYPES = {"compute", "server"}


def _is_compute(d) -> bool:
    dtype = (d.device_type or "").lower()
    cat   = (d.raw.get("category") or "").lower()
    return dtype in _COMPUTE_TYPES or cat in _COMPUTE_TYPES


def _fmt_os_name(d) -> str:
    """OS hostname (secondaryName); — colored by device type if absent."""
    name = (d.raw.get("secondaryName") or "").strip()
    if name and name.lower() not in _UNNAMED_OS:
        return f"[cyan]{name}[/cyan]"
    return "[dim cyan]—[/dim cyan]" if _is_compute(d) else "[grey70]—[/grey70]"


def _fmt_ilo_name(d) -> str:
    """iLO hostname (deviceName); — colored by device type if absent."""
    name = d.raw.get("deviceName") or ""
    if name:
        return name
    return "[dim green]—[/dim green]" if _is_compute(d) else "[grey70]—[/grey70]"


def _fmt_service(d) -> str:
    svc    = d.service_name or ""
    region = _fmt_region(d.raw)
    if svc and region:
        return f"{svc}\n[dim]{region}[/dim]"
    if region:
        return region
    return "—"


def _strip_markup(s: str) -> str:
    """Strip Rich markup tags for sort-key comparison."""
    return re.sub(r'\[/?[^\]]*\]', '', s)


_TYPE_ABBREV = {
    "compute":    ("Comp", "cyan"),
    "server":     ("Comp", "cyan"),
    "storage":    ("Stor", "yellow"),
    "networking": ("Net",  "magenta"),
    "switch":     ("Net",  "magenta"),
    "network":    ("Net",  "magenta"),
}

_NETWORKING_TYPES = {"networking", "switch", "network"}


def _fmt_type(d) -> str:
    dtype = (d.device_type or "").lower()
    cat   = (d.raw.get("category") or "").lower()
    key   = dtype if dtype in _TYPE_ABBREV else cat
    abbrev, color = _TYPE_ABBREV.get(key, ((d.device_type or "?")[:4].title(), "white"))
    return f"[{color}]{abbrev}[/{color}]"


# ---------------------------------------------------------------------------
# Device field registry
# ---------------------------------------------------------------------------

_DEVICE_FIELDS: dict = {
    "device":   ("Device",    "default",    {"no_wrap": True, "min_width": 14, "ratio": 3},
                 lambda d, _u: _fmt_device_cell(d)),
    "os-name":  ("OS Name",   "default",    {"ratio": 4, "overflow": "fold"},
                 lambda d, _u: _fmt_os_name(d)),
    "ilo-name": ("iLO Name",  "green",      {"ratio": 4, "overflow": "fold"},
                 lambda d, _u: _fmt_ilo_name(d)),
    "name":     ("Name",      "bold cyan",  {"no_wrap": True, "ratio": 4},
                 lambda d, _u: d.display_name),
    "serial":   ("Serial",    "grey70",     {"no_wrap": True, "min_width": 13},
                 lambda d, _u: d.serial_number),
    "type":     ("Type",      "default",    {"no_wrap": True, "min_width": 4},
                 lambda d, _u: _fmt_type(d)),
    "model":    ("Model",     "grey70",     {"no_wrap": True, "max_width": 13},
                 lambda d, _u: d.model),
    "part":     ("Part #",    "grey70",     {"no_wrap": True, "min_width": 11},
                 lambda d, _u: d.product_id or "—"),
    "service":  ("Service",   "grey70",     {"no_wrap": True, "ratio": 2},
                 lambda d, _u: d.service_name or "—"),
    "region":   ("Region",    "grey70",     {"no_wrap": True, "min_width": 9},
                 lambda d, _u: _fmt_region(d.raw)),
    "tier":     ("Subscription Tier", "grey70", {"no_wrap": True, "min_width": 12},
                 lambda d, _u: _fmt_tier(d.raw)),
    "sub":      ("Sub",       "grey70",     {"no_wrap": True, "min_width": 3},
                 lambda d, _u: _fmt_tier_short(d.raw)),
    "flex":     ("Flex",      "grey70",     {"no_wrap": True, "min_width": 4},
                 lambda d, _u: "Yes" if d.raw.get("isFlex") else "No"),
    "sub-key":  ("Sub Key",   "grey70",     {"no_wrap": True, "min_width": 9, "max_width": 10},
                 lambda d, _u: (d.subscription_key[:8] + "…") if d.subscription_key else "—"),
    "location": ("Location",  "grey70",     {"no_wrap": True, "max_width": 10},
                 lambda d, _u: (d.raw.get("location") or {}).get("locationName") or "—"),
    "added":    ("Added",     "grey70",     {"no_wrap": True, "min_width": 10},
                 lambda d, _u: (d.raw.get("createdAt") or "")[:10] or "—"),
    "updated":  ("Updated",   "grey70",     {"no_wrap": True, "min_width": 10},
                 lambda d, _u: (d.raw.get("updatedAt") or "")[:10] or "—"),
    "added-by": ("Added By",  "grey70",     {"no_wrap": True, "ratio": 2},
                 lambda d, u: u.get(
                     ((d.raw.get("contact") or {}).get("workspaceUser") or {}).get("id", ""),
                     "—"
                 )),
}

_DEVICE_DEFAULT_FIELDS  = ("device", "model", "type", "service", "tier", "flex", "location")
_SERVER_DEFAULT_FIELDS  = ("serial", "os-name", "ilo-name", "model", "type", "sub", "region", "location")

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
                        user_cache: Optional[dict] = None,
                        default_fields: Optional[tuple] = None) -> None:
    if raw or get_output_mode() == OutputMode.JSON:
        print_json([d.raw for d in device_list])
        return

    if not device_list:
        get_console().print("[yellow]No devices found.[/yellow]")
        return

    effective_defaults = default_fields or _DEVICE_DEFAULT_FIELDS
    selected = parse_fields(fields, _DEVICE_FIELDS, effective_defaults)
    uc = user_cache or {}

    # Sorting — default to serial for stable ordering
    sort_key = (sort_by or "serial").lower()
    if sort_key not in _DEVICE_FIELDS:
        raise SystemExit(f"Unknown sort field: {sort_key}\nAvailable: {', '.join(_DEVICE_FIELDS)}")
    sorted_list = sorted(device_list,
                         key=lambda d: _strip_markup(_DEVICE_FIELDS[sort_key][3](d, uc)).lower())

    table = Table(
        title=f"GreenLake Devices ({len(device_list)} total)",
        box=box.SIMPLE_HEAD,
        show_lines=(effective_defaults is _DEVICE_DEFAULT_FIELDS),
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
