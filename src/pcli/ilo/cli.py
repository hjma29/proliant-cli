"""
hpeilo.cli
~~~~~~~~~~
Command-line interface: subcommand-based argument parsing, async host queries,
and table printing.

Usage::

    pcli ilo list firmwares                          All servers, all firmware columns
    pcli ilo list firmwares --host dl325-gen12       Single server
    pcli ilo list firmwares --fields bios,ilo        BIOS and iLO columns only
    pcli ilo list firmwares --fields model,bios,ilo  Model + BIOS + iLO
    pcli ilo list firmwares --fields nic-fw,storage-fw
    pcli ilo list ilo                                iLO firmware version
    pcli ilo list network                            NIC firmware versions
    pcli ilo list storage                            Storage firmware versions
    pcli ilo list serial                             Server model + serial (for COM onboarding)
    pcli ilo list full                               Full firmware inventory
    pcli ilo list update-method                      All firmware with BMC/UEFI/OS update method
    pcli ilo list update-method --host dl345-gen12   Single server update method view
    pcli ilo upgrade --host <name>                  Auto-upgrade outdated firmware

Available --fields for 'get firmwares' (case-insensitive):
    Model, iLO, BIOS, NIC-FW, Storage-FW

Update methods shown by 'get update-method':
    BMC  = iLO flashes directly, no reboot needed
    UEFI = UEFI processes on next reboot, no OS needed
    OS   = Requires running OS + iSUT/SUM RuntimeAgent
"""

from __future__ import annotations

# PYTHON_ARGCOMPLETE_OK
import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

import argcomplete

from pcli.common.completers import comma_sep_completer
from pcli.common.display import get_console, make_table, print_json, OutputMode, get_output_mode
from pcli.common.runner import run_parallel
from pcli.common.targets import resolve_hosts, add_target_args
from pcli.ilo import firmware, inventory
from pcli.ilo.client import ILOClient, ServerDownOrUnreachableError, ilo_session
from pcli.ilo.config import (
    COL_ILO_WIDTH,
    COL_NAME_WIDTH,
    COL_NIC_WIDTH,
    COL_SERVER_WIDTH,
    MAX_WORKERS,
    load_hosts,
)
from pcli.ilo.printers import (
    _print_json_results,
    _header_line,
    print_ilo_table,
    _print_component_table,
    _print_raw_table,
    print_disk_map_table,
    print_fleet_table,
    print_serial_table,
    print_update_method_table,
    print_full_table,
    _print_fw_components,
    _print_fw_queue,
)

FetchFn = Callable[[ILOClient], Awaitable[list[Any]]]


def _ilo_fields_completer(choices: tuple):
    """Argcomplete completer for comma-separated ilo field lists."""
    return comma_sep_completer(choices)


_FETCH_DISPATCH: dict[str, FetchFn] = {
    "ilo": inventory.fetch_ilo_version,
    "network": inventory.fetch_network_versions,
    "nic": inventory.fetch_nic_status,
    "storage": inventory.fetch_storage_versions,
    "cpu": inventory.fetch_cpu_info,
    "memory": inventory.fetch_memory_info,
    "com": inventory.fetch_com_status,
    "full": inventory.fetch_all_firmware,
    "disk_map": inventory.fetch_disk_map,
    "firmwares": inventory.fetch_fleet_summary,
    "serial": inventory.fetch_serial_info,
    "update_method": inventory.fetch_firmware_update_method,
}

_RAW_DISPATCH: dict[str, FetchFn] = {
    "ilo": inventory.fetch_firmware_raw,
    "network": inventory.fetch_network_raw,
    "nic": inventory.fetch_nic_raw,
    "storage": inventory.fetch_storage_raw,
    "cpu": inventory.fetch_cpu_raw,
    "memory": inventory.fetch_memory_raw,
    "com": inventory.fetch_com_raw,
    "full": inventory.fetch_firmware_raw,
    "disk_map": inventory.fetch_disk_map_raw,
    "firmwares": inventory.fetch_firmware_raw,
    "serial": inventory.fetch_serial_info,
    "update_method": inventory.fetch_firmware_raw,
}


async def query_host_async(host: dict, fetch_fn: FetchFn) -> tuple[str, str | None, list[Any]]:
    try:
        async with ilo_session(host) as client:
            results = await fetch_fn(client)
        return host["name"], None, results
    except ServerDownOrUnreachableError as exc:
        return host["name"], f"Unreachable: {exc}", []
    except Exception as exc:  # noqa: BLE001
        return host["name"], f"Error: {exc}", []


async def _run_parallel_async(hosts: list[dict], fetch_fn: FetchFn) -> list[tuple[str, str | None, list[Any]]]:
    return await run_parallel(hosts, fetch_fn, session_factory=ilo_session, max_workers=MAX_WORKERS)


async def _run_report_memory(args: argparse.Namespace) -> None:
    from pcli.com.inventory import aggregate_by_part_number

    c = get_console()
    hosts = _load_hosts_or_exit(getattr(args, "host", None), getattr(args, "hosts_from", None))

    with c.status("[dim]Fetching memory inventory across fleet…[/dim]"):
        results = await _run_parallel_async(hosts, inventory.fetch_memory_report_data)

    all_dimms: list[dict] = []
    for server_name, error, dimms in results:
        if error:
            c.print(f"[yellow]  {server_name}: {error}[/yellow]")
            continue
        for d in dimms:
            d["server"] = server_name
            all_dimms.append(d)

    if not all_dimms:
        c.print("[yellow]No memory inventory data returned.[/yellow]")
        return

    rows = aggregate_by_part_number(all_dimms)
    print_memory_report(rows, source="iLO")


def _build_parser() -> argparse.ArgumentParser:
    try:
        _version = _pkg_version("pcli")
    except PackageNotFoundError:
        _version = "dev"

    parser = argparse.ArgumentParser(
        prog="pcli ilo",
        description="HPE iLO firmware/hardware inventory and update tool",
    )
    parser.add_argument("--version", "-V", action="version", version=f"%(prog)s {_version}")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Output structured JSON (for piping to jq / ConvertFrom-Json)")

    def _host_completer(**_kwargs):
        try:
            return [host["name"] for host in load_hosts()]
        except Exception:
            return []

    def _add_host(p: argparse.ArgumentParser, required: bool = False) -> None:
        p.add_argument(
            "--host", metavar="NAME[,NAME,...]", required=required,
            help="Target host(s) by name — comma-separated for multiple",
        ).completer = _host_completer
        p.add_argument(
            "--hosts-from", metavar="FILE", dest="hosts_from",
            help="Read target hosts from FILE (one per line), or '-' for stdin",
        )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    get_p = subparsers.add_parser(
        "list",
        help="List hardware/firmware inventory",
        description="Query iLO and display hardware or firmware information.",
    )
    get_sub = get_p.add_subparsers(dest="what", metavar="WHAT")
    get_sub.required = True

    get_choices = {
        "firmwares": "Firmware summary: key firmware versions per server (one row per server)",
        "ilo": "iLO firmware version",
        "network": "NIC firmware versions",
        "nic": "NIC link status + MAC address",
        "storage": "Storage controller + drive firmware",
        "cpu": "CPU model + microcode version",
        "memory": "DIMM info + firmware revision",
        "com": "HPE Compute Ops Management registration status",
        "full": "Full firmware inventory",
        "disk-map": "Drive bay + serial number map (cross-ref with lsblk)",
        "serial": "Server model, serial number, and product ID (for COM onboarding)",
        "update-method": "Full firmware inventory with update method (BMC / UEFI / OS) per component",
    }
    for name, help_text in get_choices.items():
        if name == "firmwares":
            from pcli.ilo.inventory import FLEET_KEYS
            sp = get_sub.add_parser(
                name,
                help=help_text,
                description=(
                    "Show firmware summary for all servers (one row per server).\n\n"
                    "Examples:\n"
                    "  pcli ilo list firmwares                         All servers, all columns\n"
                    "  pcli ilo list firmwares --host dl325-gen12      Single server\n"
                    "  pcli ilo list firmwares --fields bios,ilo       BIOS and iLO only\n"
                    "  pcli ilo list firmwares --fields model,bios     Model and BIOS only\n"
                    "  pcli ilo list firmwares --fields nic-fw,storage-fw  NIC and Storage only\n"
                    f"\nAvailable --fields (case-insensitive): {', '.join(FLEET_KEYS)}"
                ),
                formatter_class=argparse.RawDescriptionHelpFormatter,
            )
            _add_host(sp)
            sp.add_argument("--raw", action="store_true", help="Print raw JSON instead of a formatted table")
            fleet_fields_arg = sp.add_argument(
                "--fields", metavar="FIELDS",
                help=(
                    f"Comma-separated columns (case-insensitive). "
                    f"Available: {', '.join(FLEET_KEYS)}. Default: all"
                ),
            )
            fleet_keys_lower = tuple(k.lower() for k in FLEET_KEYS)
            fleet_fields_arg.completer = _ilo_fields_completer(fleet_keys_lower)  # type: ignore[attr-defined]
        elif name == "update-method":
            sp = get_sub.add_parser(
                name,
                help=help_text,
                description=(
                    "Show all firmware components with update method classification.\n\n"
                    "Update methods:\n"
                    "  BMC  — iLO flashes the component directly (no server reboot needed)\n"
                    "  UEFI — UEFI processes it on next server reboot (no OS required)\n"
                    "  OS   — Requires a running OS + iSUT/SUM RuntimeAgent\n\n"
                    "Examples:\n"
                    "  pcli ilo list update-method                        All servers\n"
                    "  pcli ilo list update-method --host dl345-gen12     Single server\n"
                    "  pcli ilo list update-method --host dl380-gen11     Show Gen11 server\n"
                ),
                formatter_class=argparse.RawDescriptionHelpFormatter,
            )
            _add_host(sp)
            sp.add_argument("--raw", action="store_true", help="Print raw JSON instead of a formatted table")
        else:
            sp = get_sub.add_parser(name, help=help_text)
            _add_host(sp)
            sp.add_argument("--raw", action="store_true", help="Print raw JSON instead of a formatted table")

    upgrade_p = subparsers.add_parser(
        "upgrade",
        help="Firmware upgrade and task queue management",
        description=(
            "Auto-upgrade outdated firmware from HPE SDR, with optional component filtering.\n"
            "Subcommands give access to individual staging and queue operations.\n\n"
            "Examples:\n"
            "  pcli ilo upgrade --host dl325-gen12                       Upgrade all components\n"
            "  pcli ilo upgrade --host dl325-gen12 --dry-run             Preview without changes\n"
            "  pcli ilo upgrade --host dl325-gen12 --reboot              Upgrade and reboot\n"
            "  pcli ilo upgrade --host dl325-gen12 --component bios      BIOS / System ROM only\n"
            "  pcli ilo upgrade --host dl325-gen12 --component ilo       iLO firmware only\n"
            "  pcli ilo upgrade --host dl325-gen12 --component nic       NIC firmware only\n"
            "  pcli ilo upgrade --host dl325-gen12 --component storage   Storage controllers only\n"
            "  pcli ilo upgrade --host dl325-gen12 --component bios --dry-run   Preview BIOS upgrade\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    upgrade_sub = upgrade_p.add_subparsers(dest="upgrade_action", metavar="ACTION")
    upgrade_sub.required = False
    upgrade_p.set_defaults(upgrade_action="auto")

    _add_host(upgrade_p, required=False)
    upgrade_p.add_argument("--dry-run", action="store_true", dest="dry_run", help="Preview without making changes")
    upgrade_p.add_argument("--reboot", action="store_true", help="Reboot server after queuing all updates")
    upgrade_p.add_argument(
        "--component",
        metavar="FILTER",
        choices=["all", "ilo", "bios", "nic", "storage"],
        default="all",
        help="Limit upgrade to a specific component type: all | ilo | bios | nic | storage (default: all)",
    )

    up_comp = upgrade_sub.add_parser("components", help="List staged components in iLO repository")
    _add_host(up_comp, required=True)
    up_queue = upgrade_sub.add_parser("queue", help="Show the firmware update task queue")
    _add_host(up_queue, required=True)
    up_stage = upgrade_sub.add_parser("stage", help="Stage a firmware package from a URL")
    _add_host(up_stage, required=True)
    up_stage.add_argument("--url", metavar="URL", required=True, help="Direct URL to .fwpkg file on HPE SDR")
    up_stage.add_argument("--dry-run", action="store_true", dest="dry_run")
    up_flash = upgrade_sub.add_parser("flash", help="Queue a staged file for flash on next reboot")
    _add_host(up_flash, required=True)
    up_flash.add_argument("filename", metavar="FILENAME", help="Filename of the staged component to queue")
    up_flash.add_argument("--dry-run", action="store_true", dest="dry_run")
    up_clear = upgrade_sub.add_parser("clear", help="Clear all entries from the task queue")
    _add_host(up_clear, required=True)
    up_clear.add_argument("--dry-run", action="store_true", dest="dry_run")

    subparsers.add_parser(
        "init",
        help="Create a starter hosts-ilo.ini at ~/.config/pcli/ilo/hosts-ilo.ini",
        description="Create ~/.config/pcli/ilo/hosts-ilo.ini with example entries to fill in.",
    )

    report_p = subparsers.add_parser("report", help="Fleet hardware reports")
    report_sub = report_p.add_subparsers(dest="what", metavar="WHAT")
    report_sub.required = True
    rep_mem = report_sub.add_parser("memory", aliases=["mem"], help="Memory DIMM part-number breakdown")
    _add_host(rep_mem)
    rep_cpu = report_sub.add_parser("cpu", help="CPU model and core count across fleet")
    _add_host(rep_cpu)
    rep_gpu = report_sub.add_parser("gpu", help="GPU inventory across fleet")
    _add_host(rep_gpu)

    set_p = subparsers.add_parser("set", help="Change iLO configuration")
    set_sub = set_p.add_subparsers(dest="set_action", metavar="ACTION")
    set_sub.required = True
    set_dhcp = set_sub.add_parser(
        "dhcp",
        help="Switch iLO management NIC from static IP to DHCP",
        description="Patch the iLO EthernetInterface to enable DHCPv4. "
                    "The iLO will reboot its network stack and obtain a new IP from DHCP.",
    )
    _add_host(set_dhcp)
    set_dhcp.add_argument("--confirm", action="store_true", help="Skip confirmation prompt")
    set_dhcp.add_argument(
        "--reset",
        action="store_true",
        help="Reset iLO after enabling DHCP so the change takes effect immediately (current IP will be lost)",
    )

    desc_p = subparsers.add_parser(
        "describe",
        help="Show full details for a single server (identity, iLO, CPU, GPU, memory, firmware)",
    )
    desc_p.add_argument("name", metavar="NAME",
                        help="Server name from hosts-ilo.ini").completer = _host_completer

    return parser


def main(argv: list[str] | None = None) -> None:
    from pcli.common.display import set_output_mode, OutputMode
    parser = _build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    if getattr(args, "json_output", False):
        set_output_mode(OutputMode.JSON)
    asyncio.run(_async_main(args))


async def _async_main(args: argparse.Namespace) -> None:
    if args.command == "list":
        await _run_get(args)
    elif args.command == "upgrade":
        await _run_upgrade(args)
    elif args.command == "init":
        _run_init()
    elif args.command == "report":
        if args.what in ("memory", "mem"):
            await _run_report_memory(args)
        elif args.what == "cpu":
            await _run_report_cpu(args)
        elif args.what == "gpu":
            await _run_report_gpu(args)
    elif args.command == "describe":
        await _cmd_describe(args)
    elif args.command == "set":
        if args.set_action == "dhcp":
            await _run_set_dhcp(args)


def _load_hosts_or_exit(name: str | None, hosts_from: str | None = None) -> list[dict]:
    """Resolve target hosts — supports single, comma-separated, and --hosts-from."""
    try:
        if hosts_from or (name and "," in name):
            return resolve_hosts(name, hosts_from, load_hosts)
        return load_hosts(name=name)
    except FileNotFoundError:
        from pathlib import Path
        from rich.console import Console
        from rich.prompt import Confirm

        console = Console()
        dest = Path.cwd() / "hosts-ilo.ini"
        console.print(f"\n[green]No hosts-ilo.ini found.[/green] A config file is needed to connect to your iLO servers.")
        console.print(f"  It would be created at: [bold]{dest}[/bold]\n")
        if Confirm.ask("[green]Create hosts-ilo.ini now?[/green]", default=True):
            _write_hosts_ini(dest)
            console.print(f"\n[green]✓[/green] Created: [bold]{dest}[/bold]")
            console.print("  Fill in your server addresses and credentials, then re-run your command.\n")
            if Confirm.ask("[green]Open it in your default editor now?[/green]", default=True):
                _open_in_editor(dest)
        else:
            console.print("\n  Run [bold]pcli ilo init[/bold] any time to create the file.\n")
        sys.exit(0)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


_HOSTS_INI_TEMPLATE = (
    "# pcli iLO inventory\n"
    "# Place this file in the same directory as pcli (or pcli.exe on Windows).\n"
    "#\n"
    "# [defaults]  — shared credentials for all servers (can be overridden per server)\n"
    "# [section]   — one section per iLO server; section name = display name\n"
    "#               'host' is the only required field (IP or hostname, no https://)\n"
    "\n"
    "[defaults]\n"
    "username = Administrator\n"
    "password = yourpassword\n"
    "\n"
    "[my-server-1]\n"
    "host = 10.0.0.1\n"
    "\n"
    "[my-server-2]\n"
    "host = 10.0.0.2\n"
    "\n"
    "# Example: server with different credentials\n"
    "# [lab-server]\n"
    "# host = myilo.example.com\n"
    "# username = localadmin\n"
    "# password = differentpass\n"
    "\n"
    "# To store other appliances (e.g. OneView) without affecting iLO commands,\n"
    "# add 'type = oneview' — pcli ilo will skip non-ilo entries automatically.\n"
    "# [my-oneview]\n"
    "# host = 10.0.0.100\n"
    "# username = Administrator\n"
    "# password = yourpassword\n"
    "# type = oneview\n"
)


def _write_hosts_ini(dest: "Path") -> None:
    dest.write_text(_HOSTS_INI_TEMPLATE)


def _open_in_editor(path: "Path") -> None:
    import subprocess
    import os
    import platform
    try:
        if platform.system() == "Windows":
            os.startfile(str(path))
        elif platform.system() == "Darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "xdg-open"
            subprocess.run([editor, str(path)], check=False)
    except Exception:
        pass


def _run_init() -> None:
    from pathlib import Path
    from rich.console import Console
    from rich.prompt import Confirm

    console = Console()
    dest = Path.cwd() / "hosts-ilo.ini"
    if dest.exists():
        console.print(f"[green]Already exists:[/green] [bold]{dest}[/bold]")
        console.print("  Edit it to add or update your servers.")
        if Confirm.ask("\n[green]Open it in your default editor?[/green]", default=True):
            _open_in_editor(dest)
        return
    _write_hosts_ini(dest)
    console.print(f"\n[green]✓[/green] Created: [bold]{dest}[/bold]")
    console.print("  Fill in your server addresses and credentials.\n")
    console.print("  Then try: [bold]pcli ilo list firmwares[/bold]\n")
    if Confirm.ask("[green]Open it in your default editor now?[/green]", default=True):
        _open_in_editor(dest)


async def _run_set_dhcp(args: argparse.Namespace) -> None:
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    if len(hosts) > 1 and not getattr(args, "confirm", False):
        print(f"This will switch {len(hosts)} iLO(s) from static IP to DHCP.")
        print("Re-run with --confirm to proceed, or use --host <name> to target one server.")
        sys.exit(1)

    do_reset = getattr(args, "reset", False)

    for host in hosts:
        name = host["name"]
        try:
            async with ilo_session(host) as client:
                # Check current state first
                data = await client.get("/redfish/v1/Managers/1/EthernetInterfaces/1")
                ni = (data.get("IPv4Addresses") or [{}])[0]
                origin = ni.get("AddressOrigin", "Unknown")
                current_ip = ni.get("Address", "?")

                if origin == "DHCP" and current_ip != "0.0.0.0":
                    print(f"[{name}] Already using DHCP (current IP: {current_ip}) — skipping.")
                    continue

                # DHCP staged but iLO not yet reset (IP is 0.0.0.0) — or static and needs switching
                already_staged = origin == "DHCP" and current_ip == "0.0.0.0"
                if already_staged:
                    print(f"[{name}] DHCP is staged but iLO hasn't been reset yet (IP: 0.0.0.0).")
                    if not do_reset:
                        print(f"[{name}] Run with --reset to apply.")
                        continue
                    # Skip re-PATCH, go straight to reset
                else:
                    reset_note = " iLO will reset and the current IP will be lost." if do_reset else \
                                 " Run with --reset to apply immediately (requires iLO restart)."
                    if not getattr(args, "confirm", False):
                        ans = input(
                            f"[{name}] Current IP: {current_ip} (Static). "
                            f"Switch to DHCP?{reset_note} [y/N] "
                        )
                        if ans.strip().lower() != "y":
                            print(f"[{name}] Skipped.")
                            continue

                    # Dual-layer PATCH: standard Redfish + Oem.Hpe mirror (both required by iLO)
                    payload = {
                        "DHCPv4": {
                            "DHCPEnabled": True,
                            "UseDNSServers": True,
                            "UseDomainName": True,
                            "UseGateway": True,
                            "UseNTPServers": True,
                            "UseStaticRoutes": True,
                        },
                        "Oem": {
                            "Hpe": {
                                "DHCPv4": {"Enabled": True}
                            }
                        },
                    }
                    result = await client.patch("/redfish/v1/Managers/1/EthernetInterfaces/1", payload)
                    # iLO wraps success in an "error" envelope with MessageId containing "Success"
                    ext = result.get("error", {})
                    msgs = ext.get("@Message.ExtendedInfo", [])
                    is_success = not ext or any("Success" in m.get("MessageId", "") for m in msgs)
                    if not is_success:
                        msg = ext.get("message", str(result))
                        details = "; ".join(m.get("MessageId", "") for m in msgs)
                        print(f"[{name}] ERROR from iLO: {msg} ({details})", file=sys.stderr)
                        continue

                if do_reset:
                    print(f"[{name}] ✓ DHCP staged. Resetting iLO — current IP {current_ip} will be lost...")
                    await client.post(
                        "/redfish/v1/Managers/1/Actions/Manager.Reset",
                        {"ResetType": "GracefulRestart"},
                    )
                    print(f"[{name}] iLO reset triggered. It will come up with a DHCP-assigned IP.")
                else:
                    print(f"[{name}] ✓ DHCP staged. Run with --reset to apply (requires iLO restart).")
        except Exception as exc:
            print(f"[{name}] ERROR: {exc}", file=sys.stderr)


async def _run_report_cpu(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich import box as rich_box

    console = Console()
    hosts = _load_hosts_or_exit(getattr(args, "host", None))

    with console.status("[dim]Fetching CPU inventory across fleet…[/dim]"):
        results = await _run_parallel_async(hosts, inventory.fetch_cpu_report_data)

    table = Table(
        title="CPU Inventory",
        box=rich_box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("Server",  min_width=16, no_wrap=True)
    table.add_column("Socket",  no_wrap=True)
    table.add_column("Model",   min_width=30)
    table.add_column("Cores",   justify="right", no_wrap=True)
    table.add_column("Threads", justify="right", no_wrap=True)
    table.add_column("Max GHz", justify="right", no_wrap=True)

    for server_name, error, cpus in results:
        if error:
            table.add_row(server_name, "—", f"[yellow]{error}[/yellow]", "—", "—", "—")
            continue
        for i, cpu in enumerate(cpus):
            mhz = cpu["speed_mhz"]
            ghz = f"{mhz / 1000:.2f}" if isinstance(mhz, (int, float)) else "—"
            table.add_row(
                server_name if i == 0 else "",
                str(cpu["socket"]),
                cpu["model"],
                str(cpu["cores"]),
                str(cpu["threads"]),
                ghz,
            )

    console.print(table)


async def _run_report_gpu(args: argparse.Namespace) -> None:
    from collections import defaultdict
    from rich.console import Console
    from rich.table import Table
    from rich import box as rich_box

    console = Console()
    hosts = _load_hosts_or_exit(getattr(args, "host", None))

    with console.status("[dim]Fetching GPU inventory across fleet…[/dim]"):
        results = await _run_parallel_async(hosts, inventory.fetch_gpu_report_data)

    # Aggregate: gpu_name → {count, servers}
    groups: dict[str, dict] = {}
    for server_name, error, gpus in results:
        if error or not gpus:
            continue
        for gpu in gpus:
            key = gpu["name"]
            if key not in groups:
                groups[key] = {"count": 0, "servers": set()}
            groups[key]["count"] += 1
            groups[key]["servers"].add(server_name)

    if not groups:
        console.print("[yellow]No GPUs found across fleet.[/yellow]")
        return

    rows = sorted(groups.items(), key=lambda x: x[1]["count"], reverse=True)
    total = sum(v["count"] for _, v in rows)
    server_count = len({s for _, v in rows for s in v["servers"]})

    table = Table(
        title=f"GPU Inventory  ({total} GPUs across {server_count} servers)",
        box=rich_box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("GPU Model", min_width=28)
    table.add_column("Count",     justify="right", no_wrap=True, style="bold")
    table.add_column("Servers",   min_width=20)

    for gpu_name, v in rows:
        table.add_row(
            gpu_name,
            str(v["count"]),
            ", ".join(sorted(v["servers"])),
        )

    console.print(table)


async def _run_get(args: argparse.Namespace) -> None:
    what = args.what.replace("-", "_")
    hosts = _load_hosts_or_exit(getattr(args, "host", None), getattr(args, "hosts_from", None))
    raw = getattr(args, "raw", False)
    fetch_fn = _RAW_DISPATCH[what] if raw else _FETCH_DISPATCH[what]
    results = await _run_parallel_async(hosts, fetch_fn)

    # --json output mode: emit structured data and return
    if get_output_mode() == OutputMode.JSON:
        _print_json_results(what, results)
        return

    if raw:
        _print_raw_table(results)
        return

    printers = {
        "firmwares": lambda r: print_fleet_table(r, fields=getattr(args, "fields", None)),
        "ilo": print_ilo_table,
        "network": lambda r: _print_component_table(r, "NIC Firmware"),
        "nic": lambda r: _print_component_table(r, "NIC Link Status + MAC"),
        "storage": lambda r: _print_component_table(r, "Storage Firmware"),
        "cpu": lambda r: _print_component_table(r, "CPU Info"),
        "memory": lambda r: _print_component_table(r, "Memory Info"),
        "com": lambda r: _print_component_table(r, "HPE Compute Ops Management"),
        "full": print_full_table,
        "disk_map": print_disk_map_table,
        "serial": print_serial_table,
        "update_method": print_update_method_table,
    }
    printers[what](results)


async def _run_upgrade(args: argparse.Namespace) -> None:
    action = args.upgrade_action
    dry_run = getattr(args, "dry_run", False)

    if action == "auto":
        if not args.host:
            print("ERROR: 'pcli ilo upgrade' requires --host <name>", file=sys.stderr)
            sys.exit(1)
        host = _load_hosts_or_exit(args.host)[0]
        await _run_fw_upgrade(
            host,
            dry_run=dry_run,
            reboot=getattr(args, "reboot", False),
            component=getattr(args, "component", "all"),
        )
        return

    host = _load_hosts_or_exit(args.host)[0]
    try:
        async with ilo_session(host) as client:
            if action == "components":
                _print_fw_components(host["name"], await firmware.get_component_repository(client))
            elif action == "queue":
                _print_fw_queue(host["name"], await firmware.get_task_queue(client))
            elif action == "stage":
                result = await firmware.stage_from_uri(client, args.url, dry_run=dry_run)
                if dry_run:
                    print(f"[dry-run] Would POST to: {result['target']}")
                    print(f"[dry-run] Payload: {json.dumps(result['payload'], indent=2)}")
                else:
                    print(f"Staging initiated on {host['name']}:")
                    print(json.dumps(result, indent=2))
            elif action == "flash":
                result = await firmware.add_to_task_queue(client, args.filename, dry_run=dry_run)
                if dry_run:
                    print(f"[dry-run] Would POST to: {result['target']}")
                    print(f"[dry-run] Payload: {json.dumps(result['payload'], indent=2)}")
                else:
                    print(f"Queued '{args.filename}' for flash on next reboot ({host['name']}):")
                    print(json.dumps(result, indent=2))
            elif action == "clear":
                uris = await firmware.clear_task_queue(client, dry_run=dry_run)
                if dry_run:
                    if uris:
                        print(f"[dry-run] Would delete {len(uris)} task queue entries:")
                        for uri in uris:
                            print(f"  {uri}")
                    else:
                        print("[dry-run] Task queue is already empty.")
                else:
                    print(f"Cleared {len(uris)} task queue entries from {host['name']}.")
    except ServerDownOrUnreachableError as exc:
        print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


async def _run_fw_upgrade(host: dict, *, dry_run: bool = False, reboot: bool = False, component: str = "all") -> None:
    from rich import box
    from rich.console import Console
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table

    from pcli.ilo import sdr
    from pcli.ilo.power import reset_server

    console = Console()
    with console.status(f"[bold cyan]Connecting to {host['name']}..."):
        try:
            async with ilo_session(host) as client:
                all_fw_full = await inventory.fetch_firmware_inventory_full(client)
                nic_fw = await inventory.fetch_nic_firmware_inventory(client)
                model_info = (await client.get(await client.get_system_uri())).get("Model", "unknown")
        except ServerDownOrUnreachableError as exc:
            console.print(f"[red]ERROR:[/red] {host['name']} unreachable: {exc}")
            sys.exit(1)

    try:
        gen = sdr.detect_gen(model_info)
    except ValueError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    with console.status(f"[bold cyan]Fetching HPE SDR for Gen{gen}..."):
        try:
            pack_date, pack_url = sdr.latest_pack_url(gen)
            pack_components = sdr.list_pack(pack_url)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]ERROR:[/red] Cannot reach HPE SDR: {exc}")
            sys.exit(1)

    candidates = sdr.find_upgrades(all_fw_full + nic_fw, pack_components)
    skip_non_updatable = ("tpm", "video controller", "nvme drive", "ssd", "dimm", "memory", "processor", "microcode", "embedded video")
    non_updatable = [
        entry for entry in all_fw_full
        if not entry.get("Updateable", True)
        and entry.get("Version", "N/A") not in ("N/A", None, "")
        and not any(key in entry.get("Name", "").lower() for key in skip_non_updatable)
    ]

    table = Table(title=f"Firmware Audit — {host['name']} ({model_info})", box=box.ROUNDED, show_lines=False)
    table.add_column("Component", style="bold")
    table.add_column("Installed", style="yellow")
    table.add_column("SDR Latest", style="cyan")
    table.add_column("Status")

    updates = [candidate for candidate in candidates if candidate.needs_update]
    up_to_date = [candidate for candidate in candidates if not candidate.needs_update]

    def _component_match(candidate: Any, comp: str) -> bool:
        if comp == "all":
            return True
        name_lower = candidate.name.lower()
        if comp == "ilo":
            return name_lower.startswith("ilo")
        if comp == "bios":
            return "system rom" in name_lower or "bios" in name_lower
        if comp == "nic":
            return bool(getattr(candidate.sdr, "chip_model", None)) or (candidate.sdr is None and candidate.updateable)
        if comp == "storage":
            return any(key in name_lower for key in ("controller", "array", "boot controller", "ns204", "nvme"))
        return True

    if component != "all":
        original_count = len(updates)
        updates = [candidate for candidate in updates if _component_match(candidate, component)]
        if len(updates) < original_count:
            console.print(f"  [dim]Filtered to component=[bold]{component}[/bold]: {len(updates)} of {original_count} updates selected[/dim]")

    for candidate in updates:
        table.add_row(candidate.name, candidate.current, candidate.sdr.filename, "[green bold]UPDATE AVAILABLE[/green bold]")
    for candidate in up_to_date:
        sdr_col = candidate.sdr.filename if candidate.sdr else "—"
        status = "[dim]up to date[/dim]" if candidate.sdr else "[dim italic]no SDR package[/dim italic]"
        table.add_row(candidate.name, candidate.current, sdr_col, status)
    for entry in non_updatable:
        table.add_row(entry.get("Name", "?"), entry.get("Version", "?"), "—", "[dim italic]not updatable via iLO[/dim italic]")

    console.print()
    console.print(table)
    console.print(f"  SDR pack: [dim]{pack_date}[/dim]  |  [green]{len(updates)} update(s) available[/green], {len(up_to_date)} up to date\n")

    if not updates:
        console.print("[green]✓ All firmware is up to date.[/green]")
        return

    if dry_run:
        console.print("[yellow][dry-run] Would stage and queue:[/yellow]")
        for candidate in updates:
            console.print(f"  • {candidate.sdr.filename}  ({candidate.current} → SDR {candidate.sdr.version_str})")
        return

    def _upgrade_priority(candidate: Any) -> int:
        name_lower = candidate.name.lower()
        if name_lower.startswith("ilo"):
            return 0
        if "system rom" in name_lower or "bios" in name_lower:
            return 1
        return 2

    updates.sort(key=_upgrade_priority)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        for idx, candidate in enumerate(updates, 1):
            filename = candidate.sdr.filename
            label = f"[{idx}/{len(updates)}] {filename}"
            is_ilo = candidate.name.lower().startswith("ilo")
            task = progress.add_task(f"{label}  staging…", total=None)
            try:
                async with ilo_session(host) as client:
                    await firmware.stage_from_uri(client, candidate.sdr.url)
                progress.update(task, description=f"{label}  waiting for iLO to download…")
                async with ilo_session(host) as client:
                    await firmware.wait_for_stage(client, filename, timeout=300, poll_interval=10)
                progress.update(task, description=f"{label}  queueing for flash…")
                async with ilo_session(host) as client:
                    await firmware.add_to_task_queue(client, filename)
                progress.update(task, description=f"[green]✓[/green] {label}  queued  ({candidate.current} → {candidate.sdr.version_str})")
                if is_ilo:
                    progress.update(task, description=f"{label}  iLO restarting (~90s)…")
                    try:
                        await firmware.wait_for_online(host, offline_grace=15, timeout=180)
                        progress.update(task, description=f"[green]✓[/green] {label}  iLO back online  ({candidate.current} → {candidate.sdr.version_str})")
                    except TimeoutError:
                        progress.update(task, description=f"[yellow]⚠[/yellow] {label}  iLO restart timed out — continuing")
            except Exception as exc:  # noqa: BLE001
                progress.update(task, description=f"[red]✗[/red] {label}  FAILED: {exc}")
                console.print(f"[red]ERROR staging {filename}: {exc}[/red]")

    if reboot:
        console.print(f"\n[bold yellow]Rebooting {host['name']}...[/bold yellow]")
        try:
            async with ilo_session(host) as client:
                await reset_server(client, reset_type="GracefulRestart")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]ERROR: reboot failed: {exc}[/red]")
            sys.exit(1)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Waiting for server to come back online…", total=None)
            try:
                await firmware.wait_for_online(host, offline_grace=30, timeout=600)
                progress.update(task, description="[green]✓ Server back online[/green]")
            except TimeoutError as exc:
                progress.update(task, description=f"[red]✗ {exc}[/red]")
                sys.exit(1)

        console.print("\n[bold]Verifying firmware versions after reboot...[/bold]")
        async with ilo_session(host) as client:
            new_fw = dict(await inventory.fetch_all_firmware(client))

        result_table = Table(box=box.SIMPLE)
        result_table.add_column("Component")
        result_table.add_column("Before")
        result_table.add_column("After")
        result_table.add_column("Result")
        for candidate in updates:
            after = new_fw.get(candidate.name, "?")
            ok = sdr.parse_inventory_version(after) >= candidate.sdr.version
            result_table.add_row(
                candidate.name,
                candidate.current,
                after,
                "[green]✓ Updated[/green]" if ok else "[yellow]⚠ Check manually[/yellow]",
            )
        console.print(result_table)

        async with ilo_session(host) as client:
            queue = await firmware.get_task_queue(client)
            stale = [task for task in queue if task.get("State") in ("Pending", "Complete")]
            if stale:
                await firmware.clear_task_queue(client)
                console.print(f"  [dim]Cleared {len(stale)} stale task(s) from queue.[/dim]")
    else:
        console.print(
            f"\n[bold yellow]Updates queued.[/bold yellow] Reboot [bold]{host['name']}[/bold] to apply.\n"
            f"  • Use [bold]--reboot[/bold] flag to reboot automatically\n"
            f"  • Run [bold]pcli ilo upgrade queue --host {host['name']}[/bold] to check queue status"
        )


async def _cmd_describe(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich import box as rich_box

    console = Console()
    hosts = _load_hosts_or_exit(args.name)
    if not hosts:
        console.print("[red]No host found.[/red]")
        sys.exit(1)
    host = hosts[0]

    async with ILOClient(host["url"], host["username"], host["password"]) as c:
        try:
            with console.status("[dim]Fetching server details…[/dim]"):
                sys_uri, mgr_uri = await asyncio.gather(
                    c.get_system_uri(), c.get_manager_uri()
                )
                system, manager = await asyncio.gather(
                    c.get(sys_uri), c.get(mgr_uri)
                )
                # Fetch detailed resources sequentially to avoid overwhelming iLO
                fw_list = await inventory.fetch_firmware_inventory_full(c)
                cpus    = await inventory.fetch_cpu_report_data(c)
                gpus    = await inventory.fetch_gpu_report_data(c)
                dimms   = await inventory.fetch_memory_population(c)
        except ServerDownOrUnreachableError:
            console.print(f"[red]Cannot connect to iLO at {host['url']}[/red]")
            sys.exit(1)

    model      = system.get("Model", "—")
    serial     = system.get("SerialNumber", "—")
    sku        = system.get("SKU", "—")
    bios_ver   = system.get("BiosVersion", "—")
    power      = system.get("PowerState", "—")
    health_obj = (system.get("Status") or {})
    health_str = health_obj.get("Health", "—")
    uuid       = system.get("UUID", "—")

    mgr_model  = manager.get("Model", "—")
    mgr_fw     = manager.get("FirmwareVersion", "—") or "—"
    mgr_host   = manager.get("HostName", "—") or "—"
    ilo_status = (manager.get("Status") or {}).get("Health", "—")

    ilo_eth_uri = (
        manager.get("EthernetInterfaces", {}).get("@odata.id")
    )

    _HS = {"OK": "green", "Warning": "yellow", "Critical": "red"}

    def _h(v):
        s = _HS.get(v or "", "")
        return f"[{s}]{v}[/{s}]" if s else (v or "—")

    # ── Header ────────────────────────────────────────────────────────────────
    console.print(Panel(
        f"[bold]{host['name']}[/bold]   [dim]{model}[/dim]",
        expand=False,
    ))

    # ── Identity ──────────────────────────────────────────────────────────────
    id_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    id_t.add_column(style="dim", no_wrap=True)
    id_t.add_column()
    id_t.add_row("Serial",     serial)
    id_t.add_row("Product ID", sku)
    id_t.add_row("UUID",       uuid)
    id_t.add_row("BIOS",       bios_ver)
    id_t.add_row("Power",      _h(power))
    id_t.add_row("Health",     _h(health_str))
    console.print(id_t)

    # ── iLO ───────────────────────────────────────────────────────────────────
    console.print("[bold]iLO[/bold]")
    ilo_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    ilo_t.add_column(style="dim", no_wrap=True)
    ilo_t.add_column()
    ilo_t.add_row("Model",    mgr_model)
    ilo_t.add_row("Firmware", mgr_fw)
    ilo_t.add_row("Hostname", mgr_host)
    ilo_t.add_row("Health",   _h(ilo_status))
    console.print(ilo_t)

    # ── CPU ───────────────────────────────────────────────────────────────────
    if cpus:
        console.print("[bold]CPU[/bold]")
        cpu_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        cpu_t.add_column("Socket", no_wrap=True)
        cpu_t.add_column("Model")
        cpu_t.add_column("Cores", justify="right")
        cpu_t.add_column("Threads", justify="right")
        cpu_t.add_column("Max MHz", justify="right", style="dim")
        for cpu in cpus:
            cpu_t.add_row(
                cpu.get("socket", "—"),
                cpu.get("model", "—"),
                str(cpu.get("cores", "—")),
                str(cpu.get("threads", "—")),
                str(cpu.get("speed_mhz", "—")),
            )
        console.print(cpu_t)

    # ── GPU ───────────────────────────────────────────────────────────────────
    if gpus:
        console.print("[bold]GPU[/bold]")
        gpu_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        gpu_t.add_column("Name")
        gpu_t.add_column("Model")
        for gpu in gpus:
            gpu_t.add_row(gpu.get("name", "—"), gpu.get("model", "—"))
        console.print(gpu_t)

    # ── Memory population map ─────────────────────────────────────────────────
    if dimms:
        populated  = [d for d in dimms if d["present"]]
        empty_cnt  = sum(1 for d in dimms if not d["present"])
        total_gb   = sum(d["cap_gb"] for d in populated)
        console.print("[bold]Memory[/bold]")
        mem_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        mem_t.add_column("Slot",     no_wrap=True)
        mem_t.add_column("Capacity", justify="right")
        mem_t.add_column("Type",     no_wrap=True)
        mem_t.add_column("Speed",    justify="right")
        mem_t.add_column("Part Number", style="dim")
        for d in dimms:
            if d["present"]:
                speed_s = f"{d['speed']} MT/s" if d["speed"] else "—"
                cap_s   = f"{d['cap_gb']} GB"
                mem_t.add_row(d["slot"], cap_s, d["type"] or "—", speed_s, d["part"] or "—")
            else:
                mem_t.add_row(f"[dim]{d['slot']}[/dim]", "[dim]empty[/dim]", "", "", "")
        console.print(mem_t)
        console.print(
            f"  [dim]{len(populated)} DIMMs populated, {empty_cnt} empty"
            f" — {total_gb} GB total[/dim]"
        )

    # ── Firmware ──────────────────────────────────────────────────────────────
    if fw_list:
        console.print("[bold]Firmware[/bold]")
        fw_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        fw_t.add_column("Component", no_wrap=True)
        fw_t.add_column("Version")
        fw_t.add_column("Location", style="dim")
        for fw in fw_list:
            loc = (fw.get("Oem") or {}).get("Hpe", {}).get("DeviceContext", "")
            fw_t.add_row(fw.get("Name", ""), fw.get("Version", ""), loc)
        console.print(fw_t)


if __name__ == "__main__":
    main()
