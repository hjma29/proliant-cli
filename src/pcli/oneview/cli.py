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


from rich import box
from rich.table import Table

from pcli.common.display import get_console, make_table, print_memory_report
from pcli.common.runner import run_sync


# ── async runner ─────────────────────────────────────────────────────────────

def _run(coro):
    return run_sync(coro)


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
        with get_console().status(f"[dim]Fetching server inventory from OneView (API v{client.api_version})…[/dim]"):
            servers = await list_servers_with_profiles(client)

    if not servers:
        get_console().print("[yellow]No servers found in OneView.[/yellow]")
        return

    # Default columns (ilo_ip omitted for Synergy — always blank)
    all_fields = ["name", "model", "serial", "ilo", "power", "state", "profile"]
    show = fields if fields else all_fields

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

    table = make_table(
        f"OneView Servers  ({len(servers)} total)",
        *[(col_map[f][0], col_map[f][1]) for f in show if f in col_map],
    )

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

    get_console().print(table)


def _cmd_servers_list(args: argparse.Namespace) -> None:
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    try:
        _run(_async_servers_list(fields))
    except Exception as exc:
        get_console().print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview firmware list ────────────────────────────────────────────────

async def _async_firmware_fleet() -> None:
    """Show firmware for all servers — one OneView API call."""
    from pcli.oneview.firmware import get_fleet_firmware

    async with _load_client() as client:
        with get_console().status("[dim]Fetching fleet firmware inventory…[/dim]"):
            fleet = await get_fleet_firmware(client)

    if not fleet:
        get_console().print("[yellow]No firmware data returned.[/yellow]")
        return

    for entry in fleet:
        server_name = entry["server_name"]
        fw_list = entry["firmware"]

        table = make_table(
            f"[bold]{server_name}[/bold]",
            ("Component", {"min_width": 40, "no_wrap": True}),
            ("Version",   {"no_wrap": True, "justify": "right"}),
            ("Location",  {"style": "dim"}),
            box_style=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
        )

        for fw in fw_list:
            table.add_row(fw["name"], fw["version"], fw["location"])

        get_console().print(table)
        get_console().print()


async def _async_firmware_server(server_name: str) -> None:
    """Show firmware for a single server."""
    from pcli.oneview.servers import get_server
    from pcli.oneview.firmware import get_server_firmware

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching firmware for {server_name}…[/dim]"):
            server = await get_server(client, server_name)
            fw_list = await get_server_firmware(client, server["uri"])

    table = make_table(
        f"[bold]{server_name}[/bold]  Firmware Inventory",
        ("Component", {"min_width": 40, "no_wrap": True}),
        ("Version",   {"no_wrap": True, "justify": "right"}),
        ("Location",  {"style": "dim"}),
    )

    for fw in fw_list:
        table.add_row(fw["name"], fw["version"], fw["location"])

    get_console().print(table)


def _cmd_firmware_list(args: argparse.Namespace) -> None:
    try:
        if args.server:
            _run(_async_firmware_server(args.server))
        else:
            _run(_async_firmware_fleet())
    except Exception as exc:
        get_console().print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview list networks ────────────────────────────────────────────────

async def _async_networks_list() -> None:
    from pcli.oneview.network import list_networks

    async with _load_client() as client:
        with get_console().status("[dim]Fetching ethernet networks…[/dim]"):
            nets = await list_networks(client)

    if not nets:
        get_console().print("[yellow]No ethernet networks found.[/yellow]")
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
    get_console().print(table)


def _cmd_networks_list(args: argparse.Namespace) -> None:
    try:
        _run(_async_networks_list())
    except Exception as exc:
        get_console().print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview list networksets ─────────────────────────────────────────────

async def _async_networksets_list() -> None:
    from pcli.oneview.network import list_network_sets

    async with _load_client() as client:
        with get_console().status("[dim]Fetching network sets…[/dim]"):
            sets = await list_network_sets(client)

    if not sets:
        get_console().print("[yellow]No network sets found.[/yellow]")
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
    get_console().print(table)


def _cmd_networksets_list(args: argparse.Namespace) -> None:
    try:
        _run(_async_networksets_list())
    except Exception as exc:
        get_console().print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview list uplinksets ─────────────────────────────────────────────

async def _async_uplinksets_list() -> None:
    from pcli.oneview.network import list_uplink_sets

    async with _load_client() as client:
        with get_console().status("[dim]Fetching uplink sets…[/dim]"):
            uplinks = await list_uplink_sets(client)

    if not uplinks:
        get_console().print("[yellow]No uplink sets found.[/yellow]")
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
    get_console().print(table)


def _cmd_uplinksets_list(args: argparse.Namespace) -> None:
    try:
        _run(_async_uplinksets_list())
    except Exception as exc:
        get_console().print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview list server-profiles ────────────────────────────────────────

async def _async_profiles_list() -> None:
    from pcli.oneview.profiles import list_profiles

    async with _load_client() as client:
        with get_console().status("[dim]Fetching server profiles…[/dim]"):
            profiles = await list_profiles(client)

    if not profiles:
        get_console().print("[yellow]No server profiles found.[/yellow]")
        return

    table = Table(
        title=f"Server Profiles  ({len(profiles)} total)",
        box=box.ROUNDED, show_header=True, header_style="bold cyan",
    )
    table.add_column("Name",        min_width=22, no_wrap=True)
    table.add_column("Server",      min_width=20, no_wrap=True)
    table.add_column("Status",      justify="center", no_wrap=True)
    table.add_column("State",       justify="center", no_wrap=True)
    table.add_column("Description", style="dim")

    for p in profiles:
        table.add_row(
            p["name"], p["server_name"],
            _status_style(p["status"]), p["state"],
            p["description"] or "—",
        )
    get_console().print(table)


def _cmd_profiles_list(args: argparse.Namespace) -> None:
    try:
        _run(_async_profiles_list())
    except Exception as exc:
        get_console().print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


# ── pcli oneview describe ─────────────────────────────────────────────────────

async def _async_describe_uplinkset(name: str) -> None:
    from pcli.oneview.network import describe_uplink_set
    from rich.panel import Panel
    from rich.columns import Columns
    import rich.text as rt

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching uplink set '{name}'…[/dim]"):
            u = await describe_uplink_set(client, name)

    reach = u["reachability"]
    reach_s = f"[green]{reach}[/green]" if reach == "Reachable" else f"[yellow]{reach}[/yellow]"

    get_console().print(Panel(
        f"[bold]{u['name']}[/bold]\n"
        f"Logical IC:    [cyan]{u['li_name']}[/cyan]\n"
        f"Type:          {u['network_type']}  |  Mode: {u['conn_mode']}\n"
        f"Reachability:  {reach_s}  |  Status: {_status_style(u['status'])}  |  State: {u['state']}",
        title="Uplink Set", border_style="cyan",
    ))

    # Ports table
    port_table = Table(title="Ports", box=box.SIMPLE_HEAD, header_style="bold")
    port_table.add_column("Bay",   justify="center", no_wrap=True)
    port_table.add_column("Port",  no_wrap=True)
    port_table.add_column("Speed", no_wrap=True)
    port_table.add_column("FEC",   no_wrap=True)
    for p in u["ports"]:
        port_table.add_row(p["bay"], p["port"], p["speed"], p["fec"])
    get_console().print(port_table)

    # Networks table
    net_table = Table(title=f"Member Networks ({len(u['networks'])})", box=box.SIMPLE_HEAD, header_style="bold")
    net_table.add_column("Name",   min_width=24, no_wrap=True)
    net_table.add_column("VLAN",   justify="right", no_wrap=True)
    net_table.add_column("Type",   no_wrap=True)
    net_table.add_column("Status", justify="center", no_wrap=True)
    for n in u["networks"]:
        vlan = str(n["vlan"]) if n["vlan"] else "—"
        net_table.add_row(n["name"], vlan, n["type"], _status_style(n["status"]))
    get_console().print(net_table)


async def _async_describe_networkset(name: str) -> None:
    from pcli.oneview.network import describe_network_set
    from rich.panel import Panel

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching network set '{name}'…[/dim]"):
            s = await describe_network_set(client, name)

    get_console().print(Panel(
        f"[bold]{s['name']}[/bold]\n"
        f"Type:           {s['type']}\n"
        f"Native Network: {s['native_network'] or '—'}\n"
        f"Status: {_status_style(s['status'])}  |  State: {s['state']}",
        title="Network Set", border_style="cyan",
    ))

    net_table = Table(title=f"Member Networks ({len(s['networks'])})", box=box.SIMPLE_HEAD, header_style="bold")
    net_table.add_column("Name",    min_width=28, no_wrap=True)
    net_table.add_column("VLAN",    justify="right", no_wrap=True)
    net_table.add_column("Type",    no_wrap=True)
    net_table.add_column("Purpose", no_wrap=True)
    net_table.add_column("Status",  justify="center", no_wrap=True)
    net_table.add_column("Native",  justify="center", no_wrap=True)
    for n in s["networks"]:
        vlan = str(n["vlan"]) if n["vlan"] else "—"
        net_table.add_row(
            n["name"], vlan, n["type"], n["purpose"],
            _status_style(n["status"]),
            "[green]✓[/green]" if n["native"] else "",
        )
    get_console().print(net_table)


async def _async_describe_profile(name: str) -> None:
    from pcli.oneview.profiles import describe_profile
    from rich.panel import Panel

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching profile '{name}'…[/dim]"):
            p = await describe_profile(client, name)

    fw_line = ""
    if p["fw_baseline"]:
        managed = "  [dim](managed)[/dim]" if p["manage_fw"] else "  [dim](unmanaged)[/dim]"
        fw_line = f"\nFW Baseline:    {p['fw_baseline']} {p['fw_version']}{managed}"
        if p["fw_install_type"]:
            fw_line += f"\nFW Install:     {p['fw_install_type']}"

    boot = ", ".join(p["boot_order"]) if p["boot_order"] else "—"

    server_line = p["server_name"]
    if p.get("server_model"):
        server_line += f"  [dim]({p['server_model']} | SN: {p['server_serial']} | Power: {p['server_power']})[/dim]"

    desc_line = f"\nDescription:    {p['description']}" if p["description"] else ""

    get_console().print(Panel(
        f"[bold]{p['name']}[/bold]\n"
        f"Server:         {server_line}\n"
        f"Enclosure Group: [cyan]{p['eg_name']}[/cyan]\n"
        f"Status: {_status_style(p['status'])}  |  State: {p['state']}"
        f"{fw_line}\n"
        f"Boot Order:     {boot}"
        f"{desc_line}",
        title="Server Profile", border_style="cyan",
    ))

    if p["connections"]:
        conn_table = Table(title="Connections", box=box.SIMPLE_HEAD, header_style="bold")
        conn_table.add_column("ID",       justify="center")
        conn_table.add_column("Name",     min_width=16, no_wrap=True)
        conn_table.add_column("Network",  no_wrap=True)
        conn_table.add_column("Function", no_wrap=True)
        conn_table.add_column("Speed",    no_wrap=True)
        for c in p["connections"]:
            conn_table.add_row(
                str(c.get("id", "")), c.get("name", ""),
                c.get("networkUri", "").rsplit("/", 1)[-1],
                c.get("functionType", ""), c.get("requestedMbps", ""),
            )
        get_console().print(conn_table)


def _cmd_describe(args: argparse.Namespace) -> None:
    try:
        if args.resource == "uplinkset":
            _run(_async_describe_uplinkset(args.name))
        elif args.resource == "networkset":
            _run(_async_describe_networkset(args.name))
        elif args.resource == "server-profile":
            _run(_async_describe_profile(args.name))
    except ValueError as exc:
        get_console().print(f"[red]{exc}[/red]")
        sys.exit(1)
    except Exception as exc:
        get_console().print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


def _cmd_report_memory(args: argparse.Namespace) -> None:
    from pcli.oneview.servers import get_fleet_memory
    from pcli.com.inventory import aggregate_by_part_number

    async def _run_report():
        async with _load_client() as client:
            with get_console().status("[dim]Fetching memory inventory across fleet…[/dim]"):
                return await get_fleet_memory(client)

    dimms = _run(_run_report())

    if not dimms:
        get_console().print("[yellow]No memory inventory data returned.[/yellow]")
        return

    rows = aggregate_by_part_number(dimms)
    print_memory_report(rows, source="OneView")


# ── argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pcli oneview",
        description="HPE OneView management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  pcli oneview list servers                          List all managed servers
  pcli oneview list firmware                         Fleet firmware (all servers)
  pcli oneview list firmware --server "Enc1, bay 1"
  pcli oneview list networks                         All ethernet networks
  pcli oneview list networksets                      All network sets
  pcli oneview list uplinksets                       All uplink sets
  pcli oneview list server-profiles                  All server profiles
  pcli oneview describe uplinkset "pvlan-uplinkset"  Full uplink set detail
  pcli oneview describe networkset "network-set-for-FM"
  pcli oneview describe server-profile "ocp-single-node"
""",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # ── list ─────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List resources")
    s_list = p_list.add_subparsers(dest="what", metavar="WHAT")
    s_list.required = True

    p_srv = s_list.add_parser("servers", aliases=["server"], help="List all managed servers")
    p_srv.add_argument("--fields", metavar="FIELDS",
        help="Comma-separated columns: name,model,serial,ilo,ilo_ip,power,state,profile")
    p_srv.set_defaults(func=_cmd_servers_list)

    p_fw = s_list.add_parser("firmware", help="Show firmware inventory")
    p_fw.add_argument("--server", metavar="NAME",
        help='Server name (e.g. "Enc1, bay 1"). Omit for all servers.')
    p_fw.set_defaults(func=_cmd_firmware_list)

    p_net = s_list.add_parser("networks", aliases=["network"], help="List all ethernet networks")
    p_net.set_defaults(func=_cmd_networks_list)

    p_ns = s_list.add_parser("networksets", aliases=["networkset"], help="List all network sets")
    p_ns.set_defaults(func=_cmd_networksets_list)

    p_ul = s_list.add_parser("uplinksets", aliases=["uplinkset"], help="List all uplink sets")
    p_ul.set_defaults(func=_cmd_uplinksets_list)

    p_sp = s_list.add_parser("server-profiles", aliases=["server-profile"], help="List all server profiles")
    p_sp.set_defaults(func=_cmd_profiles_list)

    # ── describe ──────────────────────────────────────────────────────────
    p_desc = sub.add_parser("describe", help="Show detailed info for a single resource")
    s_desc = p_desc.add_subparsers(dest="resource", metavar="RESOURCE")
    s_desc.required = True

    for res, aliases in (
        ("uplinkset",      ["uplinksets"]),
        ("networkset",     ["networksets"]),
        ("server-profile", ["server-profiles"]),
    ):
        rp = s_desc.add_parser(res, aliases=aliases, help=f"Describe a {res}")
        rp.add_argument("name", metavar="NAME", help=f"Name of the {res}")
        rp.set_defaults(func=_cmd_describe, resource=res)

    # ── report ────────────────────────────────────────────────────────────
    p_report = sub.add_parser("report", help="Fleet hardware reports")
    s_report = p_report.add_subparsers(dest="what", metavar="WHAT")
    s_report.required = True
    p_rep_mem = s_report.add_parser("memory", aliases=["mem"], help="Memory DIMM part-number breakdown")
    p_rep_mem.set_defaults(func=_cmd_report_memory)

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
