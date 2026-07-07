"""
proliant.oneview.cli — OneView subcommands.

Usage:
    proliant oneview servers list [--fields ...]
    proliant oneview servers firmware list [--server NAME]
    proliant oneview firmware bundles list
    proliant oneview firmware repository list
    proliant oneview firmware compliance list
"""

# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from contextlib import asynccontextmanager


from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from proliant.common.completers import comma_sep_completer, suppress_file_completion
from proliant.common.display import get_console, get_output_mode, make_table as _make_table, OutputMode, print_json, print_memory_report, set_output_mode
from proliant.common.runner import run_sync

_SERVER_FIELDS = ("name", "model", "serial", "ilo", "ilo_ip", "power", "state", "profile")


# ── helpers ───────────────────────────────────────────────────────────────────

def make_table(title: str, *columns: tuple[str, dict], box_style=box.SIMPLE_HEAD, **kwargs) -> Table:
    kwargs.setdefault("header_style", "bold")
    return _make_table(title, *columns, box_style=box_style, **kwargs)


def _short_server_name(name: str) -> str:
    match = re.match(r"^Enclosure[-_ ]?(\d+),\s*bay\s*(\d+)\s*$", name or "", re.IGNORECASE)
    if match:
        return f"Enc-{match.group(1)} bay {match.group(2)}"
    return (name or "").replace("Enclosure", "Enc")


def _load_client():
    """Yield a connected OneViewClient, showing a "Connecting..." hint.

    A wrong/unreachable appliance host previously left the terminal looking
    frozen with zero feedback until the login handshake finally timed out.
    Print a status hint for the duration of the connect/login only -- it
    disappears the moment we get a real response (success or failure).
    """
    from proliant.oneview.config import load_oneview_config
    from proliant.oneview.client import OneViewClient

    cfg = load_oneview_config()
    client = OneViewClient(cfg["host"], cfg["username"], cfg["password"])

    @asynccontextmanager
    async def _connect():
        with get_console().status(f"[dim]Connecting to OneView at {cfg['host']}…[/dim]"):
            await client.__aenter__()
        try:
            yield client
        finally:
            await client.__aexit__(None, None, None)

    return _connect()


def _oneview_cached_object_names(cache_key: str, uri: str) -> list[str]:
    """Fetch all object names for a OneView REST collection, cached briefly on disk.

    Every completer call re-runs the whole CLI process (argcomplete invokes
    it fresh per keystroke), so an uncached fetch pays a full OneView login
    handshake + GET + logout each time -- a couple of seconds per TAB press.
    `cached_names()` keeps the last fetch on disk for a short TTL so a burst
    of TAB presses while typing one command is effectively instant.
    """
    from proliant.oneview.config import load_oneview_config
    from proliant.oneview.client import OneViewClient
    from proliant.common.completers import cached_names

    cfg = load_oneview_config()

    def _fetch_names() -> list[str]:
        async def _fetch() -> list[str]:
            async with OneViewClient(cfg["host"], cfg["username"], cfg["password"]) as client:
                items = await client.get_all(uri)
                return [item["name"] for item in items if item.get("name")]

        return asyncio.run(_fetch())

    return cached_names(f"oneview-{cache_key}-{cfg['host']}", _fetch_names)


def _oneview_network_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete --network-name by querying OneView ethernet networks."""
    try:
        names = _oneview_cached_object_names("networks", "/rest/ethernet-networks")
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _oneview_networkset_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete networkset name by querying OneView network sets."""
    try:
        names = _oneview_cached_object_names("networksets", "/rest/network-sets")
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _oneview_server_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete server names by querying OneView server hardware."""
    try:
        names = _oneview_cached_object_names("servers", "/rest/server-hardware")
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _oneview_uplinkset_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete uplink set names by querying OneView."""
    try:
        names = _oneview_cached_object_names("uplinksets", "/rest/uplink-sets")
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _oneview_profile_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete server profile names by querying OneView."""
    try:
        names = _oneview_cached_object_names("profiles", "/rest/server-profiles")
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _oneview_enclosure_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete enclosure names by querying OneView."""
    try:
        names = _oneview_cached_object_names("enclosures", "/rest/enclosures")
        return [n for n in names if n.lower().startswith(prefix.lower())]
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
    if _status_rank(state) >= 2:
        return _status_style(state)
    return state or ""


def _status_style(status: str | None) -> str:
    if not status:
        return ""
    s = status.lower()
    if s == "ok":
        return "OK"
    if s in ("warning", "degraded"):
        return f"[yellow]{status}[/yellow]"
    if s == "critical":
        return f"[red]{status}[/red]"
    return status


def _status_rank(status: str | None) -> int:
    s = (status or "").lower()
    if s == "critical":
        return 3
    if s in ("warning", "degraded"):
        return 2
    if s == "ok":
        return 1
    return 0


def _worst_status(*statuses: str | None) -> str:
    worst = ""
    for status in statuses:
        if _status_rank(status) > _status_rank(worst):
            worst = status or ""
    return worst


def _alert_marker(status: str | None) -> str:
    s = (status or "").lower()
    if s == "critical":
        return "◆"
    if s in ("warning", "degraded"):
        return "▲"
    return ""


def _section_banner(title: str, style: str = "bold cyan") -> Text:
    return Text.assemble("########### ", (title, style), " ###########")


def _alert_message_text(status: str | None, message: str) -> Text:
    marker = _alert_marker(status)
    rendered = Text()
    if marker:
        rendered.append(marker, style="red" if marker == "◆" else "yellow")
        rendered.append(" ")
    rendered.append(message)
    return rendered


def _style_alert_markers(text: str) -> Text:
    rendered = Text(text)
    for offset, char in enumerate(text):
        if char == "◆":
            rendered.stylize("red", offset, offset + 1)
        elif char == "▲":
            rendered.stylize("yellow", offset, offset + 1)
    return rendered


def _style_dim_phrases(text: str, phrases: list[str]) -> Text:
    rendered = _style_alert_markers(text)
    for phrase in phrases:
        if not phrase:
            continue
        start = 0
        while True:
            index = text.find(phrase, start)
            if index == -1:
                break
            rendered.stylize("grey50", index, index + len(phrase))
            start = index + len(phrase)
    return rendered


def _table_alert_marker(status: str | None) -> str:
    marker = _alert_marker(status)
    if marker == "◆":
        return "[red]◆[/red]"
    if marker == "▲":
        return "[yellow]▲[/yellow]"
    return ""


def _profile_alert_messages(profile: dict) -> list[str]:
    messages = []
    server_status = profile.get("server_status", "")
    if _status_rank(server_status) >= 2:
        state = profile.get("server_state") or "unknown"
        messages.append(f"Server hardware status is {server_status} (state: {state}).")

    for connection in profile.get("connections") or []:
        status = str(connection.get("status") or "")
        if not status or status.lower() in ("ok", "normal"):
            continue
        name = connection.get("name") or f"connection {connection.get('id', '')}".strip()
        details = []
        state = connection.get("state")
        if state:
            details.append(f"state: {state}")
        port = connection.get("port_id")
        if port:
            details.append(f"port: {port}")
        allocated = connection.get("allocated_mbps")
        if allocated not in (None, ""):
            details.append(f"allocated: {allocated} Mbps")
        suffix = f" ({', '.join(details)})" if details else ""
        messages.append(f"Connection {name} is {status}{suffix}.")

    for label, value in (
        ("Firmware", profile.get("fw_consistency")),
        ("BIOS", profile.get("bios_consistency")),
    ):
        value_text = str(value or "")
        if value_text and value_text.lower() not in ("consistent", "unknown"):
            messages.append(f"{label} consistency is {value_text}.")

    if not messages and _status_rank(profile.get("status")) >= 2:
        messages.append("No detailed alert message was returned by OneView.")
    return messages


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

    all_fields = list(_SERVER_FIELDS)
    show = fields if fields else all_fields

    col_map = {
        "name":    ("Name",        dict(min_width=12, no_wrap=True)),
        "model":   ("Model",       dict(min_width=10, no_wrap=True)),
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
                row.append(_short_server_name(s["name"]))
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


# ── proliant oneview servers firmware list ────────────────────────────────────────

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
            _state_style(n["state"]),
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
            _status_style(s["status"]), _state_style(s["state"]),
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
            _status_style(p["status"]), _state_style(p["state"]),
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
    from proliant.oneview.topology import build_network_set_map, render_network_map_ascii
    from rich.panel import Panel

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching network set '{name}'…[/dim]"):
            s = await describe_network_set(client, name)
        with get_console().status(f"[dim]Building fabric map for {name}…[/dim]"):
            nm = await build_network_set_map(client, name)

    if get_output_mode() == OutputMode.JSON:
        print_json({"overview": s, "mapping": nm})
        return

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

    get_console().rule("[bold]Mapping[/bold]", style="cyan")
    get_console().print(render_network_map_ascii(nm, color=True), markup=True, highlight=False)


async def _async_describe_profile(name: str) -> None:
    from proliant.oneview.profiles import describe_profile

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching profile '{name}'…[/dim]"):
            p = await describe_profile(client, name)

    if get_output_mode() == OutputMode.JSON:
        print_json(p)
        return

    console = get_console()
    console.print(f"[bold]{p['name']}[/bold]")
    console.print(f"Status: {_status_style(p['status']) or '—'}  |  State: {p['state'] or '—'}")

    alert_messages = _profile_alert_messages(p)
    if alert_messages:
        alert_style = "red" if _status_rank(p["status"]) >= 3 else "yellow"
        console.print(_section_banner("Alert", f"bold {alert_style}"), highlight=False)
        for message in alert_messages:
            console.print(_alert_message_text(p.get("status"), message), highlight=False)

    console.print(_section_banner("General"), highlight=False)
    general = make_table(
        "",
        ("Field", {"no_wrap": True, "style": "bold"}),
        ("Value", {"min_width": 36}),
        box_style=box.SIMPLE_HEAD,
        show_header=False,
    )
    general_rows = [
        ("Description", p["description"] or "—"),
        ("Server profile template", p.get("template_name") or "—"),
        ("Server hardware", p.get("server_name") or "—"),
        ("Server hardware type", p.get("server_hardware_type") or "—"),
        ("Server hardware status", f"{_status_style(p.get('server_status'))}  |  State: {p.get('server_state') or '—'}"),
        ("Enclosure group", p.get("eg_name") or "—"),
        ("Affinity", p.get("affinity") or "—"),
        ("Server power", p.get("server_power") or "—"),
        ("Server serial", p.get("server_serial") or "—"),
        ("Profile serial", f"{p.get('serial_number') or '—'}  ({p.get('serial_number_type') or '—'})"),
    ]
    for field, value in general_rows:
        general.add_row(field, str(value))
    console.print(general)

    console.print(_section_banner("Firmware"), highlight=False)
    firmware = make_table(
        "",
        ("Field", {"no_wrap": True, "style": "bold"}),
        ("Value", {"min_width": 36}),
        box_style=box.SIMPLE_HEAD,
        show_header=False,
    )
    firmware_rows = [
        ("Firmware baseline", " ".join(part for part in [p.get("fw_baseline", ""), p.get("fw_version", "")] if part) or "—"),
        ("Manage firmware", "Yes" if p.get("manage_fw") else "No"),
        ("Install type", p.get("fw_install_type") or "—"),
        ("Activation", p.get("fw_activation_type") or "—"),
        ("Install action", p.get("fw_install_action") or "—"),
        ("Consistency", p.get("fw_consistency") or "—"),
    ]
    for field, value in firmware_rows:
        firmware.add_row(field, str(value))
    console.print(firmware)

    if p["connections"]:
        console.print(_section_banner("Connections"), highlight=False)
        conn_table = make_table(
            "",
            ("ID",       {"justify": "center", "no_wrap": True}),
            ("Name",     {"min_width": 12, "no_wrap": True}),
            ("Network",  {"min_width": 18, "no_wrap": True}),
            ("Port",     {"no_wrap": True}),
            ("MAC",      {"no_wrap": True}),
            ("Speed",    {"justify": "right", "no_wrap": True}),
            ("State",    {"no_wrap": True}),
            ("Status",   {"justify": "center", "no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
            header_style="bold",
        )
        for c in p["connections"]:
            speed = str(c.get("allocated_mbps") or c.get("requested_mbps") or "")
            if speed:
                speed = f"{speed} Mbps"
            conn_table.add_row(
                str(c.get("id", "")),
                c.get("name", ""),
                c.get("network", ""),
                c.get("port_id", ""),
                c.get("mac", ""),
                speed,
                c.get("state", ""),
                _status_style(c.get("status")),
            )
        console.print(conn_table)

    console.print(_section_banner("Boot Settings"), highlight=False)
    boot = make_table("", ("Field", {"style": "bold"}), ("Value", {}), box_style=box.SIMPLE_HEAD, show_header=False)
    boot.add_row("Manage boot", "Yes" if p.get("manage_boot") else "No")
    boot.add_row("Boot order", ", ".join(p["boot_order"]) if p["boot_order"] else "—")
    console.print(boot)

    console.print(_section_banner("BIOS Settings"), highlight=False)
    bios = make_table("", ("Field", {"style": "bold"}), ("Value", {}), box_style=box.SIMPLE_HEAD, show_header=False)
    bios.add_row("Manage BIOS", "Yes" if p.get("manage_bios") else "No")
    bios.add_row("Consistency", p.get("bios_consistency") or "—")
    bios.add_row("Overrides", str(len(p.get("bios_overrides") or [])))
    console.print(bios)

    console.print(_section_banner("Advanced"), highlight=False)
    advanced = make_table("", ("Field", {"style": "bold"}), ("Value", {}), box_style=box.SIMPLE_HEAD, show_header=False)
    advanced.add_row("MAC addresses", p.get("mac_type") or "—")
    advanced.add_row("WWN addresses", p.get("wwn_type") or "—")
    advanced.add_row("iSCSI initiator", f"{p.get('iscsi_initiator_name_type') or '—'}  {p.get('iscsi_initiator_name') or ''}".rstrip())
    console.print(advanced)


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
            _status_style(li["status"]), _state_style(li["state"]),
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
        table.add_row(lg["name"], _state_style(lg["state"]))
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
            _status_style(ic["status"]), _state_style(ic["state"]), ic["serial"],
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

    columns = [
        ("MAC Address",    {"no_wrap": True, "min_width": 18}),
        ("Interconnect",   {"no_wrap": True}),
        ("Port",           {"no_wrap": True}),
    ]
    show_profile_columns = any(e.get("profile") or e.get("connection") for e in entries)
    if show_profile_columns:
        columns.extend([
            ("Server Profile", {"no_wrap": True}),
            ("Connection",     {"no_wrap": True}),
        ])
    show_last_updated = any(e.get("last_updated") for e in entries)
    if show_last_updated:
        columns.append(("Last Updated", {"no_wrap": True}))
    table = make_table(f"MAC Address Table  ({len(entries)} entries)", *columns)
    for e in entries:
        row = [
            e["mac"], _short_ic_name(e["ic_name"]), e["port"],
        ]
        if show_profile_columns:
            row.extend([
                e.get("profile") or "[dim]—[/dim]",
                e.get("connection") or "[dim]—[/dim]",
            ])
        if show_last_updated:
            row.append(e.get("last_updated") or "")
        table.add_row(*row)
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
    from proliant.oneview.topology import trace_mac, render_network_map_ascii

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
        get_console().print(render_network_map_ascii(nm, mac=mac, color=True), markup=True, highlight=False)


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
        table.add_row(e["name"], e["model"], e["serial"], _status_style(e["status"]), _state_style(e["state"]))
    get_console().print(table)


async def _cmd_enclosures_list(args: argparse.Namespace) -> None:
    await _async_enclosures_list()


def _short_model(model: str, limit: int = 30) -> str:
    if len(model) <= limit:
        return model
    return model[:limit - 1].rstrip() + "…"


def _short_bay_label(name: str, fallback: str) -> str:
    if not name:
        return fallback
    if ", " in name:
        return name.split(", ", 1)[1]
    return name


def _fit(value: object, width: int) -> str:
    text = str(value or "")
    if len(text) > width:
        text = text[:max(0, width - 1)].rstrip() + "…"
    return text.ljust(width)


def _equal_widths(columns: int, total_width: int) -> list[int]:
    available = total_width - 2 - (columns - 1)
    base = max(1, available // columns)
    remainder = max(0, available - (base * columns))
    return [base + (1 if index < remainder else 0) for index in range(columns)]


def _front_bay_height(cell_width: int) -> int:
    height = max(10, min(14, round(cell_width * 0.66)))
    return height if height % 2 == 0 else min(14, height + 1)


def _line(left: str, separator: str, right: str, widths: list[int]) -> str:
    return left + separator.join("─" * width for width in widths) + right


def _frame_title(title: str, total_width: int) -> str:
    return title.center(total_width)


def _cell(lines: list[str], width: int, height: int) -> list[str]:
    content = lines[:height]
    content.extend([""] * (height - len(content)))
    return [_fit(line, width) for line in content]


def _grid_row(cells: list[list[str]], widths: list[int], height: int) -> list[str]:
    prepared = [_cell(lines, width, height) for lines, width in zip(cells, widths)]
    rows = []
    for line_index in range(height):
        rows.append("│" + "│".join(cell[line_index] for cell in prepared) + "│")
    return rows


def _full_width_row(lines: list[str], total_width: int, height: int = 1) -> list[str]:
    width = total_width - 2
    return ["│" + line + "│" for line in _cell(lines, width, height)]


def _full_width_label_row(left: str, right: str, total_width: int) -> str:
    width = total_width - 2
    left_text = str(left or "")
    right_text = str(right or "")
    gap = max(1, width - len(left_text) - len(right_text))
    return "│" + (left_text + (" " * gap) + right_text)[:width].ljust(width) + "│"


def _bay_rows(count: int, columns: int) -> list[list[int]]:
    return [list(range(start, min(start + columns, count + 1))) for start in range(1, count + 1, columns)]


def _front_bay_columns(count: int) -> int:
    if count >= 12:
        return 6
    if count >= 8:
        return 4
    return max(1, min(count, 4))


def _rear_bay_columns(count: int) -> int:
    return max(1, min(count, 3))


def _bay_tile(title: str, body: str, border_style: str) -> Panel:
    return Panel(
        body,
        title=title,
        title_align="left",
        border_style=border_style,
        box=box.SQUARE,
        padding=(0, 1),
        expand=True,
    )


def _empty_bay_tile(bay: int) -> Panel:
    return _bay_tile(f"[bold]{bay}[/bold]", "[dim italic]Empty[/dim italic]\n[dim]No device[/dim]\n", "grey35")


def _empty_bay_text(bay: int) -> str:
    return f"[bold]{bay}[/bold]\n\n[dim italic]empty[/dim italic]\n\n"


def _bay_grid(title: str, count: int, items: list[dict], cell_fn, columns: int) -> object:
    if count <= 0:
        return Panel("[dim]No bays reported.[/dim]", title=title, border_style="cyan")

    if columns == 6 and get_console().width < 112:
        columns = 3

    table = Table.grid(expand=True, padding=(0, 1))
    for _ in range(columns):
        table.add_column(ratio=1, min_width=14)

    by_bay = {item["bay"]: item for item in items}
    for bay_row in _bay_rows(count, columns):
        row = []
        for bay in bay_row:
            item = by_bay.get(bay)
            row.append(cell_fn(bay, item) if item else _empty_bay_tile(bay))
        while len(row) < columns:
            row.append("")
        table.add_row(*row)
    return Panel(table, title=title, border_style="cyan", box=box.ROUNDED, padding=(1, 1))


def _server_bay_lines(bay: int, server: dict) -> list[str]:
    serial = server.get("serial", "")
    label = server.get("profile") or serial
    status = _worst_status(server.get("status"), server.get("profile_status"))
    marker = _alert_marker(status)
    state = server.get("state") or server.get("profile_state") or ""
    state_line = state if state and state.lower() not in ("profileapplied", "normal") else ""
    return [
        f"{bay}  {marker}".rstrip(),
        label,
        server.get("model", ""),
        state_line,
        "",
    ]


def _server_bay_text(bay: int, server: dict) -> str:
    return "\n".join(_server_bay_lines(bay, server))


def _appliance_bay_text(index: int, appliance: dict | None = None) -> str:
    return "\n".join(_appliance_bay_lines(index, appliance))


def _appliance_bay_lines(index: int, appliance: dict | None = None) -> list[str]:
    if not appliance:
        return [f"C{index}", "", "", "Appliance", "bay"]
    model = appliance.get("model") or "Appliance"
    serial = appliance.get("serial") or ""
    status = appliance.get("status") or ""
    status_line = status if status and status.lower() != "ok" else ""
    return [f"C{index}", model, serial, status_line, ""]


def _empty_bay_lines(bay: int) -> list[str]:
    return [str(bay), "", "empty", "", ""]


def _front_chassis_frame(title: str, count: int, servers: list[dict], appliances: list[dict] | None = None) -> object:
    if count < 12 or get_console().width < 118:
        return _bay_grid(title, count, servers, _server_bay_cell, _front_bay_columns(count))

    total_width = min(get_console().width, 150)
    widths = _equal_widths(7, total_width)
    bay_height = _front_bay_height(widths[1])
    by_bay = {server["bay"]: server for server in servers}
    by_appliance_bay = {appliance["bay"]: appliance for appliance in appliances or []}
    top = [_appliance_bay_lines(1, by_appliance_bay.get(1))]
    bottom = [_appliance_bay_lines(2, by_appliance_bay.get(2))]
    for bay in range(1, 7):
        server = by_bay.get(bay)
        top.append(_server_bay_lines(bay, server) if server else _empty_bay_lines(bay))
    for bay in range(7, 13):
        server = by_bay.get(bay)
        bottom.append(_server_bay_lines(bay, server) if server else _empty_bay_lines(bay))

    lines = [
        _frame_title(title, total_width),
        _line("┌", "┬", "┐", widths),
        *_grid_row(top, widths, bay_height),
        _line("├", "┼", "┤", widths),
        *_grid_row(bottom, widths, bay_height),
        _line("└", "┴", "┘", widths),
    ]
    return _style_alert_markers("\n".join(lines))


def _interconnect_bay_lines(bay: int, interconnect: dict) -> list[str]:
    label = _short_bay_label(interconnect.get("name", ""), "interconnect")
    power = _watts(interconnect.get("power_allocation_watts"))
    power_line = f"   {power} allocated" if power else ""
    return [f"{bay}  {label}", f"   {interconnect.get('model', '')}", power_line]


def _interconnect_bay_text(bay: int, interconnect: dict) -> str:
    return "\n".join(_interconnect_bay_lines(bay, interconnect))


def _power_supply_lines(bay: int, power_supply: dict | None = None) -> list[str]:
    if not power_supply:
        return [f"Power Supply {bay}", "empty", ""]
    status = power_supply.get("status") or ""
    status_line = status if status and status.lower() != "ok" else ""
    return [
        f"Power Supply {bay}",
        _short_power_supply_model(power_supply.get("model", "")),
        status_line,
    ]


def _short_power_supply_model(model: str) -> str:
    return model.replace(" Hot Plug Power Supply", "").replace(" Power Supply", "")


def _rear_chassis_frame(title: str, count: int, interconnects: list[dict], power_supplies: list[dict] | None = None) -> object:
    if count < 6 or get_console().width < 118:
        return _bay_grid(title, count, interconnects, _interconnect_bay_cell, _rear_bay_columns(count))

    total_width = min(get_console().width, 150)
    fan_widths = _equal_widths(6, total_width)
    power_widths = _equal_widths(3, total_width)
    by_bay = {interconnect["bay"]: interconnect for interconnect in interconnects}
    power_by_bay = {power_supply["bay"]: power_supply for power_supply in power_supplies or []}
    bay3 = by_bay.get(3)
    bay6 = by_bay.get(6)
    top_power_rows = [_power_supply_lines(bay, power_by_bay.get(bay)) for bay in range(1, 4)]
    bottom_power_rows = [_power_supply_lines(bay, power_by_bay.get(bay)) for bay in range(4, 7)]
    dim_phrases = [line for row in top_power_rows + bottom_power_rows for line in row if line]

    lines = [
        _frame_title(title, total_width),
        _line("┌", "─", "┐", [total_width - 2]),
        _full_width_label_row("1", "empty", total_width),
        _line("├", "─", "┤", [total_width - 2]),
        _full_width_label_row("2", "empty", total_width),
        _line("├", "┬", "┤", fan_widths),
        *_grid_row([["FLM1"], ["Fan 1"], ["Fan 2"], ["Fan 3"], ["Fan 4"], ["Fan 5"]], fan_widths, 3),
        _line("├", "─", "┤", [total_width - 2]),
        *_full_width_row(_interconnect_bay_lines(3, bay3) if bay3 else _empty_bay_lines(3)[:2], total_width, 3),
        _line("├", "┬", "┤", power_widths),
        *_grid_row(top_power_rows, power_widths, 3),
        _line("├", "─", "┤", [total_width - 2]),
        _full_width_label_row("4", "empty", total_width),
        _line("├", "─", "┤", [total_width - 2]),
        _full_width_label_row("5", "empty", total_width),
        _line("├", "┬", "┤", fan_widths),
        *_grid_row([["FLM2"], ["Fan 6"], ["Fan 7"], ["Fan 8"], ["Fan 9"], ["Fan 10"]], fan_widths, 3),
        _line("├", "─", "┤", [total_width - 2]),
        *_full_width_row(_interconnect_bay_lines(6, bay6) if bay6 else _empty_bay_lines(6)[:2], total_width, 3),
        _line("├", "┬", "┤", power_widths),
        *_grid_row(bottom_power_rows, power_widths, 3),
        _line("└", "┴", "┘", power_widths),
    ]
    return _style_dim_phrases("\n".join(lines), dim_phrases)


def _front_frame_ratios(count: int) -> list[int]:
    return [1, 1, 1, 1, 1, 1, 1] if count >= 12 else [1] * max(1, count)


def _rear_frame_ratios(count: int) -> list[int]:
    return [1, 1, 1, 1, 1, 1, 1] if count >= 6 else [1] * max(1, count)


def _watts(value: object) -> str:
    return f"{value}W" if value else ""


def _print_enclosure_detail_tables(enclosure: dict) -> None:
    console = get_console()

    firmware = enclosure.get("firmware") or []
    if firmware:
        table = make_table(
            f"Firmware ({len(firmware)})",
            ("Name", {"min_width": 22, "no_wrap": True}),
            ("Component", {"min_width": 28}),
            ("Installed", {"no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
        )
        for item in firmware:
            table.add_row(item["name"], item["component"], item["installed"])
        console.print(table)

    devices = enclosure.get("devices") or []
    if devices:
        table = make_table(
            f"Devices ({len(devices)})",
            ("Bay", {"justify": "right", "no_wrap": True}),
            ("", {"justify": "center", "no_wrap": True}),
            ("Hardware", {"min_width": 18, "no_wrap": True}),
            ("Server Name", {"min_width": 20, "no_wrap": True}),
            ("Model", {"min_width": 18, "no_wrap": True}),
            ("Server Profile", {"min_width": 18, "no_wrap": True}),
            ("Watts", {"justify": "right", "no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
        )
        for item in devices:
            status = _worst_status(item.get("status"), item.get("profile_status"))
            table.add_row(
                str(item["bay"]),
                _table_alert_marker(status),
                item["hardware"],
                item["server_name"],
                item["model"],
                item["profile"] or "[dim italic]none[/dim italic]",
                str(item["power_allocation_watts"]) if item["power_allocation_watts"] else "",
            )
        console.print(table)

    interconnects = enclosure.get("interconnects") or []
    if interconnects:
        table = make_table(
            f"Interconnects ({len(interconnects)})",
            ("Bay", {"justify": "right", "no_wrap": True}),
            ("Name", {"min_width": 20, "no_wrap": True}),
            ("Model", {"min_width": 28}),
            ("Serial", {"no_wrap": True}),
            ("Part", {"no_wrap": True}),
            ("Watts", {"justify": "right", "no_wrap": True}),
            ("Status", {"justify": "center", "no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
        )
        for item in interconnects:
            table.add_row(
                str(item["bay"]),
                item["name"],
                item["model"],
                item["serial"],
                item.get("part_number", ""),
                str(item.get("power_allocation_watts") or ""),
                _status_style(item["status"]),
            )
        console.print(table)

    fans = enclosure.get("fans") or []
    if fans:
        table = make_table(
            f"Fans ({len(fans)})",
            ("Bay", {"justify": "right", "no_wrap": True}),
            ("Model", {"min_width": 22}),
            ("Serial", {"no_wrap": True}),
            ("Part", {"no_wrap": True}),
            ("Spare", {"no_wrap": True}),
            ("Status", {"justify": "center", "no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
        )
        for item in fans:
            table.add_row(str(item["bay"]), item["model"], item["serial"], item["part_number"], item["spare_part_number"], _status_style(item["status"]))
        console.print(table)

    frame_link_modules = enclosure.get("frame_link_modules") or []
    if frame_link_modules:
        table = make_table(
            f"Frame Link Modules ({len(frame_link_modules)})",
            ("Bay", {"justify": "right", "no_wrap": True}),
            ("Role", {"no_wrap": True}),
            ("Model", {"min_width": 24}),
            ("FW", {"no_wrap": True}),
            ("Serial", {"no_wrap": True}),
            ("IP", {"no_wrap": True}),
            ("Status", {"justify": "center", "no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
        )
        for item in frame_link_modules:
            table.add_row(str(item["bay"]), item["role"], item["model"], item["fw_version"], item["serial"], item["ip_address"], _status_style(item["status"]))
        console.print(table)

    power_supplies = enclosure.get("power_supplies") or []
    if power_supplies:
        table = make_table(
            f"Power Supplies ({len(power_supplies)})",
            ("Bay", {"justify": "right", "no_wrap": True}),
            ("Model", {"min_width": 30}),
            ("Serial", {"no_wrap": True}),
            ("Capacity", {"justify": "right", "no_wrap": True}),
            ("Part", {"no_wrap": True}),
            ("Spare", {"no_wrap": True}),
            ("Status", {"justify": "center", "no_wrap": True}),
            box_style=box.SIMPLE_HEAD,
        )
        for item in power_supplies:
            table.add_row(str(item["bay"]), item["model"], item["serial"], _watts(item["capacity_watts"]), item["part_number"], item["spare_part_number"], _status_style(item["status"]))
        console.print(table)



def _server_bay_cell(bay: int, server: dict) -> Panel:
    profile = server.get("profile") or "no profile"
    power = server.get("power") or "—"
    state = server.get("state") or server.get("status") or "—"
    label = profile if server.get("profile") else _short_bay_label(server.get("name", ""), "server")
    border_style = "green" if power.lower() == "on" else "yellow" if power.lower() == "off" else "cyan"
    return _bay_tile(
        f"[bold]{bay}[/bold]  {_power_style(power)}",
        f"[cyan]{_short_model(label, 18)}[/cyan]\n"
        f"[dim]{_short_model(server.get('model', ''), 18)}[/dim]\n"
        f"[dim]{_short_model(state, 18)}[/dim]",
        border_style,
    )


def _interconnect_bay_cell(bay: int, interconnect: dict) -> Panel:
    status = interconnect.get("status") or "—"
    state = interconnect.get("state") or "—"
    li_name = interconnect.get("logical_interconnect") or "no logical IC"
    label = _short_bay_label(interconnect.get("name", ""), "interconnect")
    border_style = "green" if status.lower() == "ok" else "yellow" if status else "cyan"
    return _bay_tile(
        f"[bold]{bay}[/bold]  {_status_style(status)}",
        f"[cyan]{_short_model(label, 26)}[/cyan]\n"
        f"[dim]{_short_model(interconnect.get('model', ''), 26)}[/dim]\n"
        f"[dim]{_short_model(f'{li_name} / {state}', 26)}[/dim]",
        border_style,
    )


async def _async_enclosure_describe(name: str) -> None:
    from proliant.oneview.enclosures import describe_enclosure

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching enclosure '{name}'…[/dim]"):
            enclosure = await describe_enclosure(client, name)

    if get_output_mode() == OutputMode.JSON:
        print_json(enclosure)
        return

    details = [
        f"[bold]{enclosure['name']}[/bold]",
        f"Model:              {enclosure['model'] or '—'}",
        f"Serial:             {enclosure['serial'] or '—'}",
        f"Logical enclosure:  {enclosure['logical_enclosure'] or '—'}",
        f"Enclosure group:    {enclosure['enclosure_group'] or '—'}",
        f"Status: {_status_style(enclosure['status'])}  |  State: {enclosure['state'] or '—'}",
    ]
    get_console().print(Panel("\n".join(details), title="Enclosure", border_style="cyan"))
    front_frame = _front_chassis_frame("Front View — Server Bays", enclosure["front_bay_count"], enclosure["servers"], enclosure.get("appliances", []))
    rear_frame = _rear_chassis_frame("Rear View — Interconnect Bays", enclosure["rear_bay_count"], enclosure["interconnects"], enclosure.get("power_supplies", []))
    get_console().print(front_frame, highlight=False)
    get_console().print(rear_frame, highlight=False)
    _print_enclosure_detail_tables(enclosure)


async def _cmd_enclosures_describe(args: argparse.Namespace) -> None:
    await _async_enclosure_describe(args.name)


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
            _status_style(le["status"]), _state_style(le["state"]),
        )
    get_console().print(table)


async def _cmd_logical_enclosures_list(args: argparse.Namespace) -> None:
    await _async_logical_enclosures_list()


# ── proliant oneview upgrade readiness / cleanup ──────────────────────────────

_VERDICT_STYLE = {"PASS": "green", "WARN": "yellow", "FAIL": "red"}
_STATUS_STYLE = {"PASS": "[green]PASS[/green]", "WARN": "[yellow]WARN[/yellow]",
                 "FAIL": "[red]FAIL[/red]", "INFO": "[cyan]INFO[/cyan]"}


async def _async_upgrade_readiness() -> None:
    from proliant.oneview.upgrade import gather_readiness

    async with _load_client() as client:
        with get_console().status("[dim]Assessing OneView upgrade readiness…[/dim]"):
            report = await gather_readiness(client)

    if get_output_mode() == OutputMode.JSON:
        print_json(report)
        return

    console = get_console()
    app = report["appliance"]
    up = report["upgrade_path"]
    verdict = report["verdict"]
    vstyle = _VERDICT_STYLE.get(verdict, "white")

    header = [
        f"[bold]{app.get('model') or 'OneView appliance'}[/bold]  "
        f"({app.get('family') or '—'})",
        f"Current version:  [bold]{app.get('software_version') or '—'}[/bold]",
    ]
    if up.get("at_latest"):
        header.append(f"Upgrade path:     at/above latest ({up.get('latest')})")
    else:
        path = " -> ".join(up.get("path_to_latest", [])) or "—"
        header.append(f"Recommended next: [bold cyan]{up.get('recommended_next')}[/bold cyan]  "
                      f"(to latest {up.get('latest')}: {path})")
    header.append(f"\nReadiness verdict: [bold {vstyle}]{verdict}[/bold {vstyle}]")
    console.print(Panel("\n".join(header), title="OneView Upgrade Readiness", border_style=vstyle))

    table = make_table(
        "Readiness Checks",
        ("Check", {"min_width": 26, "no_wrap": True}),
        ("Status", {"justify": "center", "no_wrap": True}),
        ("Detail", {"min_width": 40}),
    )
    for c in report["checks"]:
        table.add_row(c["name"], _STATUS_STYLE.get(c["status"], c["status"]), c["detail"])
    console.print(table)

    stale = report.get("stale_baselines", {})
    if stale.get("count"):
        console.print(
            f"[dim]Tip:[/dim] {stale['count']} unused firmware baseline(s) using "
            f"[bold]{stale['reclaimable_gb']:.1f} GB[/bold] can be freed with "
            f"[bold]proliant oneview upgrade cleanup[/bold].",
            highlight=False,
        )
    if stale.get("external_unused"):
        console.print(
            f"[dim]Note:[/dim] {stale['external_unused']} additional unused baseline(s) exist "
            f"only in an external repository and are not deletable via OneView — see "
            f"[bold]proliant oneview upgrade cleanup[/bold] for details.",
            highlight=False,
        )
    console.print(f"[dim]Upgrade-path source: {up.get('source_url', '')}[/dim]", highlight=False)


async def _cmd_upgrade_readiness(args: argparse.Namespace) -> None:
    await _async_upgrade_readiness()


def _fmt_gb(size_bytes: int) -> str:
    gb = size_bytes / (1024 ** 3)
    return f"{gb:.2f} GB" if gb >= 1 else f"{size_bytes / (1024 ** 2):.0f} MB"


def _fmt_gb_value(gb: float) -> str:
    """Format a value already expressed in GB (e.g. from normalize_repositories)."""
    return f"{gb:.2f} GB" if gb >= 1 else f"{gb * 1024:.0f} MB"


# ── proliant oneview firmware bundles/repository/compliance ──────────────────
# Appliance/repository-level firmware views matching the OneView GUI's
# Firmware section — distinct from `servers firmware list` above, which is
# per-server component inventory.

async def _async_firmware_bundles_list() -> None:
    from proliant.oneview.firmware import list_bundles

    async with _load_client() as client:
        with get_console().status("[dim]Fetching firmware bundles…[/dim]"):
            bundles = await list_bundles(client)

    if get_output_mode() == OutputMode.JSON:
        print_json(bundles)
        return

    console = get_console()
    if not bundles:
        console.print("[yellow]No firmware bundles registered on this appliance.[/yellow]")
        return

    table = make_table(
        f"Firmware Bundles  ({len(bundles)})",
        ("Name", {"no_wrap": True}),
        ("Version", {"no_wrap": True}),
        ("Type", {"no_wrap": True}),
        ("Released", {"no_wrap": True}),
        ("Size", {"justify": "right", "no_wrap": True}),
        ("Repository", {"no_wrap": True, "style": "dim"}),
    )
    for b in bundles:
        released = (b.get("release_date") or "")[:10]
        table.add_row(b["name"], b["version"], b.get("bundle_type", ""), released,
                      _fmt_gb(b["size_bytes"]), b.get("repository_names", ""))
    console.print(table)


async def _cmd_firmware_bundles_list(args: argparse.Namespace) -> None:
    await _async_firmware_bundles_list()


async def _async_firmware_repository_list() -> None:
    from proliant.oneview.firmware import list_repositories

    async with _load_client() as client:
        with get_console().status("[dim]Fetching firmware repositories…[/dim]"):
            repos = await list_repositories(client)

    if get_output_mode() == OutputMode.JSON:
        print_json(repos)
        return

    console = get_console()
    if not repos:
        console.print("[yellow]No firmware repositories configured.[/yellow]")
        return

    table = make_table(
        f"Firmware Repositories  ({len(repos)})",
        ("Name", {"min_width": 18, "no_wrap": True}),
        ("Type", {"no_wrap": True}),
        ("Total", {"justify": "right", "no_wrap": True}),
        ("Available", {"justify": "right", "no_wrap": True}),
        ("Bundles", {"justify": "right", "no_wrap": True}),
    )
    for r in repos:
        type_label = "Internal" if "internal" in r["repository_type"].lower() else "External"
        table.add_row(r["name"], type_label, _fmt_gb_value(r["total_gb"]),
                      _fmt_gb_value(r["available_gb"]), str(r["bundle_count"]))
    console.print(table)


async def _cmd_firmware_repository_list(args: argparse.Namespace) -> None:
    await _async_firmware_repository_list()


_COMPLIANCE_STYLE = {
    "Consistent": "[green]Consistent[/green]",
    "Inconsistent": "[red]Inconsistent[/red]",
    "Unknown": "[dim]Unknown[/dim]",
    "Not managed": "[dim]Not managed[/dim]",
}


async def _async_firmware_compliance_list() -> None:
    from proliant.oneview.firmware import list_compliance

    async with _load_client() as client:
        with get_console().status("[dim]Checking firmware compliance…[/dim]"):
            rows = await list_compliance(client)

    if get_output_mode() == OutputMode.JSON:
        print_json(rows)
        return

    console = get_console()
    if not rows:
        console.print("[yellow]No server profiles found.[/yellow]")
        return

    table = make_table(
        f"Firmware Compliance  ({len(rows)})",
        ("Hardware", {"no_wrap": True}),
        ("Model", {"no_wrap": True}),
        ("Logical Resource", {"no_wrap": True}),
        ("Firmware Bundle", {}),
        ("Status", {"no_wrap": True}),
    )
    for r in rows:
        bundle = f"{r['bundle_name']} ({r['bundle_version']})" if r["bundle_name"] else "—"
        status = _COMPLIANCE_STYLE.get(r["consistency_state"], r["consistency_state"])
        table.add_row(_short_server_name(r["hardware"]), r["model"], r["logical_resource"], bundle, status)
    console.print(table)
    console.print(
        "[dim]Status reflects OneView's own firmware consistencyState per server profile. "
        "The GUI's Update Category / Estimated Update Time columns are computed by an "
        "internal component-diff engine not exposed via the REST API.[/dim]",
        highlight=False,
    )


async def _cmd_firmware_compliance_list(args: argparse.Namespace) -> None:
    await _async_firmware_compliance_list()


async def _async_upgrade_cleanup(do_delete: bool) -> None:
    from proliant.oneview.upgrade import delete_baseline, gather_stale_baselines

    async with _load_client() as client:
        with get_console().status("[dim]Scanning firmware baselines…[/dim]"):
            summary = await gather_stale_baselines(client)

        prunable = summary["prunable"]
        retained = summary.get("retained_newer", [])
        external_unused = summary.get("external_unused", [])

        if get_output_mode() == OutputMode.JSON and not do_delete:
            print_json(summary)
            return

        console = get_console()

        def _print_external_unused() -> None:
            if not external_unused:
                return
            ext_table = make_table(
                f"External-Repository Baselines  "
                f"({len(external_unused)} — not deletable via OneView)",
                ("Name", {"min_width": 22, "no_wrap": True}),
                ("Version", {"no_wrap": True}),
                ("Type", {"no_wrap": True}),
                ("Released", {"no_wrap": True}),
                ("Repository", {"no_wrap": True}),
            )
            for b in external_unused:
                released = (b.get("release_date") or "")[:10]
                repo_names = ", ".join(sorted(set((b.get("locations") or {}).values()))) or "?"
                ext_table.add_row(b["name"], b["version"], b.get("bundle_type", ""),
                                   released, repo_names)
            console.print()
            console.print(ext_table)
            console.print(
                "[dim]These are unused but exist only in an external repository — "
                "OneView does not allow deleting them via this command. Remove them "
                "from the source repository directly if no longer needed.[/dim]",
                highlight=False,
            )

        if not prunable:
            if get_output_mode() == OutputMode.JSON:
                print_json({
                    "deleted": [], "failed": [], "reclaimed_gb": 0,
                    "external_unused_count": len(external_unused),
                })
            else:
                console.print("[green]No old unused firmware baselines to remove.[/green] "
                              "All baselines are either assigned/in use or newer than the "
                              "assigned baseline (kept as upgrade targets).")
                _print_external_unused()
            return

        table = make_table(
            f"Prunable Firmware Baselines  ({len(prunable)} — {summary['reclaimable_gb']:.1f} GB reclaimable)",
            ("Name", {"min_width": 22, "no_wrap": True}),
            ("Version", {"no_wrap": True}),
            ("Type", {"no_wrap": True}),
            ("Released", {"no_wrap": True}),
            ("Size", {"justify": "right", "no_wrap": True}),
        )
        for b in prunable:
            released = (b.get("release_date") or "")[:10]
            table.add_row(b["name"], b["version"], b.get("bundle_type", ""),
                          released, _fmt_gb(b["size_bytes"]))
        console.print(table)

        if retained:
            names = ", ".join(b["version"] for b in retained[:6])
            more = f" (+{len(retained) - 6} more)" if len(retained) > 6 else ""
            console.print(
                f"[dim]Retained {len(retained)} newer unused baseline(s) as upgrade "
                f"targets: {names}{more}[/dim]",
                highlight=False,
            )

        _print_external_unused()

        if not do_delete:
            console.print(
                "\n[yellow]Dry run.[/yellow] These baselines are unused (not assigned to any "
                "logical enclosure, logical interconnect, or server profile) and older than "
                "your assigned baseline.\n"
                "Re-run with [bold]--yes[/bold] to delete them and reclaim disk. "
                "This only removes SPP/SSP files from the appliance repository — "
                "it never touches running enclosures or interconnects.",
                highlight=False,
            )
            return

        # Actual deletion — per-item so one protected baseline can't abort the rest.
        deleted, failed = [], []
        with console.status("[dim]Deleting unused baselines…[/dim]"):
            for b in prunable:
                try:
                    await delete_baseline(client, b["uri"])
                    deleted.append(b)
                except Exception as exc:  # noqa: BLE001 — report and continue
                    failed.append({**b, "error": str(exc)})

    reclaimed = sum(b["size_bytes"] for b in deleted) / (1024 ** 3)
    if get_output_mode() == OutputMode.JSON:
        print_json({
            "deleted": [b["name"] for b in deleted],
            "failed": [{"name": b["name"], "error": b["error"]} for b in failed],
            "reclaimed_gb": round(reclaimed, 2),
            "external_unused_count": len(external_unused),
        })
        return

    console = get_console()
    console.print(f"[green]Deleted {len(deleted)} baseline(s), reclaimed "
                  f"{reclaimed:.2f} GB.[/green]")
    for b in failed:
        console.print(f"[yellow]Skipped[/yellow] {b['name']}: {b['error']}", highlight=False)


async def _cmd_upgrade_cleanup(args: argparse.Namespace) -> None:
    await _async_upgrade_cleanup(do_delete=bool(getattr(args, "yes", False)))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="proliant oneview",
        description="HPE OneView management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  proliant oneview servers list                          List all managed servers
  proliant oneview servers firmware list                 Fleet firmware (all servers)
  proliant oneview servers firmware list --server "Enc1, bay 1"
  proliant oneview firmware bundles list                 Registered SPP/SSP bundles
  proliant oneview firmware repository list              Internal + external repositories
  proliant oneview firmware compliance list              Per-server firmware drift
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
    proliant oneview enclosures describe Enclosure-01       Enclosure bay layout
  proliant oneview enclosure-groups list                 Enclosure groups
  proliant oneview logical-enclosures list               Logical enclosures
  proliant oneview uplinksets describe "pvlan-uplinkset" Full uplink set detail
  proliant oneview networksets describe "network-set-for-FM"
  proliant oneview server-profiles describe "ocp-single-node"
  proliant oneview reports memory
  proliant oneview upgrade readiness                     Pre-upgrade readiness report
  proliant oneview upgrade cleanup                       Preview unused firmware baselines
  proliant oneview upgrade cleanup --yes                 Delete unused baselines (free disk)
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
    server_fields_arg = p_srv.add_argument("--fields", metavar="FIELDS",
        help="Comma-separated columns: name,model,serial,ilo,ilo_ip,power,state,profile")
    server_fields_arg.completer = comma_sep_completer(_SERVER_FIELDS)
    p_srv.set_defaults(func=_cmd_servers_list)

    p_srv_fw = s_servers.add_parser("firmware", help="Show per-server firmware inventory")
    s_srv_fw = p_srv_fw.add_subparsers(dest="action", metavar="ACTION")
    s_srv_fw.required = True
    p_srv_fw_list = s_srv_fw.add_parser("list", help="Show firmware inventory (all servers or one)")
    server_arg = p_srv_fw_list.add_argument("--server", metavar="NAME",
        help='Server name (e.g. "Enc1, bay 1"). Omit for all servers.')
    server_arg.completer = _oneview_server_name_completer
    p_srv_fw_list.set_defaults(func=_cmd_firmware_list)

    p_firmware = sub.add_parser("firmware", help="Appliance firmware bundles, repositories, and compliance")
    s_firmware = p_firmware.add_subparsers(dest="what", metavar="ACTION")
    s_firmware.required = True

    p_fw_bundles = s_firmware.add_parser("bundles", aliases=["bundle"],
        help="List registered firmware bundles (SPP/SSP)")
    s_fw_bundles = p_fw_bundles.add_subparsers(dest="action", metavar="ACTION")
    s_fw_bundles.required = True
    s_fw_bundles.add_parser("list", help="List all firmware bundles").set_defaults(func=_cmd_firmware_bundles_list)

    p_fw_repo = s_firmware.add_parser("repository", aliases=["repositories", "repo"],
        help="List firmware repositories (Internal + external)")
    s_fw_repo = p_fw_repo.add_subparsers(dest="action", metavar="ACTION")
    s_fw_repo.required = True
    s_fw_repo.add_parser("list", help="List all firmware repositories").set_defaults(func=_cmd_firmware_repository_list)

    p_fw_compliance = s_firmware.add_parser("compliance",
        help="Per-server firmware compliance vs assigned bundle")
    s_fw_compliance = p_fw_compliance.add_subparsers(dest="action", metavar="ACTION")
    s_fw_compliance.required = True
    s_fw_compliance.add_parser("list", help="List per-server firmware compliance").set_defaults(
        func=_cmd_firmware_compliance_list)


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
    p_ns_desc_arg = p_ns_desc.add_argument("name", metavar="NAME", help="Name of the network set")
    p_ns_desc_arg.completer = _oneview_networkset_name_completer
    p_ns_desc.set_defaults(func=_cmd_describe, resource="networkset")

    p_uplinksets = sub.add_parser("uplinksets", aliases=["uplinkset"], help="List or describe uplink sets")
    s_uplinksets = p_uplinksets.add_subparsers(dest="what", metavar="ACTION")
    s_uplinksets.required = True
    p_ul_list = s_uplinksets.add_parser("list", help="List all uplink sets")
    p_ul_list.set_defaults(func=_cmd_uplinksets_list)
    p_ul_desc = s_uplinksets.add_parser("describe", help="Describe an uplink set")
    p_ul_desc_name = p_ul_desc.add_argument("name", metavar="NAME", help="Name of the uplink set")
    p_ul_desc_name.completer = _oneview_uplinkset_name_completer
    p_ul_desc.set_defaults(func=_cmd_describe, resource="uplinkset")

    p_profiles = sub.add_parser("server-profiles", aliases=["server-profile"], help="List or describe server profiles")
    s_profiles = p_profiles.add_subparsers(dest="what", metavar="ACTION")
    s_profiles.required = True
    p_sp_list = s_profiles.add_parser("list", help="List all server profiles")
    p_sp_list.set_defaults(func=_cmd_profiles_list)
    p_sp_desc = s_profiles.add_parser("describe", help="Describe a server profile")
    p_sp_desc_name = p_sp_desc.add_argument("name", metavar="NAME", help="Name of the server profile")
    p_sp_desc_name.completer = _oneview_profile_name_completer
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
    mac_address_arg = p_mac_list.add_argument("--address", "-a", metavar="MAC",
        help="Filter by MAC address (e.g. 00:9C:02:73:33:6D)")
    mac_address_arg.completer = suppress_file_completion()
    mac_vlan_arg = p_mac_list.add_argument("--vlan", "-v", metavar="VLAN", type=int,
        help="Filter by VLAN ID (e.g. 100)")
    mac_vlan_arg.completer = suppress_file_completion()
    arg_nn = p_mac_list.add_argument("--network-name", "-n", metavar="NAME", dest="network_name",
        help="Filter by network name substring (e.g. ACI-Tunnel-Net)")
    arg_nn.completer = _oneview_network_name_completer
    p_mac_list.set_defaults(func=_cmd_mac_list)

    p_mac_desc = s_mac.add_parser("describe",
        help="Trace a MAC address end-to-end through the fabric")
    mac_desc_address_arg = p_mac_desc.add_argument("address", metavar="MAC",
        help="MAC address to trace (e.g. 00:9C:02:73:33:6D)")
    mac_desc_address_arg.completer = suppress_file_completion()
    p_mac_desc.set_defaults(func=_cmd_mac_describe)

    # ── enclosures ────────────────────────────────────────────────────────
    p_encs = sub.add_parser("enclosures", aliases=["enclosure"], help="List physical enclosures")
    s_encs = p_encs.add_subparsers(dest="what", metavar="ACTION")
    s_encs.required = True
    s_encs.add_parser("list", help="List all enclosures").set_defaults(func=_cmd_enclosures_list)
    p_enc_desc = s_encs.add_parser("describe", help="Show enclosure bay layout")
    p_enc_desc_name = p_enc_desc.add_argument("name", metavar="NAME", help="Name of the enclosure")
    p_enc_desc_name.completer = _oneview_enclosure_name_completer
    p_enc_desc.set_defaults(func=_cmd_enclosures_describe)

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

    # ── upgrade (readiness + disk cleanup) ────────────────────────────────
    p_upgrade = sub.add_parser("upgrade", help="Appliance upgrade readiness & disk cleanup")
    s_upgrade = p_upgrade.add_subparsers(dest="what", metavar="ACTION")
    s_upgrade.required = True
    p_up_ready = s_upgrade.add_parser("readiness", aliases=["check"],
        help="Read-only pre-upgrade readiness report (version, health, path)")
    p_up_ready.set_defaults(func=_cmd_upgrade_readiness)
    p_up_clean = s_upgrade.add_parser("cleanup",
        help="List (and optionally delete) unused firmware baselines to free disk")
    p_up_clean.add_argument("--yes", "-y", action="store_true",
        help="Actually delete the unused baselines (default is a dry-run preview)")
    p_up_clean.set_defaults(func=_cmd_upgrade_cleanup)

    return parser


def _normalize_global_json_arg(argv: list[str] | None) -> list[str] | None:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--json" not in args:
        return argv
    return ["--json", *[arg for arg in args if arg != "--json"]]


def main(argv: list[str] | None = None) -> None:
    try:
        import argcomplete
        parser = _build_parser()
        argcomplete.autocomplete(parser)
    except ImportError:
        parser = _build_parser()

    args = parser.parse_args(_normalize_global_json_arg(argv))
    if getattr(args, "json_output", False):
        set_output_mode(OutputMode.JSON)
    try:
        run_sync(args.func(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        from rich.markup import escape
        get_console().print(f"[red]Error:[/red] {escape(str(exc))}", highlight=False)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
