"""
pcli.oneview.cli — OneView subcommands.

Usage:
    pcli oneview list servers [--fields ...]
    pcli oneview list firmware [--server NAME]
"""

# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import asyncio
import sys

from rich.console import Console
from rich import box
from rich.table import Table

console = Console()

# ── async runner ─────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.run(coro)


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_client():
    """Return a connected OneViewClient context manager."""
    from pcli.oneview.config import load_oneview_config
    from pcli.oneview.client import OneViewClient

    cfg = load_oneview_config()
    return OneViewClient(cfg["host"], cfg["username"], cfg["password"])


def _power_style(state: str) -> str:
    s = state.lower()
    if s == "on":
        return "[green]On[/green]"
    if s == "off":
        return "[dim]Off[/dim]"
    return f"[yellow]{state}[/yellow]"


def _state_style(state: str) -> str:
    s = state.lower()
    if s == "managed":
        return "[green]Managed[/green]"
    if s == "monitored":
        return "[cyan]Monitored[/cyan]"
    return f"[yellow]{state}[/yellow]"


def _status_style(status: str) -> str:
    s = status.lower()
    if s == "ok":
        return "[green]OK[/green]"
    if s in ("warning", "degraded"):
        return f"[yellow]{status}[/yellow]"
    if s == "critical":
        return f"[red]{status}[/red]"
    return status


# ── pcli oneview servers list ─────────────────────────────────────────────────

async def _async_servers_list(fields: list[str] | None) -> None:
    from pcli.oneview.servers import list_servers_with_profiles

    async with _load_client() as client:
        with console.status(f"[dim]Fetching server inventory from OneView (API v{client.api_version})…[/dim]"):
            servers = await list_servers_with_profiles(client)

    if not servers:
        console.print("[yellow]No servers found in OneView.[/yellow]")
        return

    # Default columns (ilo_ip omitted for Synergy — always blank)
    all_fields = ["name", "model", "serial", "ilo", "power", "state", "profile"]
    show = fields if fields else all_fields

    table = Table(
        title=f"OneView Servers  ({len(servers)} total)",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )

    col_map = {
        "name":    ("Name",        dict(min_width=20, no_wrap=True)),
        "model":   ("Model",       dict(min_width=22)),
        "serial":  ("Serial",      dict(no_wrap=True)),
        "ilo":     ("iLO",         dict(no_wrap=True)),
        "ilo_ip":  ("iLO IP",      dict(no_wrap=True)),
        "power":   ("Power",       dict(justify="center", no_wrap=True)),
        "state":   ("State",       dict(justify="center")),
        "profile": ("Profile",     dict(min_width=16)),
    }

    for f in show:
        if f in col_map:
            label, kwargs = col_map[f]
            table.add_column(label, **kwargs)

    for s in servers:
        row = []
        for f in show:
            if f == "name":
                row.append(s["name"])
            elif f == "model":
                row.append(s["model"])
            elif f == "serial":
                row.append(s["serial"])
            elif f == "ilo":
                row.append(f"{s['ilo_model']} v{s['ilo_version']}" if s["ilo_model"] else "")
            elif f == "ilo_ip":
                row.append(s["ilo_ip"])
            elif f == "power":
                row.append(_power_style(s["power"]))
            elif f == "state":
                row.append(_state_style(s["state"]))
            elif f == "profile":
                row.append(s["profile"])
        table.add_row(*row)

    console.print(table)


def _cmd_servers_list(args: argparse.Namespace) -> None:
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    try:
        _run(_async_servers_list(fields))
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview firmware list ────────────────────────────────────────────────

async def _async_firmware_fleet() -> None:
    """Show firmware for all servers — one OneView API call."""
    from pcli.oneview.firmware import get_fleet_firmware

    async with _load_client() as client:
        with console.status("[dim]Fetching fleet firmware inventory…[/dim]"):
            fleet = await get_fleet_firmware(client)

    if not fleet:
        console.print("[yellow]No firmware data returned.[/yellow]")
        return

    for entry in fleet:
        server_name = entry["server_name"]
        fw_list = entry["firmware"]

        table = Table(
            title=f"[bold]{server_name}[/bold]",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
        )
        table.add_column("Component", min_width=40, no_wrap=True)
        table.add_column("Version",   no_wrap=True, justify="right")
        table.add_column("Location",  style="dim")

        for fw in fw_list:
            table.add_row(fw["name"], fw["version"], fw["location"])

        console.print(table)
        console.print()


async def _async_firmware_server(server_name: str) -> None:
    """Show firmware for a single server."""
    from pcli.oneview.servers import get_server
    from pcli.oneview.firmware import get_server_firmware

    async with _load_client() as client:
        with console.status(f"[dim]Fetching firmware for {server_name}…[/dim]"):
            server = await get_server(client, server_name)
            fw_list = await get_server_firmware(client, server["uri"])

    table = Table(
        title=f"[bold]{server_name}[/bold]  Firmware Inventory",
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Component", min_width=40, no_wrap=True)
    table.add_column("Version",   no_wrap=True, justify="right")
    table.add_column("Location",  style="dim")

    for fw in fw_list:
        table.add_row(fw["name"], fw["version"], fw["location"])

    console.print(table)


def _cmd_firmware_list(args: argparse.Namespace) -> None:
    try:
        if args.server:
            _run(_async_firmware_server(args.server))
        else:
            _run(_async_firmware_fleet())
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview list networks ────────────────────────────────────────────────

async def _async_networks_list() -> None:
    from pcli.oneview.network import list_networks

    async with _load_client() as client:
        with console.status("[dim]Fetching ethernet networks…[/dim]"):
            nets = await list_networks(client)

    if not nets:
        console.print("[yellow]No ethernet networks found.[/yellow]")
        return

    table = Table(
        title=f"Ethernet Networks  ({len(nets)} total)",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    table.add_column("Name",       min_width=24, no_wrap=True)
    table.add_column("VLAN",       justify="right", no_wrap=True)
    table.add_column("Type",       no_wrap=True)
    table.add_column("Purpose",    no_wrap=True)
    table.add_column("Status",     justify="center", no_wrap=True)
    table.add_column("State",      justify="center", no_wrap=True)
    table.add_column("SmartLink",  justify="center", no_wrap=True)

    for n in nets:
        vlan = str(n["vlan"]) if n["vlan"] else "—"
        table.add_row(
            n["name"], vlan, n["type"], n["purpose"],
            _status_style(n["status"]),
            n["state"],
            "[green]✓[/green]" if n["smart_link"] else "[dim]—[/dim]",
        )
    console.print(table)


def _cmd_networks_list(args: argparse.Namespace) -> None:
    try:
        _run(_async_networks_list())
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview list networksets ─────────────────────────────────────────────

async def _async_networksets_list() -> None:
    from pcli.oneview.network import list_network_sets

    async with _load_client() as client:
        with console.status("[dim]Fetching network sets…[/dim]"):
            sets = await list_network_sets(client)

    if not sets:
        console.print("[yellow]No network sets found.[/yellow]")
        return

    table = Table(
        title=f"Network Sets  ({len(sets)} total)",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    table.add_column("Name",           min_width=24, no_wrap=True)
    table.add_column("Type",           no_wrap=True)
    table.add_column("Networks",       justify="right", no_wrap=True)
    table.add_column("Native Network", no_wrap=True, style="dim")
    table.add_column("Status",         justify="center", no_wrap=True)
    table.add_column("State",          justify="center", no_wrap=True)

    for s in sets:
        table.add_row(
            s["name"], s["type"], str(s["num_networks"]),
            s["native_network"] or "—",
            _status_style(s["status"]), s["state"],
        )
    console.print(table)


def _cmd_networksets_list(args: argparse.Namespace) -> None:
    try:
        _run(_async_networksets_list())
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview list uplinksets ─────────────────────────────────────────────

async def _async_uplinksets_list() -> None:
    from pcli.oneview.network import list_uplink_sets

    async with _load_client() as client:
        with console.status("[dim]Fetching uplink sets…[/dim]"):
            uplinks = await list_uplink_sets(client)

    if not uplinks:
        console.print("[yellow]No uplink sets found.[/yellow]")
        return

    table = Table(
        title=f"Uplink Sets  ({len(uplinks)} total)",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    table.add_column("Name",          min_width=20, no_wrap=True)
    table.add_column("Type",          no_wrap=True)
    table.add_column("Mode",          no_wrap=True)
    table.add_column("Reachability",  no_wrap=True)
    table.add_column("Networks",      justify="right", no_wrap=True)
    table.add_column("Ports",         style="dim")
    table.add_column("Logical IC",    no_wrap=True)
    table.add_column("Status",        justify="center", no_wrap=True)

    for u in uplinks:
        reach = u["reachability"]
        reach_styled = f"[green]{reach}[/green]" if reach == "Reachable" else f"[yellow]{reach}[/yellow]"
        table.add_row(
            u["name"], u["network_type"], u["conn_mode"],
            reach_styled, str(u["num_networks"]),
            u["ports"], u["li_name"],
            _status_style(u["status"]),
        )
    console.print(table)


def _cmd_uplinksets_list(args: argparse.Namespace) -> None:
    try:
        _run(_async_uplinksets_list())
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcli oneview",
        description="HPE OneView management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  pcli oneview list servers                     List all managed servers
  pcli oneview list servers --fields name,model,serial,power
  pcli oneview list firmware                    Fleet firmware (all servers)
  pcli oneview list firmware --server "Enc1, bay 1"
  pcli oneview list networks                    All ethernet networks
  pcli oneview list networksets                 All network sets
  pcli oneview list uplinksets                  All uplink sets
""",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ── list ─────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List resources")
    s_list = p_list.add_subparsers(dest="what", metavar="WHAT")
    s_list.required = True

    # pcli oneview list servers
    p_srv = s_list.add_parser("servers", help="List all managed servers")
    p_srv.add_argument(
        "--fields",
        metavar="FIELDS",
        help="Comma-separated columns: name,model,serial,ilo,ilo_ip,power,state,profile",
    )
    p_srv.set_defaults(func=_cmd_servers_list)

    # pcli oneview list firmware
    p_fw = s_list.add_parser("firmware", help="Show firmware inventory")
    p_fw.add_argument(
        "--server",
        metavar="NAME",
        help='Server name (e.g. "Enc1, bay 1"). Omit for all servers.',
    )
    p_fw.set_defaults(func=_cmd_firmware_list)

    # pcli oneview list networks
    p_net = s_list.add_parser("networks", help="List all ethernet networks")
    p_net.set_defaults(func=_cmd_networks_list)

    # pcli oneview list networksets
    p_ns = s_list.add_parser("networksets", help="List all network sets")
    p_ns.set_defaults(func=_cmd_networksets_list)

    # pcli oneview list uplinksets
    p_ul = s_list.add_parser("uplinksets", help="List all uplink sets")
    p_ul.set_defaults(func=_cmd_uplinksets_list)

    return parser


def main(argv: list[str] | None = None) -> None:
    try:
        import argcomplete
        parser = _build_parser()
        argcomplete.autocomplete(parser)
    except ImportError:
        parser = _build_parser()

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
