"""
proliant.oneview.cli — OneView subcommands.

Usage:
    proliant oneview servers list [--fields ...]
    proliant oneview servers firmware list [--server NAME]
    proliant oneview firmware bundles
    proliant oneview firmware repository
    proliant oneview compliance list
    proliant oneview compliance describe <resource name>
"""

# PYTHON_ARGCOMPLETE_OK
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any


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


def _load_client(name: str | None = None):
    """Yield a connected OneViewClient, showing a "Connecting..." hint.

    A wrong/unreachable appliance host previously left the terminal looking
    frozen with zero feedback until the login handshake finally timed out.
    Print a status hint for the duration of the connect/login only -- it
    disappears the moment we get a real response (success or failure).

    *name* targets a specific configured appliance (by inventory section name);
    omitted, it uses the active appliance.
    """
    from proliant.oneview.config import list_oneview_appliances, load_oneview_config
    from proliant.oneview.client import OneViewClient

    appliances = list_oneview_appliances()
    cfg = load_oneview_config(name)
    client = OneViewClient(cfg["host"], cfg["username"], cfg["password"])

    # With only one appliance configured, keep the status line as-is (no
    # need to name it). With 2+, show which one is active so it's obvious
    # without a separate 'appliances list' call -- see 'appliances use' to
    # switch which one commands target.
    label = f" '{cfg['name']}'" if len(appliances) > 1 else ""

    @asynccontextmanager
    async def _connect():
        with get_console().status(f"[dim]Connecting to OneView{label} at {cfg['host']}…[/dim]"):
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


def _oneview_appliance_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete appliance names for 'appliances use'."""
    try:
        from proliant.oneview.config import list_oneview_appliances
        names = [a["name"] for a in list_oneview_appliances()]
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


async def _cmd_appliances_list(args: argparse.Namespace) -> None:
    from proliant.oneview.config import list_oneview_appliances, get_active_appliance_name

    appliances = list_oneview_appliances()
    if getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON:
        print_json(appliances)
        return
    if not appliances:
        get_console().print("[yellow]No OneView appliances configured.[/yellow] Run 'proliant setup' to add one.")
        return

    active_name = get_active_appliance_name(appliances)
    table = make_table(
        f"OneView Appliances ({len(appliances)} total)",
        ("", {"style": "bold green", "no_wrap": True}),
        ("Name", {"style": "bold cyan", "no_wrap": True}),
        ("Host", {"style": "white"}),
        ("Username", {"style": "dim"}),
        box_style=box.ROUNDED,
    )
    for a in appliances:
        table.add_row("* " if a["name"] == active_name else "  ", a["name"], a["host"], a["username"])
    get_console().print(table)
    if len(appliances) > 1:
        get_console().print("[dim]  * = active appliance -- switch with 'proliant oneview appliances use <name>'[/dim]")


async def _cmd_appliances_use(args: argparse.Namespace) -> None:
    from proliant.oneview.config import set_active_appliance

    try:
        resolved = set_active_appliance(args.name)
        get_console().print(f"[bold green]✓ Switched to OneView appliance:[/bold green] {resolved}")
    except ValueError as e:
        get_console().print(f"[red]Error:[/red] {e}")
        sys.exit(1)


async def _cmd_appliances_describe(args: argparse.Namespace) -> None:
    """Show the appliance General page: HA nodes, memory, uptime, firmware."""
    from proliant.oneview.appliance_info import fetch_appliance_info

    name = getattr(args, "name", None)
    async with _load_client(name) as client:
        info = await fetch_appliance_info(client)

    if getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON:
        print_json(info)
        return

    _render_appliance_describe(info)


def _local_timestamp(raw: str) -> str:
    """Format an ISO timestamp in the CLI machine's local time, OneView-style."""
    from proliant.oneview.appliance_info import _parse_iso, fmt_timestamp

    dt = _parse_iso(raw)
    return fmt_timestamp(dt.astimezone() if dt is not None else None)


def _appliance_node_tile(node: dict) -> Panel:
    role = (node.get("role") or "").lower()
    state = (node.get("state") or "").upper()
    dot = "[green]●[/green]" if state == "OK" else "[yellow]●[/yellow]"
    body = f"[bold]{node.get('name') or '—'}[/bold]\n{dot} [dim]{role or 'unknown'}[/dim]"
    return Panel(body, box=box.SQUARE, padding=(0, 1), border_style="cyan", expand=True)


def _render_appliance_describe(info: dict) -> None:
    from rich.console import Group

    console = get_console()

    # ── update-task banner ────────────────────────────────────────────────
    lu = info.get("last_update")
    if lu:
        state = lu.get("state") or ""
        dur = lu.get("duration") or ""
        owner = lu.get("owner") or ""
        when = _local_timestamp(lu.get("finished_raw") or "")
        meta = "  ".join(x for x in (owner, when) if x and x != "—")
        console.print(
            f"[green]●[/green] [bold]{lu.get('name')}[/bold]  "
            f"[green]{state}[/green] {dur}" + (f"    [dim]{meta}[/dim]" if meta else ""),
            highlight=False,
        )

    nodes = info.get("nodes") or []

    # ── node topology: active  —Connected—  standby ───────────────────────
    topo = Table.grid(expand=True, padding=(0, 1))
    if len(nodes) >= 2:
        topo.add_column(ratio=1)
        topo.add_column(justify="center", vertical="middle", no_wrap=True)
        topo.add_column(ratio=1)
        link = ("[green]✓ Connected[/green]" if info.get("connected")
                else "[yellow]Not connected[/yellow]")
        topo.add_row(_appliance_node_tile(nodes[0]), link, _appliance_node_tile(nodes[1]))
    else:
        topo.add_column(ratio=1)
        topo.add_row(_appliance_node_tile(nodes[0]) if nodes else "[dim]No HA nodes reported.[/dim]")

    # ── general details ───────────────────────────────────────────────────
    details = Table.grid(padding=(0, 3))
    details.add_column(style="dim", no_wrap=True)
    details.add_column()
    details.add_row("Model", info.get("model") or "—")
    details.add_row("Memory", info.get("memory") or "—")

    by_role = {(n.get("role") or "").lower(): n for n in nodes}
    active = by_role.get("active")
    standby = by_role.get("standby")

    st_lines = []
    for n, tag in ((active, "active"), (standby, "standby")):
        if n and n.get("start_time"):
            st_lines.append(f"{_local_timestamp(n['start_time'])} [dim italic]{tag}[/dim italic]")
    details.add_row("Start time", "\n".join(st_lines) if st_lines else "—")

    from proliant.oneview.appliance_info import fmt_uptime
    up_lines = []
    for n, tag in ((active, "active"), (standby, "standby")):
        if n and n.get("uptime"):
            up_lines.append(f"{fmt_uptime(n['uptime'])} [dim italic]{tag}[/dim italic]")
    details.add_row("Uptime", "\n".join(up_lines) if up_lines else "—")

    # ── firmware ──────────────────────────────────────────────────────────
    fw = info.get("firmware") or {}
    fw_grid = Table.grid(padding=(0, 3))
    fw_grid.add_column(style="dim", no_wrap=True)
    fw_grid.add_column()
    fw_grid.add_row("Version", fw.get("version") or "—")
    from proliant.oneview.appliance_info import fmt_fw_date
    fw_grid.add_row("Date", fmt_fw_date(fw.get("date_raw") or "") or "—")

    general = Group(
        topo, "", details, "", "[bold]Firmware[/bold]", fw_grid,
    )
    console.print(Panel(general, title="General", title_align="left", border_style="cyan"))

    # ── Composable Infrastructure Appliances ──────────────────────────────
    if nodes:
        table = make_table(
            "Composable Infrastructure Appliances",
            ("Name", {"style": "bold cyan", "no_wrap": True}),
            ("Model", {"style": "white"}),
            ("iLO Address", {"style": "dim"}),
            box_style=box.SIMPLE_HEAD,
        )
        for n in sorted(nodes, key=lambda x: x.get("bay") or 0, reverse=True):
            table.add_row(n.get("name") or "—", n.get("model") or "—", n.get("ilo_address") or "not set")
        console.print(table)


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


def _oneview_logical_enclosure_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete logical enclosure names by querying OneView."""
    try:
        names = _oneview_cached_object_names("logical-enclosures", "/rest/logical-enclosures")
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _oneview_ssp_baseline_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete registered SSP/SPP baseline versions for '--baseline'.

    Queries live firmware-drivers from OneView (no static/bundled list) --
    same source ``select_baseline()`` matches against at apply time.
    """
    try:
        from proliant.oneview.config import load_oneview_config
        from proliant.oneview.client import OneViewClient
        from proliant.oneview.ssp_update import FW_DRIVERS_URI, service_pack_baselines
        from proliant.common.completers import cached_names

        cfg = load_oneview_config()

        def _fetch_versions() -> list[str]:
            async def _fetch() -> list[str]:
                async with OneViewClient(cfg["host"], cfg["username"], cfg["password"]) as client:
                    raw = await client.get_all(FW_DRIVERS_URI)
                    return [b["version"] for b in service_pack_baselines(raw) if b.get("version")]

            return asyncio.run(_fetch())

        versions = cached_names(f"oneview-ssp-baselines-{cfg['host']}", _fetch_versions)
        return [v for v in versions if v.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _oneview_interconnect_name_completer(prefix: str, **kwargs) -> list[str]:
    """Tab-complete interconnect names by querying OneView."""
    try:
        names = _oneview_cached_object_names("interconnects", "/rest/interconnects")
        return [n for n in names if n.lower().startswith(prefix.lower())]
    except Exception:
        return []


def _add_power_common_flags(parser: argparse.ArgumentParser, *, include_yes: bool) -> None:
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Show the OneView request without sending it")
    if include_yes:
        parser.add_argument("--yes", action="store_true",
                            help="Confirm eFuse hard power-cycle operations")


def _add_power_target_parsers(
    parent_parser: argparse.ArgumentParser,
    func,
    *,
    targets: tuple[str, ...] = ("server", "profile", "interconnect", "flm"),
    include_yes: bool = False,
) -> None:
    """Attach TARGET subparsers to a parser.

    Shared between ``proliant oneview power <action>`` (graceful on/off/
    shutdown, which only apply to server/profile targets, and never need a
    ``--yes`` confirmation) and the top-level ``proliant oneview efuse``
    command (a hard power-cycle, available for all four target types and
    always requiring ``--yes`` unless ``--dry-run`` is used).
    """
    targets_parser = parent_parser.add_subparsers(dest="power_target", metavar="TARGET")
    targets_parser.required = True

    if "server" in targets:
        p_server = targets_parser.add_parser(
            "server",
            aliases=["servers"],
            help="Target server hardware by name or enclosure bay",
        )
        p_server.set_defaults(func=func, power_target_type="server")
        server_name_arg = p_server.add_argument("name", metavar="NAME", nargs="?",
            help="Server hardware name (omit when using --enclosure/--bay)")
        server_name_arg.completer = _oneview_server_name_completer
        server_enclosure_arg = p_server.add_argument("--enclosure", metavar="NAME",
            help="Target server hardware by enclosure name")
        server_enclosure_arg.completer = _oneview_enclosure_name_completer
        server_bay_arg = p_server.add_argument("--bay", metavar="N", type=int,
            help="Target server hardware by bay number with --enclosure")
        server_bay_arg.completer = suppress_file_completion()
        _add_power_common_flags(p_server, include_yes=include_yes)

    if "profile" in targets:
        p_profile = targets_parser.add_parser("profile", help="Target the server assigned to a server profile")
        p_profile.set_defaults(func=func, power_target_type="profile")
        profile_name_arg = p_profile.add_argument("name", metavar="NAME", help="Server profile name")
        profile_name_arg.completer = _oneview_profile_name_completer
        _add_power_common_flags(p_profile, include_yes=include_yes)

    if "interconnect" in targets:
        p_interconnect = targets_parser.add_parser(
            "interconnect",
            aliases=["icm"],
            help="Target an interconnect by name or enclosure bay",
        )
        p_interconnect.set_defaults(func=func, power_target_type="interconnect")
        interconnect_name_arg = p_interconnect.add_argument("name", metavar="NAME", nargs="?",
            help="Interconnect name (omit when using --enclosure/--bay)")
        interconnect_name_arg.completer = _oneview_interconnect_name_completer
        interconnect_enclosure_arg = p_interconnect.add_argument("--enclosure", metavar="NAME",
            help="Target interconnect by enclosure name")
        interconnect_enclosure_arg.completer = _oneview_enclosure_name_completer
        interconnect_bay_arg = p_interconnect.add_argument("--bay", metavar="N", type=int,
            help="Target interconnect by bay number with --enclosure")
        interconnect_bay_arg.completer = suppress_file_completion()
        _add_power_common_flags(p_interconnect, include_yes=include_yes)

    if "flm" in targets:
        p_flm = targets_parser.add_parser("flm", help="Target a frame link module bay")
        p_flm.set_defaults(func=func, power_target_type="flm")
        flm_enclosure_arg = p_flm.add_argument("enclosure", metavar="ENCLOSURE", help="Enclosure name")
        flm_enclosure_arg.completer = _oneview_enclosure_name_completer
        flm_bay_arg = p_flm.add_argument("bay", metavar="BAY", type=int, help="Frame link module bay number")
        flm_bay_arg.completer = suppress_file_completion()
        _add_power_common_flags(p_flm, include_yes=include_yes)


def _add_power_action_parser(subparsers, action_name: str, help_text: str) -> None:
    action_parser = subparsers.add_parser(action_name, help=help_text)
    _add_power_target_parsers(action_parser, _cmd_power, targets=("server", "profile"), include_yes=False)


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


def _step_segment(payload: dict) -> str:
    """A "step 15/24" progress segment from a task-progress event's
    ``completed_steps``/``total_steps`` (see ``normalize_task()``), or ``""``
    if this tick didn't carry step data. Some OneView task types (e.g. a
    server-profile firmware "Apply profile" task) leave their own plain
    ``percentComplete`` frozen at 0 for their entire run while quietly
    completing steps underneath -- OneView's ``computedPercentComplete``
    already accounts for this (see ``normalize_task()``), but showing the
    raw step count alongside it makes the same "still working, here's how
    far" detail the GUI's own subtask log conveys instead of a lone number.

    ``completed_steps`` can exceed ``total_steps`` -- confirmed live: OneView
    appears to increment ``completedSteps`` for every progress-log entry
    (including retried power-cycle attempts and the final failure entry
    itself), without revising ``totalSteps`` upward from its original plan.
    A plain "26/24" fraction reads as a bug, so once completed overtakes the
    original plan this shows the plan separately instead of a bogus >100%
    ratio.
    """
    total = payload.get("total_steps")
    if not isinstance(total, int) or total <= 0:
        return ""
    completed = payload.get("completed_steps")
    completed = completed if isinstance(completed, int) else 0
    if completed > total:
        return f"step {completed} (plan: {total})"
    return f"step {completed}/{total}"


def _print_new_progress_log_lines(
    console, bars: dict, key: str, payload: dict, *, label: str = "",
) -> None:
    """Print any progress-log lines not yet shown for *key*, above the live bar.

    ``progress_log`` (see ``ssp_update.normalize_task``) is the full,
    append-only history of an in-flight task's progress updates -- e.g.
    every "Stage component N/6 - name.fwpkg" / "Install component N/6" line
    the GUI's own expanded Activity view shows as it happens, not just the
    single latest one the compact live bar's description line has room for.
    Rich lets a Progress's own ``console.print()`` interleave permanent lines
    above an active bar without corrupting it, so each newly-seen line is
    printed once (tracked by position, not text, since the same line -- e.g.
    a retried "Power off server." -- can legitimately repeat) the moment it
    shows up, giving the terminal the same scrolling detail as the GUI while
    the bar itself keeps showing just the current phase + percent.

    *label* (e.g. a target's name) prefixes each printed line -- needed when
    several targets run concurrently (``update enclosure --concurrency``) so
    their interleaved logs on screen stay attributable to the right target.
    """
    log = payload.get("progress_log") or []
    printed = bars.get(key, 0)
    if len(log) > printed:
        prefix = f"[dim]{label}:[/dim] " if label else ""
        for line in log[printed:]:
            console.print(f"  {prefix}[dim]{line}[/dim]", highlight=False)
        bars[key] = len(log)


# ── proliant oneview server-profiles reapply ──────────────────────────────────────

async def _cmd_profiles_reapply(args: argparse.Namespace) -> None:
    from proliant.oneview.profile_reapply import run_profile_reapply

    json_mode = getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON
    console = get_console()

    bars: dict = {}

    def _stop_bar() -> None:
        p = bars.pop("bar", None)
        bars.pop("task", None)
        if p is not None:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass

    def on_event(kind: str, payload: dict) -> None:
        if json_mode:
            return
        if kind == "applying":
            desc = f"[bold]Server profile: {payload.get('name')}[/bold]"
            bars["log"] = 0
            p = bars.get("bar")
            if p is not None:
                p.reset(bars["task"], total=100, completed=0, description=desc)
                return
            from rich.progress import (
                BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                TextColumn, TimeElapsedColumn,
            )
            p = Progress(
                SpinnerColumn(), TextColumn("{task.description}"),
                BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
            )
            p.start()
            bars["bar"] = p
            bars["task"] = p.add_task(desc, total=100)
        elif kind == "task-progress":
            p = bars.get("bar")
            if p is None:
                return
            _print_new_progress_log_lines(console, bars, "log", payload)
            pct = payload.get("percent")
            state = payload.get("state") or payload.get("status") or "working…"
            stage = payload.get("stage") or ""
            res = payload.get("resource") or ""
            step = _step_segment(payload)
            segs = ["[bold]Server profile[/bold]", f"[cyan]{state}[/cyan]"]
            if stage:
                segs.append(f"[dim]{stage}[/dim]")
            if step:
                segs.append(f"[dim]{step}[/dim]")
            if res:
                segs.append(f"[dim]({res})[/dim]")
            desc = "  ".join(segs)
            if isinstance(pct, (int, float)):
                p.update(bars["task"], completed=pct, description=desc)
            else:
                p.update(bars["task"], description=desc)

    def confirm(info: dict) -> bool:
        if getattr(args, "yes", False):
            return True
        if json_mode:
            return False  # never silently reconfigure hardware in scripted mode
        name = info.get("name", "")
        console.print(Panel(
            f"About to reapply server profile [bold]{name}[/bold]'s stored configuration to "
            "its assigned hardware.\nThis re-runs OneView's own profile-apply state machine "
            "and can briefly reconfigure network/storage settings or reboot the server.",
            title="Confirm reapply server profile", border_style="red"))
        ans = console.input(
            f'Type the profile name "{name}" to proceed (or anything else to abort): ',
            markup=False,
        )
        return ans.strip() == name

    factory = _oneview_client_factory()
    try:
        result = await run_profile_reapply(
            factory, name=args.name, confirm=confirm, on_event=on_event,
            poll_interval_s=_SSP_POLL_S, task_timeout_s=_SSP_TASK_TIMEOUT_S,
        )
    finally:
        _stop_bar()

    if json_mode:
        print_json(result)
        return

    status = result.get("status")
    name = (result.get("profile") or {}).get("name") or args.name
    if status == "not-found":
        console.print(f"[red]Server profile '{args.name}' not found.[/red]")
        console.print(f"[dim]Known: {result.get('known') or 'none found'}[/dim]")
    elif status == "aborted":
        console.print("\n[yellow]Aborted — nothing was modified.[/yellow]")
    elif status == "applied":
        console.print(f"\n[green]✓ Reapplied server profile[/green] [bold]{name}[/bold].")
    elif status == "failed":
        last = (result.get("results") or [{}])[-1]
        console.print(
            f"\n[red]Reapply failed[/red] on [bold]{name}[/bold] "
            f"({last.get('status') or last.get('state') or 'error'}).\n"
            "See [bold]proliant oneview activity[/bold] (or the OneView UI Activity page) "
            "for the full task detail."
        )
    elif status == "timeout":
        console.print(
            f"\n[yellow]Reapply did not finish within the timeout[/yellow] on [bold]{name}[/bold] "
            "— check [bold]proliant oneview activity[/bold] for its current progress."
        )


# ── proliant oneview server-profiles update (single named profile) ────────────────

async def _cmd_profiles_update(args: argparse.Namespace) -> None:
    from proliant.oneview.ssp_update import (
        INSTALL_TYPES,
        fetch_apply_targets,
        find_profile_by_name,
        run_ssp_apply,
        select_baseline,
    )

    json_mode = getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON
    console = get_console()

    async with _load_client() as client:
        with console.status("[dim]Fetching SSP baselines and server profiles…[/dim]"):
            data = await fetch_apply_targets(client)

    profile = find_profile_by_name(data["server_profiles"], args.name)
    if profile is None:
        known = ", ".join(p["name"] for p in data["server_profiles"]) or "none found"
        if json_mode:
            print_json({"status": "error", "reason": "server profile not found", "query": args.name})
        else:
            console.print(f"[red]Server profile '{args.name}' not found.[/red]")
            console.print(f"[dim]Known: {known}[/dim]")
        return

    baseline = select_baseline(data["baselines"], getattr(args, "baseline", None))
    if baseline is None:
        names = ", ".join(f"{b['name']} ({b['version']})" for b in data["baselines"][:8]) or "none registered"
        if json_mode:
            print_json({"status": "error", "reason": "baseline not found",
                        "query": getattr(args, "baseline", None), "available": data["baselines"]})
        else:
            q = getattr(args, "baseline", None)
            console.print(f"[red]No SSP baseline matches '{q}'.[/red]" if q
                          else "[red]No SSP/SPP baselines are registered on this appliance.[/red]")
            console.print(f"[dim]Available: {names}[/dim]")
        return

    install_type = INSTALL_TYPES.get(getattr(args, "install_type", None) or "")
    execute = bool(getattr(args, "execute", False))

    bars: dict = {}

    def _stop_bar() -> None:
        p = bars.pop("bar", None)
        bars.pop("task", None)
        if p is not None:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass

    def on_event(kind: str, payload: dict) -> None:
        if json_mode:
            return
        if kind == "plan":
            _render_ssp_plan(console, payload)
        elif kind == "applying":
            desc = f"[bold]Server profile: {payload.get('name')}[/bold]"
            bars["log"] = 0
            p = bars.get("bar")
            if p is not None:
                p.reset(bars["task"], total=100, completed=0, description=desc)
                return
            from rich.progress import (
                BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                TextColumn, TimeElapsedColumn,
            )
            p = Progress(
                SpinnerColumn(), TextColumn("{task.description}"),
                BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
            )
            p.start()
            bars["bar"] = p
            bars["task"] = p.add_task(desc, total=100)
        elif kind == "task-progress":
            p = bars.get("bar")
            if p is None:
                return
            _print_new_progress_log_lines(console, bars, "log", payload)
            pct = payload.get("percent")
            state = payload.get("state") or payload.get("status") or "working…"
            stage = payload.get("stage") or ""
            res = payload.get("resource") or ""
            step = _step_segment(payload)
            segs = ["[bold]Server profile[/bold]", f"[cyan]{state}[/cyan]"]
            if stage:
                segs.append(f"[dim]{stage}[/dim]")
            if step:
                segs.append(f"[dim]{step}[/dim]")
            if res:
                segs.append(f"[dim]({res})[/dim]")
            desc = "  ".join(segs)
            if isinstance(pct, (int, float)):
                p.update(bars["task"], completed=pct, description=desc)
            else:
                p.update(bars["task"], description=desc)

    def confirm(plan: dict) -> bool:
        if getattr(args, "yes", False):
            return True
        if json_mode:
            return False  # never silently flash hardware in scripted mode
        token = baseline.get("version") or baseline.get("name") or "apply"
        compat = plan.get("compat") or {}
        compat_warn = ""
        if compat.get("status") == "unsupported":
            compat_warn = (
                f"\n[red]⚠ {compat.get('message', '')}[/red] "
                "Verify the SSP release notes before proceeding."
            )
        console.print(Panel(
            f"About to APPLY SSP [bold]{baseline.get('name')} ({baseline.get('version')})[/bold] to "
            f"server profile [bold]{profile['name']}[/bold].\n"
            "[red]The compute module will power-cycle — ensure the host is ready to reboot.[/red]"
            + compat_warn,
            title="Confirm SSP firmware apply", border_style="red"))
        ans = console.input(
            f'Type the baseline version "{token}" to proceed (or anything else to abort): ',
            markup=False,
        )
        return ans.strip() == token

    def on_validation_blocked(info: dict) -> str:
        # Mirrors update enclosure's own callback -- see its docstring for the
        # full A/B rationale. Server-profile firmware rarely hits this (the
        # non-redundant-fabric guard is mainly a Logical-Interconnect concept),
        # but the underlying engine offers the same retry hook for any target.
        if getattr(args, "yes", False):
            return "proceed"
        if json_mode:
            return "abort"
        _stop_bar()
        reason = info.get("reason") or (
            "OneView flagged this update as potentially disruptive and refused "
            "to apply it, but did not report a specific reason."
        )
        resolution = info.get("resolution") or ""
        body = f"[yellow]{reason}[/yellow]"
        if resolution:
            body += f"\n\n[bold]Resolution:[/bold] {resolution}"
        console.print(Panel(
            body,
            title=f"⚠ Validation warning — {info.get('name')}", border_style="yellow",
            subtitle="Review the warnings. If the conditions are acceptable, then click OK to proceed.",
            subtitle_align="left"))
        console.print(
            "[bold]Choose an action:[/bold]\n"
            "  [cyan]A[/cyan]) Abort — investigate the warning above first (recommended)\n"
            "  [cyan]B[/cyan]) Force the update through now — [red]disruptive[/red]"
        )
        ans = console.input("Your choice [A/b]: ", markup=False).strip().lower()
        return "proceed" if ans in ("b", "f", "force") else "abort"

    factory = _oneview_client_factory()
    try:
        result = await run_ssp_apply(
            factory,
            baseline=baseline, le_targets=[], profile_targets=[profile],
            install_type=install_type, force=bool(getattr(args, "force", False)),
            execute=execute, confirm=confirm if execute else None,
            on_validation_blocked=on_validation_blocked if execute else None, on_event=on_event,
            poll_interval_s=_SSP_POLL_S, task_timeout_s=_SSP_TASK_TIMEOUT_S,
            verify_timeout_s=_SSP_VERIFY_TIMEOUT_S,
            appliance_version=data.get("appliance_version", ""),
            baselines=data.get("baselines", []),
        )
    finally:
        _stop_bar()

    if json_mode:
        print_json(result)
        return

    _render_ssp_apply_result(
        console, result, plan_message="Re-run with [bold]--execute[/bold] to apply.",
    )


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

    # Ports table — matches GUI columns: Uplink / State / Op Speed / Req Speed /
    # Auto-neg / FEC / LAG / Connected To
    port_table = make_table(
        "Ports",
        ("Uplink",        {"no_wrap": True}),
        ("State",         {"no_wrap": True}),
        ("Op Speed",      {"justify": "right", "no_wrap": True}),
        ("Req Speed",     {"justify": "right", "no_wrap": True}),
        ("Auto-neg",      {"no_wrap": True}),
        ("FEC",           {"no_wrap": True}),
        ("LAG",           {"no_wrap": True}),
        ("Connected To",  {"no_wrap": True}),
        box_style=box.SIMPLE_HEAD,
        header_style="bold",
    )
    for p in u["ports"]:
        state = p["link_state"]
        if "active" in state:
            state_s = f"[green]{state}[/green]"
        elif "standby" in state:
            state_s = f"[yellow]{state}[/yellow]"
        elif state.lower() in ("unlinked", "—"):
            state_s = f"[dim]{state}[/dim]"
        else:
            state_s = state
        op = p["op_speed"]
        op_s = f"{op} Gb/s" if op and op != "unknown" else f"[dim]{op or '—'}[/dim]"
        req = p["req_speed"].replace("Speed", "").replace("G", "") if p["req_speed"] else "Auto"
        req_s = f"{req} Gb/s" if req not in ("Auto", "") else f"[dim]{req}[/dim]"
        port_table.add_row(
            f"Bay{p['bay']}:{p['port']}",
            state_s,
            op_s,
            req_s,
            p["auto_neg"] or "—",
            p["fec"] or "—",
            p["lag"],
            p["connected_to"],
        )
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


# ── proliant oneview interconnects describe ───────────────────────────────────

def _link_status_style(state: str) -> str:
    s = (state or "").lower()
    if s == "linked":
        return f"[green]{state}[/green]"
    if s == "unlinked":
        return f"[dim]{state}[/dim]"
    return state or ""


async def _async_interconnects_describe(name: str) -> None:
    from proliant.oneview.interconnects import describe_interconnect

    async with _load_client() as client:
        with get_console().status(f"[dim]Fetching interconnect '{name}'…[/dim]"):
            ic = await describe_interconnect(client, name)

    if get_output_mode() == OutputMode.JSON:
        print_json(ic)
        return

    console = get_console()
    g = ic["general"]
    h = ic["hardware"]

    ipv4 = f"{g['ipv4']} ({g['ipv4_type']})" if g["ipv4"] else "—"
    ipv6 = f"{g['ipv6']} ({g['ipv6_type']})" if g["ipv6"] else "—"

    # Mirror the GUI's "General" page exactly: it always shows these three
    # plain fields (Firmware baseline / Firmware version from baseline /
    # Installed firmware version) with no separate "target"/"not yet
    # applied" concept -- a mismatch between the baseline's version and the
    # installed version is simply visible by comparing the two lines, same
    # as the GUI. There is no update in progress here, so inventing a
    # "target baseline" field doesn't apply and only confuses live testing.
    fw_lines = [
        f"Firmware baseline:              {g['firmware_baseline_name'] or '—'}",
        f"Firmware version from baseline: {g['firmware_version_from_baseline'] or '—'}",
        f"Installed firmware version:     {g['installed_firmware_version'] or '—'}",
    ]

    details = [
        f"[bold]{ic['name']}[/bold]",
        f"Status: {_status_style(ic['status'])}  |  State: {_state_style(ic['state'])}  |  Power: {_power_style(g['power'])}",
        f"Logical interconnect:            {g['logical_interconnect'] or '—'}",
        *fw_lines,
        f"Management interface:            {g['mgmt_interface']}",
        f"Stacking:                        domain {g['stacking_domain_id'] or '—'} / member {g['stacking_member_id'] or '—'} ({g['stacking_domain_role'] or '—'})",
        f"Host name:                       {g['host_name'] or '—'}",
        f"IPv4:                            {ipv4}",
        f"IPv6:                            {ipv6}",
    ]
    console.print(Panel("\n".join(details), title="General", border_style="cyan"))

    hw_details = [
        f"Product name:      {h['product_name'] or '—'}",
        f"Location:          {h['location'] or '—'}",
        f"Management MAC:    {h['mgmt_mac'] or '—'}",
        f"Base WWN:          {h['base_wwn'] or '—'}",
        f"Serial number:     {h['serial_number'] or '—'}",
        f"Part number:       {h['part_number'] or '—'}",
        f"Spare part number: {h['spare_part_number'] or '—'}",
        f"Hardware health:   {_status_style(h['health'])}",
    ]
    console.print(Panel("\n".join(hw_details), title="Hardware", border_style="cyan"))

    link_ports = ic["link_ports"]
    if link_ports:
        table = make_table(
            "Interconnect Link Ports",
            ("Port", {"no_wrap": True}),
            ("State", {"justify": "center", "no_wrap": True}),
            ("Connected To", {"no_wrap": True}),
        )
        for p in link_ports:
            table.add_row(p["port"], _link_status_style(p["state"]), p["connected_to"])
        console.print(table)

    uplink_ports = ic["uplink_ports"]
    if uplink_ports:
        table = make_table(
            f"Uplink Ports  ({len(uplink_ports)})",
            ("Port", {"no_wrap": True}),
            ("Type", {"no_wrap": True}),
            ("State", {"justify": "center", "no_wrap": True}),
            ("Speed (Gb/s)", {"justify": "right", "no_wrap": True}),
            ("Uplink Set", {"min_width": 14, "no_wrap": True}),
            ("Port WWN", {"no_wrap": True}),
            ("Connector", {"no_wrap": True}),
            ("Connected To", {"no_wrap": True}),
        )
        for p in uplink_ports:
            table.add_row(
                p["port"], p["type"] or "—", _link_status_style(p["state"]),
                p["speed"] or "unknown", p["uplink_set"] or "—",
                p["port_wwn"], p["connector_type"], p["connected_to"],
            )
        console.print(table)

    downlink_ports = ic["downlink_ports"]
    if downlink_ports:
        table = make_table(
            f"Downlink Ports  ({len(downlink_ports)})",
            ("Port", {"no_wrap": True}),
            ("State", {"justify": "center", "no_wrap": True}),
            ("Speed (Gb/s)", {"justify": "right", "no_wrap": True}),
            ("Server Hardware", {"min_width": 18, "no_wrap": True}),
            ("Adapter Port", {"no_wrap": True}),
            ("Server Profile", {"min_width": 14, "no_wrap": True}),
        )
        for p in downlink_ports:
            table.add_row(
                p["port"], _link_status_style(p["state"]), p["speed"],
                p["server_hardware"], p["adapter_port"], p["server_profile"],
            )
        console.print(table)

    u = ic["utilization"]
    lines = []
    if u["cpu_pct"] is not None:
        lines.append(f"CPU:          {u['cpu_pct']:g}%")
    if u["memory_used_mb"] is not None:
        cap = f" of {u['memory_capacity_mb']:g} MB" if u["memory_capacity_mb"] else ""
        pct = f"  ({u['memory_pct']:g}%)" if u["memory_pct"] is not None else ""
        lines.append(f"Memory:       {u['memory_used_mb']:g} MB{cap}{pct}")
    if u["power_avg_w"] is not None:
        cap = f" of {u['power_capacity_w']:g} W" if u["power_capacity_w"] else ""
        lines.append(f"Power:        {u['power_avg_w']:g} W{cap}")
    if u["temperature_f"] is not None:
        lines.append(f"Temperature:  {u['temperature_f']:g} °F")
    if lines:
        console.print(Panel("\n".join(lines), title="Utilization", border_style="cyan"))

    rs = ic["remote_support"]
    rs_style = "[green]Enabled[/green]" if rs["enabled"] else f"[dim]{rs['state'] or '—'}[/dim]"
    console.print(f"Remote support: {rs_style}")


async def _cmd_interconnects_describe(args: argparse.Namespace) -> None:
    await _async_interconnects_describe(args.name)


# ── proliant oneview power ─────────────────────────────────────────────────────

def _power_request_description(args: argparse.Namespace) -> str:
    target_type = getattr(args, "power_target_type", "")
    action = getattr(args, "power_action", "efuse")
    if target_type == "flm":
        return f"{action} {target_type} {args.enclosure} bay {args.bay}"
    name = getattr(args, "name", None)
    if name:
        return f"{action} {target_type} {name}"
    enclosure = getattr(args, "enclosure", None)
    bay = getattr(args, "bay", None)
    return f"{action} {target_type} {enclosure or '?'} bay {bay or '?'}"


def _render_power_result(result: dict) -> None:
    from rich.markup import escape

    console = get_console()
    status = result.get("status", "")
    target = escape(str(result.get("target", "target")))
    target_type = escape(str(result.get("target_type", "target")))
    action_label = escape(str(result.get("action_label", result.get("action", "power action"))))
    method = escape(str(result.get("method", "")))

    if status == "dry-run":
        console.print(f"[yellow]Dry run:[/yellow] would {action_label} {target_type} [bold]{target}[/bold] via {method}.")
        console.print(f"[dim]URL:[/dim] {escape(str(result.get('url', '')))}")
        console.print_json(data=result.get("payload"))
        return

    console.print(f"[green]✓[/green] Requested {action_label} for {target_type} [bold]{target}[/bold] via {method}.")
    task_uri = result.get("task_uri")
    if task_uri:
        console.print(f"[dim]Task:[/dim] {escape(str(task_uri))}")


async def _cmd_power(args: argparse.Namespace) -> None:
    from proliant.oneview.power import run_power_action

    async with _load_client() as client:
        desc = _power_request_description(args)
        with get_console().status(f"[dim]Issuing OneView power action ({desc})…[/dim]"):
            result = await run_power_action(
                client,
                args.power_action,
                args.power_target_type,
                name=getattr(args, "name", None),
                enclosure=getattr(args, "enclosure", None),
                bay=getattr(args, "bay", None),
                dry_run=args.dry_run,
            )

    if getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON:
        print_json(result)
        return

    _render_power_result(result)


async def _cmd_efuse(args: argparse.Namespace) -> None:
    from proliant.oneview.efuse import run_efuse_action

    if not args.dry_run and not args.yes:
        raise ValueError("eFuse performs a hard power-cycle; rerun with --yes or use --dry-run")

    async with _load_client() as client:
        desc = _power_request_description(args)
        with get_console().status(f"[dim]Issuing OneView eFuse power-cycle ({desc})…[/dim]"):
            result = await run_efuse_action(
                client,
                args.power_target_type,
                name=getattr(args, "name", None),
                enclosure=getattr(args, "enclosure", None),
                bay=getattr(args, "bay", None),
                dry_run=args.dry_run,
            )

    if getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON:
        print_json(result)
        return

    _render_power_result(result)


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


def _filter_mac_maps(
    maps: list[dict],
    vlan: int = 0,
    network_name: str = "",
) -> list[dict]:
    """Filter MAC-trace maps by VLAN and/or network-name substring."""
    filtered = maps
    if vlan:
        filtered = [
            m for m in filtered
            if (m.get("network") or {}).get("vlan") == vlan
        ]
    if network_name:
        needle = network_name.lower()
        filtered = [
            m for m in filtered
            if needle in ((m.get("network") or {}).get("name") or "").lower()
        ]
    return filtered


async def _async_describe_mac(mac: str, vlan: int = 0, network_name: str = "") -> None:
    from proliant.oneview.topology import trace_mac, render_network_map_ascii

    async with _load_client() as client:
        filter_desc = []
        if vlan:
            filter_desc.append(f"vlan={vlan}")
        if network_name:
            filter_desc.append(f"network={network_name}")
        suffix = f" ({', '.join(filter_desc)})" if filter_desc else ""
        with get_console().status(f"[dim]Tracing MAC {mac}{suffix} across the fabric…[/dim]"):
            maps = await trace_mac(client, mac)
    maps = _filter_mac_maps(maps, vlan=vlan, network_name=network_name)

    if get_output_mode() == OutputMode.JSON:
        print_json(maps)
        return
    if not maps:
        if vlan or network_name:
            criteria = []
            if vlan:
                criteria.append(f"vlan={vlan}")
            if network_name:
                criteria.append(f"network={network_name}")
            get_console().print(
                f"[yellow]MAC {mac} had no trace paths matching {', '.join(criteria)}.[/yellow]"
            )
        else:
            get_console().print(f"[yellow]MAC {mac} not found in any forwarding table.[/yellow]")
        return
    for i, nm in enumerate(maps):
        if i:
            get_console().rule(style="dim")
        get_console().print(render_network_map_ascii(nm, mac=mac, color=True), markup=True, highlight=False)


async def _cmd_network_describe(args: argparse.Namespace) -> None:
    await _async_describe_network(args.name)


async def _cmd_mac_describe(args: argparse.Namespace) -> None:
    await _async_describe_mac(
        args.address,
        vlan=args.vlan or 0,
        network_name=getattr(args, "network_name", "") or "",
    )


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


# ── proliant oneview release (HPE Synergy Software Releases matrix) ──────────

async def _async_release_matrix(args: argparse.Namespace) -> None:
    from proliant.oneview.ssp_update import (
        SSP_COMPAT_AS_OF, SSP_COMPAT_SOURCE_URL, compat_matrix, oneview_track,
    )

    json_mode = getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON
    console = get_console()

    current_track = ""
    current_version = ""
    try:
        async with _load_client() as client:
            ver = await client.get("/rest/appliance/nodeinfo/version")
        current_version = (ver or {}).get("softwareVersion", "")
        current_track = oneview_track(current_version)
    except Exception:  # noqa: BLE001 — advisory only; still show the full matrix offline
        pass

    rows = compat_matrix()

    if json_mode:
        print_json({
            "as_of": SSP_COMPAT_AS_OF,
            "source_url": SSP_COMPAT_SOURCE_URL,
            "current_appliance_version": current_version,
            "current_track": current_track,
            "releases": rows,
        })
        return

    table = make_table(
        f"HPE Synergy Software Releases  (snapshot as of {SSP_COMPAT_AS_OF})",
        ("Composer (HPE OneView)", {"no_wrap": True}),
        ("Recommended SSP",        {"no_wrap": True}),
        ("Additionally Supported SSP", {"no_wrap": False}),
    )
    for r in rows:
        label = f"Composer (HPE OneView) {r['track']}"
        if r["track"] and r["track"] == current_track:
            label += "  [green]← this appliance[/green]"
        table.add_row(label, r["recommended"], ", ".join(r["supported"]) or "—")
    console.print(table)
    console.print(f"[dim]Source: {SSP_COMPAT_SOURCE_URL}[/dim]", highlight=False)
    if current_version and not current_track:
        console.print(
            f"[dim]Note: could not match this appliance's version ({current_version}) "
            "to a row above.[/dim]", highlight=False,
        )


async def _cmd_release(args: argparse.Namespace) -> None:
    await _async_release_matrix(args)


# ── proliant oneview activity (GUI Activity feed: tasks + alerts) ─────────────

_ACTIVITY_STATE_STYLE = {
    # task states
    "completed": "green", "running": "cyan", "warning": "yellow",
    "error": "red", "terminated": "red", "timeout": "red",
    # alert states
    "active": "yellow", "locked": "yellow", "cleared": "dim",
}
_ACTIVITY_SEVERITY_STYLE = {
    "ok": "green", "warning": "yellow", "critical": "red", "disabled": "dim",
    "unknown": "dim",
}


def _activity_state_markup(row: dict) -> str:
    """Colour a row's State cell. Alerts fold severity in (e.g. Cleared but
    was Critical) so a glance matches the GUI's severity dot + state label."""
    state = row.get("state") or ""
    style = _ACTIVITY_STATE_STYLE.get(state.lower(), "white")
    text = state or "—"
    if row.get("kind") == "alert" and row.get("severity"):
        sev = row["severity"]
        sev_style = _ACTIVITY_SEVERITY_STYLE.get(sev.lower(), "white")
        # Only annotate severity when it adds signal (not a plain OK/cleared).
        if sev.lower() not in ("ok",):
            text = f"{state} [{sev_style}]({sev})[/{sev_style}]"
    return f"[{style}]{text}[/{style}]"


async def _async_activity(args: argparse.Namespace) -> None:
    from proliant.oneview.activity import fetch_activity

    json_mode = getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON
    console = get_console()

    include_tasks = not getattr(args, "alerts_only", False)
    include_alerts = not getattr(args, "tasks_only", False)
    limit = getattr(args, "limit", 20) or 20
    toplevel_only = not getattr(args, "all_tasks", False)

    async with _load_client() as client:
        with console.status("[dim]Fetching OneView activity…[/dim]"):
            rows = await fetch_activity(
                client, limit=limit,
                include_tasks=include_tasks, include_alerts=include_alerts,
                resource=getattr(args, "resource", None),
                state=getattr(args, "state", None),
                toplevel_only=toplevel_only,
            )

    if json_mode:
        print_json({"activity": rows})
        return

    from proliant.oneview.activity import format_local, format_elapsed

    table = make_table(
        "OneView Activity",
        ("Name", {"no_wrap": False, "overflow": "fold", "min_width": 24}),
        ("Resource", {"no_wrap": True, "overflow": "ellipsis"}),
        ("Date", {"no_wrap": True}),
        ("Duration", {"no_wrap": True, "justify": "right"}),
        ("State", {"no_wrap": True}),
        ("Owner", {"no_wrap": True}),
    )
    for r in rows:
        marker = "•" if r["kind"] == "task" else "!"
        marker_style = "cyan" if r["kind"] == "task" else "magenta"
        dur = r.get("duration") or ""
        # A running top-level task's own percent/modified stay flat while its
        # subtasks do the work, so show live elapsed (GUI-style) instead.
        if r["kind"] == "task" and (r.get("state") or "").lower() == "running":
            dur = format_elapsed(r.get("created")) or dur
        name = r["name"] or "—"
        table.add_row(
            f"[{marker_style}]{marker}[/{marker_style}] {name}",
            _short_server_name(r["resource"]) or "—",
            format_local(r["created"]),
            dur or "—",
            _activity_state_markup(r),
            r["owner"] or "[dim]System[/dim]",
        )

    if not rows:
        console.print("[dim]No activity found.[/dim]")
        return
    console.print(table)
    console.print(
        "[dim]• = task (an operation OneView ran)   ! = alert (a health/condition notice). "
        "Mirrors the OneView GUI Activity page.[/dim]",
        highlight=False,
    )


async def _cmd_activity(args: argparse.Namespace) -> None:
    if getattr(args, "watch", False) or getattr(args, "tree", False):
        await _async_activity_detail(args)
    else:
        await _async_activity(args)


# ── activity subtask tree / live watch (the GUI's expandable operation view) ──

def _tree_state_markup(row: dict) -> str:
    state = row.get("state") or ""
    style = _ACTIVITY_STATE_STYLE.get(state.lower(), "white")
    return f"[{style}]{state or '—'}[/{style}]"


def _build_activity_tree_table(node: dict, target: dict):
    """Rich table for a task's subtask hierarchy, mirroring the GUI's expanded
    Activity view: indented Name, per-node Resource / State / % / Phase text."""
    from proliant.oneview.activity import flatten_tree, phase_text, format_elapsed

    root_row = node["task"]
    running = (root_row.get("state") or "").lower() == "running"
    dur = format_elapsed(root_row.get("created")) if running else (root_row.get("duration") or "")
    title = f"{root_row.get('name') or 'Task'}"
    if root_row.get("resource"):
        title += f" — {_short_server_name(root_row['resource'])}"
    subtitle = f"{root_row.get('state') or ''}"
    if dur:
        subtitle += f"  ({dur})"

    def _phase_lines(row: dict) -> list[str]:
        # The full progress log (e.g. every "Stage component N/6" / "Install
        # component N/6" line as it happened) when the task carries one --
        # matches the GUI's scrolling log under a task like "Apply profile".
        # Most subtasks (e.g. "Power on"/"Power off") never accumulate more
        # than their own single status line, so this falls back to that.
        log = row.get("progress_log") or []
        if log:
            return log
        single = phase_text(row)
        return [single] if single else []

    table = make_table(
        title,
        ("Name / phase", {"no_wrap": False, "overflow": "fold", "min_width": 30}),
        ("Resource", {"no_wrap": True, "overflow": "ellipsis"}),
        ("State", {"no_wrap": True}),
        ("%", {"no_wrap": True, "justify": "right"}),
    )
    for depth, row in flatten_tree(node):
        indent = "  " * depth
        marker = "└ " if depth else ""
        pct = row.get("percent")
        pct_txt = f"{int(pct)}%" if isinstance(pct, (int, float)) else "—"
        step = _step_segment(row)
        name_cell = f"{indent}{marker}{row.get('name') or '—'}"
        for line in _phase_lines(row):
            name_cell += f"\n{indent}   [dim]{line}[/dim]"
        if step:
            name_cell += f"\n{indent}   [dim]{step}[/dim]"
        table.add_row(
            name_cell,
            _short_server_name(row.get("resource") or "") or "—",
            _tree_state_markup(row),
            pct_txt,
        )
    return table, subtitle


async def _async_activity_detail(args: argparse.Namespace) -> None:
    """Show one operation's subtask tree (``--tree``) and, with ``--watch``,
    live-refresh it until it reaches a terminal state — the CLI equivalent of
    watching the OneView GUI Activity page's expanded subtasks."""
    import asyncio

    from proliant.oneview.activity import (
        fetch_task_tree, find_active_task, find_task, tree_is_terminal,
    )

    console = get_console()
    resource = getattr(args, "resource", None)
    tree_token = getattr(args, "tree", None)
    if tree_token is True:  # bare --tree flag, no value
        tree_token = None
    watch = getattr(args, "watch", False)
    json_mode = getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON
    lookup_count = max(500, int(getattr(args, "limit", 20) or 20))

    async with _load_client() as client:
        with console.status("[dim]Locating operation…[/dim]"):
            if watch:
                target = await find_active_task(
                    client, resource=resource, token=tree_token, count=lookup_count)
                if target is None:
                    target = await find_task(
                        client, resource=resource, token=tree_token, count=lookup_count)
            else:
                target = await find_task(
                    client, resource=resource, token=tree_token, count=lookup_count)

        if target is None:
            console.print("[yellow]No matching operation found in recent activity.[/yellow]")
            console.print("[dim]Try 'proliant oneview activity' to see the feed, or widen "
                          "--resource / --tree filters.[/dim]")
            return

        if json_mode:
            tree = await fetch_task_tree(client, target["uri"])
            print_json({"task": tree})
            return

        if not watch:
            tree = await fetch_task_tree(client, target["uri"])
            table, subtitle = _build_activity_tree_table(tree, target)
            console.print(table)
            console.print(f"[dim]{subtitle}[/dim]", highlight=False)
            if not tree_is_terminal(tree):
                console.print("[dim]Still running — add [bold]--watch[/bold] to follow it "
                              "live.[/dim]", highlight=False)
            return

        # Live watch loop.
        from rich.console import Group
        from rich.live import Live

        interval = float(getattr(args, "watch_interval", 4) or 4)
        console.print(
            f"[dim]Watching '{target.get('name')}' on "
            f"{_short_server_name(target.get('resource') or '')} — Ctrl-C to stop.[/dim]",
            highlight=False,
        )
        final_tree = None
        try:
            with Live(console=console, refresh_per_second=4, transient=False) as live:
                while True:
                    tree = await fetch_task_tree(client, target["uri"])
                    final_tree = tree
                    if tree is None:
                        live.update("[red]Task disappeared from OneView.[/red]")
                        break
                    table, subtitle = _build_activity_tree_table(tree, target)
                    live.update(Group(table, f"[dim]{subtitle}[/dim]"))
                    if tree_is_terminal(tree):
                        break
                    await asyncio.sleep(interval)
        except KeyboardInterrupt:
            console.print("[dim]Stopped watching (task keeps running on the "
                          "appliance).[/dim]", highlight=False)
            return

        if final_tree is not None:
            state = (final_tree["task"].get("state") or "").lower()
            if state == "completed":
                console.print("[green]✓ Operation completed.[/green]")
            elif state == "warning":
                console.print("[yellow]⚠ Operation finished with a warning "
                              "(see phase text above / 'proliant oneview activity').[/yellow]")
            elif state in ("error", "terminated", "killed"):
                console.print("[red]✗ Operation failed "
                              "(see phase text above / 'proliant oneview activity').[/red]")


# ── proliant oneview update appliance readiness / cleanup ─────────────────────

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
            f"[bold]proliant oneview update appliance cleanup[/bold].",
            highlight=False,
        )
    if stale.get("external_unused"):
        console.print(
            f"[dim]Note:[/dim] {stale['external_unused']} additional unused baseline(s) exist "
            f"only in an external repository and are not deletable via OneView — see "
            f"[bold]proliant oneview update appliance cleanup[/bold] for details.",
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


def _resource_kind_label(kind: str) -> str:
    return {
        "server-profile": "Profile",
        "frame-link-module": "FLM",
        "interconnect": "IC",
    }.get(kind, kind)


def _short_baseline_label(label: str) -> str:
    text = str(label or "").strip()
    if not text or text == "—":
        return "—"
    match = re.search(r"(?:SY-)?(\d{4}\.\d{2}\.\d{2}(?:\.\d{2})?)", text)
    return match.group(1) if match else text


def _short_firmware_resource_name(row: dict[str, Any]) -> str:
    name = row.get("resource_name") or "—"
    kind = row.get("kind") or ""
    if kind == "frame-link-module":
        return re.sub(r"\bframe link module\b", "FLM", name, flags=re.IGNORECASE)
    if kind == "interconnect":
        return re.sub(r"\binterconnect\b", "IC", name, flags=re.IGNORECASE)
    return name


def _plain_component_name(component: dict[str, Any]) -> str:
    return str(component.get("name") or "").lower()


_MONTH_ABBR_TO_NUM = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _normalize_embedded_dates(text: str) -> str:
    """Reformat dates embedded in firmware version strings to the YYYY.MM.DD
    style used for baseline labels (e.g. "2024.11.01"), so mixed date formats
    like "(03/16/2023)" or "Mar 07 2023" render consistently."""

    def _mmddyyyy(match: re.Match[str]) -> str:
        month, day, year = match.group(1), match.group(2), match.group(3)
        return f"{year}.{int(month):02d}.{int(day):02d}"

    def _mon_dd_yyyy(match: re.Match[str]) -> str:
        month = _MONTH_ABBR_TO_NUM.get(match.group(1).lower())
        if not month:
            return match.group(0)
        day, year = match.group(2), match.group(3)
        return f"{year}.{month}.{int(day):02d}"

    text = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", _mmddyyyy, text)
    text = re.sub(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s+(\d{4})\b",
        _mon_dd_yyyy,
        text,
        flags=re.IGNORECASE,
    )
    return text


def _known_firmware_version(version: Any) -> str:
    text = str(version or "").strip()
    if text.lower() in {"unknown", "n/a", "na", "none"}:
        return ""
    return _normalize_embedded_dates(text)


def _firmware_component_role(component: dict[str, Any]) -> str:
    name = _plain_component_name(component)
    tokens = set(re.findall(r"[a-z0-9]+", name))
    if "ilo" in tokens or {"integrated", "lights", "out"} <= tokens:
        if {"driver", "drivers"} & tokens or "ilorest" in name:
            return ""
        return "iLO"
    if "redundant" in tokens and "rom" in tokens:
        return ""
    if "bios" in tokens or "rom" in tokens:
        return "BIOS"
    return ""


def _parse_version_key(text: str) -> tuple[int, ...] | None:
    """Extract a comparable numeric version key (e.g. (2, 78)) from a firmware
    version string, ignoring any parenthesized date and non-numeric family/board
    codes — e.g. "I42 v2.78 (2023.03.16)" -> (2, 78)."""
    value = _known_firmware_version(text)
    if not value:
        return None
    stripped = re.sub(r"\([^)]*\)", "", value)
    match = re.search(r"\d+(?:\.\d+){1,4}", stripped)
    if not match:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def _target_is_newer(current: str, target: str) -> bool:
    cur_key = _parse_version_key(current)
    tgt_key = _parse_version_key(target)
    if cur_key is None or tgt_key is None:
        return False
    length = max(len(cur_key), len(tgt_key))
    cur_key += (0,) * (length - len(cur_key))
    tgt_key += (0,) * (length - len(tgt_key))
    return tgt_key > cur_key


def _firmware_version_style(current: str, target: str, *, is_target: bool) -> str:
    if is_target and _target_is_newer(current, target):
        return "green"
    return "white"


def _component_update_style(update_required: bool | None) -> str:
    if update_required is True:
        return "[yellow]Update[/yellow]"
    if update_required is False:
        return "[green]Current[/green]"
    return "[dim]Unknown[/dim]"


def _styled_firmware_version(label: str, version: str, style: str) -> str:
    value = _known_firmware_version(version) or "—"
    text = f"{label}:{value}" if label else value
    return f"[{style}]{text}[/]"


def _server_component_score(component: dict[str, Any], role: str) -> int:
    name = _plain_component_name(component)
    tokens = set(re.findall(r"[a-z0-9]+", name))
    score = 0
    if _known_firmware_version(component.get("target_version")):
        score += 20
    if component.get("update_required") is True:
        score += 10
    if role == "BIOS" and {"system", "rom"} <= tokens:
        score += 30
    if role == "iLO" and re.search(r"\bilo\s*\d+\b", name, flags=re.IGNORECASE):
        score += 30
    return score


def _server_version_summary(components: list[dict[str, Any]], *, target: bool) -> str:
    by_role: dict[str, dict[str, Any]] = {}
    for component in components:
        role = _firmware_component_role(component)
        if not role:
            continue
        if role not in by_role or _server_component_score(component, role) > _server_component_score(by_role[role], role):
            by_role[role] = component

    segments: list[str] = []
    for role in ("BIOS", "iLO"):
        component = by_role.get(role)
        if not component:
            continue
        current_version = _known_firmware_version(component.get("current_version"))
        target_version = _known_firmware_version(component.get("target_version"))
        style = _firmware_version_style(current_version, target_version, is_target=target)
        value = target_version if target else current_version
        if role == "iLO" and value:
            value = f"({value})"
        segments.append(_styled_firmware_version(role, value, style))
    return " ".join(segments) if segments else "—"


def _single_component_version_summary(components: list[dict[str, Any]], *, target: bool) -> str:
    component = components[0] if components else {}
    current_version = _known_firmware_version(component.get("current_version"))
    target_version = _known_firmware_version(component.get("target_version"))
    style = _firmware_version_style(current_version, target_version, is_target=target)
    value = target_version if target else current_version
    return _styled_firmware_version("", value, style)


def _firmware_version_summary(row: dict[str, Any], *, target: bool) -> str:
    components = row.get("components") or []
    if row.get("kind") == "server-profile":
        return _server_version_summary(components, target=target)
    return _single_component_version_summary(components, target=target)


def _row_matches_resource(row: dict[str, Any], name: str) -> bool:
    query = (name or "").strip().lower()
    if not query:
        return False
    candidates = {
        (row.get("resource_name") or "").lower(),
        _short_firmware_resource_name(row).lower(),
    }
    return query in candidates


def _find_compliance_resource(rows: list[dict[str, Any]], name: str) -> dict[str, Any]:
    matches = [row for row in rows if _row_matches_resource(row, name)]
    if len(matches) == 1:
        return matches[0]
    if matches:
        names = ", ".join(row.get("resource_name") or "" for row in matches)
        raise ValueError(f"Resource name '{name}' is ambiguous. Matches: {names}")
    known = ", ".join(row.get("resource_name") or "" for row in rows)
    raise ValueError(f"Compliance resource '{name}' not found. Known resources: {known}")


async def _fetch_compliance_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    from proliant.oneview.firmware import list_compliance

    baseline = getattr(args, "baseline", None)
    target_desc = f"baseline {baseline}" if baseline else "latest registered SSP/SPP"
    async with _load_client() as client:
        with get_console().status(f"[dim]Checking firmware compliance against {target_desc}…[/dim]"):
            return await list_compliance(client, baseline=baseline)


async def _async_compliance_list(args: argparse.Namespace) -> None:
    rows = await _fetch_compliance_rows(args)
    if get_output_mode() == OutputMode.JSON:
        print_json(rows)
        return

    console = get_console()
    if not rows:
        console.print("[yellow]No firmware compliance resources found.[/yellow]")
        return

    target_label = _short_baseline_label(rows[0]["target_baseline_label"])
    table = make_table(
        f"Firmware Compliance vs {target_label}  ({len(rows)} resources)",
        ("Resource", {"min_width": 24, "no_wrap": True}),
        ("Current BL", {"no_wrap": True}),
        ("Target BL", {"no_wrap": True}),
        ("Current Version", {"no_wrap": True}),
        ("Target Version", {"no_wrap": True}),
    )
    for r in rows:
        table.add_row(
            _short_firmware_resource_name(r),
            _short_baseline_label(r["current_baseline_label"]),
            _short_baseline_label(r["target_baseline_label"]),
            _firmware_version_summary(r, target=False),
            _firmware_version_summary(r, target=True),
        )
    console.print(table)


def _render_compliance_describe(row: dict[str, Any]) -> None:
    console = get_console()
    console.print(Panel(
        f"[bold]{row.get('resource_name') or '—'}[/bold]\n"
        f"Type:             {_resource_kind_label(row.get('kind') or '')}\n"
        f"Current baseline: {_short_baseline_label(row.get('current_baseline_label') or '')}\n"
        f"Target baseline:  {_short_baseline_label(row.get('target_baseline_label') or '')}",
        title="Firmware Compliance",
        border_style="cyan",
    ))

    components = row.get("components") or []
    if not components:
        console.print("[yellow]No component firmware details returned for this resource.[/yellow]")
        return

    table = make_table(
        f"Component Firmware  ({len(components)})",
        ("Component", {"min_width": 28}),
        ("Location", {"no_wrap": True}),
        ("Current Version", {"no_wrap": True}),
        ("Target Version", {"no_wrap": True}),
        ("Status", {"justify": "center", "no_wrap": True}),
    )
    for component in components:
        current = component.get("current_version") or ""
        target = component.get("target_version") or ""
        table.add_row(
            component.get("name") or "Unknown component",
            component.get("location") or "—",
            _styled_firmware_version("", current, _firmware_version_style(current, target, is_target=False)),
            _styled_firmware_version("", target, _firmware_version_style(current, target, is_target=True)),
            _component_update_style(component.get("update_required")),
        )
    console.print(table)


async def _async_compliance_describe(args: argparse.Namespace) -> None:
    rows = await _fetch_compliance_rows(args)
    try:
        row = _find_compliance_resource(rows, args.name)
    except ValueError as exc:
        get_console().print(f"[red]Error:[/red] {exc}")
        sys.exit(1)

    if get_output_mode() == OutputMode.JSON:
        print_json(row)
        return
    _render_compliance_describe(row)


async def _cmd_compliance_list(args: argparse.Namespace) -> None:
    await _async_compliance_list(args)


async def _cmd_compliance_describe(args: argparse.Namespace) -> None:
    await _async_compliance_describe(args)


# ── proliant oneview update enclosure (SSP baseline rollout) ──────────────────

_SSP_POLL_S = 5
_SSP_TASK_TIMEOUT_S = 90 * 60
# How long to keep repolling the *actual* installed firmware after OneView's
# LE-level task reports "Completed" before giving up and calling it
# "unverified". Verified live: a "Completed" report can land in ~7s while the
# real interconnect stage+activate cycle underneath keeps running for several
# more minutes -- 5 min gives that room to finish without hanging forever on
# a genuinely stuck/no-op update.
_SSP_VERIFY_TIMEOUT_S = 5 * 60


def _is_affirmative(answer: str) -> bool:
    """Treat y/yes/ok/okay (case-insensitive) as an affirmative answer.

    "ok"/"okay" are accepted in addition to the conventional y/yes because the
    validation-warning panel's subtitle deliberately echoes the OneView GUI's
    own wording ("...click OK to proceed"), so typing "ok" is the natural
    response to what's on screen -- and previously wasn't recognized at all.
    """
    return answer.strip().lower() in ("y", "yes", "ok", "okay")


def _render_ssp_plan(console, plan: dict) -> None:
    """Render the non-destructive apply plan (baseline + per-target changes)."""
    b = plan.get("baseline") or {}
    released = (b.get("release_date") or "")[:10]
    compat = plan.get("compat") or {}
    _compat_style = {
        "recommended": "green", "supported": "green",
        "unsupported": "red", "unknown": "yellow",
    }.get(compat.get("status"), "dim")
    compat_line = ""
    if compat:
        appliance = compat.get("appliance_version") or "unknown"
        compat_line = (
            f"\nOneView:         {appliance}"
            f"\nCompatibility:   [{_compat_style}]{compat.get('message', '')}[/{_compat_style}]"
        )
        rec = compat.get("recommended")
        if rec:
            rec_date = compat.get("recommended_release_date")
            rec_txt = f"SSP {rec}" + (f" (released {rec_date})" if rec_date else "")
            compat_line += f"\nHPE recommends:  [green]{rec_txt}[/green]"
    console.print(Panel(
        f"Baseline:        [bold]{b.get('name') or '—'}[/bold]"
        + (f"  ({b.get('version')})" if b.get("version") else "")
        + (f"\nReleased:        {released}" if released else "")
        + compat_line
        + f"\nChanges:         [bold]{plan.get('changes', 0)}[/bold] target(s) would be updated",
        title="SSP Firmware Apply — Plan", border_style="cyan"))
    if compat.get("source_url"):
        url = compat["source_url"]
        console.print(
            f"[dim]OneView↔SSP compatibility per HPE Synergy Software Releases "
            f"(as of {compat.get('as_of', '')}) — double-check at:[/dim]"
        )
        console.print(
            f"  [cyan underline][link={url}]{url}[/link][/cyan underline]",
            soft_wrap=True, highlight=False,
        )

    rows = plan.get("logical_enclosures", []) + plan.get("server_profiles", [])
    if not rows:
        return
    table = make_table(
        "Targets",
        ("Scope", {"no_wrap": True}),
        ("Target", {"no_wrap": True}),
        ("Change", {"no_wrap": True}),
        ("Detail", {"style": "dim"}),
    )
    _scope_label = {"logical-enclosure": "Shared infra", "server-profile": "Compute"}
    for r in rows:
        change = "[yellow]update[/yellow]" if r["will_change"] else "[green]current[/green]"
        table.add_row(_scope_label.get(r["kind"], r["kind"]), r["name"], change, r.get("detail", ""))
    console.print(table)


# ── interactive wizard (bare `proliant oneview update enclosure`, no NAME) ────

class _WizardBack(Exception):
    """Raised when the operator types 'b' to go back a step in the wizard."""


class _WizardCancelled(Exception):
    """Raised when the operator types 'c'/'q' to cancel the wizard outright."""


def _wizard_default_index(options: list[tuple[Any, str]], current: Any, fallback: int | None) -> int | None:
    """Prefer the position of the previously-chosen value over a hardcoded
    fallback, so going 'back' to a step and returning to it shows your prior
    answer as the default instead of resetting it."""
    if current is not None:
        for idx, (value, _) in enumerate(options):
            if isinstance(value, dict) and isinstance(current, dict):
                if value.get("uri") and value.get("uri") == current.get("uri"):
                    return idx
            elif value == current:
                return idx
    return fallback


def _wizard_choice(
    console, title: str, options: list[tuple[Any, str]], *,
    default_index: int | None = 0, allow_back: bool = True,
) -> Any:
    """Print a numbered menu and prompt for a selection by number.

    Typing 'b' goes back a step (only if *allow_back*); 'c'/'q' cancels the
    whole wizard. Blank input picks *default_index* when one is given.
    """
    console.print(f"\n[bold cyan]{title}[/bold cyan]")
    for i, (_, label) in enumerate(options, start=1):
        console.print(f"  {i}. {label}")
    hint = f"1-{len(options)}"
    if allow_back:
        hint += ", b=back"
    hint += ", c=cancel"
    default_hint = f"  (default {default_index + 1})" if default_index is not None else ""
    while True:
        raw = console.input(f"Select ({hint}){default_hint}: ", markup=False).strip().lower()
        if not raw:
            if default_index is not None:
                return options[default_index][0]
            console.print(f"[red]Enter a number 1-{len(options)}.[/red]")
            continue
        if raw in ("c", "q", "cancel"):
            raise _WizardCancelled()
        if allow_back and raw in ("b", "back"):
            raise _WizardBack()
        if not raw.isdigit() or not (1 <= int(raw) <= len(options)):
            console.print(f"[red]Enter a number 1-{len(options)}, "
                           f"{'b to go back, ' if allow_back else ''}or c to cancel.[/red]")
            continue
        return options[int(raw) - 1][0]


_WIZARD_STEPS = ("le", "baseline", "scope", "install_type", "activation", "force", "review")


async def _wizard_update_enclosure(args: argparse.Namespace) -> None:
    """Interactive, menu-driven alternative to passing every --flag up front.

    Triggered by omitting NAME: `proliant oneview update enclosure` with no
    arguments. Walks through logical enclosure -> baseline -> scope ->
    install type -> activation mode -> force -> review/execute one numbered
    question at a time; 'b' goes back a step, 'c' cancels without changing
    anything. Once every answer is collected it fills in the same
    ``args`` fields the flag-driven form uses and delegates to
    ``_async_update_enclosure`` so the plan / type-to-confirm /
    validation-warning behavior is identical either way.
    """
    from proliant.oneview.ssp_update import fetch_apply_targets

    console = get_console()
    json_mode = getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON
    if json_mode:
        console.print("[red]NAME is required with --json[/red] (the interactive wizard needs a terminal).")
        return
    if not sys.stdin.isatty():
        console.print(
            "[red]NAME is required when not running interactively[/red] (e.g. piped input/CI). "
            "Pass it directly: proliant oneview update enclosure <NAME> [--baseline ...] [--execute]."
        )
        return

    async with _load_client() as client:
        with console.status("[dim]Fetching logical enclosures and SSP baselines…[/dim]"):
            data = await fetch_apply_targets(client)

    les = data["logical_enclosures"]
    baselines = data["baselines"]
    if not les:
        console.print("[red]No logical enclosures found on this appliance.[/red]")
        return
    if not baselines:
        console.print("[red]No SSP/SPP baselines are registered on this appliance.[/red]")
        return

    state: dict[str, Any] = {
        "le": None, "baseline": None, "scope": "shared-infra",
        "install_type": None, "activation_mode": "orchestrated",
        "force": False, "execute": False,
    }
    i = 0
    direction = 1
    while 0 <= i < len(_WIZARD_STEPS):
        step = _WIZARD_STEPS[i]
        # install_type is never shown in the wizard — GUI doesn't expose it either
        # and the default "keep existing" matches GUI behaviour.  Operators who
        # want a non-default value must pass --install-type on the command line.
        if step == "install_type":
            i += direction
            continue
        # activation mode only matters when interconnects are actually part of
        # this rollout — "profiles-only" never touches them, so asking would
        # just be a confusing no-op question.
        if step == "activation" and state["scope"] == "profiles-only":
            i += direction
            continue
        try:
            if step == "le":
                options = [
                    (le, le["name"] + (f"  [dim]({le['status']})[/dim]" if le.get("status") else ""))
                    for le in les
                ]
                default_index = 0 if len(les) == 1 else _wizard_default_index(options, state["le"], None)
                state["le"] = _wizard_choice(
                    console, "Which logical enclosure do you want to update?",
                    options, default_index=default_index, allow_back=False,
                )
            elif step == "baseline":
                options = []
                for b in baselines:
                    released = (b.get("release_date") or "")[:10]
                    label = f"{b.get('name') or 'SSP'} ({b.get('version') or '?'})"
                    if released:
                        label += f"  [dim]released {released}[/dim]"
                    options.append((b, label))
                default_index = _wizard_default_index(options, state["baseline"], 0)  # newest-first
                state["baseline"] = _wizard_choice(
                    console, "Which SSP baseline do you want to apply?", options, default_index=default_index,
                )
            elif step == "scope":
                options = [
                    ("shared-infra", "Shared infrastructure only (frame link modules + interconnects)"),
                    ("shared-infra-and-profiles",
                     "Shared infrastructure + every server profile in this enclosure "
                     "[yellow](compute modules will power-cycle)[/yellow]"),
                    ("profiles-only",
                     "Server profiles only, skip shared infra "
                     "[yellow](compute modules will power-cycle)[/yellow] -- no GUI equivalent, "
                     "useful if shared infra is already current or stuck"),
                ]
                default_index = _wizard_default_index(options, state["scope"], 0)
                state["scope"] = _wizard_choice(
                    console, "What should this rollout cover?", options, default_index=default_index,
                )
            elif step == "install_type":
                options = [
                    (None, "Keep each profile's existing install-type setting (default)"),
                    ("firmware-only", "Firmware only"),
                    ("firmware-and-drivers", "Firmware + OS drivers"),
                    ("firmware-offline", "Firmware only, offline mode"),
                ]
                default_index = _wizard_default_index(options, state["install_type"], 0)
                state["install_type"] = _wizard_choice(
                    console, "Compute install type override?", options, default_index=default_index,
                )
            elif step == "activation":
                options = [
                    ("orchestrated",
                     "Orchestrated (default) — one redundant side at a time, non-disruptive; "
                     "requires real interconnect redundancy"),
                    ("parallel",
                     "[bold red]Parallel[/bold red] — flashes every interconnect at once, regardless "
                     "of redundancy; a full network outage, and compute modules must be powered off first"),
                ]
                default_index = _wizard_default_index(options, state["activation_mode"], 0)
                state["activation_mode"] = _wizard_choice(
                    console, "Interconnect activation mode?", options, default_index=default_index,
                )
            elif step == "force":
                options = [
                    (False, "No (default) — keep OneView's own non-disruptive validation"),
                    (True, "Yes — force reinstall / bypass non-disruptive validation"),
                ]
                default_index = _wizard_default_index(options, state["force"], 0)
                state["force"] = _wizard_choice(
                    console, "Force reinstall even if already current?", options, default_index=default_index,
                )
            elif step == "review":
                le = state["le"]
                baseline = state["baseline"]
                summary = (
                    f"Logical enclosure: [bold]{le['name']}[/bold]\n"
                    f"Baseline:          [bold]{baseline.get('name')} ({baseline.get('version')})[/bold]\n"
                    f"Scope:             {state['scope']}\n"
                )
                if state["scope"] in ("shared-infra-and-profiles", "profiles-only"):
                    summary += f"Install type:      {state['install_type'] or 'keep existing'}\n"
                if state["scope"] != "profiles-only":
                    summary += f"Activation mode:   {state['activation_mode']}\n"
                summary += f"Force:             {state['force']}"
                console.print(Panel(summary, title="Review your selections", border_style="cyan"))
                options = [
                    (False, "No — just show the plan, don't change anything"),
                    (True, "Yes — apply this now"),
                ]
                default_index = _wizard_default_index(options, state["execute"], 0)
                state["execute"] = _wizard_choice(
                    console, "Execute this now?", options, default_index=default_index,
                )
        except _WizardBack:
            direction = -1
            i -= 1
            continue
        except _WizardCancelled:
            console.print("[yellow]Cancelled — nothing was changed.[/yellow]")
            return
        direction = 1
        i += 1

    args.name = state["le"]["name"]
    args.baseline = state["baseline"].get("version") or state["baseline"].get("name")
    args.scope = state["scope"]
    args.install_type = state["install_type"]
    args.activation_mode = state["activation_mode"]
    args.force = state["force"]
    args.execute = state["execute"]
    await _async_update_enclosure(args)


async def _async_update_enclosure(args: argparse.Namespace) -> None:
    from proliant.oneview.ssp_update import (
        INSTALL_TYPES,
        LE_SCOPE_SHARED,
        LE_SCOPE_SHARED_AND_PROFILES,
        fetch_apply_targets,
        find_le_by_name,
        profiles_under_le,
        run_ssp_apply,
        select_baseline,
    )

    json_mode = getattr(args, "json_output", False) or get_output_mode() == OutputMode.JSON
    console = get_console()

    async with _load_client() as client:
        with console.status("[dim]Fetching SSP baselines and rollout targets…[/dim]"):
            data = await fetch_apply_targets(client)

    le = find_le_by_name(data["logical_enclosures"], args.name)
    if le is None:
        known = ", ".join(x["name"] for x in data["logical_enclosures"]) or "none found"
        if json_mode:
            print_json({"status": "error", "reason": "logical enclosure not found", "query": args.name})
        else:
            console.print(f"[red]Logical enclosure '{args.name}' not found.[/red]")
            console.print(f"[dim]Known: {known}[/dim]")
        return

    baseline = select_baseline(data["baselines"], getattr(args, "baseline", None))
    if baseline is None:
        names = ", ".join(f"{b['name']} ({b['version']})" for b in data["baselines"][:8]) or "none registered"
        if json_mode:
            print_json({"status": "error", "reason": "baseline not found",
                        "query": getattr(args, "baseline", None), "available": data["baselines"]})
        else:
            q = getattr(args, "baseline", None)
            console.print(f"[red]No SSP baseline matches '{q}'.[/red]" if q
                          else "[red]No SSP/SPP baselines are registered on this appliance.[/red]")
            console.print(f"[dim]Available: {names}[/dim]")
        return

    # `or 1` would silently treat --concurrency 0 as the default (0 is falsy)
    # instead of catching it as invalid -- use an explicit None check so 0
    # and negative values both hit the validation below.
    raw_concurrency = getattr(args, "concurrency", None)
    concurrency = int(raw_concurrency) if raw_concurrency is not None else 1
    if concurrency < 1:
        if json_mode:
            print_json({"status": "error", "reason": "--concurrency must be at least 1", "value": concurrency})
        else:
            console.print("[red]--concurrency must be at least 1.[/red]")
        return

    scope = getattr(args, "scope", None) or "shared-infra"
    # "profiles-only" skips the logical-enclosure/interconnect step entirely --
    # useful when shared infra is already current, mid-investigation, or (as
    # seen live) stuck/unverified, since compute firmware is applied via its
    # own independent PUT /rest/server-profiles/{id} call and was never
    # HPE-documented as depending on the LE's own rollout completing first.
    les = [] if scope == "profiles-only" else [le]
    profs = (
        profiles_under_le(le, data["server_profiles"], data["hardware_enclosure_map"])
        if scope in ("shared-infra-and-profiles", "profiles-only") else []
    )

    install_type = INSTALL_TYPES.get(getattr(args, "install_type", None) or "")
    execute = bool(getattr(args, "execute", False))
    activation_mode = getattr(args, "activation_mode", None) or "orchestrated"
    interconnect_activation_mode = "Parallel" if activation_mode == "parallel" else "Orchestrated"

    bars: dict = {}

    def _stop_bar() -> None:
        p = bars.pop("bar", None)
        bars.pop("rows", None)
        if p is not None:
            try:
                p.stop()
            except Exception:  # noqa: BLE001
                pass

    def _row_key(payload: dict) -> str:
        # With --concurrency 1 (the default) every event -- shared infra AND
        # every profile in turn -- shares one reused row, exactly like before
        # this feature existed. Only split into one row per in-flight target
        # when concurrency>1 actually means multiple profiles can be running
        # at once (payload["target"] is the tag _run_one_profile adds; plain
        # LE events never carry it, so LE always stays on its own row too).
        if concurrency > 1:
            return payload.get("target") or "__single__"
        return "__single__"

    def on_event(kind: str, payload: dict) -> None:
        if json_mode:
            return
        if kind == "plan":
            _render_ssp_plan(console, payload)
        elif kind == "applying":
            label = "Shared infra" if payload.get("kind") == "logical-enclosure" else "Compute"
            full_label = f"{label}: {payload.get('name')}"
            desc = f"[bold]{full_label}[/bold]"
            row_key = _row_key(payload)
            rows = bars.setdefault("rows", {})
            p = bars.get("bar")
            if p is None:
                from rich.progress import (
                    BarColumn, Progress, SpinnerColumn, TaskProgressColumn,
                    TextColumn, TimeElapsedColumn,
                )
                p = Progress(
                    SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
                    BarColumn(), TaskProgressColumn(), TimeElapsedColumn(), console=console,
                )
                p.start()
                bars["bar"] = p
            row = rows.get(row_key)
            if row is not None:
                # Reuse the row's existing task instead of stop()-ing and
                # re-start()-ing a brand new Progress/Live each time. Rich
                # only allows one *active* Live per Console — repeatedly
                # tearing one down and standing up a new one (especially
                # right around a console.input() prompt for the
                # validation-warning confirm) has caused garbled, overlapping
                # terminal output in practice. reset() clears the
                # percent/elapsed-time clock for the new target.
                p.reset(row["task_id"], total=100, completed=0, description=desc)
                row["label"] = full_label
                row["log_len"] = 0
            else:
                rows[row_key] = {"task_id": p.add_task(desc, total=100), "label": full_label, "log_len": 0}
        elif kind == "task-progress":
            p = bars.get("bar")
            rows = bars.get("rows") or {}
            row = rows.get(_row_key(payload))
            if p is None or row is None:
                return
            _print_new_progress_log_lines(console, row, "log_len", payload, label=row.get("label", ""))
            pct = payload.get("percent")
            state = payload.get("state") or payload.get("status") or "working…"
            stage = payload.get("stage") or ""
            res = payload.get("resource") or ""
            step = _step_segment(payload)
            label = row.get("label", "")
            segs = []
            if label:
                segs.append(f"[bold]{label}[/bold]")
            segs.append(f"[cyan]{stage}[/cyan]" if stage else state)
            if stage and state:
                # A "Warning" terminal state doesn't mean the update actually
                # landed -- OneView uses it for both "succeeded with a minor
                # note" and "refused to apply due to a validation guard", and
                # 100% here only means *OneView's own task* finished running,
                # not that firmware was pushed. Call it out in yellow instead
                # of blending it in as dim, ordinary progress chatter.
                state_style = "yellow" if state.lower() == "warning" else "dim"
                segs.append(f"[{state_style}]{state}[/{state_style}]")
            if step:
                segs.append(f"[dim]{step}[/dim]")
            if res and res not in label:
                segs.append(f"[dim]({res})[/dim]")
            desc = "  ".join(segs)
            if isinstance(pct, (int, float)):
                p.update(row["task_id"], completed=pct, description=desc)
            else:
                p.update(row["task_id"], description=desc)
        elif kind == "applied":
            # Print a permanent one-line result for this target (Rich lets you
            # print "above" an active Progress/Live using the same console).
            # With --concurrency 1 the single reused row is left running so
            # the *next* target's "applying" event can reset() it in place;
            # with concurrency>1 each target has its own row, so it's removed
            # once done rather than left sitting at a stale 100% forever.
            name = payload.get("name", "?")
            state = (payload.get("state") or "").lower()
            if state == "completed":
                console.print(f"[green]✓[/green] {name} updated.", highlight=False)
            elif state == "warning":
                console.print(f"[yellow]⚠[/yellow] {name} finished with a warning.", highlight=False)
            else:
                console.print(
                    f"[red]✗[/red] {name} — {payload.get('state') or payload.get('status') or 'error'}.",
                    highlight=False,
                )
            row_key = _row_key(payload)
            if row_key != "__single__":
                p = bars.get("bar")
                rows = bars.get("rows") or {}
                row = rows.pop(row_key, None)
                if p is not None and row is not None:
                    try:
                        p.remove_task(row["task_id"])
                    except Exception:  # noqa: BLE001
                        pass

    def confirm(plan: dict) -> bool:
        if getattr(args, "yes", False):
            return True
        if json_mode:
            return False  # never silently flash hardware in scripted mode
        token = baseline.get("version") or baseline.get("name") or "apply"
        compat = plan.get("compat") or {}
        compat_warn = ""
        if compat.get("status") == "unsupported":
            compat_warn = (
                f"\n[red]⚠ {compat.get('message', '')}[/red] "
                "Verify the SSP release notes before proceeding."
            )
        infra_ct = sum(1 for r in plan.get("logical_enclosures", []) if r.get("will_change"))
        compute_ct = sum(1 for r in plan.get("server_profiles", []) if r.get("will_change"))
        impact = []
        if infra_ct:
            if interconnect_activation_mode == "Parallel":
                impact.append(
                    "[bold red]Parallel activation: ALL interconnects flash at the same "
                    "time — expect a network outage during the update, regardless of "
                    "redundancy.[/bold red]"
                )
            else:
                impact.append("[red]Interconnects will reboot (one redundant side at a time).[/red]")
        if compute_ct:
            impact.append(
                f"[red]{compute_ct} compute module(s) will power-cycle — ensure hosts are ready "
                "for their servers to reboot.[/red]"
            )
            if concurrency > 1:
                wave_ct = min(concurrency, compute_ct)
                impact.append(
                    f"[bold red]--concurrency {concurrency}: up to {wave_ct} of those compute "
                    "module(s) will power-cycle AT THE SAME TIME (not one at a time) — ensure "
                    "that many hosts can go down together.[/bold red]"
                )
        else:
            impact.append(
                "[green]No server profiles are in this plan — compute modules will NOT be "
                "power-cycled and running servers stay up.[/green]"
            )
        console.print(Panel(
            f"About to APPLY SSP [bold]{baseline.get('name')} ({baseline.get('version')})[/bold] to "
            f"[bold]{plan.get('changes', 0)}[/bold] target(s).\n"
            + " ".join(impact)
            + compat_warn,
            title="Confirm SSP firmware apply", border_style="red"))
        ans = console.input(
            f'Type the baseline version "{token}" to proceed (or anything else to abort): ',
            markup=False,
        )
        return ans.strip() == token

    def on_validation_blocked(info: dict) -> str:
        """OneView refused the update as potentially disruptive (a non-redundant
        fabric). Present the same choice the GUI does — with the extra "which
        uplink set / which leg" detail the GUI makes you hunt for — and let the
        operator decide on the spot:

          A) abort and fix the fabric redundancy first (recommended, clean); or
          B) force the update through now (disruptive to the affected uplinks and
             any server profiles riding them), matching clicking through the
             GUI's warning and accepting the disruption.

        Returns the decision string the SSP engine understands ("abort",
        "proceed", or "force")."""
        if getattr(args, "yes", False):
            return "proceed"  # --yes: proceed non-disruptively; use --force to force
        if json_mode:
            return "abort"  # never silently proceed/force in scripted mode
        _stop_bar()
        reason = info.get("reason") or (
            "OneView flagged this update as potentially disruptive and refused "
            "to apply it, but did not report a specific reason."
        )
        resolution = info.get("resolution") or ""
        body = f"[yellow]{reason}[/yellow]"
        if resolution:
            body += f"\n\n[bold]Resolution:[/bold] {resolution}"
        for u in info.get("uplinks") or []:
            head = f"\n\n[bold]Uplink set [cyan]{u.get('name', '?')}[/cyan]"
            if u.get("li_name"):
                head += f"[/bold] (on {u['li_name']})"
            else:
                head += "[/bold]"
            if u.get("status"):
                head += f" — status [yellow]{u['status']}[/yellow]"
            body += head
            for leg in u.get("legs", []):
                sp = leg.get("speed") or ""
                sp = f"{sp}G" if sp and sp != "unknown" else "no link"
                body += (
                    f"\n    {leg.get('location', '?')} {leg.get('port', '')}: "
                    f"{leg.get('state', '?')}, {sp}"
                )
            if u.get("note"):
                body += f"\n    → {u['note']}"
        console.print(Panel(
            body,
            title=f"⚠ Validation warning — {info.get('name')}", border_style="yellow",
            subtitle="Review the warnings. If the conditions are acceptable, then click OK to proceed.",
            subtitle_align="left"))
        # markup=False on the input so bracketed hints render literally rather
        # than being parsed (and silently dropped) as Rich markup.
        console.print(
            "[bold]Choose an action:[/bold]\n"
            "  [cyan]A[/cyan]) Abort — fix the uplink redundancy above first "
            "(recommended, no disruption)\n"
            "  [cyan]B[/cyan]) Force the update through now — [red]disruptive[/red]: "
            "the affected uplinks (and any server profiles riding them) may briefly "
            "lose connectivity"
        )
        if interconnect_activation_mode != "Parallel":
            # In Orchestrated mode, force can't flash a fabric that has only one
            # live leg (there's no redundant side to sequence). Say so up front so
            # the operator isn't surprised when B still gets refused.
            console.print(
                "[dim]     Note: in Orchestrated mode, forcing may still be refused if an uplink "
                "set has only one live leg — a single-legged fabric can only be updated by fixing "
                "redundancy or re-running with --activation-mode parallel.[/dim]"
            )
        ans = console.input("Your choice [A/b]: ", markup=False).strip().lower()
        # "proceed" = bypass the non-disruptive validation guard only (matches
        # the GUI's plain "OK to proceed").  We do NOT set forceInstallFirmware
        # here — the user didn't tick "Force re-installation or downgrade".
        return "proceed" if ans in ("b", "f", "force") else "abort"

    factory = _oneview_client_factory()
    try:
        result = await run_ssp_apply(
            factory,
            baseline=baseline, le_targets=les, profile_targets=profs,
            scope=LE_SCOPE_SHARED_AND_PROFILES if scope in ("shared-infra-and-profiles", "profiles-only") else LE_SCOPE_SHARED,
            install_type=install_type, force=bool(getattr(args, "force", False)),
            interconnect_activation_mode=interconnect_activation_mode,
            execute=execute, confirm=confirm if execute else None,
            on_validation_blocked=on_validation_blocked if execute else None, on_event=on_event,
            poll_interval_s=_SSP_POLL_S, task_timeout_s=_SSP_TASK_TIMEOUT_S,
            verify_timeout_s=_SSP_VERIFY_TIMEOUT_S, profile_concurrency=concurrency,
            appliance_version=data.get("appliance_version", ""),
            baselines=data.get("baselines", []),
        )
    finally:
        _stop_bar()

    if json_mode:
        print_json(result)
        return

    order = "compute only (shared infrastructure not included)" if scope == "profiles-only" \
        else "shared infrastructure first, then compute"
    _render_ssp_apply_result(
        console, result,
        plan_message=f"Re-run with [bold]--execute[/bold] to apply ({order}).",
        interconnect_activation_mode=interconnect_activation_mode,
    )


def _render_ssp_apply_result(
    console, result: dict, *, plan_message: str, interconnect_activation_mode: str = "Orchestrated",
) -> None:
    """Render ``run_ssp_apply()``'s human-readable result.

    Shared by ``update enclosure`` and ``server-profiles update`` so the
    plan/applied/failed/blocked/unverified wording (including the blocked-
    uplink retry guidance) stays consistent across both entry points.
    """
    status = result.get("status")
    if status == "planned":
        console.print(f"\n[green]Plan only.[/green] {plan_message}")
    elif status == "nothing-to-do":
        console.print("\n[green]All selected targets already match this baseline.[/green] "
                      "Use [bold]--force[/bold] to reapply anyway.")
    elif status == "aborted":
        console.print("\n[yellow]Aborted — nothing was modified.[/yellow]")
    elif status == "applied":
        console.print(f"\n[green]SSP apply complete.[/green] Updated "
                      f"[bold]{len(result.get('results', []))}[/bold] target(s).")
    elif status in ("failed", "blocked", "unverified"):
        done = result.get("results", [])
        # Find every entry that actually carries *this* outcome via its own
        # "outcome" marker -- important now that a failed/blocked/unverified
        # server profile no longer stops the rest of the batch (see
        # run_ssp_apply's profile wave loop), so `done` can hold a mix of
        # outcomes and the *last* entry is no longer reliably "the" problem.
        # Falls back to the last entry for callers/results that predate the
        # "outcome" field (e.g. a single-target result with no marker set).
        matching = [r for r in done if r.get("outcome") == status]
        if not matching:
            matching = [done[-1]] if done else [{}]
        if len(done) > 1 and len(matching) < len(done):
            ok_count = len(done) - len(matching)
            console.print(
                f"\n[bold]{ok_count}[/bold] of [bold]{len(done)}[/bold] target(s) applied "
                f"normally; [bold]{len(matching)}[/bold] did not — see below for each:"
            )
        for entry in matching:
            _render_ssp_apply_entry_problem(console, status, entry, interconnect_activation_mode)


def _render_ssp_apply_entry_problem(
    console, status: str, entry: dict, interconnect_activation_mode: str,
) -> None:
    """Render the failed/blocked/unverified detail for one ``results`` entry.

    Split out of ``_render_ssp_apply_result`` so it can be called once per
    problem entry -- a batch update no longer stops at the first
    failed/blocked/unverified server profile, so there can be more than one.
    """
    if status == "failed":
        reason = entry.get("failed_reason") or ""
        resolution = entry.get("failed_resolution") or ""
        console.print(
            f"\n[red]SSP apply failed[/red] on [bold]{entry.get('name', '?')}[/bold] "
            f"({entry.get('status') or entry.get('state') or 'error'}).\n"
            + (f"[dim]{reason}[/dim]\n" if reason else "")
            + (f"[dim]Resolution: {resolution}[/dim]\n" if resolution else "")
            + "See [bold]proliant oneview activity[/bold] (or the OneView UI Activity page) "
            "for the full task detail."
        )
    elif status == "blocked":
        reason = entry.get("blocked_reason") or (
            "OneView validated the update and refused to apply it, but did not "
            "report a specific reason — check the OneView UI Activity log."
        )
        resolution = entry.get("blocked_resolution") or ""
        console.print(
            f"\n[yellow]SSP apply not applied[/yellow] on [bold]{entry.get('name', '?')}[/bold] — "
            "nothing was changed.\n"
            f"[dim]{reason}[/dim]\n"
            + (f"[dim]Resolution: {resolution}[/dim]\n" if resolution else "")
        )
        for u in entry.get("blocked_uplinks") or []:
            note = u.get("note") or ""
            console.print(
                f"[dim] • {u.get('name', '?')}"
                + (f" (on {u['li_name']})" if u.get("li_name") else "")
                + (f": {note}" if note else "")
                + "[/dim]"
            )
        if entry.get("blocked_forced"):
            # The operator already forced it (chose B or passed --force) and
            # OneView STILL refused. Two distinct root causes:
            #   1. Uplink not redundant — one live leg, Orchestrated can't sequence it
            #   2. Downlink not redundant — server profiles have single-homed NICs;
            #      Orchestrated can't flash the IC they're on without dropping them
            # Distinguish by whether uplink details are present.
            has_uplink_detail = bool(entry.get("blocked_uplinks"))
            if interconnect_activation_mode == "Parallel":
                console.print(
                    "You already forced this in [bold]Parallel[/bold] mode and OneView still "
                    "refused, so the block is not just the non-disruptive guard — check the "
                    "OneView UI Activity log and the uplink/interconnect health above before "
                    "retrying."
                )
            elif has_uplink_detail:
                console.print(
                    "Forcing did [bold]not[/bold] help: an [bold]Orchestrated[/bold] update flashes "
                    "one redundant leg at a time, so it cannot update a fabric that has only one "
                    "live leg — not even with force. To proceed, either:\n"
                    "  • [green]restore redundancy[/green] on the uplink set(s) above (bring the "
                    "down leg up), then re-run — clean, no outage; or\n"
                    "  • re-run with [bold]--activation-mode parallel[/bold] to flash all "
                    "interconnects at once — this [red]will drop the fabric[/red] (and any server "
                    "profiles riding it) for the duration of the update."
                )
            else:
                # Downlink-only block: uplinks are fine, but server profiles have
                # single-homed NICs. Orchestrated mode can't flash an IC without
                # dropping any profile whose only downlink is on that IC.
                console.print(
                    "Forcing did [bold]not[/bold] help: the block is [bold]downlink[/bold] "
                    "redundancy, not uplink. The server profile(s) listed above have single-homed "
                    "NIC connections — [bold]Orchestrated[/bold] mode cannot flash an interconnect "
                    "without dropping them. To proceed, either:\n"
                    "  • [green]configure NIC teaming/bonding[/green] on the affected server "
                    "profile(s) so each profile has connections on both ICs — then re-run "
                    "(non-disruptive); or\n"
                    "  • re-run with [bold]--activation-mode parallel[/bold] to flash all "
                    "interconnects at once — the affected server profiles will [red]briefly lose "
                    "network connectivity[/red] during the IC reboot."
                )
        else:
            console.print(
                "Re-run interactively and choose [bold]B[/bold] at the warning to force it through "
                "([red]disruptive[/red]), or pass [bold]--force[/bold] to do the same "
                "non-interactively — or fix the uplink redundancy above first for a clean, "
                "non-disruptive update."
            )
    elif status == "unverified":
        # OneView itself reported "Completed" -- unlike "blocked" there's no
        # known validation reason to retry with force, so this isn't treated
        # as a failure, just an honest "we couldn't confirm it" instead of a
        # blind "SSP apply complete".
        reason = entry.get("unverified_reason") or (
            "OneView reported this update as completed, but re-checking the actual "
            "installed baseline did not confirm it."
        )
        console.print(
            f"\n[yellow]SSP apply reported complete but could not be verified[/yellow] on "
            f"[bold]{entry.get('name', '?')}[/bold].\n"
            f"[dim]{reason}[/dim]\n"
            "This can happen if OneView's internal state takes a moment to catch up after "
            "a task finishes. Check [bold]proliant oneview interconnects describe[/bold] or "
            "[bold]proliant oneview activity[/bold] to confirm the real installed firmware "
            "before trusting this result."
        )


async def _cmd_update_enclosure(args: argparse.Namespace) -> None:
    if not getattr(args, "name", None):
        await _wizard_update_enclosure(args)
    else:
        await _async_update_enclosure(args)


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


# ── proliant oneview update appliance run / pending / cancel ──────────────────
# Appliance SOFTWARE upgrade: upload an update .bin -> stage it -> (guarded)
# install -> monitor the reboot. The staging half is safe/read-mostly; the
# install half reboots the appliance, so it is gated behind --execute plus a
# typed confirmation. See proliant.oneview.appliance_update for the REST flow.

_REBOOT_WAIT_TIMEOUT_S = 40 * 60
_REBOOT_POLL_S = 20


def _oneview_client_factory():
    """Return a zero-arg factory that builds a fresh (unconnected) OneViewClient.

    Used for the install/reboot polling loop, which must reconnect across the
    appliance restart. A plain factory (no 'Connecting…' status spinner) keeps
    the progress display clean.
    """
    from proliant.oneview.client import OneViewClient
    from proliant.oneview.config import load_oneview_config

    cfg = load_oneview_config()

    def factory():
        return OneViewClient(cfg["host"], cfg["username"], cfg["password"])

    return factory


def _print_pending_panel(console, pending: dict) -> None:
    lines = [
        f"Staged image:  [bold]{pending.get('file_name') or '—'}[/bold]",
        f"Version:       [bold]{pending.get('version') or '—'}[/bold]",
    ]
    est = pending.get("estimated_upgrade_minutes")
    if est:
        lines.append(f"Est. duration: ~{est} min")
    reboot = pending.get("reboot_required", True)
    lines.append(f"Reboot:        {'required' if reboot else 'not required'}")
    console.print(Panel("\n".join(lines), title="Staged Appliance Update", border_style="cyan"))


async def _async_upgrade_pending() -> None:
    from proliant.oneview.appliance_update import read_pending

    async with _load_client() as client:
        with get_console().status("[dim]Reading staged appliance update…[/dim]"):
            pending = await read_pending(client)

    if get_output_mode() == OutputMode.JSON:
        print_json(pending or {})
        return

    console = get_console()
    if not pending:
        console.print("[green]No appliance update is currently staged.[/green]")
        return
    _print_pending_panel(console, pending)


async def _cmd_upgrade_pending(args: argparse.Namespace) -> None:
    await _async_upgrade_pending()


async def _async_upgrade_cancel(do: bool) -> None:
    from proliant.oneview.appliance_update import clear_pending, read_pending

    console = get_console()
    async with _load_client() as client:
        pending = await read_pending(client)
        if not pending:
            if get_output_mode() == OutputMode.JSON:
                print_json({"removed": False, "reason": "nothing staged"})
            else:
                console.print("[green]Nothing staged — no pending update to cancel.[/green]")
            return
        if not do:
            if get_output_mode() == OutputMode.JSON:
                print_json({"removed": False, "pending": pending})
            else:
                _print_pending_panel(console, pending)
                console.print("\n[yellow]Dry run.[/yellow] Re-run with [bold]--yes[/bold] "
                              "to remove this staged update.")
            return
        with console.status("[dim]Removing staged update…[/dim]"):
            await clear_pending(client)

    if get_output_mode() == OutputMode.JSON:
        print_json({"removed": True, "pending": pending})
    else:
        console.print("[green]Staged appliance update removed.[/green]")


async def _cmd_upgrade_cancel(args: argparse.Namespace) -> None:
    await _async_upgrade_cancel(do=bool(getattr(args, "yes", False)))


def _resolve_upgrade_image(args: argparse.Namespace, platform: str, console, json_mode: bool):
    """Resolve the update image from --image or --from-dir. Returns ApplianceImage or None."""
    from proliant.oneview.appliance_update import discover_images, parse_image_filename

    image_path = getattr(args, "image", None)
    from_dir = getattr(args, "from_dir", None)

    if image_path:
        if not os.path.isfile(image_path):
            console.print(f"[red]Image not found:[/red] {image_path}")
            return None
        name = os.path.basename(image_path)
        size = os.path.getsize(image_path)
        img = parse_image_filename(name, path=image_path, size_bytes=size)
        if img is None:
            # Unrecognized name — allow it, but we can't infer version/platform.
            from proliant.oneview.appliance_update import ApplianceImage
            console.print(f"[yellow]Warning:[/yellow] '{name}' doesn't match the expected "
                          "appliance-update naming; proceeding but version can't be verified.")
            img = ApplianceImage(path=image_path, filename=name, platform="unknown",
                                 family_label="", version="", version_tuple=(0, 0, 0),
                                 size_bytes=size)
        return img

    if from_dir:
        try:
            images = discover_images(from_dir, platform=platform)
        except OSError as exc:
            console.print(f"[red]Cannot read image directory:[/red] {exc}")
            return None
        if not images:
            console.print(f"[yellow]No {platform} appliance update images found in[/yellow] {from_dir}")
            return None
        if len(images) == 1:
            return images[0]
        if json_mode:
            console.print("[red]Multiple images found; specify one with --image in JSON mode.[/red]")
            return None
        # Interactive picker (newest last).
        table = make_table(
            f"Available {platform} appliance updates in {from_dir}",
            ("#", {"justify": "right", "no_wrap": True}),
            ("Version", {"no_wrap": True}),
            ("File", {"no_wrap": True}),
            ("Size", {"justify": "right", "no_wrap": True}),
        )
        for i, im in enumerate(images, 1):
            table.add_row(str(i), im.version, im.filename, f"{im.as_dict()['size_gb']:.2f} GB")
        console.print(table)
        try:
            choice = console.input(
                f"Select image [1-{len(images)}] (default {len(images)} = newest): ", markup=False
            ).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Cancelled.[/yellow]")
            return None
        if not choice:
            return images[-1]
        if not choice.isdigit() or not (1 <= int(choice) <= len(images)):
            console.print("[red]Invalid selection.[/red]")
            return None
        return images[int(choice) - 1]

    console.print("[red]Provide an image with[/red] --image <path> [red]or[/red] --from-dir <dir>.")
    return None


async def _async_upgrade_run(args: argparse.Namespace) -> None:
    from proliant.oneview.appliance_update import (
        platform_for_appliance,
        read_pending,
        run_appliance_upgrade,
    )
    from proliant.oneview.upgrade import gather_readiness

    console = get_console()
    json_mode = get_output_mode() == OutputMode.JSON

    # 1. Connect once for the readiness gate + platform/version detection.
    async with _load_client() as client:
        with console.status("[dim]Assessing OneView upgrade readiness…[/dim]"):
            report = await gather_readiness(client)
        existing = await read_pending(client)

    app = report["appliance"]
    verdict = report["verdict"]
    platform = platform_for_appliance(app.get("model") or app.get("family") or "")

    if not json_mode:
        vstyle = _VERDICT_STYLE.get(verdict, "white")
        console.print(Panel(
            f"[bold]{app.get('model') or 'OneView appliance'}[/bold]  "
            f"({app.get('family') or '—'})\n"
            f"Current version:  [bold]{app.get('software_version') or '—'}[/bold]\n"
            f"Readiness verdict: [bold {vstyle}]{verdict}[/bold {vstyle}]",
            title="Appliance Upgrade", border_style=vstyle))

    if verdict == "FAIL" and not getattr(args, "force", False):
        msg = "Readiness verdict is FAIL. Resolve the failing checks (see " \
              "'proliant oneview update appliance readiness') or re-run with --force."
        if json_mode:
            print_json({"status": "aborted", "reason": "readiness FAIL", "verdict": verdict})
        else:
            console.print(f"[red]{msg}[/red]")
        return

    # 2. Resolve the update image.
    img = _resolve_upgrade_image(args, platform, console, json_mode)
    if img is None:
        return

    if not json_mode:
        console.print(f"Selected image: [bold]{img.filename}[/bold]"
                      + (f"  (version {img.version})" if img.version else ""))

    # 3. Build a reconnect-capable factory and run the staged/guarded flow.
    factory = _oneview_client_factory()
    execute = bool(getattr(args, "execute", False))

    # Live progress bars: a byte bar for the multi-GB upload and a phase/percent
    # bar for the install. Rich only allows one live display at a time, but the
    # upload bar is always stopped (on 'staged') before the install bar starts
    # and before any confirm prompt, so they never overlap.
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    bars: dict = {}

    def _stop_bar(key: str) -> None:
        p = bars.pop(key, None)
        bars.pop(key + "_task", None)
        if p is not None:
            try:
                p.stop()
            except Exception:  # noqa: BLE001 — never let display teardown mask the result
                pass

    def on_event(kind: str, data: dict) -> None:
        if json_mode:
            return
        if kind == "clearing":
            console.print("[dim]Clearing previously staged update…[/dim]")
        elif kind == "already-staged":
            console.print("[dim]Requested image is already staged — skipping upload.[/dim]")
        elif kind == "uploading":
            total = data.get("size_bytes") or 0
            p = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
            )
            p.start()
            bars["upload"] = p
            bars["upload_task"] = p.add_task(
                f"[bold]Uploading {data.get('filename')}", total=total or None
            )
        elif kind == "upload-progress":
            p = bars.get("upload")
            if p is not None:
                p.update(
                    bars["upload_task"],
                    completed=data.get("completed") or 0,
                    total=data.get("total") or None,
                )
        elif kind == "staged":
            _stop_bar("upload")
            _print_pending_panel(console, data)
        elif kind == "installing":
            console.print("[yellow]Starting appliance install — do NOT power-cycle the "
                          "appliance until it completes.[/yellow]")
            p = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                console=console,
            )
            p.start()
            bars["install"] = p
            bars["install_task"] = p.add_task("Starting install…", total=100)
        elif kind == "progress":
            p = bars.get("install")
            if p is None:
                return
            pct = data.get("percent")
            step = data.get("step") or data.get("status") or "working…"
            status = data.get("status") or ""
            desc = step if (not status or status.lower() in step.lower()) \
                else f"{step}  [dim]({status})[/dim]"
            if isinstance(pct, (int, float)):
                p.update(bars["install_task"], completed=pct, description=desc)
            else:
                p.update(bars["install_task"], description=desc)
        elif kind == "rebooting":
            p = bars.get("install")
            if p is not None:
                p.update(bars["install_task"],
                         description="[dim]appliance rebooting — waiting for it to come back…[/dim]")

    def confirm(staged: dict) -> bool:
        if getattr(args, "yes", False):
            return True
        if json_mode:
            return False  # never silently execute a reboot in scripted mode
        console.print(Panel(
            f"About to INSTALL [bold]{staged.get('version') or staged.get('file_name')}[/bold] "
            f"on [bold]{app.get('model')}[/bold] ({app.get('software_version')}).\n"
            "[red]The appliance will REBOOT and be offline for the duration.[/red]\n"
            "Ensure you have a current backup before proceeding.",
            title="Confirm appliance install", border_style="red"))
        token = app.get("software_version") or "upgrade"
        ans = console.input(f'Type the CURRENT version "{token}" to proceed (or anything else to abort): ')
        return ans.strip() == token

    try:
        result = await run_appliance_upgrade(
            factory, img,
            execute=execute,
            confirm=confirm if execute else None,
            on_event=on_event,
            clear_existing=bool(getattr(args, "clear_pending", False)),
            poll_interval_s=_REBOOT_POLL_S,
            reboot_timeout_s=_REBOOT_WAIT_TIMEOUT_S,
        )
    finally:
        _stop_bar("upload")
        _stop_bar("install")

    if json_mode:
        print_json(result)
        return

    status = result.get("status")
    if status == "staged":
        console.print("\n[green]Image staged.[/green] Re-run with [bold]--execute[/bold] "
                      "to install and reboot the appliance.")
    elif status == "conflict":
        console.print("\n[yellow]A different update is already staged.[/yellow] "
                      "Re-run with [bold]--clear-pending[/bold] to replace it, or "
                      "[bold]proliant oneview update appliance cancel --yes[/bold] to remove it.")
    elif status == "aborted":
        console.print("\n[yellow]Aborted — appliance not modified.[/yellow]")
    elif status == "completed":
        ver = (result.get("version") or {}).get("software_version", "")
        console.print(f"\n[green]Upgrade complete.[/green] Appliance now on "
                      f"[bold]{ver or 'the new version'}[/bold].")
    elif status == "failed":
        console.print("\n[red]Install failed.[/red] Check the appliance console / "
                      "'proliant oneview update appliance pending' and the OneView UI.")
    elif status == "timeout":
        console.print("\n[yellow]Timed out waiting for the appliance to return.[/yellow] "
                      "It may still be completing — check the OneView UI.")


async def _cmd_upgrade_run(args: argparse.Namespace) -> None:
    await _async_upgrade_run(args)


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
  proliant oneview firmware bundles                      Registered SPP/SSP bundles
  proliant oneview firmware repository                   Internal + external repositories
  proliant oneview compliance list                       Firmware compliance vs latest or selected baseline
  proliant oneview compliance describe aci-FM-host1      Per-component firmware comparison for one resource
  proliant oneview networks list                         All ethernet networks
  proliant oneview networks describe VLAN-160            Network overview + fabric mapping
  proliant oneview networksets list                      All network sets
  proliant oneview uplinksets list                       All uplink sets
  proliant oneview server-profiles list                  All server profiles
  proliant oneview power shutdown profile "ocp-host-1"   Gracefully shut down a profile's server
  proliant oneview power off server "Enclosure-01, bay 6"
                                                          Force power off server hardware
  proliant oneview efuse interconnect "Enclosure-01, interconnect 6" --yes
                                                          Hard eFuse power-cycle an interconnect bay
  proliant oneview efuse flm Enclosure-01 1 --yes         Hard eFuse power-cycle a frame link module
  proliant oneview li list                               Logical interconnects
  proliant oneview lig list                              Logical interconnect groups
  proliant oneview interconnects list                    Interconnect hardware
  proliant oneview interconnects describe "Enclosure-01, interconnect 6"
                                                          Interconnect detail (ports, utilization, firmware)
  proliant oneview mac list --address 00:11:22:33:44:55  MAC forwarding table by address
  proliant oneview mac list --vlan 100                   MAC forwarding table by VLAN
  proliant oneview mac describe 00:11:22:33:44:55        Trace a MAC end-to-end through the fabric
  proliant oneview mac describe 00:11:22:33:44:55 --vlan 160
  proliant oneview mac describe 00:11:22:33:44:55 --network-name VLAN-160
  proliant oneview enclosures list                       Physical enclosures
    proliant oneview enclosures describe Enclosure-01       Enclosure bay layout
  proliant oneview enclosure-groups list                 Enclosure groups
  proliant oneview logical-enclosures list               Logical enclosures
  proliant oneview uplinksets describe "pvlan-uplinkset" Full uplink set detail
  proliant oneview networksets describe "network-set-for-FM"
  proliant oneview server-profiles describe "ocp-single-node"
  proliant oneview reports memory
  proliant oneview release                              Composer <-> SSP compatibility matrix
  proliant oneview activity                             Recent tasks + alerts (GUI Activity feed)
  proliant oneview activity --resource LE01 --limit 30  Filter the feed to one resource
  proliant oneview activity --state Error               Only failed operations
  proliant oneview update enclosure LE01                 Plan an SSP rollout (shared infra only)
  proliant oneview update enclosure LE01 --baseline SY-2026.01.02 --scope shared-infra-and-profiles
                                                          Plan shared infra + every profile in LE01
  proliant oneview update enclosure LE01 --execute        Apply it (reboots interconnects/compute)
  proliant oneview update enclosure LE01 --execute --activation-mode parallel
                                                          Force through a non-redundant fabric (disruptive)
  proliant oneview update enclosure LE01 --scope profiles-only --execute
                                                          Update just server-profile firmware, skip shared infra
  proliant oneview update appliance readiness             Pre-upgrade readiness report
  proliant oneview update appliance cleanup                Preview unused firmware baselines
  proliant oneview update appliance cleanup --yes           Delete unused baselines (free disk)
  proliant oneview update appliance run --from-dir "\\\\srv\\iso\\Composer BIN"   Pick + stage an update image
  proliant oneview update appliance run --image update.bin        Stage a specific update image
  proliant oneview update appliance run --image update.bin --execute   Stage + install (reboots appliance)
  proliant oneview update appliance pending                Show the currently staged update
  proliant oneview update appliance cancel --yes           Remove a stuck staged update
  proliant oneview appliances list                       List configured appliances (* = active)
  proliant oneview appliances describe                   Show the active appliance's General page
  proliant oneview appliances use datacenter-b           Switch which appliance commands target
""",
    )

    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output as JSON (for piping/scripting)")

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    p_servers = sub.add_parser("servers", help="List managed servers")
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

    p_firmware = sub.add_parser("firmware", help="Appliance firmware bundles and repositories")
    s_firmware = p_firmware.add_subparsers(dest="what", metavar="ACTION")
    s_firmware.required = True

    p_fw_bundles = s_firmware.add_parser("bundles",
        help="List registered firmware bundles (SPP/SSP)")
    p_fw_bundles.set_defaults(func=_cmd_firmware_bundles_list)

    p_fw_repo = s_firmware.add_parser("repository",
        help="List firmware repositories (Internal + external)")
    p_fw_repo.set_defaults(func=_cmd_firmware_repository_list)

    p_compliance = sub.add_parser("compliance", help="Firmware compliance list and per-resource details")
    s_compliance = p_compliance.add_subparsers(dest="what", metavar="ACTION")
    s_compliance.required = True
    p_compliance_list = s_compliance.add_parser("list",
        help="List firmware compliance vs latest or selected SSP/SPP baseline")
    compliance_list_baseline = p_compliance_list.add_argument("--baseline", metavar="VERSION|NAME",
        help="Target registered SSP/SPP baseline (default: newest registered ServicePack bundle)")
    compliance_list_baseline.completer = _oneview_ssp_baseline_completer  # type: ignore[attr-defined]
    p_compliance_list.set_defaults(func=_cmd_compliance_list)

    p_compliance_desc = s_compliance.add_parser("describe",
        help="Show per-component firmware comparison for one compliance resource")
    compliance_desc_name = p_compliance_desc.add_argument("name", metavar="RESOURCE",
        help="Resource name from 'proliant oneview compliance list'")
    compliance_desc_name.completer = _oneview_profile_name_completer
    compliance_desc_baseline = p_compliance_desc.add_argument("--baseline", metavar="VERSION|NAME",
        help="Target registered SSP/SPP baseline (default: newest registered ServicePack bundle)")
    compliance_desc_baseline.completer = _oneview_ssp_baseline_completer  # type: ignore[attr-defined]
    p_compliance_desc.set_defaults(func=_cmd_compliance_describe)

    p_networks = sub.add_parser("networks", help="List or describe ethernet networks")
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

    p_networksets = sub.add_parser("networksets", help="List or describe network sets")
    s_networksets = p_networksets.add_subparsers(dest="what", metavar="ACTION")
    s_networksets.required = True
    p_ns_list = s_networksets.add_parser("list", help="List all network sets")
    p_ns_list.set_defaults(func=_cmd_networksets_list)
    p_ns_desc = s_networksets.add_parser("describe", help="Describe a network set")
    p_ns_desc_arg = p_ns_desc.add_argument("name", metavar="NAME", help="Name of the network set")
    p_ns_desc_arg.completer = _oneview_networkset_name_completer
    p_ns_desc.set_defaults(func=_cmd_describe, resource="networkset")

    p_uplinksets = sub.add_parser("uplinksets", help="List or describe uplink sets")
    s_uplinksets = p_uplinksets.add_subparsers(dest="what", metavar="ACTION")
    s_uplinksets.required = True
    p_ul_list = s_uplinksets.add_parser("list", help="List all uplink sets")
    p_ul_list.set_defaults(func=_cmd_uplinksets_list)
    p_ul_desc = s_uplinksets.add_parser("describe", help="Describe an uplink set")
    p_ul_desc_name = p_ul_desc.add_argument("name", metavar="NAME", help="Name of the uplink set")
    p_ul_desc_name.completer = _oneview_uplinkset_name_completer
    p_ul_desc.set_defaults(func=_cmd_describe, resource="uplinkset")

    p_profiles = sub.add_parser("server-profiles", help="List, describe, reapply, or update server profiles")
    s_profiles = p_profiles.add_subparsers(dest="what", metavar="ACTION")
    s_profiles.required = True
    p_sp_list = s_profiles.add_parser("list", help="List all server profiles")
    p_sp_list.set_defaults(func=_cmd_profiles_list)
    p_sp_desc = s_profiles.add_parser("describe", help="Describe a server profile")
    p_sp_desc_name = p_sp_desc.add_argument("name", metavar="NAME", help="Name of the server profile")
    p_sp_desc_name.completer = _oneview_profile_name_completer
    p_sp_desc.set_defaults(func=_cmd_describe, resource="server-profile")
    p_sp_reapply = s_profiles.add_parser("reapply",
        help="Reapply a server profile's stored configuration to its assigned hardware",
        description="Push the server profile's current, already-stored configuration back onto "
                    "its assigned server hardware -- the CLI equivalent of the OneView GUI's "
                    "'Reapply configuration' action. Nothing about the profile itself changes; "
                    "this makes OneView reconcile whatever is actually out of sync on the live "
                    "hardware (network/storage settings, BIOS, boot order, firmware consistency), "
                    "which is what clears alerts like 'Reapply the server profile'.")
    p_sp_reapply_name = p_sp_reapply.add_argument("name", metavar="NAME",
        help="Name of the server profile")
    p_sp_reapply_name.completer = _oneview_profile_name_completer
    p_sp_reapply.add_argument("--yes", action="store_true",
        help="Skip the type-to-confirm prompt. This reconfigures live hardware and can trigger "
             "a reboot -- use with care.")
    p_sp_reapply.set_defaults(func=_cmd_profiles_reapply)

    p_sp_update = s_profiles.add_parser("update",
        help="Update one server profile's SSP firmware baseline",
        description="Roll out an SSP (Synergy Service Pack) firmware baseline to a single named "
                    "server profile's compute module, without touching its logical enclosure's "
                    "shared infrastructure or any other profile -- useful when you only need to "
                    "bring one server current (e.g. after an eFuse/reapply) rather than every "
                    "profile under the same enclosure. Default is a non-destructive plan; add "
                    "--execute to apply. Equivalent to `update enclosure --scope profiles-only` "
                    "narrowed to one profile.")
    p_sp_update_name = p_sp_update.add_argument("name", metavar="NAME",
        help="Name of the server profile")
    p_sp_update_name.completer = _oneview_profile_name_completer
    p_sp_update_baseline = p_sp_update.add_argument("--baseline", metavar="NAME|VERSION",
        help="SSP bundle to apply (version / short name / uri id). Defaults to the newest "
             "registered SSP -- pass a specific one to repeat the same rollout for testing.")
    p_sp_update_baseline.completer = _oneview_ssp_baseline_completer  # type: ignore[attr-defined]
    p_sp_update.add_argument("--install-type", choices=("firmware-only", "firmware-and-drivers", "firmware-offline"),
        help="Compute install type override (default: keep the profile's existing setting).")
    p_sp_update.add_argument("--force", action="store_true",
        help="Force reinstall even if already at the baseline / bypass non-disruptive validation.")
    p_sp_update.add_argument("--execute", action="store_true",
        help="Actually apply (power-cycles the compute module). Default is plan only.")
    p_sp_update.add_argument("--yes", action="store_true",
        help="Skip the type-to-confirm prompt and any validation-warning prompt (with --execute).")
    p_sp_update.set_defaults(func=_cmd_profiles_update)

    p_power = sub.add_parser(
        "power",
        help="Gracefully power OneView-managed servers on/off",
        description=(
            "Power OneView-managed server hardware or server profiles on, off, "
            "or gracefully shut down, via OneView's server-hardware powerState "
            "API. For a hard power-cycle, use 'proliant oneview efuse' instead."
        ),
        epilog=(
            "Examples:\n"
            "  proliant oneview power shutdown profile ocp-host-1\n"
            "  proliant oneview power off server \"Enclosure-01, bay 6\"\n"
            "  proliant oneview power on server --enclosure Enclosure-01 --bay 6\n\n"
            "Note: on/off/shutdown use OneView server-hardware powerState and are\n"
            "      supported for server/profile targets only. For a hard\n"
            "      power-cycle (Synergy bay eFuse), use 'proliant oneview efuse'."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    s_power = p_power.add_subparsers(dest="power_action", metavar="ACTION")
    s_power.required = True
    for action_name, help_text in {
        "on": "Power on server hardware",
        "off": "Force power off server hardware",
        "shutdown": "Gracefully shut down server hardware",
    }.items():
        _add_power_action_parser(s_power, action_name, help_text)

    p_efuse = sub.add_parser(
        "efuse",
        help="Hard eFuse power-cycle a Synergy bay",
        description=(
            "Hard eFuse power-cycle an OneView-managed Synergy bay: server "
            "hardware, a server profile's assigned server, an interconnect, "
            "or a frame link module (FLM). This PATCHes the enclosure "
            "resource's bayPowerState to 'E-Fuse', the same mechanism "
            "OneView's own GUI uses for a hard reset -- distinct from the "
            "graceful on/off/shutdown actions under 'proliant oneview power', "
            "which use the server-hardware powerState resource instead. "
            "eFuse is purely a hard power-cycle: it has no on/off concept."
        ),
        epilog=(
            "Examples:\n"
            "  proliant oneview efuse server \"Enclosure-01, bay 6\" --yes\n"
            "  proliant oneview efuse server --enclosure Enclosure-01 --bay 6 --yes\n"
            "  proliant oneview efuse profile ocp-host-1 --yes\n"
            "  proliant oneview efuse interconnect \"Enclosure-01, interconnect 6\" --yes\n"
            "  proliant oneview efuse flm Enclosure-01 1 --yes\n\n"
            "Note: eFuse is a hard power-cycle, equivalent to physically "
            "removing and reseating a bay. It always requires --yes unless "
            "--dry-run is used."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_power_target_parsers(p_efuse, _cmd_efuse, include_yes=True)

    # ── logical interconnects ─────────────────────────────────────────────
    p_li = sub.add_parser("li", help="List logical interconnects")
    s_li = p_li.add_subparsers(dest="what", metavar="ACTION")
    s_li.required = True
    s_li.add_parser("list", help="List all logical interconnects").set_defaults(func=_cmd_li_list)

    p_lig = sub.add_parser("lig", help="List logical interconnect groups")
    s_lig = p_lig.add_subparsers(dest="what", metavar="ACTION")
    s_lig.required = True
    s_lig.add_parser("list", help="List all logical interconnect groups").set_defaults(func=_cmd_lig_list)

    p_ics = sub.add_parser("interconnects", help="List interconnect hardware")
    s_ics = p_ics.add_subparsers(dest="what", metavar="ACTION")
    s_ics.required = True
    s_ics.add_parser("list", help="List all interconnect hardware").set_defaults(func=_cmd_interconnects_list)
    p_ics_desc = s_ics.add_parser("describe", help="Show interconnect detail (ports, utilization, firmware)")
    p_ics_desc_name = p_ics_desc.add_argument("name", metavar="NAME", help="Name of the interconnect (e.g. 'Enclosure-01, interconnect 6')")
    p_ics_desc_name.completer = _oneview_interconnect_name_completer
    p_ics_desc.set_defaults(func=_cmd_interconnects_describe)

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
    mac_desc_vlan_arg = p_mac_desc.add_argument("--vlan", "-v", metavar="VLAN", type=int,
        help="Filter traced paths by VLAN ID (e.g. 100)")
    mac_desc_vlan_arg.completer = suppress_file_completion()
    arg_mac_desc_nn = p_mac_desc.add_argument("--network-name", "-n", metavar="NAME", dest="network_name",
        help="Filter traced paths by network name substring (e.g. ACI-Tunnel-Net)")
    arg_mac_desc_nn.completer = _oneview_network_name_completer
    p_mac_desc.set_defaults(func=_cmd_mac_describe)

    # ── enclosures ────────────────────────────────────────────────────────
    p_encs = sub.add_parser("enclosures", help="List physical enclosures")
    s_encs = p_encs.add_subparsers(dest="what", metavar="ACTION")
    s_encs.required = True
    s_encs.add_parser("list", help="List all enclosures").set_defaults(func=_cmd_enclosures_list)
    p_enc_desc = s_encs.add_parser("describe", help="Show enclosure bay layout")
    p_enc_desc_name = p_enc_desc.add_argument("name", metavar="NAME", help="Name of the enclosure")
    p_enc_desc_name.completer = _oneview_enclosure_name_completer
    p_enc_desc.set_defaults(func=_cmd_enclosures_describe)

    p_egs = sub.add_parser("enclosure-groups", help="List enclosure groups")
    s_egs = p_egs.add_subparsers(dest="what", metavar="ACTION")
    s_egs.required = True
    s_egs.add_parser("list", help="List all enclosure groups").set_defaults(func=_cmd_enclosure_groups_list)

    p_les = sub.add_parser("logical-enclosures", help="List logical enclosures")
    s_les = p_les.add_subparsers(dest="what", metavar="ACTION")
    s_les.required = True
    s_les.add_parser("list", help="List all logical enclosures").set_defaults(func=_cmd_logical_enclosures_list)

    p_release = sub.add_parser("release",
        help="HPE Synergy Software Releases matrix (Composer <-> SSP compatibility)",
        description="Show HPE's published Composer (OneView) <-> SSP compatibility matrix "
                    "(https://support.hpe.com/docs/display/public/synergy-sw-release/index.html) "
                    "-- which SSP baseline is recommended vs. additionally supported for each "
                    "OneView version. Marks the row matching the active appliance when reachable.")
    p_release.set_defaults(func=_cmd_release)

    # ── activity (GUI Activity feed: tasks + alerts merged) ────────────────
    p_activity = sub.add_parser("activity",
        help="Recent OneView activity (tasks + alerts) — mirrors the GUI Activity page",
        description="Show OneView's Activity feed: named operations it ran (/rest/tasks -- "
                    "firmware updates, refreshes, inventory collection) merged with health "
                    "and condition alerts (/rest/alerts), newest first. This is where a "
                    "firmware update's real per-phase progress and its actual failure reason "
                    "show up — the same view the OneView GUI's Activity page presents.")
    p_activity.add_argument("--limit", "-n", type=int, default=20, metavar="N",
        help="Maximum number of activity rows to show (default: 20).").completer = suppress_file_completion()  # type: ignore[attr-defined]
    act_res = p_activity.add_argument("--resource", "-r", metavar="NAME",
        help="Only rows whose associated resource name contains this text "
             "(e.g. LE01, Enclosure-01).")
    act_res.completer = suppress_file_completion()  # type: ignore[attr-defined]
    act_state = p_activity.add_argument("--state", "-s", metavar="STATE",
        help="Only rows in this state (e.g. Error, Warning, Running, Completed, Active).")
    act_state.completer = suppress_file_completion()  # type: ignore[attr-defined]
    act_grp = p_activity.add_mutually_exclusive_group()
    act_grp.add_argument("--tasks-only", action="store_true",
        help="Show only tasks (operations), not health/condition alerts.")
    act_grp.add_argument("--alerts-only", action="store_true",
        help="Show only alerts (health/condition notices), not tasks.")
    p_activity.add_argument("--all-tasks", action="store_true",
        help="Include subtasks in the feed. By default only top-level operations "
             "are listed (matching the GUI); use --tree/--watch to see subtasks.")
    act_tree = p_activity.add_argument("--tree", nargs="?", const=True, default=False,
        metavar="NAME",
        help="Show the subtask tree of an operation (like expanding a row in the "
             "GUI Activity page). Optionally give part of the task name OR the "
             "resource (e.g. LE01) to pick one; otherwise the newest top-level "
             "task is used.")
    act_tree.completer = suppress_file_completion()  # type: ignore[attr-defined]
    p_activity.add_argument("--watch", "-w", action="store_true",
        help="Live-follow a running operation's subtask tree, refreshing until it "
             "finishes (Ctrl-C to stop). Pair with --resource/--tree to pick one.")
    act_int = p_activity.add_argument("--interval", type=float, default=4, dest="watch_interval",
        metavar="SECONDS",
        help="Refresh interval for --watch, in seconds (default: 4).")
    act_int.completer = suppress_file_completion()  # type: ignore[attr-defined]
    p_activity.set_defaults(func=_cmd_activity)

    # ── reports ───────────────────────────────────────────────────────────
    p_reports = sub.add_parser("reports", help="Fleet hardware reports")
    s_reports = p_reports.add_subparsers(dest="what", metavar="REPORT")
    s_reports.required = True
    p_rep_mem = s_reports.add_parser("memory", help="Memory DIMM part-number breakdown")
    p_rep_mem.set_defaults(func=_cmd_report_memory)

    # ── update (SSP enclosure rollout + appliance software) ────────────────
    p_update = sub.add_parser("update", help="Roll out SSP firmware baselines or update the appliance itself")
    s_update = p_update.add_subparsers(dest="target", metavar="TARGET")
    s_update.required = True

    p_upd_enc = s_update.add_parser("enclosure",
        help="Update a logical enclosure's SSP firmware baseline",
        description="Roll out an SSP (Synergy Service Pack) firmware baseline to one logical "
                    "enclosure, matching the OneView GUI's 'Update firmware' dialog: pick the "
                    "baseline and whether to update shared infrastructure only or shared "
                    "infrastructure plus every server profile in this enclosure. Default is a "
                    "non-destructive plan; add --execute to apply. Omit NAME to walk through the "
                    "same choices interactively, one numbered question at a time (type 'b' to go "
                    "back a step, 'c' to cancel).")
    upd_enc_name = p_upd_enc.add_argument("name", metavar="NAME", nargs="?", default=None,
        help="Logical enclosure name (e.g. LE01). Omit to launch an interactive step-by-step "
             "wizard instead of passing every flag up front.")
    upd_enc_name.completer = _oneview_logical_enclosure_name_completer  # type: ignore[attr-defined]
    upd_enc_baseline = p_upd_enc.add_argument("--baseline", metavar="NAME|VERSION",
        help="SSP bundle to apply (version / short name / uri id). Defaults to the newest "
             "registered SSP -- pass a specific one to repeat the same rollout for testing.")
    upd_enc_baseline.completer = _oneview_ssp_baseline_completer  # type: ignore[attr-defined]
    p_upd_enc.add_argument("--scope",
        choices=("shared-infra", "shared-infra-and-profiles", "profiles-only"),
        default="shared-infra",
        help="'shared-infra' updates only the frame link modules + interconnects. "
             "'shared-infra-and-profiles' also updates every server profile in this "
             "enclosure's compute modules (matches the GUI's 'Shared infrastructure and "
             "profiles' option). 'profiles-only' (no GUI equivalent -- this CLI only) "
             "updates just the server profiles' compute firmware and skips the logical "
             "enclosure/interconnect step entirely -- useful when shared infra is already "
             "current, or is stuck/unverified and you don't want that blocking compute "
             "progress; a server profile firmware PUT is its own independent OneView "
             "operation, not dependent on the LE rollout finishing. Default: shared-infra.")
    p_upd_enc.add_argument("--install-type", choices=("firmware-only", "firmware-and-drivers", "firmware-offline"),
        help="Compute install type override when --scope includes profiles (default: keep "
             "each profile's existing setting).")
    p_upd_enc.add_argument("--activation-mode", choices=("orchestrated", "parallel"),
        default="orchestrated",
        help="Interconnect activation mode (matches OneView's own -InterconnectActivationMode). "
             "'orchestrated' (default) flashes one side of each redundant pair at a time so the "
             "fabric stays up; if an uplink set isn't redundant OneView raises a non-disruptive "
             "validation warning, which you can proceed through (as in the GUI) to apply it with "
             "a brief interruption on those uplinks. 'parallel' flashes every interconnect at "
             "once regardless of redundancy -- a full network outage during the update, and "
             "OneView requires the affected compute modules to be powered off first.")
    p_upd_enc.add_argument("--force", action="store_true",
        help="Force reinstall even if already at the baseline / bypass non-disruptive validation.")
    p_upd_enc_concurrency = p_upd_enc.add_argument("--concurrency", type=int, default=1, metavar="N",
        help="How many server-profile firmware updates to run at once when --scope includes "
             "profiles (default: 1, fully sequential). No official HPE tool -- GUI, PowerShell, "
             "Python SDK, or Ansible -- updates server-profile firmware in bulk; they all submit "
             "one profile at a time, same as this CLI's default. Raising this overlaps the "
             "wall-clock wait across independent servers (each still installs at its own pace), "
             "at the cost of N compute modules power-cycling simultaneously and no HPE-documented "
             "concurrency ceiling to size it against -- start small for large fleets.")
    p_upd_enc_concurrency.completer = suppress_file_completion()  # type: ignore[attr-defined]
    p_upd_enc.add_argument("--execute", action="store_true",
        help="Actually apply (reboots interconnects, and compute modules if --scope includes "
             "profiles). Default is plan only.")
    p_upd_enc.add_argument("--yes", action="store_true",
        help="Skip the type-to-confirm prompt and any validation-warning prompt (with --execute).")
    p_upd_enc.set_defaults(func=_cmd_update_enclosure)

    p_upd_app = s_update.add_parser("appliance",
        help="Update the OneView/Composer appliance software itself (readiness, stage, install)")
    s_upd_app = p_upd_app.add_subparsers(dest="what", metavar="ACTION")
    s_upd_app.required = True
    p_up_ready = s_upd_app.add_parser("readiness",
        help="Read-only pre-upgrade readiness report (version, health, path)")
    p_up_ready.set_defaults(func=_cmd_upgrade_readiness)
    p_up_clean = s_upd_app.add_parser("cleanup",
        help="List (and optionally delete) unused firmware baselines to free disk")
    p_up_clean.add_argument("--yes", "-y", action="store_true",
        help="Actually delete the unused baselines (default is a dry-run preview)")
    p_up_clean.set_defaults(func=_cmd_upgrade_cleanup)

    p_up_run = s_upd_app.add_parser("run",
        help="Upload + stage an appliance software update (guarded --execute installs it)")
    g_up_src = p_up_run.add_mutually_exclusive_group()
    up_image_arg = g_up_src.add_argument("--image", metavar="PATH",
        help="Path to the appliance update .bin (local or UNC)")
    up_image_arg.completer = suppress_file_completion()
    up_dir_arg = g_up_src.add_argument("--from-dir", metavar="DIR", dest="from_dir",
        help="Directory of update images to pick from (e.g. a network share)")
    up_dir_arg.completer = suppress_file_completion()
    p_up_run.add_argument("--execute", action="store_true",
        help="Actually install after staging (reboots the appliance). Default: stage only")
    p_up_run.add_argument("--yes", "-y", action="store_true",
        help="Skip the typed confirmation for --execute")
    p_up_run.add_argument("--force", action="store_true",
        help="Proceed even if the readiness verdict is FAIL")
    p_up_run.add_argument("--clear-pending", action="store_true", dest="clear_pending",
        help="Replace a different already-staged update instead of aborting")
    p_up_run.set_defaults(func=_cmd_upgrade_run)

    p_up_pending = s_upd_app.add_parser("pending",
        help="Show the currently staged appliance update (read-only)")
    p_up_pending.set_defaults(func=_cmd_upgrade_pending)

    p_up_cancel = s_upd_app.add_parser("cancel",
        help="Remove a stuck/aborted staged appliance update")
    p_up_cancel.add_argument("--yes", "-y", action="store_true",
        help="Actually remove the staged update (default is a dry-run preview)")
    p_up_cancel.set_defaults(func=_cmd_upgrade_cancel)

    # ── appliances (multi-appliance switching) ────────────────────────────
    # inventory.ini can hold more than one OneView appliance (each its own
    # section with 'type = oneview') -- these commands mirror
    # 'proliant com workspaces list/use' so you can see and switch which one
    # every other 'proliant oneview' command targets.
    p_appliances = sub.add_parser("appliances", help="List or switch OneView appliances")
    s_appliances = p_appliances.add_subparsers(dest="what", metavar="ACTION")
    s_appliances.required = True
    p_app_list = s_appliances.add_parser("list", help="List configured appliances (* = active)")
    p_app_list.set_defaults(func=_cmd_appliances_list)
    p_app_use = s_appliances.add_parser("use", help="Switch active appliance")
    p_app_use.add_argument(
        "name", metavar="NAME", help="Appliance section name (see 'appliances list')"
    ).completer = _oneview_appliance_name_completer  # type: ignore[attr-defined]
    p_app_use.set_defaults(func=_cmd_appliances_use)
    p_app_describe = s_appliances.add_parser(
        "describe", help="Show an appliance's General page (HA nodes, memory, uptime, firmware)"
    )
    p_app_describe.add_argument(
        "name", metavar="NAME", nargs="?",
        help="Appliance section name (see 'appliances list'); defaults to the active appliance",
    ).completer = _oneview_appliance_name_completer  # type: ignore[attr-defined]
    p_app_describe.set_defaults(func=_cmd_appliances_describe)

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
