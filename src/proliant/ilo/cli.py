"""
hpeilo.cli
~~~~~~~~~~
Command-line interface: subcommand-based argument parsing, async host queries,
and table printing.

Usage::

    proliant ilo list firmwares                          All servers, all firmware columns
    proliant ilo list firmwares --host dl325-gen12       Single server
    proliant ilo list firmwares --fields bios,ilo        BIOS and iLO columns only
    proliant ilo list firmwares --fields model,bios,ilo  Model + BIOS + iLO
    proliant ilo list firmwares --fields nic-fw,storage-fw
    proliant ilo list ilo                                iLO firmware version
    proliant ilo list network                            NIC firmware versions
    proliant ilo list storage                            Storage firmware versions
    proliant ilo list serial                             Server model + serial (for COM onboarding)
    proliant ilo list full                               Full firmware inventory
    proliant ilo list update-method                      All firmware with BMC/UEFI/OS update method
    proliant ilo list update-method --host dl345-gen12   Single server update method view
    proliant ilo upgrade --host <name>                  Auto-upgrade outdated firmware

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
import difflib
import json
import sys
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from typing import Any

import argcomplete

from proliant.ilo.boot import fetch_boot_order, set_one_time_pxe
from proliant.ilo.inventory import apply_license_key
from proliant.common.completers import comma_sep_completer, file_completion, suppress_file_completion, cached_names
from proliant.common.display import get_console, make_table, print_json, OutputMode, get_output_mode
from proliant.common.runner import run_parallel, run_sync
from proliant.common.targets import resolve_hosts, add_target_args
from proliant.ilo import firmware, inventory
from proliant.ilo.client import ILOClient, ServerDownOrUnreachableError, ilo_session
from proliant.ilo.config import (
    COL_ILO_WIDTH,
    COL_NAME_WIDTH,
    COL_NIC_WIDTH,
    COL_SERVER_WIDTH,
    MAX_WORKERS,
    load_hosts,
)
from proliant.ilo.bios import (fetch_bios, format_bios, set_workload_profile,
                           set_serial_console, WORKLOAD_PROFILES,
                           SERIAL_CONSOLE_PORTS, EMS_CONSOLE_VALUES,
                           VIRTUAL_SERIAL_PORT_VALUES)
from proliant.ilo.describe import run_describe, run_describe_ilo_nic, run_describe_fw_update
from proliant.ilo.power import RESET_TYPES, force_off, graceful_shutdown, power_on, reset_server
from proliant.ilo.printers import (
    _print_json_results,
    _header_line,
    _print_component_table,
    print_network_table,
    print_nic_ilo_table,
    _print_raw_table,
    print_disk_map_table,
    print_fleet_table,
    print_license_table,
    print_serial_table,
    print_servers_table,
    print_update_method_table,
    print_full_table,
    _print_fw_components,
    _print_fw_queue,
)
from proliant.ilo.reports import run_report_cpu, run_report_gpu, run_report_memory
from proliant.ilo.upgrade import run_fw_upgrade, run_upgrade_action

FetchFn = Callable[[ILOClient], Awaitable[list[Any]]]


def _ilo_fields_completer(choices: tuple):
    """Argcomplete completer for comma-separated ilo field lists."""
    return comma_sep_completer(choices)


_FETCH_DISPATCH: dict[str, FetchFn] = {
    "servers": inventory.fetch_server_list_info,
    "nic_host": inventory.fetch_network_versions,
    "nic_ilo":  inventory.fetch_ilo_nic_summary,
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
    "license": inventory.fetch_license_info,
}

_RAW_DISPATCH: dict[str, FetchFn] = {
    "servers": inventory.fetch_server_list_info,
    "nic_host": inventory.fetch_network_raw,
    "nic_ilo":  inventory.fetch_ilo_nic_summary,
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
    "license": inventory.fetch_license_info,
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


class _SuggestingArgumentParser(argparse.ArgumentParser):
    """ArgumentParser that suggests close matches on invalid choice errors."""

    _GREEN  = "\033[32m"
    _YELLOW = "\033[33m"
    _BOLD   = "\033[1m"
    _RESET  = "\033[0m"

    def error(self, message: str) -> None:
        import re
        import sys
        suggestion = None
        match = re.search(r"(invalid choice: '[^']+') \(choose from ([^)]+)\)", message)
        if match:
            bad_part = match.group(1)
            choices_str = match.group(2)
            choices = [c.strip().strip("'") for c in choices_str.split(",")]
            close = difflib.get_close_matches(
                re.search(r"'([^']+)'", bad_part).group(1), choices, n=1, cutoff=0.6
            )
            if close:
                suggestion = close[0]
            colored_choices = ", ".join(
                f"{self._YELLOW}{c}{self._RESET}" for c in choices
            )
            message = re.sub(
                r"\(choose from [^)]+\)",
                f"(choose from {colored_choices})",
                message,
            )
        if suggestion:
            sys.stderr.write(
                f"\n{self._GREEN}{self._BOLD}  Did you mean: '{suggestion}'?{self._RESET}\n\n"
            )
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: error: {message}\n")
        sys.exit(2)


def _build_parser() -> argparse.ArgumentParser:
    try:
        _version = _pkg_version("proliant")
    except PackageNotFoundError:
        _version = "dev"

    parser = _SuggestingArgumentParser(
        prog="proliant ilo",
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

    def _staged_firmware_filename_completer(prefix: str, parsed_args: argparse.Namespace, **_kwargs) -> list[str]:
        host_name = getattr(parsed_args, "host", None)
        if not host_name:
            return []
        try:
            host = load_hosts(host_name)[0]

            def _fetch_names() -> list[str]:
                async def _fetch() -> list[str]:
                    async with ilo_session(host) as client:
                        components = await firmware.get_component_repository(client)
                    return [c.get("Filename") or c.get("Name") or "" for c in components]

                return [n for n in asyncio.run(_fetch()) if n]

            # Cached briefly -- a fresh Redfish login/logout per keystroke
            # otherwise costs a couple of seconds on every TAB press.
            names = cached_names(f"ilo-staged-fw-{host_name}", _fetch_names)
            return [n for n in names if n.lower().startswith(prefix.lower())]
        except Exception:
            return []

    def _pxe_port_completer(prefix: str, parsed_args: argparse.Namespace, **_kwargs) -> list[str]:
        host_name = getattr(parsed_args, "host", None)
        if not host_name:
            return []
        try:
            host = load_hosts(host_name)[0]

            def _fetch_names() -> list[str]:
                async def _fetch() -> list[str]:
                    async with ilo_session(host) as client:
                        boot = await fetch_boot_order(client)
                    values: list[str] = []
                    for option in boot.get("pxe_ipv4", []):
                        values.extend(
                            value for value in (
                                option.get("port_hint", ""),
                                option.get("display_name", ""),
                                option.get("mac", ""),
                            ) if value
                        )
                    seen = set()
                    return [v for v in values if not (v in seen or seen.add(v))]

                return asyncio.run(_fetch())

            names = cached_names(f"ilo-pxe-ports-{host_name}", _fetch_names)
            return [v for v in names if v.lower().startswith(prefix.lower())]
        except Exception:
            return []

    def _add_host_target(
        p: argparse.ArgumentParser,
        *,
        required: bool,
        allow_hosts_from: bool = False,
        metavar: str = "SERVER",
        help_text: str = "Server name from inventory.ini",
    ) -> None:
        arg = p.add_argument(
            "host",
            metavar=metavar,
            nargs=None if required else "?",
            help=help_text,
        )
        arg.completer = _host_completer
        if allow_hosts_from:
            hosts_from_arg = p.add_argument(
                "--hosts-from",
                metavar="FILE",
                dest="hosts_from",
                help="Read target hosts from FILE (one per line), or '-' for stdin",
            )
            hosts_from_arg.completer = file_completion()

    def _add_list_action(
        resource_parser: argparse.ArgumentParser,
        *,
        fetch_key: str,
        help_text: str,
        description: str | None = None,
    ) -> argparse.ArgumentParser:
        action_sub = resource_parser.add_subparsers(dest="action", metavar="ACTION")
        action_sub.required = True
        kwargs: dict[str, object] = {"help": help_text}
        if description:
            kwargs["description"] = description
            kwargs["formatter_class"] = argparse.RawDescriptionHelpFormatter
        list_p = action_sub.add_parser("list", **kwargs)
        list_p.set_defaults(command="list", what=fetch_key)
        _add_host_target(list_p, required=False, allow_hosts_from=True)
        list_p.add_argument("--raw", action="store_true", help="Dump unprocessed Redfish API response (bypasses proliant field parsing)")
        return list_p

    subparsers = parser.add_subparsers(dest="resource", metavar="RESOURCE",
                                       parser_class=_SuggestingArgumentParser)
    subparsers.required = True

    servers_p = subparsers.add_parser("servers", help="Server inventory and details")
    servers_sub = servers_p.add_subparsers(dest="servers_action", metavar="ACTION",
                                           parser_class=_SuggestingArgumentParser)
    servers_sub.required = True
    servers_list = servers_sub.add_parser("list", help="List servers")
    servers_list.set_defaults(command="list", what="servers")
    _add_host_target(servers_list, required=False, allow_hosts_from=True)
    servers_list.add_argument("--raw", action="store_true", help="Dump unprocessed Redfish API response (bypasses proliant field parsing)")
    servers_desc = servers_sub.add_parser("describe", help="Show full details for a single server")
    servers_desc.set_defaults(command="describe")
    _add_host_target(servers_desc, required=True, metavar="NAME")
    servers_desc.add_argument("--ilo-nic", action="store_true", dest="ilo_nic",
                              help="Show iLO dedicated NIC details (DHCP/static, IP, DNS, routes, LLDP, MAC)")
    servers_desc.add_argument("--raw", action="store_true",
                              help="With --ilo-nic: dump unprocessed Redfish JSON for Manager EthernetInterfaces")
    servers_desc.add_argument("--firmware-update", action="store_true", dest="firmware_update",
                              help="Show firmware update status: UpdateService state, last bundle report, component repository")

    firmware_p = subparsers.add_parser(
        "firmware",
        help="Firmware inventory and update operations",
        description=(
            "List firmware inventory or manage staged firmware updates.\n\n"
            "Examples:\n"
            "  proliant ilo firmware list\n"
            "  proliant ilo firmware list dl325-gen12\n"
            "  proliant ilo firmware list --fields bios,ilo\n"
            "  proliant ilo firmware upgrade dl325-gen12 --dry-run\n"
            "  proliant ilo firmware queue dl325-gen12\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    firmware_sub = firmware_p.add_subparsers(dest="firmware_action", metavar="ACTION")
    firmware_sub.required = True
    firmware_list = firmware_sub.add_parser(
        "list",
        help="List firmware summary",
        description=(
            "Show firmware summary for all servers (one row per server).\n\n"
            "Examples:\n"
            "  proliant ilo firmware list\n"
            "  proliant ilo firmware list dl325-gen12\n"
            "  proliant ilo firmware list --fields bios,ilo\n"
            "  proliant ilo firmware list --fields model,bios\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    firmware_list.set_defaults(command="list", what="firmwares")
    _add_host_target(firmware_list, required=False, allow_hosts_from=True)
    firmware_list.add_argument("--raw", action="store_true", help="Dump unprocessed Redfish API response (bypasses proliant field parsing)")
    from proliant.ilo.inventory import FLEET_KEYS
    fields_arg = firmware_list.add_argument(
        "--fields",
        metavar="FIELDS",
        help=(
            f"Comma-separated columns (case-insensitive). "
            f"Available: {', '.join(FLEET_KEYS)}. Default: all"
        ),
    )
    fields_arg.completer = _ilo_fields_completer(tuple(k.lower() for k in FLEET_KEYS))  # type: ignore[attr-defined]

    fw_upgrade = firmware_sub.add_parser("upgrade", help="Upgrade outdated firmware")
    fw_upgrade.set_defaults(command="upgrade", upgrade_action="auto")
    _add_host_target(fw_upgrade, required=True)
    fw_upgrade.add_argument("--dry-run", action="store_true", dest="dry_run", help="Preview without making changes")
    fw_upgrade.add_argument("--reboot", action="store_true", help="Reboot server after queuing all updates")
    fw_upgrade.add_argument(
        "--component",
        metavar="FILTER",
        choices=["all", "ilo", "bios", "nic", "storage"],
        default="all",
        help="Limit upgrade to a specific component type: all | ilo | bios | nic | storage (default: all)",
    )
    fw_components = firmware_sub.add_parser("components", help="List staged components in iLO repository")
    fw_components.set_defaults(command="upgrade", upgrade_action="components")
    _add_host_target(fw_components, required=True)
    fw_queue = firmware_sub.add_parser("queue", help="Show the firmware update task queue")
    fw_queue.set_defaults(command="upgrade", upgrade_action="queue")
    _add_host_target(fw_queue, required=True)
    fw_stage = firmware_sub.add_parser("stage", help="Stage a firmware package from a URL")
    fw_stage.set_defaults(command="upgrade", upgrade_action="stage")
    _add_host_target(fw_stage, required=True)
    fw_stage_url = fw_stage.add_argument("--url", metavar="URL", required=True, help="Direct URL to .fwpkg file on HPE SDR")
    fw_stage_url.completer = suppress_file_completion()
    fw_stage.add_argument("--dry-run", action="store_true", dest="dry_run")
    fw_flash = firmware_sub.add_parser("flash", help="Queue a staged file for flash on next reboot")
    fw_flash.set_defaults(command="upgrade", upgrade_action="flash")
    _add_host_target(fw_flash, required=True)
    fw_flash_filename = fw_flash.add_argument("filename", metavar="FILENAME", help="Filename of the staged component to queue")
    fw_flash_filename.completer = _staged_firmware_filename_completer
    fw_flash.add_argument("--dry-run", action="store_true", dest="dry_run")
    fw_clear = firmware_sub.add_parser("clear", help="Clear all entries from the task queue")
    fw_clear.set_defaults(command="upgrade", upgrade_action="clear")
    _add_host_target(fw_clear, required=True)
    fw_clear.add_argument("--dry-run", action="store_true", dest="dry_run")

    list_resources = {
        "nic-host": "Host NIC firmware versions",
        "nic-ilo": "iLO dedicated NIC: LLDP status, neighbor info, IP",
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
    for resource, help_text in list_resources.items():
        desc = None
        if resource == "update-method":
            desc = (
                "Show all firmware components with update method classification.\n\n"
                "Update methods:\n"
                "  BMC  — iLO flashes the component directly (no server reboot needed)\n"
                "  UEFI — UEFI processes it on next server reboot (no OS required)\n"
                "  OS   — Requires a running OS + iSUT/SUM RuntimeAgent\n\n"
                "Examples:\n"
                "  proliant ilo update-method list\n"
                "  proliant ilo update-method list dl345-gen12\n"
            )
        resource_p = subparsers.add_parser(resource, help=help_text)
        _add_list_action(resource_p, fetch_key=resource, help_text=f"List {resource}", description=desc)

    license_p = subparsers.add_parser("license", help="iLO license info and key management")
    license_sub = license_p.add_subparsers(dest="license_action", metavar="ACTION")
    license_sub.required = True
    license_list = license_sub.add_parser("list", help="List iLO license info across fleet")
    license_list.set_defaults(command="list", what="license")
    _add_host_target(license_list, required=False, allow_hosts_from=True)
    license_list.add_argument("--raw", action="store_true")
    license_describe = license_sub.add_parser("describe", help="Show license details for a single server")
    license_describe.set_defaults(command="list", what="license")
    _add_host_target(license_describe, required=True)
    license_set = license_sub.add_parser("set", help="Apply a license key to a server")
    license_set.set_defaults(command="license")
    _add_host_target(license_set, required=True)
    license_key = license_set.add_argument("key", metavar="KEY", help="License key (format: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX)")
    license_key.completer = suppress_file_completion()

    power_p = subparsers.add_parser(
        "power",
        help="Server power operations",
        description=(
            "Issue Redfish ComputerSystem.Reset actions through iLO.\n\n"
            "Examples:\n"
            "  proliant ilo power reset dl325-gen12\n"
            "  proliant ilo power reset dl325-gen12 --reset-type ForceRestart\n"
            "  proliant ilo power shutdown dl325-gen12\n"
            "  proliant ilo power off dl325-gen12\n"
            "  proliant ilo power on dl325-gen12\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    power_sub = power_p.add_subparsers(dest="power_action", metavar="ACTION")
    power_sub.required = True
    power_reset = power_sub.add_parser("reset", help="Reset a server")
    power_reset.set_defaults(command="power", power_action="reset")
    _add_host_target(power_reset, required=True)
    power_reset.add_argument(
        "--reset-type",
        choices=sorted(RESET_TYPES),
        default="GracefulRestart",
        help="Redfish ResetType to send (default: GracefulRestart)",
    )
    power_reset.add_argument("--dry-run", action="store_true", dest="dry_run")
    for action_name, help_text in {
        "on": "Power on a server",
        "off": "Force power off a server",
        "shutdown": "Gracefully shut down a server",
    }.items():
        action_parser = power_sub.add_parser(action_name, help=help_text)
        action_parser.set_defaults(command="power", power_action=action_name)
        _add_host_target(action_parser, required=True)
        action_parser.add_argument("--dry-run", action="store_true", dest="dry_run")

    uid_p = subparsers.add_parser("uid", help="Control server UID indicator light")
    uid_sub = uid_p.add_subparsers(dest="uid_action", metavar="ACTION")
    uid_sub.required = True
    for uid_action, uid_help in (("on", "Turn UID light on"), ("off", "Turn UID light off")):
        uid_ap = uid_sub.add_parser(uid_action, help=uid_help)
        uid_ap.set_defaults(command="uid", uid_action=uid_action)
        _add_host_target(uid_ap, required=True)

    boot_p = subparsers.add_parser(
        "boot",
        help="Inspect and override server boot behavior",
        description=(
            "Show current boot order and set one-time PXE IPv4 boot.\n\n"
            "Examples:\n"
            "  proliant ilo boot describe dl325-gen12\n"
            "  proliant ilo boot set pxe dl325-gen12\n"
            "  proliant ilo boot set pxe dl325-gen12 --port \"Slot 1 Port 1\"\n"
            "  proliant ilo boot set pxe dl325-gen12 --port BC97E1E296C0 --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    boot_sub = boot_p.add_subparsers(dest="boot_resource_action", metavar="ACTION")
    boot_sub.required = True
    boot_desc = boot_sub.add_parser("describe", help="Show current boot order and one-time override state")
    boot_desc.set_defaults(command="boot", boot_action="show")
    _add_host_target(boot_desc, required=True)
    boot_set = boot_sub.add_parser("set", help="Change one-time boot behavior")
    boot_set_sub = boot_set.add_subparsers(dest="boot_set_action", metavar="SETTING")
    boot_set_sub.required = True
    boot_pxe = boot_set_sub.add_parser("pxe", help="Set one-time PXE IPv4 boot")
    boot_pxe.set_defaults(command="boot", boot_action="pxe")
    _add_host_target(boot_pxe, required=True)
    boot_pxe_port = boot_pxe.add_argument("--port", metavar="MATCH",
                          help="Specific PXE IPv4 port to boot from; matches boot option display text or MAC")
    boot_pxe_port.completer = _pxe_port_completer
    boot_pxe.add_argument("--dry-run", action="store_true", dest="dry_run")

    bios_p = subparsers.add_parser(
        "bios",
        help="Inspect and change BIOS settings",
        description=(
            "Show or change key BIOS settings for a server.\n\n"
            "Examples:\n"
            "  proliant ilo bios describe dl325-gen12\n"
            "  proliant ilo bios describe dl325-gen12 --pending\n"
            "  proliant ilo bios set workload-profile dl325-gen12 LowLatency\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bios_sub = bios_p.add_subparsers(dest="bios_action", metavar="ACTION")
    bios_sub.required = True
    bios_desc = bios_sub.add_parser("describe", help="Show important BIOS settings")
    bios_desc.set_defaults(command="bios", bios_action="show")
    _add_host_target(bios_desc, required=True)
    bios_desc.add_argument("--pending", action="store_true",
                           help="Show pending (staged) BIOS settings instead of current active settings")
    bios_set = bios_sub.add_parser("set", help="Change a BIOS setting (staged, takes effect after reboot)")
    bios_set_sub = bios_set.add_subparsers(dest="bios_set_action", metavar="SETTING")
    bios_set_sub.required = True
    bios_set_sc = bios_set_sub.add_parser("serial-console", help="Set serial console port redirect")
    bios_set_sc.set_defaults(command="bios", bios_action="set", bios_set_action="serial-console")
    _add_host_target(bios_set_sc, required=True)
    bios_set_sc.add_argument("--port", choices=SERIAL_CONSOLE_PORTS, metavar="PORT",
                              help=f"SerialConsolePort: {', '.join(SERIAL_CONSOLE_PORTS)}")
    bios_set_sc.add_argument("--ems", choices=EMS_CONSOLE_VALUES, metavar="EMS",
                              help=f"EmsConsole: {', '.join(EMS_CONSOLE_VALUES)}")
    bios_set_sc.add_argument("--vsp", choices=VIRTUAL_SERIAL_PORT_VALUES, metavar="VSP",
                              help=f"VirtualSerialPort: {', '.join(VIRTUAL_SERIAL_PORT_VALUES)}")
    bios_set_wl = bios_set_sub.add_parser("workload-profile", help=f"Set WorkloadProfile ({', '.join(WORKLOAD_PROFILES)})")
    bios_set_wl.set_defaults(command="bios", bios_action="set", bios_set_action="workload-profile")
    _add_host_target(bios_set_wl, required=True)
    bios_set_wl.add_argument("profile", choices=WORKLOAD_PROFILES, metavar="PROFILE",
                             help=f"One of: {', '.join(WORKLOAD_PROFILES)}")

    network_p = subparsers.add_parser("network", help="Change iLO network configuration")
    network_sub = network_p.add_subparsers(dest="network_action", metavar="ACTION")
    network_sub.required = True
    network_set = network_sub.add_parser("set", help="Change iLO dedicated NIC settings")
    network_set_sub = network_set.add_subparsers(dest="set_action", metavar="SETTING")
    network_set_sub.required = True
    set_dhcp = network_set_sub.add_parser(
        "dhcp",
        help="Switch iLO management NIC from static IP to DHCP",
        description="Patch the iLO EthernetInterface to enable DHCPv4 and reset iLO. "
                    "The iLO will reboot its network stack and obtain a new IP from DHCP. "
                    "The current static IP will be unreachable after reset.",
    )
    set_dhcp.set_defaults(command="set", set_action="dhcp")
    _add_host_target(set_dhcp, required=True)
    set_dhcp.add_argument("--confirm", action="store_true", help="Skip confirmation prompt")
    set_dhcp.add_argument("--no-reset", action="store_true", dest="no_reset",
                          help="Stage the DHCP change without resetting iLO (change will NOT persist across iLO reboots)")
    set_static = network_set_sub.add_parser(
        "static",
        help="Switch iLO management NIC from DHCP to a static IP",
        description="Patch the iLO EthernetInterface to disable DHCPv4 and assign a static IP, "
                    "then reset iLO. The current DHCP-assigned IP will be unreachable after reset.",
    )
    set_static.set_defaults(command="set", set_action="static")
    _add_host_target(set_static, required=True)
    static_ip = set_static.add_argument("--ip", metavar="ADDR", required=True, help="Static IPv4 address")
    static_ip.completer = suppress_file_completion()
    static_mask = set_static.add_argument("--mask", metavar="MASK", required=True, help="Subnet mask (e.g. 255.255.252.0)")
    static_mask.completer = suppress_file_completion()
    static_gateway = set_static.add_argument("--gateway", metavar="GW", required=True, help="Default gateway")
    static_gateway.completer = suppress_file_completion()
    static_dns = set_static.add_argument("--dns", metavar="DNS", action="append", dest="dns",
                            help="DNS server (repeat for multiple, e.g. --dns 8.8.8.8 --dns 8.8.4.4)")
    static_dns.completer = suppress_file_completion()
    set_static.add_argument("--confirm", action="store_true", help="Skip confirmation prompt")
    set_static.add_argument("--no-reset", action="store_true", dest="no_reset",
                            help="Stage the static-IP change without resetting iLO (change will NOT persist across iLO reboots)")
    set_route = network_set_sub.add_parser("route", help="Add a static route to the iLO dedicated NIC (static IP mode only)")
    set_route.set_defaults(command="set", set_action="route")
    _add_host_target(set_route, required=True)
    route_dest = set_route.add_argument("--destination", metavar="DEST", required=True, help="Destination network (e.g. 192.168.10.0)")
    route_dest.completer = suppress_file_completion()
    route_mask = set_route.add_argument("--mask", metavar="MASK", required=True, help="Subnet mask (e.g. 255.255.255.0)")
    route_mask.completer = suppress_file_completion()
    route_gateway = set_route.add_argument("--gateway", metavar="GW", required=True, help="Gateway for this route")
    route_gateway.completer = suppress_file_completion()
    set_route.add_argument("--confirm", action="store_true", help="Skip confirmation prompt")
    set_route.add_argument("--no-reset", action="store_true", dest="no_reset",
                           help="Do not reset iLO even if ResetRequired (change may not take effect immediately)")
    set_ipmi = network_set_sub.add_parser("ipmi", help="Enable or disable IPMI over LAN on iLO")
    set_ipmi.set_defaults(command="set", set_action="ipmi")
    _add_host_target(set_ipmi, required=True)
    set_ipmi.add_argument("state", choices=["enable", "disable"], metavar="STATE",
                          help="enable or disable IPMI over LAN (port 623)")

    reports_p = subparsers.add_parser("reports", help="Fleet hardware reports")
    reports_sub = reports_p.add_subparsers(dest="what", metavar="REPORT")
    reports_sub.required = True
    for report_name, aliases in (("memory", ["mem"]), ("cpu", []), ("gpu", [])):
        report_p = reports_sub.add_parser(report_name, aliases=aliases, help=f"{report_name.title()} fleet report")
        report_action = report_p.add_subparsers(dest="report_action", metavar="ACTION")
        report_action.required = True
        report_list = report_action.add_parser("list", help=f"List the {report_name} fleet report")
        report_list.set_defaults(command="report", what=report_name)
        _add_host_target(report_list, required=False, allow_hosts_from=True)

    subparsers.add_parser(
        "init",
        help="Guided setup of inventory.ini (alias for 'proliant setup')",
        description="Guided step-by-step setup of inventory.ini. Alias for 'proliant setup'.",
    ).set_defaults(command="init")

    return parser


def main(argv: list[str] | None = None) -> None:
    from proliant.common.display import set_output_mode, OutputMode
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
        await _run_init()
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
        elif args.set_action == "static":
            await _run_set_static(args)
        elif args.set_action == "route":
            await _run_set_route(args)
        elif args.set_action == "ipmi":
            await _run_set_ipmi(args)
    elif args.command == "power":
        await _run_power(args)
    elif args.command == "uid":
        await _run_uid(args)
    elif args.command == "boot":
        await _run_boot(args)
    elif args.command == "bios":
        await _run_bios(args)
    elif args.command == "license":
        await _run_license(args)


def _load_hosts_or_exit(name: str | None, hosts_from: str | None = None) -> list[dict]:
    """Resolve target hosts — supports single, comma-separated, and --hosts-from."""
    try:
        if hosts_from or (name and "," in name):
            return resolve_hosts(name, hosts_from, load_hosts)
        return load_hosts(name=name)
    except FileNotFoundError:
        from rich.console import Console

        console = Console()
        console.print(
            "\n[red]No inventory.ini found.[/red] A config file is needed to connect to your iLO servers."
        )
        console.print("  Run [bold]proliant setup[/bold] to add your servers (guided, tests each connection).\n")
        sys.exit(1)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


async def _run_init() -> None:
    """Backward-compatible alias for 'proliant ilo init' -- delegates to the
    top-level 'proliant setup' wizard, which handles both iLO and OneView
    entries in the same inventory.ini.
    """
    from proliant.setup.wizard import run_setup_wizard

    await run_setup_wizard()


async def _manager_network_targets(client: ILOClient) -> tuple[str, str]:
    manager_uri = await client.get_manager_uri()
    manager = await client.get(manager_uri)

    eth_collection_uri = manager.get("EthernetInterfaces", {}).get("@odata.id")
    if not eth_collection_uri:
        raise RuntimeError("Manager has no EthernetInterfaces collection")

    members = (await client.get(eth_collection_uri)).get("Members", [])
    if not members:
        raise RuntimeError("Manager EthernetInterfaces collection is empty")

    interface_uri = members[0].get("@odata.id")
    if not interface_uri:
        raise RuntimeError("Manager EthernetInterface member is missing @odata.id")

    reset_target = (manager.get("Actions") or {}).get("#Manager.Reset", {}).get("target")
    if not reset_target:
        raise RuntimeError("Manager.Reset action is not available on this iLO")

    return interface_uri, reset_target


async def _run_set_dhcp(args: argparse.Namespace) -> None:
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    if len(hosts) > 1 and not getattr(args, "confirm", False):
        print(f"This will switch {len(hosts)} iLO(s) from static IP to DHCP.")
        print("Re-run with --confirm to proceed, or use --host <name> to target one server.")
        sys.exit(1)

    do_reset = not getattr(args, "no_reset", False)

    for host in hosts:
        name = host["name"]
        try:
            async with ilo_session(host, show_hint=True) as client:
                interface_uri, reset_target = await _manager_network_targets(client)
                # Check current state first
                data = await client.get(interface_uri)
                ni = (data.get("IPv4Addresses") or [{}])[0]
                origin = ni.get("AddressOrigin", "Unknown")
                current_ip = ni.get("Address", "?")
                oem_hpe = (data.get("Oem") or {}).get("Hpe", {})
                config_state = oem_hpe.get("ConfigurationSettings", "Current")

                # "SomePendingReset" means PATCH was already applied, just needs reset
                pending_reset = config_state == "SomePendingReset"

                if origin == "DHCP" and current_ip != "0.0.0.0" and not pending_reset:
                    print(f"[{name}] Already using DHCP (current IP: {current_ip}) — skipping.")
                    continue

                if pending_reset:
                    # PATCH already done (e.g. from a previous run), skip straight to reset
                    print(f"[{name}] DHCP change is already staged (ConfigurationSettings: SomePendingReset).")
                    if not do_reset:
                        print(f"[{name}] Run without --no-reset to complete the change.")
                        continue
                else:
                    reset_note = " iLO will reset and the current IP will be lost." if do_reset else \
                                 " WARNING: --no-reset specified; change will NOT persist across iLO reboots."
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
                    result = await client.patch(interface_uri, payload)
                    # iLO wraps responses in an "error" envelope.
                    # "Success" = accepted. "ResetRequired" = accepted, reset needed (also a success).
                    # Any other MessageId is a real error.
                    ext = result.get("error", {})
                    msgs = ext.get("@Message.ExtendedInfo", [])
                    _OK = ("Success", "ResetRequired", "SystemResetRequired")
                    is_success = not ext or any(
                        any(s in m.get("MessageId", "") for s in _OK) for m in msgs
                    )
                    if not is_success:
                        msg = ext.get("message", str(result))
                        details = "; ".join(m.get("MessageId", "") for m in msgs)
                        print(f"[{name}] ERROR from iLO: {msg} ({details})", file=sys.stderr)
                        continue

                if do_reset:
                    print(f"[{name}] ✓ DHCP staged. Resetting iLO — current IP {current_ip} will be lost...")
                    await client.post(reset_target, {"ResetType": "GracefulRestart"})
                    print(f"[{name}] iLO reset triggered. It will come up with a DHCP-assigned IP.")
                else:
                    print(f"[{name}] ✓ DHCP staged (--no-reset). WARNING: change will revert on next iLO reboot.")
        except Exception as exc:
            print(f"[{name}] ERROR: {exc}", file=sys.stderr)


async def _run_set_static(args: argparse.Namespace) -> None:
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    if len(hosts) > 1 and not getattr(args, "confirm", False):
        print(f"This will switch {len(hosts)} iLO(s) to static IP {args.ip}.")
        print("Re-run with --confirm to proceed, or use --host <name> to target one server.")
        sys.exit(1)

    do_reset = not getattr(args, "no_reset", False)
    target_ip   = args.ip.strip()
    target_mask = args.mask.strip()
    target_gw   = args.gateway.strip()
    dns_servers = [s.strip() for s in (args.dns or [])]
    if not dns_servers:
        print("Warning: no --dns specified. iLO will have no DNS servers after switching to static.")

    for host in hosts:
        name = host["name"]
        try:
            async with ilo_session(host, show_hint=True) as client:
                interface_uri, reset_target = await _manager_network_targets(client)
                data = await client.get(interface_uri)

                ni = (data.get("IPv4Addresses") or [{}])[0]
                origin     = ni.get("AddressOrigin", "Unknown")
                current_ip = ni.get("Address", "?")
                oem_hpe    = (data.get("Oem") or {}).get("Hpe", {})
                config_state = oem_hpe.get("ConfigurationSettings", "Current")
                pending_reset = config_state == "SomePendingReset"

                # Check if already static at the target IP
                sta = (data.get("IPv4StaticAddresses") or [{}])[0]
                already_static_target = (
                    origin != "DHCP"
                    and sta.get("Address") == target_ip
                    and not pending_reset
                )
                if already_static_target:
                    print(f"[{name}] Already using static IP {target_ip} — skipping.")
                    continue

                # Previous run staged this exact static IP — skip PATCH, just reset
                staged_this_ip = (
                    pending_reset
                    and sta.get("Address") == target_ip
                )
                if staged_this_ip:
                    print(f"[{name}] Static IP {target_ip} already staged (ConfigurationSettings: SomePendingReset).")
                    if not do_reset:
                        print(f"[{name}] Run without --no-reset to complete the change.")
                        continue
                else:
                    reset_note = f" iLO will reset — current IP {current_ip} will be unreachable." if do_reset else \
                                 " WARNING: --no-reset specified; change will NOT persist across iLO reboots."
                    if not getattr(args, "confirm", False):
                        ans = input(
                            f"[{name}] Current IP: {current_ip} ({origin}). "
                            f"Switch to static {target_ip}?{reset_note} [y/N] "
                        )
                        if ans.strip().lower() != "y":
                            print(f"[{name}] Skipped.")
                            continue

                    # Dual-layer PATCH: disable DHCP + set static address (all three fields required)
                    # Pad DNS list to 3 entries with "0.0.0.0" as iLO expects exactly 3 slots
                    dns_padded = (dns_servers + ["0.0.0.0", "0.0.0.0", "0.0.0.0"])[:3]
                    payload = {
                        "DHCPv4": {
                            "DHCPEnabled":    False,
                            "UseDNSServers":  False,
                            "UseDomainName":  False,
                            "UseGateway":     False,
                            "UseNTPServers":  False,
                            "UseStaticRoutes": False,
                        },
                        "IPv4StaticAddresses": [
                            {"Address": target_ip, "SubnetMask": target_mask, "Gateway": target_gw}
                        ],
                        "Oem": {
                            "Hpe": {
                                "DHCPv4": {"Enabled": False},
                                "IPv4":   {"DNSServers": dns_padded},
                            }
                        },
                    }
                    result = await client.patch(interface_uri, payload)
                    ext  = result.get("error", {})
                    msgs = ext.get("@Message.ExtendedInfo", [])
                    _OK  = ("Success", "ResetRequired", "SystemResetRequired")
                    is_success = not ext or any(
                        any(s in m.get("MessageId", "") for s in _OK) for m in msgs
                    )
                    if not is_success:
                        msg     = ext.get("message", str(result))
                        details = "; ".join(m.get("MessageId", "") for m in msgs)
                        print(f"[{name}] ERROR from iLO: {msg} ({details})", file=sys.stderr)
                        continue

                if do_reset:
                    print(f"[{name}] ✓ Static IP {target_ip} staged. Resetting iLO — current IP {current_ip} will be lost...")
                    await client.post(reset_target, {"ResetType": "GracefulRestart"})
                    print(f"[{name}] iLO reset triggered. It will come up at {target_ip}.")
                else:
                    print(f"[{name}] ✓ Static IP {target_ip} staged (--no-reset). WARNING: change will revert on next iLO reboot.")
        except Exception as exc:
            print(f"[{name}] ERROR: {exc}", file=sys.stderr)


async def _run_set_route(args: argparse.Namespace) -> None:
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    dest    = args.destination.strip()
    mask    = args.mask.strip()
    gateway = args.gateway.strip()

    for host in hosts:
        name = host["name"]
        try:
            async with ilo_session(host, show_hint=True) as client:
                interface_uri, reset_target = await _manager_network_targets(client)
                data    = await client.get(interface_uri)
                oem_hpe = (data.get("Oem") or {}).get("Hpe", {})

                # Warn if DHCP is active — routes are locked out on iLO when DHCP is enabled
                dhcp_on = (data.get("DHCPv4") or {}).get("DHCPEnabled", False)
                if dhcp_on:
                    print(f"[{name}] WARNING: DHCP is enabled — iLO does not allow static routes in DHCP mode.")
                    continue

                # Read existing routes (3 fixed slots)
                existing: list[dict] = (oem_hpe.get("IPv4") or {}).get("StaticRoutes") or []
                # Normalise to exactly 3 slots, filling missing with empty
                _empty = {"Destination": "0.0.0.0", "SubnetMask": "0.0.0.0", "Gateway": "0.0.0.0"}
                slots: list[dict] = [dict(r) for r in existing[:3]]
                while len(slots) < 3:
                    slots.append(dict(_empty))

                # Check duplicate
                for r in slots:
                    if r.get("Destination") == dest and r.get("SubnetMask") == mask:
                        print(f"[{name}] Route to {dest}/{mask} already exists (gateway {r.get('Gateway')}) — skipping.")
                        break
                else:
                    # Find first empty slot
                    slot_idx = next(
                        (i for i, r in enumerate(slots) if r.get("Destination") in (None, "", "0.0.0.0")),
                        None,
                    )
                    if slot_idx is None:
                        print(f"[{name}] All 3 static route slots are occupied. Remove one before adding.", file=sys.stderr)
                        continue

                    if not getattr(args, "confirm", False):
                        ans = input(f"[{name}] Add route {dest}/{mask} via {gateway} (slot {slot_idx + 1}/3)? [y/N] ")
                        if ans.strip().lower() != "y":
                            print(f"[{name}] Skipped.")
                            continue

                    slots[slot_idx] = {"Destination": dest, "SubnetMask": mask, "Gateway": gateway}
                    payload = {"Oem": {"Hpe": {"IPv4": {"StaticRoutes": slots}}}}
                    result  = await client.patch(interface_uri, payload)
                    ext     = result.get("error", {})
                    msgs    = ext.get("@Message.ExtendedInfo", [])
                    _OK     = ("Success", "ResetRequired", "SystemResetRequired")
                    is_success = not ext or any(
                        any(s in m.get("MessageId", "") for s in _OK) for m in msgs
                    )
                    if not is_success:
                        details = "; ".join(m.get("MessageId", "") for m in msgs)
                        print(f"[{name}] ERROR from iLO: {ext.get('message', str(result))} ({details})", file=sys.stderr)
                        continue

                    needs_reset = any("ResetRequired" in m.get("MessageId", "") for m in msgs)
                    if needs_reset and not getattr(args, "no_reset", False):
                        print(f"[{name}] ✓ Route added. iLO requires reset — resetting now...")
                        await client.post(reset_target, {"ResetType": "GracefulRestart"})
                        print(f"[{name}] iLO reset triggered.")
                    elif needs_reset:
                        print(f"[{name}] ✓ Route added (--no-reset). Run without --no-reset to apply immediately.")
                    else:
                        print(f"[{name}] ✓ Route {dest}/{mask} via {gateway} added.")
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
    noun = "host" if len(hosts) == 1 else "hosts"
    with get_console().status(f"[dim]Querying {len(hosts)} {noun}…[/dim]"):
        results = await _run_parallel_async(hosts, fetch_fn)

    # --json output mode: emit structured data and return
    if get_output_mode() == OutputMode.JSON:
        _print_json_results(what, results)
        return

    if raw:
        _print_raw_table(results)
        return

    printers = {
        "firmwares":    lambda r: print_fleet_table(r, fields=getattr(args, "fields", None)),
        "nic_host":     print_network_table,
        "nic_ilo":      print_nic_ilo_table,
        "nic":          lambda r: _print_component_table(r, "NIC Link Status + MAC"),
        "storage":      lambda r: _print_component_table(r, "Storage Firmware"),
        "cpu":          lambda r: _print_component_table(r, "CPU Info"),
        "memory":       lambda r: _print_component_table(r, "Memory Info"),
        "com":          lambda r: _print_component_table(r, "HPE Compute Ops Management"),
        "full":         print_full_table,
        "disk_map":     print_disk_map_table,
        "servers":      lambda r: print_servers_table(
            r, {h["name"]: h["url"].replace("https://", "").replace("http://", "") for h in hosts}
        ),
        "serial":       print_serial_table,
        "update_method": print_update_method_table,
        "license":      print_license_table,
    }
    printers[what](results)


async def _run_upgrade(args: argparse.Namespace) -> None:
    action = args.upgrade_action
    dry_run = getattr(args, "dry_run", False)
    host = _load_hosts_or_exit(args.host)[0]

    if action == "auto":
        if not args.host:
            print("ERROR: 'proliant ilo upgrade' requires --host <name>", file=sys.stderr)
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


async def _run_set_ipmi(args: argparse.Namespace) -> None:
    host = _load_hosts_or_exit(args.host)[0]
    enabled = args.state == "enable"
    try:
        async with ilo_session(host, show_hint=True) as client:
            resp = await client.patch(
                "/redfish/v1/Managers/1/NetworkProtocol/",
                {"IPMI": {"ProtocolEnabled": enabled}},
            )
            msg_id = resp.get("error", {}).get("@Message.ExtendedInfo", [{}])[0].get("MessageId", "")
            if msg_id and "Success" not in msg_id:
                raise RuntimeError(f"Unexpected iLO response: {msg_id}")
    except ServerDownOrUnreachableError as exc:
        print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    state_str = "enabled" if enabled else "disabled"
    print(f"✓ IPMI over LAN {state_str} on {host['name']} (port 623)")


async def _run_uid(args: argparse.Namespace) -> None:
    host = _load_hosts_or_exit(args.host)[0]
    turn_on = args.uid_action == "on"
    try:
        async with ilo_session(host, show_hint=True) as client:
            sys_uri = await client.get_system_uri()
            system  = await client.get(sys_uri)
            # iLO 7 uses LocationIndicatorActive (bool); iLO 6 uses IndicatorLED (str)
            if "LocationIndicatorActive" in system:
                payload = {"LocationIndicatorActive": turn_on}
            else:
                payload = {"IndicatorLED": "Lit" if turn_on else "Off"}
            resp = await client.patch(sys_uri, payload)
            msg_id = resp.get("error", {}).get("@Message.ExtendedInfo", [{}])[0].get("MessageId", "")
            if msg_id and "Success" not in msg_id:
                raise RuntimeError(f"Unexpected iLO response: {msg_id}")
    except ServerDownOrUnreachableError as exc:
        print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    state_str = "on" if turn_on else "off"
    print(f"✓ UID light turned {state_str} on {host['name']}")


async def _run_power(args: argparse.Namespace) -> None:
    action = args.power_action
    dry_run = getattr(args, "dry_run", False)
    host = _load_hosts_or_exit(args.host)[0]

    try:
        async with ilo_session(host, show_hint=True) as client:
            if action == "reset":
                result = await reset_server(
                    client,
                    reset_type=getattr(args, "reset_type", "GracefulRestart"),
                    dry_run=dry_run,
                )
            elif action == "on":
                result = await power_on(client, dry_run=dry_run)
            elif action == "off":
                result = await force_off(client, dry_run=dry_run)
            elif action == "shutdown":
                result = await graceful_shutdown(client, dry_run=dry_run)
            else:  # pragma: no cover - parser restricts this
                raise ValueError(f"Unsupported power action: {action}")
    except ServerDownOrUnreachableError as exc:
        print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if get_output_mode() == OutputMode.JSON:
        print_json({"host": host["name"], "action": action, **result})
        return

    status = result["status"]
    reset_type = result["reset_type"]
    if status == "dry-run":
        get_console().print(
            f"[yellow][dry-run][/yellow] Would send [bold]{reset_type}[/bold] to "
            f"[bold]{host['name']}[/bold] via [dim]{result['url']}[/dim]"
        )
        return

    get_console().print(
        f"[green]✓[/green] Sent [bold]{reset_type}[/bold] to "
        f"[bold]{host['name']}[/bold]"
    )


async def _run_boot(args: argparse.Namespace) -> None:
    action = args.boot_action
    host = _load_hosts_or_exit(args.host)[0]

    try:
        async with ilo_session(host, show_hint=True) as client:
            if action == "show":
                result = await fetch_boot_order(client)
            elif action == "pxe":
                result = await set_one_time_pxe(
                    client,
                    port=getattr(args, "port", None),
                    dry_run=getattr(args, "dry_run", False),
                )
            else:  # pragma: no cover - parser restricts this
                raise ValueError(f"Unsupported boot action: {action}")
    except ServerDownOrUnreachableError as exc:
        print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if get_output_mode() == OutputMode.JSON:
        print_json({"host": host["name"], "action": action, **result})
        return

    console = get_console()
    if action == "show":
        summary = make_table("", box_style=None, show_header=False, padding=(0, 2))
        summary.add_column(style="dim", no_wrap=True)
        summary.add_column()
        summary.add_row("Mode", result["mode"])
        summary.add_row("Override Enabled", result["override_enabled"])
        summary.add_row("Override Target", result["override_target"])
        if result["uefi_target"] not in ("", "None", "—"):
            summary.add_row("UEFI Target", result["uefi_target"])
        console.print(summary)
        console.print()

        order_table = make_table(
            "Boot Order",
            ("Pos", {"justify": "right", "no_wrap": True, "style": "cyan"}),
            ("Ref", {"no_wrap": True, "style": "green"}),
            ("Type", {"no_wrap": True}),
            ("Entry", {}),
            box_style=None,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        for entry in result["order"]:
            pos = str(entry["position"])
            if entry["position"] == 1:
                pos = f"{pos}*"
            order_table.add_row(pos, entry["reference"], entry["kind"], entry["display_name"])
        console.print(order_table)

        if result["pxe_ipv4"]:
            console.print()
            pxe_table = make_table(
                "PXE IPv4 Targets",
                ("Ref", {"no_wrap": True, "style": "green"}),
                ("Port", {"no_wrap": True, "style": "cyan"}),
                ("MAC", {"no_wrap": True}),
                ("Entry", {}),
                box_style=None,
                show_header=True,
                header_style="bold",
                padding=(0, 1),
            )
            for entry in result["pxe_ipv4"]:
                pxe_table.add_row(
                    entry["reference"],
                    entry["port_hint"] or "—",
                    entry["mac"] or "—",
                    entry["display_name"],
                )
            console.print(pxe_table)

        if result.get("desired_boot_devices"):
            console.print()
            desired_table = make_table(
                "Pending Desired Boot Devices",
                ("Port", {"no_wrap": True, "style": "cyan"}),
                ("MAC", {"no_wrap": True}),
                ("Entry", {}),
                box_style=None,
                show_header=True,
                header_style="bold",
                padding=(0, 1),
            )
            for entry in result["desired_boot_devices"]:
                desired_table.add_row(
                    entry.get("port_hint") or "—",
                    entry.get("mac") or "—",
                    entry.get("display_name") or entry.get("correlatable_id") or "—",
                )
            console.print(desired_table)

        if result.get("persistent_order"):
            console.print()
            persist_table = make_table(
                "Persistent Boot Config Order",
                ("Pos", {"justify": "right", "no_wrap": True, "style": "cyan"}),
                ("Device", {"min_width": 30}),
                box_style=None,
                show_header=True,
                header_style="bold",
                padding=(0, 1),
            )
            for i, entry in enumerate(result["persistent_order"], start=1):
                persist_table.add_row(str(i), entry)
            console.print(persist_table)
        return

    selected = result.get("selected")
    if result["status"] == "dry-run":
        message = "[yellow][dry-run][/yellow] Would set one-time "
    else:
        message = "[green]✓[/green] Set one-time "
    message += "[bold]PXE IPv4[/bold] boot"
    if selected:
        message += f" via [bold]{selected['display_name']}[/bold]"
    message += " [dim](pending until next reset)[/dim]"
    console.print(message)


async def _run_bios(args: argparse.Namespace) -> None:
    host = _load_hosts_or_exit(args.host)[0]
    action = args.bios_action
    try:
        async with ilo_session(host, show_hint=True) as client:
            if action == "show":
                pending = getattr(args, "pending", False)
                attrs = await fetch_bios(client, pending=pending)
                for line in format_bios(attrs, host["name"], pending=pending):
                    print(line)
            elif action == "set":
                if args.bios_set_action == "workload-profile":
                    profile = await set_workload_profile(client, args.profile)
                    print(f"✓ WorkloadProfile set to '{profile}' (staged — reboot to apply)")
                elif args.bios_set_action == "serial-console":
                    await set_serial_console(
                        client,
                        port=getattr(args, "port", None),
                        ems=getattr(args, "ems", None),
                        vsp=getattr(args, "vsp", None),
                    )
                    changes = []
                    if getattr(args, "port", None):
                        changes.append(f"SerialConsolePort={args.port}")
                    if getattr(args, "ems", None):
                        changes.append(f"EmsConsole={args.ems}")
                    if getattr(args, "vsp", None):
                        changes.append(f"VirtualSerialPort={args.vsp}")
                    print(f"✓ Serial console updated: {', '.join(changes)} (staged — reboot to apply)")
    except ServerDownOrUnreachableError as exc:
        print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
        sys.exit(1)
    except (RuntimeError, TimeoutError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


async def _run_license(args: argparse.Namespace) -> None:
    action = args.license_action
    if action in ("list", "describe"):
        await _run_get(args)
    elif action == "set":
        host = _load_hosts_or_exit(args.host)[0]
        try:
            async with ilo_session(host, show_hint=True) as client:
                await apply_license_key(client, args.key)
        except ServerDownOrUnreachableError as exc:
            print(f"ERROR: {host['name']} unreachable: {exc}", file=sys.stderr)
            sys.exit(1)
        except (RuntimeError, TimeoutError, ValueError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"✓ License key applied to {host['name']}")


async def _cmd_describe(args: argparse.Namespace) -> None:
    hosts = _load_hosts_or_exit(getattr(args, "host", None) or getattr(args, "name", None))
    if not hosts:
        get_console().print("[red]No host found.[/red]")
        sys.exit(1)
    if getattr(args, "ilo_nic", False):
        if getattr(args, "raw", False):
            with get_console().status(f"[dim]Connecting to {hosts[0]['name']} ({hosts[0]['url']})…[/dim]"):
                results = await _run_parallel_async(hosts[:1], inventory.fetch_ilo_nic_raw)
            _print_raw_table(results)
        else:
            await run_describe_ilo_nic(hosts[0])
    elif getattr(args, "firmware_update", False):
        await run_describe_fw_update(hosts[0])
    else:
        await run_describe(hosts[0])


if __name__ == "__main__":
    main()
