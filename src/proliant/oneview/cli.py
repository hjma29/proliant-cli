"""
proliant.oneview.cli — OneView subcommands.

Usage:
    proliant oneview servers list [--fields ...]
    proliant oneview firmware list [--server NAME]
"""

# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import asyncio
import sys


from rich import box

from proliant.common.display import get_console, get_output_mode, make_table, OutputMode, print_json, print_memory_report, set_output_mode
from proliant.common.runner import run_sync


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_client():
    """Return a connected OneViewClient context manager."""
    from proliant.oneview.config import load_oneview_config
    from proliant.oneview.client import OneViewClient

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


# ── proliant oneview servers list ─────────────────────────────────────────────────

async def _async_servers_list(fields: list[str] | None) -> None:
    from proliant.oneview.servers import list_servers_with_profiles

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching server inventory from OneView (API v{client.api_version})…[/dim]"):
            servers = await list_servers_with_profiles(client)

    if not servers:
        get_console().print("[yellow]No servers found in OneView.[/yellow]")
        return

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json(servers)
        return

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


async def _cmd_servers_list(args: argparse.Namespace) -> None:
    fields = [f.strip() for f in args.fields.split(",")] if args.fields else None
    await _async_servers_list(fields)


# ── proliant oneview firmware list ────────────────────────────────────────────────

async def _async_firmware_fleet() -> None:
    """Show firmware for all servers — one OneView API call."""
    from proliant.oneview.firmware import get_fleet_firmware

    async with _load_client() as client:
        with get_console().status("[dim]Fetching fleet firmware inventory…[/dim]"):
            fleet = await get_fleet_firmware(client)

    if not fleet:
        get_console().print("[yellow]No firmware data returned.[/yellow]")
        return

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json(fleet)
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
    from proliant.oneview.servers import get_server
    from proliant.oneview.firmware import get_server_firmware

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching firmware for {server_name}…[/dim]"):
            server = await get_server(client, server_name)
            fw_list = await get_server_firmware(client, server["uri"])

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json({"server": server_name, "firmware": fw_list})
        return

    table = make_table(
        f"[bold]{server_name}[/bold]  Firmware Inventory",
        ("Component", {"min_width": 40, "no_wrap": True}),
        ("Version",   {"no_wrap": True, "justify": "right"}),
        ("Location",  {"style": "dim"}),
    )

    for fw in fw_list:
        table.add_row(fw["name"], fw["version"], fw["location"])

    get_console().print(table)


async def _cmd_firmware_list(args: argparse.Namespace) -> None:
    if args.server:
        await _async_firmware_server(args.server)
    else:
        await _async_firmware_fleet()


# ── proliant oneview networks list ────────────────────────────────────────────────

async def _async_networks_list() -> None:
    from proliant.oneview.network import list_networks

    async with _load_client() as client:
        with get_console().status("[dim]Fetching ethernet networks…[/dim]"):
            nets = await list_networks(client)

    if not nets:
        get_console().print("[yellow]No ethernet networks found.[/yellow]")
        return

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json(nets)
        return

    table = make_table(
        f"Ethernet Networks  ({len(nets)} total)",
        ("Name",      {"min_width": 24, "no_wrap": True}),
        ("VLAN",      {"justify": "right", "no_wrap": True}),
        ("Type",      {"no_wrap": True}),
        ("Purpose",   {"no_wrap": True}),
        ("Status",    {"justify": "center", "no_wrap": True}),
        ("State",     {"justify": "center", "no_wrap": True}),
        ("SmartLink", {"justify": "center", "no_wrap": True}),
    )

    for n in nets:
        vlan = str(n["vlan"]) if n["vlan"] else "—"
        table.add_row(
            n["name"], vlan, n["type"], n["purpose"],
            _status_style(n["status"]),
            n["state"],
            "[green]✓[/green]" if n["smart_link"] else "[dim]—[/dim]",
        )
    get_console().print(table)


async def _cmd_networks_list(args: argparse.Namespace) -> None:
    await _async_networks_list()


# ── proliant oneview networksets list ─────────────────────────────────────────────

async def _async_networksets_list() -> None:
    from proliant.oneview.network import list_network_sets

    async with _load_client() as client:
        with get_console().status("[dim]Fetching network sets…[/dim]"):
            sets = await list_network_sets(client)

    if not sets:
        get_console().print("[yellow]No network sets found.[/yellow]")
        return

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json(sets)
        return

    table = make_table(
        f"Network Sets  ({len(sets)} total)",
        ("Name",           {"min_width": 24, "no_wrap": True}),
        ("Type",           {"no_wrap": True}),
        ("Networks",       {"justify": "right", "no_wrap": True}),
        ("Native Network", {"no_wrap": True, "style": "dim"}),
        ("Status",         {"justify": "center", "no_wrap": True}),
        ("State",          {"justify": "center", "no_wrap": True}),
    )

    for s in sets:
        table.add_row(
            s["name"], s["type"], str(s["num_networks"]),
            s["native_network"] or "—",
            _status_style(s["status"]), s["state"],
        )
    get_console().print(table)


async def _cmd_networksets_list(args: argparse.Namespace) -> None:
    await _async_networksets_list()


# ── proliant oneview uplinksets list ──────────────────────────────────────────────

async def _async_uplinksets_list() -> None:
    from proliant.oneview.network import list_uplink_sets

    async with _load_client() as client:
        with get_console().status("[dim]Fetching uplink sets…[/dim]"):
            uplinks = await list_uplink_sets(client)

    if not uplinks:
        get_console().print("[yellow]No uplink sets found.[/yellow]")
        return

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json(uplinks)
        return

    table = make_table(
        f"Uplink Sets  ({len(uplinks)} total)",
        ("Name",         {"min_width": 20, "no_wrap": True}),
        ("Type",         {"no_wrap": True}),
        ("Mode",         {"no_wrap": True}),
        ("Reachability", {"no_wrap": True}),
        ("Networks",     {"justify": "right", "no_wrap": True}),
        ("Ports",        {"style": "dim"}),
        ("Logical IC",   {"no_wrap": True}),
        ("Status",       {"justify": "center", "no_wrap": True}),
    )

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


async def _cmd_uplinksets_list(args: argparse.Namespace) -> None:
    await _async_uplinksets_list()


# ── proliant oneview server-profiles list ─────────────────────────────────────────

async def _async_profiles_list() -> None:
    from proliant.oneview.profiles import list_profiles

    async with _load_client() as client:
        with get_console().status("[dim]Fetching server profiles…[/dim]"):
            profiles = await list_profiles(client)

    if not profiles:
        get_console().print("[yellow]No server profiles found.[/yellow]")
        return

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json(profiles)
        return

    table = make_table(
        f"Server Profiles  ({len(profiles)} total)",
        ("Name",        {"min_width": 22, "no_wrap": True}),
        ("Server",      {"min_width": 20, "no_wrap": True}),
        ("Status",      {"justify": "center", "no_wrap": True}),
        ("State",       {"justify": "center", "no_wrap": True}),
        ("Description", {"style": "dim"}),
    )

    for p in profiles:
        table.add_row(
            p["name"], p["server_name"],
            _status_style(p["status"]), p["state"],
            p["description"] or "—",
        )
    get_console().print(table)


async def _cmd_profiles_list(args: argparse.Namespace) -> None:
    await _async_profiles_list()


# ── proliant oneview noun-verb detail commands ────────────────────────────────────

async def _async_describe_uplinkset(name: str) -> None:
    from proliant.oneview.network import describe_uplink_set
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
    port_table = make_table(
        "Ports",
        ("Bay",   {"justify": "center", "no_wrap": True}),
        ("Port",  {"no_wrap": True}),
        ("Speed", {"no_wrap": True}),
        ("FEC",   {"no_wrap": True}),
        box_style=box.SIMPLE_HEAD,
        header_style="bold",
    )
    for p in u["ports"]:
        port_table.add_row(p["bay"], p["port"], p["speed"], p["fec"])
    get_console().print(port_table)

    # Networks table
    net_table = make_table(
        f"Member Networks ({len(u['networks'])})",
        ("Name",   {"min_width": 24, "no_wrap": True}),
        ("VLAN",   {"justify": "right", "no_wrap": True}),
        ("Type",   {"no_wrap": True}),
        ("Status", {"justify": "center", "no_wrap": True}),
        box_style=box.SIMPLE_HEAD,
        header_style="bold",
    )
    for n in u["networks"]:
        vlan = str(n["vlan"]) if n["vlan"] else "—"
        net_table.add_row(n["name"], vlan, n["type"], _status_style(n["status"]))
    get_console().print(net_table)


async def _async_describe_networkset(name: str) -> None:
    from proliant.oneview.network import describe_network_set
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

    net_table = make_table(
        f"Member Networks ({len(s['networks'])})",
        ("Name",    {"min_width": 28, "no_wrap": True}),
        ("VLAN",    {"justify": "right", "no_wrap": True}),
        ("Type",    {"no_wrap": True}),
        ("Purpose", {"no_wrap": True}),
        ("Status",  {"justify": "center", "no_wrap": True}),
        ("Native",  {"justify": "center", "no_wrap": True}),
        box_style=box.SIMPLE_HEAD,
        header_style="bold",
    )
    for n in s["networks"]:
        vlan = str(n["vlan"]) if n["vlan"] else "—"
        net_table.add_row(
            n["name"], vlan, n["type"], n["purpose"],
            _status_style(n["status"]),
            "[green]✓[/green]" if n["native"] else "",
        )
    get_console().print(net_table)


async def _async_describe_profile(name: str) -> None:
    from proliant.oneview.profiles import describe_profile
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
        conn_table = make_table(
            "Connections",
            ("ID",       {"justify": "center"}),
            ("Name",     {"min_width": 16, "no_wrap": True}),
            ("Network",  {"no_wrap": True}),
            ("Function", {"no_wrap": True}),
            ("Speed",    {"no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
            header_style="bold",
        )
        for c in p["connections"]:
            conn_table.add_row(
                str(c.get("id", "")), c.get("name", ""),
                c.get("networkUri", "").rsplit("/", 1)[-1],
                c.get("functionType", ""), c.get("requestedMbps", ""),
            )
        get_console().print(conn_table)


async def _cmd_describe(args: argparse.Namespace) -> None:
    if args.resource == "uplinkset":
        await _async_describe_uplinkset(args.name)
    elif args.resource == "networkset":
        await _async_describe_networkset(args.name)
    elif args.resource == "server-profile":
        await _async_describe_profile(args.name)


async def _cmd_report_memory(args: argparse.Namespace) -> None:
    from proliant.oneview.servers import get_fleet_memory
    from proliant.com.inventory import aggregate_by_part_number

    async with _load_client() as client:
        with get_console().status("[dim]Fetching memory inventory across fleet…[/dim]"):
            dimms = await get_fleet_memory(client)

    if not dimms:
        get_console().print("[yellow]No memory inventory data returned.[/yellow]")
        return

    rows = aggregate_by_part_number(dimms)
    print_memory_report(rows, source="OneView")


# ── argument parser ───────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proliant oneview",
        description="HPE OneView management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  proliant oneview servers list                          List all managed servers
  proliant oneview firmware list                         Fleet firmware (all servers)
  proliant oneview firmware list --server "Enc1, bay 1"
  proliant oneview networks list                         All ethernet networks
  proliant oneview networksets list                      All network sets
  proliant oneview uplinksets list                       All uplink sets
  proliant oneview server-profiles list                  All server profiles
  proliant oneview uplinksets describe "pvlan-uplinkset" Full uplink set detail
  proliant oneview networksets describe "network-set-for-FM"
  proliant oneview server-profiles describe "ocp-single-node"
  proliant oneview reports memory
""",
    )

    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output as JSON (for piping/scripting)")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p_servers = sub.add_parser("servers", aliases=["server"], help="List managed servers")
    s_servers = p_servers.add_subparsers(dest="what", metavar="ACTION")
    s_servers.required = True
    p_srv = s_servers.add_parser("list", help="List all managed servers")
    p_srv.add_argument("--fields", metavar="FIELDS",
        help="Comma-separated columns: name,model,serial,ilo,ilo_ip,power,state,profile")
    p_srv.set_defaults(func=_cmd_servers_list)

    p_firmware = sub.add_parser("firmware", help="Show firmware inventory")
    s_firmware = p_firmware.add_subparsers(dest="what", metavar="ACTION")
    s_firmware.required = True
    p_fw = s_firmware.add_parser("list", help="Show firmware inventory")
    p_fw.add_argument("--server", metavar="NAME",
        help='Server name (e.g. "Enc1, bay 1"). Omit for all servers.')
    p_fw.set_defaults(func=_cmd_firmware_list)

    p_networks = sub.add_parser("networks", aliases=["network"], help="List ethernet networks")
    s_networks = p_networks.add_subparsers(dest="what", metavar="ACTION")
    s_networks.required = True
    p_net = s_networks.add_parser("list", help="List all ethernet networks")
    p_net.set_defaults(func=_cmd_networks_list)

    p_networksets = sub.add_parser("networksets", aliases=["networkset"], help="List or describe network sets")
    s_networksets = p_networksets.add_subparsers(dest="what", metavar="ACTION")
    s_networksets.required = True
    p_ns_list = s_networksets.add_parser("list", help="List all network sets")
    p_ns_list.set_defaults(func=_cmd_networksets_list)
    p_ns_desc = s_networksets.add_parser("describe", help="Describe a network set")
    p_ns_desc.add_argument("name", metavar="NAME", help="Name of the network set")
    p_ns_desc.set_defaults(func=_cmd_describe, resource="networkset")

    p_uplinksets = sub.add_parser("uplinksets", aliases=["uplinkset"], help="List or describe uplink sets")
    s_uplinksets = p_uplinksets.add_subparsers(dest="what", metavar="ACTION")
    s_uplinksets.required = True
    p_ul_list = s_uplinksets.add_parser("list", help="List all uplink sets")
    p_ul_list.set_defaults(func=_cmd_uplinksets_list)
    p_ul_desc = s_uplinksets.add_parser("describe", help="Describe an uplink set")
    p_ul_desc.add_argument("name", metavar="NAME", help="Name of the uplink set")
    p_ul_desc.set_defaults(func=_cmd_describe, resource="uplinkset")

    p_profiles = sub.add_parser("server-profiles", aliases=["server-profile"], help="List or describe server profiles")
    s_profiles = p_profiles.add_subparsers(dest="what", metavar="ACTION")
    s_profiles.required = True
    p_sp_list = s_profiles.add_parser("list", help="List all server profiles")
    p_sp_list.set_defaults(func=_cmd_profiles_list)
    p_sp_desc = s_profiles.add_parser("describe", help="Describe a server profile")
    p_sp_desc.add_argument("name", metavar="NAME", help="Name of the server profile")
    p_sp_desc.set_defaults(func=_cmd_describe, resource="server-profile")

    # ── reports ───────────────────────────────────────────────────────────
    p_reports = sub.add_parser("reports", help="Fleet hardware reports")
    s_reports = p_reports.add_subparsers(dest="what", metavar="REPORT")
    s_reports.required = True
    p_rep_mem = s_reports.add_parser("memory", aliases=["mem"], help="Memory DIMM part-number breakdown")
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
    if getattr(args, "json_output", False):
        set_output_mode(OutputMode.JSON)
    run_sync(args.func(args))


if __name__ == "__main__":
    main()
