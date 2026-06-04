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
from pcli.common.runner import run_parallel, run_sync
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
from pcli.ilo.describe import run_describe
from pcli.ilo.printers import (
    _print_json_results,
    _header_line,
    print_ilo_table,
    _print_component_table,
    print_network_table,
    _print_raw_table,
    print_disk_map_table,
    print_fleet_table,
    print_serial_table,
    print_update_method_table,
    print_full_table,
    _print_fw_components,
    _print_fw_queue,
)
from pcli.ilo.reports import run_report_cpu, run_report_gpu, run_report_memory
from pcli.ilo.upgrade import run_fw_upgrade, run_upgrade_action

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
    hosts = _load_hosts_or_exit(getattr(args, "host", None), getattr(args, "hosts_from", None))
    await run_report_memory(hosts)


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
        except Exception:  # intentional: tab completion must never print to stdout
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
            sp.add_argument("--raw", action="store_true", help="Dump unprocessed Redfish API response (bypasses pcli field parsing)")
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
            sp.add_argument("--raw", action="store_true", help="Dump unprocessed Redfish API response (bypasses pcli field parsing)")
        else:
            sp = get_sub.add_parser(name, help=help_text)
            _add_host(sp)
            sp.add_argument("--raw", action="store_true", help="Dump unprocessed Redfish API response (bypasses pcli field parsing)")

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
    run_sync(_async_main(args))


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
    except Exception:  # intentional: editor launch is best-effort; don't crash the CLI
        pass
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
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    await run_report_cpu(hosts)


async def _run_report_gpu(args: argparse.Namespace) -> None:
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    await run_report_gpu(hosts)


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
        "network": print_network_table,
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
    host = _load_hosts_or_exit(args.host)[0]

    if action == "auto":
        if not args.host:
            print("ERROR: 'pcli ilo upgrade' requires --host <name>", file=sys.stderr)
            sys.exit(1)
        await run_fw_upgrade(
            host,
            dry_run=dry_run,
            reboot=getattr(args, "reboot", False),
            component=getattr(args, "component", "all"),
        )
    else:
        await run_upgrade_action(
            host, action,
            dry_run=dry_run,
            url=getattr(args, "url", None),
            filename=getattr(args, "filename", None),
        )


async def _cmd_describe(args: argparse.Namespace) -> None:
    hosts = _load_hosts_or_exit(args.name)
    if not hosts:
        get_console().print("[red]No host found.[/red]")
        sys.exit(1)
    await run_describe(hosts[0])


if __name__ == "__main__":
    main()
