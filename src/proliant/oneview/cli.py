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
import re
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


def _oneview_network_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete --network-name by querying OneView ethernet networks."""
    try:
        from proliant.oneview.config import load_oneview_config
        from proliant.oneview.client import OneViewClient

        cfg = load_oneview_config()

        async def _fetch() -> list[str]:
            async with OneViewClient(cfg["host"], cfg["username"], cfg["password"]) as client:
                nets = await client.get_all("/rest/ethernet-networks")
                return [n["name"] for n in nets if n.get("name", "").lower().startswith(prefix.lower())]

        return asyncio.run(_fetch())
    except Exception:
        return []


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


def _status_style(status: str | None) -> str:
    if not status:
        return ""
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
        if n["vlan"]:
            vlan = str(n["vlan"])
        elif n.get("internal_vlan"):
            vlan = f"[grey50]{n['internal_vlan']} (int)[/grey50]"
        else:
            vlan = "—"
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
        f"Bandwidth:      {_fmt_bw(s['pref_bw_mbps'])} preferred  |  {_fmt_bw(s['max_bw_mbps'])} maximum\n"
        f"Status: {_status_style(s['status'])}  |  State: {s['state']}",
        title="Network Set", border_style="cyan",
    ))

    if s["used_profiles"] or s["used_templates"]:
        usage = make_table(
            f"Used By  ({len(s['used_profiles'])} profiles, {len(s['used_templates'])} templates)",
            ("Kind", {"no_wrap": True}),
            ("Name", {"no_wrap": True}),
            box_style=box.SIMPLE_HEAD, header_style="bold",
        )
        for nm_p in s["used_profiles"]:
            usage.add_row("Server Profile", nm_p)
        for nm_t in s["used_templates"]:
            usage.add_row("[cyan]Profile Template[/cyan]", nm_t)
        get_console().print(usage)
    else:
        get_console().print("[dim]Not used by any server profile or template.[/dim]")

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


# ── proliant oneview li list ──────────────────────────────────────────────────

async def _async_li_list() -> None:
    from proliant.oneview.interconnects import list_lis

    async with _load_client() as client:
        with get_console().status("[dim]Fetching logical interconnects…[/dim]"):
            lis = await list_lis(client)

    if not lis:
        get_console().print("[yellow]No logical interconnects found.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(lis)
        return

    table = make_table(
        f"Logical Interconnects  ({len(lis)} total)",
        ("Name",        {"min_width": 24, "no_wrap": True}),
        ("LIG",         {"no_wrap": True}),
        ("Consistency", {"justify": "center", "no_wrap": True}),
        ("Stacking",    {"justify": "center", "no_wrap": True}),
        ("Status",      {"justify": "center", "no_wrap": True}),
        ("State",       {"justify": "center", "no_wrap": True}),
    )
    for li in lis:
        cons = li["consistency"]
        cons_s = f"[green]{cons}[/green]" if cons == "Consistent" else f"[yellow]{cons}[/yellow]"
        table.add_row(
            li["name"], li["lig_name"], cons_s, li["stacking"],
            _status_style(li["status"]), li["state"],
        )
    get_console().print(table)


async def _cmd_li_list(args: argparse.Namespace) -> None:
    await _async_li_list()


# ── proliant oneview lig list ─────────────────────────────────────────────────

async def _async_lig_list() -> None:
    from proliant.oneview.interconnects import list_ligs

    async with _load_client() as client:
        with get_console().status("[dim]Fetching logical interconnect groups…[/dim]"):
            ligs = await list_ligs(client)

    if not ligs:
        get_console().print("[yellow]No logical interconnect groups found.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(ligs)
        return

    table = make_table(
        f"Logical Interconnect Groups  ({len(ligs)} total)",
        ("Name",  {"min_width": 28, "no_wrap": True}),
        ("State", {"justify": "center", "no_wrap": True}),
    )
    for lg in ligs:
        table.add_row(lg["name"], lg["state"])
    get_console().print(table)


async def _cmd_lig_list(args: argparse.Namespace) -> None:
    await _async_lig_list()


# ── proliant oneview interconnects list ───────────────────────────────────────

async def _async_interconnects_list() -> None:
    from proliant.oneview.interconnects import list_interconnects

    async with _load_client() as client:
        with get_console().status("[dim]Fetching interconnects…[/dim]"):
            ics = await list_interconnects(client)

    if not ics:
        get_console().print("[yellow]No interconnects found.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(ics)
        return

    table = make_table(
        f"Interconnects  ({len(ics)} total)",
        ("Name",   {"min_width": 24, "no_wrap": True}),
        ("Model",  {"min_width": 18, "no_wrap": True}),
        ("LI",     {"no_wrap": True}),
        ("Status", {"justify": "center", "no_wrap": True}),
        ("State",  {"justify": "center", "no_wrap": True}),
        ("Serial", {"style": "dim", "no_wrap": True}),
    )
    for ic in ics:
        table.add_row(
            ic["name"], ic["model"], ic["li_name"],
            _status_style(ic["status"]), ic["state"], ic["serial"],
        )
    get_console().print(table)


async def _cmd_interconnects_list(args: argparse.Namespace) -> None:
    await _async_interconnects_list()


# ── proliant oneview mac list ─────────────────────────────────────────────────

def _short_ic_name(name: str) -> str:
    """Compact OneView's interconnect name for table display.

    OneView reports interconnects as ``<enclosure>, interconnect <bay>``
    (e.g. ``Enclosure-01, interconnect 6``).  Shorten the verbose middle to
    ``<enclosure> IC<bay>`` → ``Enclosure-01 IC6``.  Unrecognised names are
    returned unchanged.
    """
    m = re.match(r"^(.*),\s*interconnect\s*(\d+)\s*$", name or "", re.IGNORECASE)
    return f"{m.group(1)} IC{m.group(2)}" if m else (name or "")


async def _async_mac_list(address: str, vlan: int, network_name: str = "") -> None:
    from proliant.oneview.interconnects import get_mac_table

    async with _load_client() as client:
        filter_desc = []
        if address:
            filter_desc.append(f"mac={address}")
        if vlan:
            filter_desc.append(f"vlan={vlan}")
        if network_name:
            filter_desc.append(f"network={network_name}")
        desc = ", ".join(filter_desc) if filter_desc else "no filter"
        with get_console().status(f"[dim]Querying MAC table ({desc}) across all VCs…[/dim]"):
            entries = await get_mac_table(client, address=address, vlan=vlan)

    if network_name:
        nl = network_name.lower()
        entries = [e for e in entries if nl in e["network"].lower()]

    if not entries:
        get_console().print("[yellow]No MAC entries found.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(entries)
        return

    table = make_table(
        f"MAC Address Table  ({len(entries)} entries)",
        ("MAC Address",    {"no_wrap": True, "min_width": 18}),
        ("Interconnect",   {"no_wrap": True}),
        ("Port",           {"no_wrap": True}),
        ("Server Profile", {"no_wrap": True}),
        ("Connection",     {"no_wrap": True}),
    )
    for e in entries:
        table.add_row(
            e["mac"], _short_ic_name(e["ic_name"]), e["port"],
            e.get("profile") or "[dim]—[/dim]",
            e.get("connection") or "[dim]—[/dim]",
        )
    get_console().print(table)


async def _cmd_mac_list(args: argparse.Namespace) -> None:
    if not args.address and not args.vlan and not getattr(args, "network_name", None):
        get_console().print("[red]Error:[/red] specify at least one of --address, --vlan, or --network-name")
        sys.exit(1)
    await _async_mac_list(
        address=args.address or "",
        vlan=args.vlan or 0,
        network_name=getattr(args, "network_name", "") or "",
    )


# ── proliant oneview networks/mac describe ────────────────────────────────────

def _fmt_bw(mbps: int) -> str:
    if not mbps:
        return "—"
    if mbps >= 1000:
        return f"{mbps / 1000:g} Gb/s"
    return f"{mbps} Mb/s"


def _vlan_label(vlan, ntype: str) -> str:
    if ntype in ("Tunnel", "Untagged"):
        return ntype
    return str(vlan) if vlan else "—"


async def _async_describe_network(network_name: str) -> None:
    from proliant.oneview.network import describe_network
    from proliant.oneview.topology import build_network_map, render_network_map_ascii
    from rich.panel import Panel

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching network '{network_name}'…[/dim]"):
            ov = await describe_network(client, network_name)
        with get_console().status(f"[dim]Building topology map for {ov['name']}…[/dim]"):
            nm = await build_network_map(client, ov["name"])

    if get_output_mode() == OutputMode.JSON:
        print_json({"overview": ov, "mapping": nm})
        return

    smart = "[green]Yes[/green]" if ov["smart_link"] else "No"
    priv = "Yes" if ov["private"] else "No"
    member_of = ", ".join(ov["member_of"]) if ov["member_of"] else "[dim]no network sets[/dim]"
    get_console().print(Panel(
        f"[bold]{ov['name']}[/bold]\n"
        f"Type:        {ov['type']}\n"
        f"VLAN:        {_vlan_label(ov['vlan'], ov['type'])}\n"
        f"Purpose:     {ov['purpose'] or '—'}\n"
        f"Bandwidth:   {_fmt_bw(ov['pref_bw_mbps'])} preferred  |  {_fmt_bw(ov['max_bw_mbps'])} maximum\n"
        f"Smart Link:  {smart}  |  Private: {priv}\n"
        f"Member of:   {member_of}\n"
        f"Status: {_status_style(ov['status'])}  |  State: {ov['state']}",
        title="Network", border_style="cyan",
    ))

    if ov["used_profiles"] or ov["used_templates"]:
        usage = make_table(
            f"Used By  ({len(ov['used_profiles'])} profiles, {len(ov['used_templates'])} templates)",
            ("Kind", {"no_wrap": True}),
            ("Name", {"no_wrap": True}),
            box_style=box.SIMPLE_HEAD, header_style="bold",
        )
        for nm_p in ov["used_profiles"]:
            usage.add_row("Server Profile", nm_p)
        for nm_t in ov["used_templates"]:
            usage.add_row("[cyan]Profile Template[/cyan]", nm_t)
        get_console().print(usage)
    else:
        get_console().print("[dim]Not used by any server profile or template.[/dim]")

    get_console().rule("[bold]Mapping[/bold]", style="cyan")
    get_console().print(render_network_map_ascii(nm, color=True), markup=True, highlight=False)


async def _async_describe_mac(mac: str) -> None:
    from proliant.oneview.topology import trace_mac, render_network_map

    async with _load_client() as client:
        with get_console().status(f"[dim]Tracing MAC {mac} across the fabric…[/dim]"):
            maps = await trace_mac(client, mac)

    if get_output_mode() == OutputMode.JSON:
        print_json(maps)
        return
    if not maps:
        get_console().print(f"[yellow]MAC {mac} not found in any forwarding table.[/yellow]")
        return
    for i, nm in enumerate(maps):
        if i:
            get_console().rule(style="dim")
        get_console().print(render_network_map(nm, mac=mac))


async def _cmd_network_describe(args: argparse.Namespace) -> None:
    await _async_describe_network(args.name)


async def _cmd_mac_describe(args: argparse.Namespace) -> None:
    await _async_describe_mac(args.address)


# ── proliant oneview enclosures list ─────────────────────────────────────────

async def _async_enclosures_list() -> None:
    from proliant.oneview.enclosures import list_enclosures

    async with _load_client() as client:
        with get_console().status("[dim]Fetching enclosures…[/dim]"):
            encs = await list_enclosures(client)

    if not encs:
        get_console().print("[yellow]No enclosures found.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(encs)
        return

    table = make_table(
        f"Enclosures  ({len(encs)} total)",
        ("Name",   {"min_width": 20, "no_wrap": True}),
        ("Model",  {"no_wrap": True}),
        ("Serial", {"no_wrap": True}),
        ("Status", {"justify": "center", "no_wrap": True}),
        ("State",  {"justify": "center", "no_wrap": True}),
    )
    for e in encs:
        table.add_row(e["name"], e["model"], e["serial"], _status_style(e["status"]), e["state"])
    get_console().print(table)


async def _cmd_enclosures_list(args: argparse.Namespace) -> None:
    await _async_enclosures_list()


# ── proliant oneview enclosure-groups list ────────────────────────────────────

async def _async_enclosure_groups_list() -> None:
    from proliant.oneview.enclosures import list_enclosure_groups

    async with _load_client() as client:
        with get_console().status("[dim]Fetching enclosure groups…[/dim]"):
            egs = await list_enclosure_groups(client)

    if not egs:
        get_console().print("[yellow]No enclosure groups found.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(egs)
        return

    table = make_table(
        f"Enclosure Groups  ({len(egs)} total)",
        ("Name",   {"min_width": 24, "no_wrap": True}),
        ("LIGs",   {"no_wrap": False}),
        ("Status", {"justify": "center", "no_wrap": True}),
    )
    for eg in egs:
        ligs = ", ".join(eg["lig_names"]) if eg["lig_names"] else "—"
        table.add_row(eg["name"], ligs, _status_style(eg["status"]))
    get_console().print(table)


async def _cmd_enclosure_groups_list(args: argparse.Namespace) -> None:
    await _async_enclosure_groups_list()


# ── proliant oneview logical-enclosures list ──────────────────────────────────

async def _async_logical_enclosures_list() -> None:
    from proliant.oneview.enclosures import list_logical_enclosures

    async with _load_client() as client:
        with get_console().status("[dim]Fetching logical enclosures…[/dim]"):
            les = await list_logical_enclosures(client)

    if not les:
        get_console().print("[yellow]No logical enclosures found.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(les)
        return

    table = make_table(
        f"Logical Enclosures  ({len(les)} total)",
        ("Name",             {"min_width": 20, "no_wrap": True}),
        ("Enclosure Group",  {"no_wrap": True}),
        ("Enclosures",       {"no_wrap": False}),
        ("Logical ICs",      {"no_wrap": False}),
        ("Status",           {"justify": "center", "no_wrap": True}),
        ("State",            {"justify": "center", "no_wrap": True}),
    )
    for le in les:
        encs = ", ".join(le["enclosures"]) if le["enclosures"] else "—"
        lis  = ", ".join(le["lis"]) if le["lis"] else "—"
        table.add_row(
            le["name"], le["eg_name"], encs, lis,
            _status_style(le["status"]), le["state"],
        )
    get_console().print(table)


async def _cmd_logical_enclosures_list(args: argparse.Namespace) -> None:
    await _async_logical_enclosures_list()




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
  proliant oneview networks describe VLAN-160            Network overview + fabric mapping
  proliant oneview networksets list                      All network sets
  proliant oneview uplinksets list                       All uplink sets
  proliant oneview server-profiles list                  All server profiles
  proliant oneview li list                               Logical interconnects
  proliant oneview lig list                              Logical interconnect groups
  proliant oneview interconnects list                    Interconnect hardware
  proliant oneview mac list --address 00:11:22:33:44:55  MAC forwarding table by address
  proliant oneview mac list --vlan 100                   MAC forwarding table by VLAN
  proliant oneview mac describe 00:11:22:33:44:55        Trace a MAC end-to-end through the fabric
  proliant oneview enclosures list                       Physical enclosures
  proliant oneview enclosure-groups list                 Enclosure groups
  proliant oneview logical-enclosures list               Logical enclosures
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

    p_networks = sub.add_parser("networks", aliases=["network"], help="List or describe ethernet networks")
    s_networks = p_networks.add_subparsers(dest="what", metavar="ACTION")
    s_networks.required = True
    p_net = s_networks.add_parser("list", help="List all ethernet networks")
    p_net.set_defaults(func=_cmd_networks_list)
    p_net_desc = s_networks.add_parser("describe",
        help="Network overview + end-to-end fabric mapping")
    arg_net_name = p_net_desc.add_argument("name", metavar="NAME",
        help="Name of the ethernet network (e.g. VLAN-160)")
    arg_net_name.completer = _oneview_network_name_completer
    p_net_desc.set_defaults(func=_cmd_network_describe)

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

    # ── logical interconnects ─────────────────────────────────────────────
    p_li = sub.add_parser("li", help="List logical interconnects")
    s_li = p_li.add_subparsers(dest="what", metavar="ACTION")
    s_li.required = True
    s_li.add_parser("list", help="List all logical interconnects").set_defaults(func=_cmd_li_list)

    p_lig = sub.add_parser("lig", help="List logical interconnect groups")
    s_lig = p_lig.add_subparsers(dest="what", metavar="ACTION")
    s_lig.required = True
    s_lig.add_parser("list", help="List all logical interconnect groups").set_defaults(func=_cmd_lig_list)

    p_ics = sub.add_parser("interconnects", aliases=["interconnect"], help="List interconnect hardware")
    s_ics = p_ics.add_subparsers(dest="what", metavar="ACTION")
    s_ics.required = True
    s_ics.add_parser("list", help="List all interconnect hardware").set_defaults(func=_cmd_interconnects_list)

    # ── mac address table ─────────────────────────────────────────────────
    p_mac = sub.add_parser("mac", help="Query MAC forwarding-information-base")
    s_mac = p_mac.add_subparsers(dest="what", metavar="ACTION")
    s_mac.required = True
    p_mac_list = s_mac.add_parser("list", help="Show MAC address table entries")
    p_mac_list.add_argument("--address", "-a", metavar="MAC",
        help="Filter by MAC address (e.g. 00:9C:02:73:33:6D)")
    p_mac_list.add_argument("--vlan", "-v", metavar="VLAN", type=int,
        help="Filter by VLAN ID (e.g. 100)")
    arg_nn = p_mac_list.add_argument("--network-name", "-n", metavar="NAME", dest="network_name",
        help="Filter by network name substring (e.g. ACI-Tunnel-Net)")
    arg_nn.completer = _oneview_network_name_completer
    p_mac_list.set_defaults(func=_cmd_mac_list)

    p_mac_desc = s_mac.add_parser("describe",
        help="Trace a MAC address end-to-end through the fabric")
    p_mac_desc.add_argument("address", metavar="MAC",
        help="MAC address to trace (e.g. 00:9C:02:73:33:6D)")
    p_mac_desc.set_defaults(func=_cmd_mac_describe)

    # ── enclosures ────────────────────────────────────────────────────────
    p_encs = sub.add_parser("enclosures", aliases=["enclosure"], help="List physical enclosures")
    s_encs = p_encs.add_subparsers(dest="what", metavar="ACTION")
    s_encs.required = True
    s_encs.add_parser("list", help="List all enclosures").set_defaults(func=_cmd_enclosures_list)

    p_egs = sub.add_parser("enclosure-groups", aliases=["enclosure-group"], help="List enclosure groups")
    s_egs = p_egs.add_subparsers(dest="what", metavar="ACTION")
    s_egs.required = True
    s_egs.add_parser("list", help="List all enclosure groups").set_defaults(func=_cmd_enclosure_groups_list)

    p_les = sub.add_parser("logical-enclosures", aliases=["logical-enclosure"], help="List logical enclosures")
    s_les = p_les.add_subparsers(dest="what", metavar="ACTION")
    s_les.required = True
    s_les.add_parser("list", help="List all logical enclosures").set_defaults(func=_cmd_logical_enclosures_list)

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
    try:
        run_sync(args.func(args))
    except (ValueError, RuntimeError) as exc:
        from rich.markup import escape
        get_console().print(f"[red]Error:[/red] {escape(str(exc))}", highlight=False)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
