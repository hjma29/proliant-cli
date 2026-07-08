"""
proliant.com.printers
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

from proliant.common.completers import comma_sep_completer
from proliant.common.display import get_console, get_output_mode, OutputMode, print_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REGION_MAP = {
    "us-west":    "US West",
    "us-east":    "US East",
    "eu-central": "EU Central",
    "ap-northeast": "AP Northeast",
    "ap-southeast": "AP Southeast",
}


def _strip_markup(s: str) -> str:
    """Strip Rich markup tags for sort-key comparison."""
    return re.sub(r'\[/?[^\]]*\]', '', s)


# ---------------------------------------------------------------------------
# Server/device field registry
#
# Both 'com servers list' (COM's own /servers inventory, compute-only) and
# 'com devices list' (that same inventory plus GreenLake-claimed storage/
# network devices, adapted via servers.device_to_server_row) render through
# this one registry, operating on proliant.com.servers.Server rows. Fields
# that don't apply to a given row (e.g. Health/Baseline for a switch) simply
# render "—" rather than being guessed at.
# ---------------------------------------------------------------------------

_HEALTH_COLOR = {
    "OK": "green", "WARNING": "yellow", "CRITICAL": "red", "DISABLED": "dim",
}


def _fmt_health(v: str) -> str:
    color = _HEALTH_COLOR.get((v or "").upper())
    if not color or v in ("—", None):
        return f"[dim]{v or '—'}[/dim]"
    return f"[{color}]{v.title()}[/{color}]"


_STATE_COLOR = {
    "Connected": "green", "Not connected": "yellow", "Not activated": "red",
}


def _fmt_state(v: str) -> str:
    color = _STATE_COLOR.get(v, "")
    return f"[{color}]{v}[/{color}]" if color else (v or "—")


def _fmt_power(v: str) -> str:
    label = {"ON": "On", "OFF": "Off"}.get((v or "").upper(), v or "—")
    color = {"ON": "green", "OFF": "red"}.get((v or "").upper(), "dim")
    return f"[{color}]{label}[/{color}]"


_TYPE_ABBREV = {
    "compute": ("Comp", "cyan"), "storage": ("Stor", "yellow"),
    "switch":  ("Net",  "magenta"), "network": ("Net", "magenta"),
}


def _fmt_device_type(v: str) -> str:
    abbrev, color = _TYPE_ABBREV.get((v or "").lower(), ((v or "?")[:4].title(), "white"))
    return f"[{color}]{abbrev}[/{color}]"


def _fmt_yesno(v: bool) -> str:
    return "Yes" if v else "No"


_SERVER_FIELDS: dict = {
    "health":       ("Health",       "default", {"no_wrap": True, "min_width": 8},
                     lambda s: _fmt_health(s.health)),
    "name":         ("Name",         "bold cyan", {"no_wrap": True, "ratio": 3},
                     lambda s: s.name),
    "state":        ("State",        "default", {"no_wrap": True, "min_width": 13},
                     lambda s: _fmt_state(s.state_label)),
    "serial":       ("Serial",       "grey70",  {"no_wrap": True, "min_width": 13},
                     lambda s: s.serial_number or "—"),
    "type":         ("Type",         "default", {"no_wrap": True, "min_width": 4},
                     lambda s: _fmt_device_type(s.device_type)),
    "group":        ("Group",        "grey70",  {"no_wrap": True, "max_width": 18},
                     lambda s: s.group),
    "power":        ("Power",        "default", {"no_wrap": True, "min_width": 5},
                     lambda s: _fmt_power(s.power_state)),
    "baseline":     ("Baseline",     "grey70",  {"no_wrap": True, "min_width": 12},
                     lambda s: s.baseline),
    "model":        ("Model",        "grey70",  {"no_wrap": True, "max_width": 22},
                     lambda s: s.model),
    "generation":   ("Generation",   "grey70",  {"no_wrap": True, "min_width": 8},
                     lambda s: s.generation),
    "product-id":   ("Product ID",   "grey70",  {"no_wrap": True, "min_width": 10},
                     lambda s: s.product_id),
    "manufacturer": ("Manufacturer", "grey70",  {"no_wrap": True, "min_width": 6},
                     lambda s: s.manufacturer),
    "uuid":         ("UUID",         "dim",     {"no_wrap": True, "min_width": 20},
                     lambda s: s.uuid),
    "cpu":          ("CPU",          "grey70",  {"ratio": 2},
                     lambda s: s.cpu),
    "os":           ("Operating System", "cyan", {"ratio": 2},
                     lambda s: s.operating_system),
    "connection-type": ("Connection Type", "grey70", {"no_wrap": True, "min_width": 14},
                     lambda s: s.connection_type),
    "appliance":    ("Appliance",    "grey70",  {"no_wrap": True, "max_width": 30},
                     lambda s: s.appliance_name),
    "oneview-name": ("OneView Name", "grey70",  {"no_wrap": True, "max_width": 30},
                     lambda s: s.oneview_name),
    "oneview-state": ("OneView State", "grey70", {"no_wrap": True, "min_width": 9},
                     lambda s: s.oneview_state),
    "ilo-hostname": ("iLO/BMC Hostname", "green", {"no_wrap": True, "max_width": 36},
                     lambda s: s.ilo_hostname),
    "ilo-ip":       ("iLO/BMC IP",   "grey70",  {"no_wrap": True, "min_width": 13},
                     lambda s: s.ilo_ip),
    "ilo-version":  ("iLO/BMC Version", "grey70", {"no_wrap": True, "min_width": 10},
                     lambda s: s.ilo_version),
    "ilo-license":  ("iLO License",  "grey70",  {"no_wrap": True, "min_width": 10},
                     lambda s: s.ilo_license),
    "auto-ilo-fw":  ("Auto iLO FW Update", "grey70", {"no_wrap": True, "min_width": 6},
                     lambda s: _fmt_yesno(s.auto_ilo_fw_update)),
    "maintenance-mode": ("Maintenance Mode", "grey70", {"no_wrap": True, "min_width": 6},
                     lambda s: _fmt_yesno(s.maintenance_mode)),
    "subscription-tier": ("Subscription Tier", "grey70", {"no_wrap": True, "min_width": 12},
                     lambda s: s.subscription_tier),
}

# Default columns mirror the COM GUI's "Servers" page default column set
# (Health, Name, State, Serial, Group, Power, Baseline, Model) minus "iLO
# security", which COM does not expose over the public API today.
_SERVER_DEFAULT_FIELDS = ("health", "name", "state", "serial", "group", "power", "baseline", "model")

# 'devices list' spans compute + storage + network, so it keeps a Type
# column to distinguish rows whose COM-only fields (Group/Baseline/...)
# will legitimately show "—" for non-compute hardware.
_DEVICE_DEFAULT_FIELDS = ("health", "name", "state", "serial", "type", "group", "power", "baseline", "model")

SERVER_FIELD_NAMES = tuple(_SERVER_FIELDS.keys())
# Backward-compat alias — 'devices list' and 'servers list' share one registry.
DEVICE_FIELD_NAMES = SERVER_FIELD_NAMES


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

def _server_to_json(s) -> dict:
    """Flatten a Server row into a plain dict of clean values for --json.

    Uses the same field registry (and thus the same values) as the table,
    minus Rich color markup -- e.g. health becomes plain "OK" instead of
    "[green]Ok[/green]". Always includes every available field regardless
    of what --fields narrowed the table to, so JSON consumers (jq /
    ConvertFrom-Json) get the full picture to filter themselves.
    """
    return {
        _SERVER_FIELDS[key][0]: _strip_markup(str(_SERVER_FIELDS[key][3](s)))
        for key in _SERVER_FIELDS
    }


def print_devices_table(server_list: list,
                        fields: Optional[str] = None,
                        sort_by: Optional[str] = None,
                        default_fields: Optional[tuple] = None,
                        title: str = "GreenLake Devices") -> None:
    """Render a list of proliant.com.servers.Server rows as a table.

    Used by both 'com servers list' (COM's own inventory, compute-only) and
    'com devices list' (COM inventory + GreenLake-claimed storage/network,
    merged) — the two commands differ only in what feeds server_list and
    which default_fields tuple they pass in.
    """
    if get_output_mode() == OutputMode.JSON:
        print_json([_server_to_json(s) for s in server_list])
        return

    if not server_list:
        get_console().print("[yellow]No devices found.[/yellow]")
        return

    effective_defaults = default_fields or _DEVICE_DEFAULT_FIELDS
    selected = parse_fields(fields, _SERVER_FIELDS, effective_defaults)

    # Sorting — default to name for stable ordering
    sort_key = (sort_by or "name").lower()
    if sort_key not in _SERVER_FIELDS:
        raise SystemExit(f"Unknown sort field: {sort_key}\nAvailable: {', '.join(_SERVER_FIELDS)}")
    sorted_list = sorted(server_list,
                         key=lambda s: _strip_markup(_SERVER_FIELDS[sort_key][3](s)).lower())

    table = Table(
        title=f"{title} ({len(server_list)} total)",
        box=box.SIMPLE_HEAD,
    )
    for key in selected:
        header, style, kwargs, _ = _SERVER_FIELDS[key]
        table.add_column(header, style=style, **kwargs)

    for s in sorted_list:
        table.add_row(*[_SERVER_FIELDS[key][3](s) for key in selected])

    get_console().print(table)


def print_workspaces_table(workspace_list: list) -> None:
    if get_output_mode() == OutputMode.JSON:
        print_json([
            {
                "Active": w.active, "Name": w.name, "ID": w.id,
                "Region": w.region, "Status": w.status,
                "Address": w.address, "Description": w.description,
            }
            for w in workspace_list
        ])
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
    table.add_column("COM Region", style="green")
    table.add_column("Status",     style="yellow")
    table.add_column("Location",   style="white")
    table.add_column("Description", style="dim")

    for w in workspace_list:
        status_color = "green" if w.status == "ACTIVE" else "yellow"
        # A workspace can have COM provisioned in more than one region --
        # this column shows the *sticky* region remembered for this
        # workspace (see 'proliant com regions'), not necessarily the only
        # one available. "—" means we haven't switched into this workspace
        # yet, so no region preference has been recorded for it.
        region_display = _REGION_MAP.get(w.region, w.region) if w.region else "—"
        table.add_row(
            "* " if w.active else "  ",
            w.name,
            w.id,
            region_display,
            f"[{status_color}]{w.status}[/{status_color}]",
            w.address,
            w.description or "—",
        )

    get_console().print(table)
    get_console().print("[dim]  * = active workspace[/dim]")
    get_console().print(
        "[dim]  COM Region = last-used region for that workspace; run "
        "'proliant com regions list' after switching to see all provisioned "
        "regions.[/dim]"
    )


def print_regions_table(region_list: list) -> None:
    if get_output_mode() == OutputMode.JSON:
        print_json([
            {
                "Active": r.active, "Region": r.code,
                "Name": _REGION_MAP.get(r.code, r.code),
                "Location": r.location, "Provisioned": r.provisioned,
                "InstanceId": r.instance_id,
            }
            for r in region_list
        ])
        return

    if not region_list:
        get_console().print(
            "[yellow]No COM regions provisioned in this workspace.[/yellow] "
            "[dim]Provision Compute Ops Management in a region from the "
            "GreenLake GUI first.[/dim]"
        )
        return

    table = Table(
        title=f"Compute Ops Management Regions ({len(region_list)} total)",
        box=box.ROUNDED,
        show_lines=False,
    )
    table.add_column("",           style="bold green", no_wrap=True)  # active marker
    table.add_column("Region",     style="bold cyan",  no_wrap=True)
    table.add_column("Name",       style="green")
    table.add_column("Location",   style="white")
    table.add_column("Status",     style="yellow")
    table.add_column("Instance ID", style="dim")

    for r in region_list:
        status = "PROVISIONED" if r.provisioned else "available"
        status_color = "green" if r.provisioned else "dim"
        table.add_row(
            "* " if r.active else "  ",
            r.code,
            _REGION_MAP.get(r.code, r.code),
            r.location,
            f"[{status_color}]{status}[/{status_color}]",
            r.instance_id,
        )

    get_console().print(table)
    get_console().print("[dim]  * = active region[/dim]")


def print_bundles_table(bundle_list: list) -> None:
    if get_output_mode() == OutputMode.JSON:
        print_json([
            {
                "Generation": b.generation, "Type": b.bundle_type,
                "Version": b.release_version, "ReleaseDate": b.release_date,
                "Active": b.is_active, "DisplayName": b.display_name,
                "Name": b.name, "Id": b.id, "ResourceUri": b.resource_uri,
            }
            for b in bundle_list
        ])
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
