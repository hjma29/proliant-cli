"""
pcli.oneview.cli — OneView subcommands.

Usage:
    pcli oneview servers list [--fields ...]
    pcli oneview firmware list [--server NAME]
    pcli oneview firmware get --server NAME
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


# ── argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcli oneview",
        description="HPE OneView management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  pcli oneview servers list                     List all managed servers
  pcli oneview servers list --fields name,model,serial,power
  pcli oneview firmware list                    Fleet firmware (all servers)
  pcli oneview firmware list --server "Enc1, bay 1"
""",
    )

    sub = parser.add_subparsers(dest="resource", metavar="RESOURCE")
    sub.required = True

    # ── servers ──────────────────────────────────────────────────────────
    p_servers = sub.add_parser("servers", help="Server hardware inventory")
    s_servers = p_servers.add_subparsers(dest="action", metavar="ACTION")
    s_servers.required = True

    p_srv_list = s_servers.add_parser("list", help="List all managed servers")
    p_srv_list.add_argument(
        "--fields",
        metavar="FIELDS",
        help="Comma-separated columns: name,model,serial,ilo,ilo_ip,power,state,profile",
    )
    p_srv_list.set_defaults(func=_cmd_servers_list)

    # ── firmware ─────────────────────────────────────────────────────────
    p_fw = sub.add_parser("firmware", help="Firmware inventory")
    s_fw = p_fw.add_subparsers(dest="action", metavar="ACTION")
    s_fw.required = True

    p_fw_list = s_fw.add_parser("list", help="Show firmware inventory")
    p_fw_list.add_argument(
        "--server",
        metavar="NAME",
        help='Server name (e.g. "Enc1, bay 1"). Omit for all servers.',
    )
    p_fw_list.set_defaults(func=_cmd_firmware_list)

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
