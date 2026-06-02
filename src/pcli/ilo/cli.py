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

FetchFn = Callable[[ILOClient], Awaitable[list[Any]]]


def _ilo_fields_completer(choices: tuple):
    """Argcomplete completer for comma-separated ilo field lists."""
    def completer(prefix: str, **kwargs):
        if "," in prefix:
            before, current = prefix.rsplit(",", 1)
            before += ","
        else:
            before, current = "", prefix
        return [before + c for c in choices if c.lower().startswith(current.lower())]
    return completer


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
    semaphore = asyncio.Semaphore(MAX_WORKERS)

    async def _run_one(host: dict) -> tuple[str, str | None, list[Any]]:
        async with semaphore:
            return await query_host_async(host, fetch_fn)

    return list(await asyncio.gather(*(_run_one(host) for host in hosts)))


def _header_line(server_col: int, label_col: int, value_col: int) -> str:
    return f"{'Server':<{server_col}}   {'Name':<{label_col}}   {'Version':<{value_col}}"


def print_ilo_table(results: list[tuple[str, str | None, list]]) -> None:
    server_w, ilo_w, name_w = COL_SERVER_WIDTH, COL_ILO_WIDTH, COL_NAME_WIDTH
    total_w = server_w + ilo_w + name_w + 4
    print("\n--- iLO Firmware Versions ---")
    print(_header_line(server_w, name_w, ilo_w))
    print("-" * total_w)
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        if error:
            print(f"{host_name:<{server_w}}   {'ERROR':<{name_w}}   {error}")
            continue
        for i, (name, version) in enumerate(rows):
            label = host_name if i == 0 else ""
            print(f"{label:<{server_w}}   {name:<{name_w}}   {version}")


def _print_component_table(results: list[tuple[str, str | None, list]], title: str) -> None:
    server_w, name_w, ver_w = COL_SERVER_WIDTH, COL_NIC_WIDTH, COL_NAME_WIDTH
    total_w = server_w + name_w + ver_w + 4
    print(f"--- {title} ---")
    print(_header_line(server_w, ver_w, name_w))
    print("-" * total_w)
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        if error:
            print(f"{host_name:<{server_w}}   {'ERROR':<{ver_w}}   {error}")
            continue
        for i, (name, version) in enumerate(rows):
            label = host_name if i == 0 else ""
            print(f"{label:<{server_w}}   {name[:ver_w]:<{ver_w}}   {version}")


def _print_raw_table(results: list[tuple[str, str | None, list]]) -> None:
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        print(f"\n=== {host_name} ===")
        if error:
            print(f"ERROR: {error}")
            continue
        for uri, raw_json in rows:
            print(f"\n--- {uri} ---")
            print(raw_json)


def print_disk_map_table(results: list[tuple[str, str | None, list]]) -> None:
    server_w = COL_SERVER_WIDTH
    vol_w = 52
    bay_w = 80
    total_w = server_w + vol_w + bay_w + 4
    print("\n--- Drive Identity Map (Volume NAA → Physical Bay) ---")
    print("  On Linux/CoreOS (oc debug node/<n> -- chroot /host), run:")
    print("    for d in sda sdb sdc sdd; do")
    print("      echo \"$d: $(cat /sys/block/$d/device/wwid)\"")
    print("    done")
    print("  Strip 'naa.' prefix from wwid and match against NAA: column below.")
    print(f"\n{'Server':<{server_w}}   {'LUN  RAID   Capacity  Volume-NAA/EUI':<{vol_w}}   {'Physical Drive Bays (ServiceLabel + Serial)':<{bay_w}}")
    print("-" * total_w)
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        if error:
            print(f"{host_name:<{server_w}}   {'ERROR':<{vol_w}}   {error}")
            continue
        for i, (vol_label, bay_info) in enumerate(rows):
            label = host_name if i == 0 else ""
            print(f"{label:<{server_w}}   {vol_label:<{vol_w}}   {bay_info}")


def print_fleet_table(results: list[tuple[str, str | None, list]],
                      fields: str | None = None) -> None:
    all_keys = list(inventory.FLEET_KEYS)
    # Build case-insensitive lookup: lower → canonical
    _key_map = {k.lower(): k for k in all_keys}

    # Validate and select requested fields
    if fields:
        requested_raw = [f.strip() for f in fields.split(",") if f.strip()]
        bad = [k for k in requested_raw if k.lower() not in _key_map]
        if bad:
            valid = ", ".join(all_keys)
            raise SystemExit(f"Unknown field(s): {', '.join(bad)}\nAvailable: {valid}")
        keys = [_key_map[k.lower()] for k in requested_raw]
    else:
        keys = all_keys
    server_data: dict[str, dict[str, str]] = {}
    for host_name, error, rows in results:
        if error:
            server_data[host_name] = {k: "ERROR" for k in keys}
            server_data[host_name]["_error"] = error
        else:
            server_data[host_name] = dict(rows)

    col_w: dict[str, int] = {k: len(k) for k in keys}
    srv_w = max(len("Server"), max(len(name) for name in server_data))
    for vals in server_data.values():
        for key in keys:
            col_w[key] = max(col_w[key], len(vals.get(key, "N/A")))

    header = f"{'Server':<{srv_w}}" + "".join(f"   {key:<{col_w[key]}}" for key in keys)
    print("\n--- Fleet Summary ---")
    print(header)
    print("-" * len(header))
    for host_name in sorted(server_data):
        vals = server_data[host_name]
        row = f"{host_name:<{srv_w}}" + "".join(f"   {vals.get(key, 'N/A'):<{col_w[key]}}" for key in keys)
        print(row)
        if "_error" in vals:
            print(f"  {'':>{srv_w}}   {vals['_error']}")


def print_serial_table(results: list[tuple[str, str | None, list]]) -> None:
    keys = ("Model", "Serial", "ProductID")
    server_data: dict[str, dict[str, str]] = {}
    for host_name, error, rows in results:
        if error:
            server_data[host_name] = {k: "ERROR" for k in keys}
            server_data[host_name]["_error"] = error
        else:
            server_data[host_name] = dict(rows)

    srv_w = max(len("Server"), max(len(name) for name in server_data))
    col_w: dict[str, int] = {k: len(k) for k in keys}
    for vals in server_data.values():
        for key in keys:
            col_w[key] = max(col_w[key], len(vals.get(key, "N/A")))

    header = f"{'Server':<{srv_w}}" + "".join(f"   {key:<{col_w[key]}}" for key in keys)
    print("\n--- Server Identity ---")
    print(header)
    print("-" * len(header))
    for host_name in sorted(server_data):
        vals = server_data[host_name]
        row = f"{host_name:<{srv_w}}" + "".join(f"   {vals.get(key, 'N/A'):<{col_w[key]}}" for key in keys)
        print(row)
        if "_error" in vals:
            print(f"  {'':>{srv_w}}   {vals['_error']}")


def print_update_method_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print firmware inventory with update method classification (BMC / UEFI / OS)."""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    console = Console()
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        table = Table(
            title=f"{host_name}",
            box=box.ROUNDED,
            show_lines=False,
            show_header=True,
        )
        table.add_column("Component", style="bold", no_wrap=False, max_width=45)
        table.add_column("Version", style="dim", max_width=28)
        table.add_column("Update By", justify="center", width=10)
        table.add_column("Reboot", justify="center", width=7)
        table.add_column("Context", style="dim", max_width=28)

        if error:
            table.add_row(f"[red]ERROR:[/red] {error}", "", "", "", "")
        else:
            for entry in rows:
                method = entry["UpdateBy"]
                reboot = entry["Reboot"]
                if method == "BMC":
                    method_str = "[cyan bold]BMC[/cyan bold]"
                elif method == "UEFI":
                    method_str = "[green bold]UEFI[/green bold]"
                else:
                    method_str = "[yellow bold]OS[/yellow bold]"
                reboot_str = "[red]Yes[/red]" if reboot else "[green]No[/green]"
                table.add_row(
                    entry["Name"],
                    entry["Version"],
                    method_str,
                    reboot_str,
                    entry["Context"],
                )
        console.print(table)

    # Print legend
    console.print(
        "  [cyan bold]BMC[/cyan bold]  = iLO flashes directly (no reboot)  "
        "[green bold]UEFI[/green bold] = UEFI applies on next reboot (no OS)  "
        "[yellow bold]OS[/yellow bold]   = requires running OS + iSUT/SUM"
    )


def print_full_table(results: list[tuple[str, str | None, list]]) -> None:
    server_w, name_w, ver_w = COL_SERVER_WIDTH, COL_NAME_WIDTH, COL_ILO_WIDTH
    total_w = server_w + name_w + ver_w + 4
    print("\n--- Full Firmware Inventory ---")
    print(_header_line(server_w, name_w, ver_w))
    print("-" * total_w)
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        if error:
            print(f"{host_name:<{server_w}}   {'ERROR':<{name_w}}   {error}")
            continue
        for i, (name, version) in enumerate(rows):
            label = host_name if i == 0 else ""
            print(f"{label:<{server_w}}   {name:<{name_w}}   {version}")


async def _run_report_memory(args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich import box as rich_box
    from pcli.com.inventory import aggregate_by_part_number

    console = Console()
    hosts = _load_hosts_or_exit(getattr(args, "host", None))

    with console.status("[dim]Fetching memory inventory across fleet…[/dim]"):
        results = await _run_parallel_async(hosts, inventory.fetch_memory_report_data)

    all_dimms: list[dict] = []
    for server_name, error, dimms in results:
        if error:
            console.print(f"[yellow]  {server_name}: {error}[/yellow]")
            continue
        for d in dimms:
            d["server"] = server_name
            all_dimms.append(d)

    if not all_dimms:
        console.print("[yellow]No memory inventory data returned.[/yellow]")
        return

    rows = aggregate_by_part_number(all_dimms)
    total_dimms = sum(r["count"] for r in rows)
    total_tb = sum(r["count"] * r["capacity_gb"] for r in rows) / 1024

    table = Table(
        title=f"Memory Part-Number Breakdown  ({total_dimms} DIMMs  /  {total_tb:.1f} TB total)",
        box=rich_box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("HPE Part Number", min_width=14, no_wrap=True)
    table.add_column("Vendor",          min_width=12, no_wrap=True)
    table.add_column("Capacity",        justify="right", no_wrap=True)
    table.add_column("Type",            no_wrap=True)
    table.add_column("Speed",           justify="right", no_wrap=True)
    table.add_column("Count",           justify="right", no_wrap=True, style="bold")
    table.add_column("Total",           justify="right", no_wrap=True)
    table.add_column("Servers",         justify="right", no_wrap=True, style="dim")

    for r in rows:
        cap = f"{r['capacity_gb']} GB" if r["capacity_gb"] else "—"
        speed = f"{r['speed_mts']} MT/s" if r["speed_mts"] else "—"
        total_cap_gb = r["count"] * r["capacity_gb"]
        total_cap = f"{total_cap_gb} GB" if total_cap_gb < 1024 else f"{total_cap_gb/1024:.1f} TB"
        table.add_row(
            r["hpe_pn"], r["vendor"], cap, r["type"], speed,
            str(r["count"]), total_cap, str(len(r["servers"])),
        )

    console.print(table)


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

    def _host_completer(**_kwargs):
        try:
            return [host["name"] for host in load_hosts()]
        except Exception:
            return []

    def _add_host(p: argparse.ArgumentParser, required: bool = False) -> None:
        p.add_argument(
            "--host", metavar="NAME", required=required,
            help="Target a single host by name (from hosts-ilo.ini)",
        ).completer = _host_completer

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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
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
    elif args.command == "set":
        if args.set_action == "dhcp":
            await _run_set_dhcp(args)


def _load_hosts_or_exit(name: str | None) -> list[dict]:
    try:
        return load_hosts(name=name)
    except FileNotFoundError:
        from pcli.ilo.config import HOSTS_FILE
        print(f"ERROR: hosts-ilo.ini not found. Expected at: {HOSTS_FILE}", file=sys.stderr)
        print("       Run 'pcli ilo init' to create a starter config in the current directory.", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_init() -> None:
    from pathlib import Path
    dest = Path.cwd() / "hosts-ilo.ini"
    if dest.exists():
        print(f"Already exists: {dest}")
        print("Edit it to add or update your servers.")
        return
    dest.write_text(
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
    )
    print(f"Created: {dest}")
    print("Edit it to fill in your server addresses and credentials.")
    print("\nThen try:  pcli ilo list firmwares")


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
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    raw = getattr(args, "raw", False)
    fetch_fn = _RAW_DISPATCH[what] if raw else _FETCH_DISPATCH[what]
    results = await _run_parallel_async(hosts, fetch_fn)

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


def _print_fw_components(host_name: str, components: list[dict]) -> None:
    print(f"\n--- Staged Components: {host_name} ---")
    if not components:
        print("  (no components staged)")
        return
    name_w, ver_w, size_w = 50, 15, 12
    print(f"{'Name':<{name_w}}   {'Version':<{ver_w}}   {'Size (MB)':<{size_w}}")
    print("-" * (name_w + ver_w + size_w + 6))
    for component in components:
        name = component.get("Name", component.get("Filename", "unknown"))
        version = component.get("Version", "—")
        size = component.get("SizeBytes", 0)
        size_mb = f"{size / 1_048_576:.1f}" if size else "—"
        print(f"{name[:name_w]:<{name_w}}   {version:<{ver_w}}   {size_mb}")


def _print_fw_queue(host_name: str, queue: list[dict]) -> None:
    print(f"\n--- Update Task Queue: {host_name} ---")
    if not queue:
        print("  (task queue is empty)")
        return
    name_w, state_w, result_w = 50, 15, 20
    print(f"{'Name/Filename':<{name_w}}   {'State':<{state_w}}   {'Result':<{result_w}}")
    print("-" * (name_w + state_w + result_w + 6))
    for task in queue:
        name = task.get("Name", task.get("Filename", "unknown"))
        state = task.get("State", "—")
        result = task.get("Result", {})
        result_str = result.get("MessageId", "—") if isinstance(result, dict) else str(result)
        print(f"{name[:name_w]:<{name_w}}   {state:<{state_w}}   {result_str[:result_w]}")


if __name__ == "__main__":
    main()
