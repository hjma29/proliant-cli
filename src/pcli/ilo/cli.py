"""
hpeilo.cli
~~~~~~~~~~
Command-line interface: subcommand-based argument parsing, parallel host
queries, and table printing.

This is the only module that should import argparse, sys, or print anything.
All other modules are pure library code with no side effects at import time.

Usage::

    pcli ilo show fleet                          Fleet summary across all servers
    pcli ilo show ilo                            iLO firmware version
    pcli ilo show network                        NIC firmware versions
    pcli ilo show nic                            NIC link status + MAC
    pcli ilo show storage                        Storage controllers + drives
    pcli ilo show cpu                            CPU models + microcode
    pcli ilo show memory                         DIMM details
    pcli ilo show com                            HPE Compute Ops Management status
    pcli ilo show full                           Full firmware inventory
    pcli ilo show disk-map                       Drive bay/serial map
    pcli ilo show serial                         Server model, serial number, and product ID
    pcli ilo show ilo --host dl325-gen12         Target a single server
    pcli ilo show nic --raw                      Raw JSON output

    pcli ilo upgrade --host dl325-gen12          Auto-upgrade all firmware from HPE SDR
    pcli ilo upgrade --host dl325-gen12 --dry-run             Preview only
    pcli ilo upgrade --host dl325-gen12 --reboot              Upgrade + reboot
    pcli ilo upgrade --host dl325-gen12 --component bios      BIOS/System ROM only
    pcli ilo upgrade --host dl325-gen12 --component ilo       iLO firmware only
    pcli ilo upgrade --host dl325-gen12 --component nic       NIC firmware only
    pcli ilo upgrade --host dl325-gen12 --component storage   Storage controllers only

    pcli ilo fw components --host dl325-gen12    List staged components
    pcli ilo fw queue      --host dl325-gen12    Show update task queue
    pcli ilo fw stage      --host dl325-gen12 --url <url>   Stage from URL
    pcli ilo fw flash      --host dl325-gen12 <filename>    Queue for flash
    pcli ilo fw clear      --host dl325-gen12    Clear task queue
"""

# PYTHON_ARGCOMPLETE_OK
import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib.metadata import version as _pkg_version, PackageNotFoundError

import argcomplete

from redfish.rest.v1 import ServerDownOrUnreachableError

from pcli.ilo import firmware, inventory
from pcli.ilo.client import ilo_session
from pcli.ilo.config import (
    COL_ILO_WIDTH,
    COL_NAME_WIDTH,
    COL_NIC_WIDTH,
    COL_SERVER_WIDTH,
    MAX_WORKERS,
    load_hosts,
)

# ---------------------------------------------------------------------------
# Dispatch table — maps show subcommand name to the fetch function.
# ---------------------------------------------------------------------------
_FETCH_DISPATCH: dict[str, callable] = {
    "ilo":      inventory.fetch_ilo_version,
    "network":  inventory.fetch_network_versions,
    "nic":      inventory.fetch_nic_status,
    "storage":  inventory.fetch_storage_versions,
    "cpu":      inventory.fetch_cpu_info,
    "memory":   inventory.fetch_memory_info,
    "com":      inventory.fetch_com_status,
    "full":     inventory.fetch_all_firmware,
    "disk_map": inventory.fetch_disk_map,
    "fleet":    inventory.fetch_fleet_summary,
    "serial":   inventory.fetch_serial_info,
}

_RAW_DISPATCH: dict[str, callable] = {
    "ilo":      inventory.fetch_firmware_raw,
    "network":  inventory.fetch_network_raw,
    "nic":      inventory.fetch_nic_raw,
    "storage":  inventory.fetch_storage_raw,
    "cpu":      inventory.fetch_cpu_raw,
    "memory":   inventory.fetch_memory_raw,
    "com":      inventory.fetch_com_raw,
    "full":     inventory.fetch_firmware_raw,
    "disk_map": inventory.fetch_disk_map_raw,
    "fleet":    inventory.fetch_firmware_raw,
    "serial":   inventory.fetch_serial_info,
}


# ---------------------------------------------------------------------------
# Host querying
# ---------------------------------------------------------------------------

def query_host(host: dict, fetch_fn: callable) -> tuple[str, str | None, list[tuple[str, str]]]:
    """Connect to one iLO host, run the requested fetch, and return results.

    Parameters
    ----------
    host:
        Dict with keys: name, url, username, password.
    fetch_fn:
        A callable that accepts a RedfishClient and returns list[tuple[str, str]].

    Returns
    -------
    tuple[str, str | None, list[tuple[str, str]]]
        (host_name, error_message_or_None, results_list)
        On success  error is None and results contains data.
        On failure  error contains the exception message and results is [].
    """
    try:
        with ilo_session(host) as client:
            results = fetch_fn(client)
        return host["name"], None, results
    except ServerDownOrUnreachableError as exc:
        return host["name"], f"Unreachable: {exc}", []
    except Exception as exc:  # noqa: BLE001 — surface all errors as rows, not crashes
        return host["name"], f"Error: {exc}", []


def _run_parallel_fn(hosts: list[dict], fetch_fn: callable) -> list[tuple[str, str | None, list]]:
    """Submit all hosts to the thread pool and collect results in arrival order."""
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(query_host, host, fetch_fn): host["name"] for host in hosts}
        return [future.result() for future in as_completed(futures)]


# ---------------------------------------------------------------------------
# Table printers
# ---------------------------------------------------------------------------

def _header_line(server_col: int, label_col: int, value_col: int) -> str:
    return f"{'Server':<{server_col}}   {'Name':<{label_col}}   {'Version':<{value_col}}"


def print_ilo_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print iLO firmware version table."""
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
    """Generic table printer for NIC / storage / CPU / memory rows."""
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
    """Print raw JSON responses, one block per URI."""
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        print(f"\n=== {host_name} ===")
        if error:
            print(f"ERROR: {error}")
            continue
        for uri, raw_json in rows:
            print(f"\n--- {uri} ---")
            print(raw_json)


def print_disk_map_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print SmartArray Volume → physical drive bay mapping."""
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


def print_fleet_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print fleet comparison table — one row per server, firmware versions as columns.

    Columns: Server | Model | iLO | BIOS | NIC-FW | Storage-FW
    Servers are sorted by name. This is the go-to command for a quick
    fleet-wide firmware audit.
    """
    from pcli.ilo.inventory import FLEET_KEYS

    keys = list(FLEET_KEYS)  # ["Model", "iLO", "BIOS", "NIC-FW", "Storage-FW"]

    # Collect per-server dicts
    server_data: dict[str, dict[str, str]] = {}
    for host_name, error, rows in results:
        if error:
            server_data[host_name] = {k: "ERROR" for k in keys}
            server_data[host_name]["_error"] = error
        else:
            server_data[host_name] = dict(rows)

    # Compute column widths dynamically
    col_w: dict[str, int] = {k: len(k) for k in keys}
    srv_w = max(len("Server"), max(len(n) for n in server_data))
    for vals in server_data.values():
        for k in keys:
            col_w[k] = max(col_w[k], len(vals.get(k, "N/A")))

    # Header
    header = f"{'Server':<{srv_w}}"
    for k in keys:
        header += f"   {k:<{col_w[k]}}"
    sep = "-" * len(header)

    print("\n--- Fleet Summary ---")
    print(header)
    print(sep)

    for host_name in sorted(server_data):
        vals = server_data[host_name]
        row = f"{host_name:<{srv_w}}"
        for k in keys:
            row += f"   {vals.get(k, 'N/A'):<{col_w[k]}}"
        print(row)
        if "_error" in vals:
            print(f"  {'':>{srv_w}}   {vals['_error']}")


def print_serial_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print server identity table — one row per server with Model, Serial, ProductID."""
    KEYS = ("Model", "Serial", "ProductID")

    server_data: dict[str, dict[str, str]] = {}
    for host_name, error, rows in results:
        if error:
            server_data[host_name] = {k: "ERROR" for k in KEYS}
            server_data[host_name]["_error"] = error
        else:
            server_data[host_name] = dict(rows)

    srv_w = max(len("Server"), max(len(n) for n in server_data))
    col_w: dict[str, int] = {k: len(k) for k in KEYS}
    for vals in server_data.values():
        for k in KEYS:
            col_w[k] = max(col_w[k], len(vals.get(k, "N/A")))

    header = f"{'Server':<{srv_w}}"
    for k in KEYS:
        header += f"   {k:<{col_w[k]}}"
    sep = "-" * len(header)

    print("\n--- Server Identity ---")
    print(header)
    print(sep)

    for host_name in sorted(server_data):
        vals = server_data[host_name]
        row = f"{host_name:<{srv_w}}"
        for k in KEYS:
            row += f"   {vals.get(k, 'N/A'):<{col_w[k]}}"
        print(row)
        if "_error" in vals:
            print(f"  {'':>{srv_w}}   {vals['_error']}")


def print_full_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print all firmware inventory entries."""
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


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

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

    def _host_completer(**kwargs):
        try:
            return [h["name"] for h in load_hosts()]
        except Exception:
            return []

    def _add_host(p: argparse.ArgumentParser, required: bool = False) -> None:
        p.add_argument(
            "--host", metavar="NAME", required=required,
            help="Target a single host by name (from hosts.yml)",
        ).completer = _host_completer

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # ── show ─────────────────────────────────────────────────────────────────
    show_p = subparsers.add_parser(
        "show",
        help="Show hardware/firmware inventory",
        description="Query iLO and display hardware or firmware information.",
    )
    show_sub = show_p.add_subparsers(dest="what", metavar="WHAT")
    show_sub.required = True

    _SHOW_CHOICES = {
        "fleet":    "Fleet summary: key firmware versions per server (one row per server)",
        "ilo":      "iLO firmware version",
        "network":  "NIC firmware versions",
        "nic":      "NIC link status + MAC address",
        "storage":  "Storage controller + drive firmware",
        "cpu":      "CPU model + microcode version",
        "memory":   "DIMM info + firmware revision",
        "com":      "HPE Compute Ops Management registration status",
        "full":     "Full firmware inventory",
        "disk-map": "Drive bay + serial number map (cross-ref with lsblk)",
        "serial":   "Server model, serial number, and product ID (for COM onboarding)",
    }
    for name, help_text in _SHOW_CHOICES.items():
        sp = show_sub.add_parser(name, help=help_text)
        _add_host(sp)
        sp.add_argument("--raw", action="store_true",
                        help="Print raw JSON instead of a formatted table")

    # ── upgrade ───────────────────────────────────────────────────────────────
    upgrade_p = subparsers.add_parser(
        "upgrade",
        help="Firmware upgrade and task queue management",
        description=(
            "Auto-upgrade outdated firmware from HPE SDR, with optional component filtering.\n"
            "Subcommands give access to individual staging and queue operations.\n"
            "\n"
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
    upgrade_sub.required = False          # bare 'pcli ilo upgrade' = auto-upgrade
    upgrade_p.set_defaults(upgrade_action="auto")

    # bare: pcli ilo upgrade [--host NAME] [--dry-run] [--reboot] [--component FILTER]
    _add_host(upgrade_p, required=False)
    upgrade_p.add_argument("--dry-run", action="store_true", dest="dry_run",
                           help="Preview without making changes")
    upgrade_p.add_argument("--reboot", action="store_true",
                           help="Reboot server after queuing all updates")
    upgrade_p.add_argument(
        "--component", metavar="FILTER",
        choices=["all", "ilo", "bios", "nic", "storage"],
        default="all",
        help="Limit upgrade to a specific component type: all | ilo | bios | nic | storage (default: all)",
    )

    # pcli ilo upgrade components --host NAME
    up_comp = upgrade_sub.add_parser("components",
                                     help="List staged components in iLO repository")
    _add_host(up_comp, required=True)

    # pcli ilo upgrade queue --host NAME
    up_queue = upgrade_sub.add_parser("queue", help="Show the firmware update task queue")
    _add_host(up_queue, required=True)

    # pcli ilo upgrade stage --host NAME --url URL [--dry-run]
    up_stage = upgrade_sub.add_parser("stage",
                                      help="Stage a firmware package from a URL")
    _add_host(up_stage, required=True)
    up_stage.add_argument("--url", metavar="URL", required=True,
                          help="Direct URL to .fwpkg file on HPE SDR")
    up_stage.add_argument("--dry-run", action="store_true", dest="dry_run")

    # pcli ilo upgrade flash --host NAME FILENAME [--dry-run]
    up_flash = upgrade_sub.add_parser("flash",
                                      help="Queue a staged file for flash on next reboot")
    _add_host(up_flash, required=True)
    up_flash.add_argument("filename", metavar="FILENAME",
                          help="Filename of the staged component to queue")
    up_flash.add_argument("--dry-run", action="store_true", dest="dry_run")

    # pcli ilo upgrade clear --host NAME [--dry-run]
    up_clear = upgrade_sub.add_parser("clear",
                                      help="Clear all entries from the task queue")
    _add_host(up_clear, required=True)
    up_clear.add_argument("--dry-run", action="store_true", dest="dry_run")

    # ── init ──────────────────────────────────────────────────────────────────
    subparsers.add_parser(
        "init",
        help="Create a starter hosts.yml at ~/.config/pcli/ilo/hosts.yml",
        description="Create ~/.config/pcli/ilo/hosts.yml with example entries to fill in.",
    )

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    """Parse arguments and dispatch to the appropriate handler."""
    parser = _build_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)

    if args.command == "show":
        _run_show(args)
    elif args.command == "upgrade":
        _run_upgrade(args)
    elif args.command == "init":
        _run_init()


def _load_hosts_or_exit(name: str | None) -> list[dict]:
    """Load hosts, exiting with a clear message on failure."""
    try:
        return load_hosts(name=name)
    except FileNotFoundError:
        from pcli.ilo.config import HOSTS_FILE
        print(f"ERROR: hosts.yml not found. Searched: {HOSTS_FILE}", file=sys.stderr)
        print("       Run 'pcli ilo init' to create a starter config.", file=sys.stderr)
        sys.exit(1)
    except KeyError:
        print("ERROR: hosts.yml is missing the 'ilos' key", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_init() -> None:
    """Create a starter hosts.yml at ~/.config/pcli/ilo/hosts.yml."""
    from pathlib import Path
    dest = Path.home() / ".config" / "hpeilo" / "hosts.yml"
    if dest.exists():
        print(f"Already exists: {dest}")
        print("Edit it to add or update your servers.")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        "# hpeilo hosts file\n"
        "# Add one entry per iLO server.\n"
        "ilos:\n"
        "  - name: my-server-1\n"
        "    url: https://10.0.0.1\n"
        "    username: Administrator\n"
        "    password: yourpassword\n"
        "\n"
        "  - name: my-server-2\n"
        "    url: https://10.0.0.2\n"
        "    username: Administrator\n"
        "    password: yourpassword\n"
    )
    print(f"Created: {dest}")
    print(f"Edit it to fill in your server addresses and credentials.")
    print(f"\nThen try:  pcli ilo show fleet")


def _run_show(args: argparse.Namespace) -> None:
    """Dispatch 'pcli ilo show <what>' commands."""
    # Normalise disk-map → disk_map for dict key lookup
    what = args.what.replace("-", "_")
    hosts = _load_hosts_or_exit(getattr(args, "host", None))
    raw: bool = getattr(args, "raw", False)

    fetch_fn = _RAW_DISPATCH[what] if raw else _FETCH_DISPATCH[what]
    results = _run_parallel_fn(hosts, fetch_fn)

    if raw:
        _print_raw_table(results)
        return

    _PRINTERS = {
        "fleet":    print_fleet_table,
        "ilo":      print_ilo_table,
        "network":  lambda r: _print_component_table(r, "NIC Firmware"),
        "nic":      lambda r: _print_component_table(r, "NIC Link Status + MAC"),
        "storage":  lambda r: _print_component_table(r, "Storage Firmware"),
        "cpu":      lambda r: _print_component_table(r, "CPU Info"),
        "memory":   lambda r: _print_component_table(r, "Memory Info"),
        "com":      lambda r: _print_component_table(r, "HPE Compute Ops Management"),
        "full":     print_full_table,
        "disk_map": print_disk_map_table,
        "serial":   print_serial_table,
    }
    _PRINTERS[what](results)


def _run_upgrade(args: argparse.Namespace) -> None:
    """Dispatch 'pcli ilo upgrade [ACTION]' commands."""
    action = args.upgrade_action  # "auto" | "components" | "queue" | "stage" | "flash" | "clear"
    dry_run: bool = getattr(args, "dry_run", False)

    if action == "auto":
        # Auto-upgrade requires --host
        if not args.host:
            print("ERROR: 'pcli ilo upgrade' requires --host <name>", file=sys.stderr)
            sys.exit(1)
        hosts = _load_hosts_or_exit(args.host)
        _run_fw_upgrade(hosts[0], dry_run=dry_run, reboot=getattr(args, "reboot", False),
                        component=getattr(args, "component", "all"))
        return

    # All other actions target exactly one host (required=True enforced by parser)
    hosts = _load_hosts_or_exit(args.host)
    host = hosts[0]

    try:
        with ilo_session(host) as client:
            if action == "components":
                components = firmware.get_component_repository(client)
                _print_fw_components(host["name"], components)

            elif action == "queue":
                queue = firmware.get_task_queue(client)
                _print_fw_queue(host["name"], queue)

            elif action == "stage":
                result = firmware.stage_from_uri(client, args.url, dry_run=dry_run)
                if dry_run:
                    print(f"[dry-run] Would POST to: {result['target']}")
                    print(f"[dry-run] Payload: {json.dumps(result['payload'], indent=2)}")
                else:
                    print(f"Staging initiated on {host['name']}:")
                    print(json.dumps(result, indent=2))

            elif action == "flash":
                result = firmware.add_to_task_queue(client, args.filename, dry_run=dry_run)
                if dry_run:
                    print(f"[dry-run] Would POST to: {result['target']}")
                    print(f"[dry-run] Payload: {json.dumps(result['payload'], indent=2)}")
                else:
                    print(f"Queued '{args.filename}' for flash on next reboot ({host['name']}):")
                    print(json.dumps(result, indent=2))

            elif action == "clear":
                uris = firmware.clear_task_queue(client, dry_run=dry_run)
                if dry_run:
                    if uris:
                        print(f"[dry-run] Would delete {len(uris)} task queue entries:")
                        for u in uris:
                            print(f"  {u}")
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


def _run_fw_upgrade(host: dict, *, dry_run: bool = False, reboot: bool = False,
                    component: str = "all") -> None:
    """Auto-upgrade all outdated firmware on a single host using HPE SDR.

    Parameters
    ----------
    component:
        Filter which components to upgrade:
        "all"     — everything (default)
        "ilo"     — iLO firmware only
        "bios"    — System ROM / BIOS only
        "nic"     — Network adapter firmware only
        "storage" — Storage controllers only
    """
    from rich.console import Console
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich import box
    from pcli.ilo import sdr
    from pcli.ilo.power import reset_server

    console = Console()

    # ── Step 1: Get current firmware inventory ──────────────────────────────
    with console.status(f"[bold cyan]Connecting to {host['name']}..."):
        try:
            with ilo_session(host) as client:
                all_fw_full = inventory.fetch_firmware_inventory_full(client)
                nic_fw = inventory.fetch_nic_firmware_inventory(client)
                all_fw = [(e.get("Name","N/A"), e.get("Version","N/A")) for e in all_fw_full]
                model_info = client.get(
                    __import__("pcli.ilo.client", fromlist=["get_system_uri"]).get_system_uri(client)
                ).obj.get("Model", "unknown")
        except ServerDownOrUnreachableError as exc:
            console.print(f"[red]ERROR:[/red] {host['name']} unreachable: {exc}")
            sys.exit(1)

    # ── Step 2: Detect gen + fetch SDR ──────────────────────────────────────
    try:
        gen = sdr.detect_gen(model_info)
    except ValueError as exc:
        console.print(f"[red]ERROR:[/red] {exc}")
        sys.exit(1)

    with console.status(f"[bold cyan]Fetching HPE SDR for Gen{gen}..."):
        try:
            pack_date, pack_url = sdr.latest_pack_url(gen)
            pack_components = sdr.list_pack(pack_url)
        except Exception as exc:
            console.print(f"[red]ERROR:[/red] Cannot reach HPE SDR: {exc}")
            sys.exit(1)

    # ── Step 3: Find upgrades ───────────────────────────────────────────────
    # Combine FirmwareInventory + NIC entries (NICs don't appear in FirmwareInventory)
    candidates = sdr.find_upgrades(all_fw_full + nic_fw, pack_components)

    # Only show non-updatable entries for storage controllers — skip TPM, video, drives
    _SKIP_NON_UPDATABLE = ("tpm", "video controller", "nvme drive", "ssd", "dimm", "memory",
                           "processor", "microcode", "embedded video")
    non_updatable = [
        e for e in all_fw_full
        if not e.get("Updateable", True)
        and e.get("Version", "N/A") not in ("N/A", None, "")
        and not any(k in e.get("Name", "").lower() for k in _SKIP_NON_UPDATABLE)
    ]

    # Print comparison table
    table = Table(title=f"Firmware Audit — {host['name']} ({model_info})",
                  box=box.ROUNDED, show_lines=False)
    table.add_column("Component",   style="bold")
    table.add_column("Installed",   style="yellow")
    table.add_column("SDR Latest",  style="cyan")
    table.add_column("Status")

    updates = [c for c in candidates if c.needs_update]
    up_to_date = [c for c in candidates if not c.needs_update]

    # ── Apply component filter ──────────────────────────────────────────────────
    def _component_match(c: sdr.UpgradeCandidate, comp: str) -> bool:
        if comp == "all":
            return True
        nl = c.name.lower()
        if comp == "ilo":
            return nl.startswith("ilo")
        if comp == "bios":
            return "system rom" in nl or "bios" in nl
        if comp == "nic":
            # NIC candidates have either a chip_model on their sdr, or sdr=None (no SDR pkg)
            return bool(getattr(c.sdr, "chip_model", None)) or (c.sdr is None and c.updateable)
        if comp == "storage":
            return any(k in nl for k in ("controller", "array", "boot controller", "ns204", "nvme"))
        return True

    if component != "all":
        orig_count = len(updates)
        updates = [c for c in updates if _component_match(c, component)]
        if len(updates) < orig_count:
            console.print(f"  [dim]Filtered to component=[bold]{component}[/bold]: "
                          f"{len(updates)} of {orig_count} updates selected[/dim]")

    for c in updates:
        table.add_row(c.name, c.current, c.sdr.filename,
                      "[green bold]UPDATE AVAILABLE[/green bold]")
    for c in up_to_date:
        sdr_col = c.sdr.filename if c.sdr else "—"
        status = "[dim]up to date[/dim]" if c.sdr else "[dim italic]no SDR package[/dim italic]"
        table.add_row(c.name, c.current, sdr_col, status)
    for e in non_updatable:
        table.add_row(e.get("Name","?"), e.get("Version","?"), "—",
                      "[dim italic]not updatable via iLO[/dim italic]")

    console.print()
    console.print(table)
    console.print(f"  SDR pack: [dim]{pack_date}[/dim]  |  "
                  f"[green]{len(updates)} update(s) available[/green], "
                  f"{len(up_to_date)} up to date\n")

    if not updates:
        console.print("[green]✓ All firmware is up to date.[/green]")
        return

    if dry_run:
        console.print("[yellow][dry-run] Would stage and queue:[/yellow]")
        for c in updates:
            console.print(f"  • {c.sdr.filename}  ({c.current} → SDR {c.sdr.version_str})")
        return

    # ── Step 4: Stage + queue — HPE required order: iLO → BIOS → others ──────
    # Sort: iLO first (applied immediately, no reboot), BIOS second, rest after.
    def _upgrade_priority(c: sdr.UpgradeCandidate) -> int:
        nl = c.name.lower()
        if nl.startswith("ilo"):
            return 0   # iLO: flash immediately, no reboot needed
        if "system rom" in nl or "bios" in nl:
            return 1   # BIOS: second, applied on reboot
        return 2       # Everything else: applied on same reboot as BIOS

    updates.sort(key=_upgrade_priority)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        for idx, candidate in enumerate(updates, 1):
            fname = candidate.sdr.filename
            label = f"[{idx}/{len(updates)}] {fname}"
            is_ilo = candidate.name.lower().startswith("ilo")

            task = progress.add_task(f"{label}  staging…", total=None)

            try:
                # Stage from SDR
                with ilo_session(host) as client:
                    firmware.stage_from_uri(client, candidate.sdr.url)

                progress.update(task, description=f"{label}  waiting for iLO to download…")

                # Poll until staged (iLO downloads in background)
                with ilo_session(host) as client:
                    firmware.wait_for_stage(client, fname, timeout=300, poll_interval=10)

                progress.update(task, description=f"{label}  queueing for flash…")

                # Queue for flash
                with ilo_session(host) as client:
                    firmware.add_to_task_queue(client, fname)

                progress.update(task,
                    description=f"[green]✓[/green] {label}  queued  "
                                f"({candidate.current} → {candidate.sdr.version_str})")

                # iLO applies itself immediately — wait for it to come back before continuing
                if is_ilo:
                    progress.update(task, description=f"{label}  iLO restarting (~90s)…")
                    try:
                        firmware.wait_for_online(host, offline_grace=15, timeout=180)
                        progress.update(task,
                            description=f"[green]✓[/green] {label}  iLO back online  "
                                        f"({candidate.current} → {candidate.sdr.version_str})")
                    except TimeoutError:
                        progress.update(task,
                            description=f"[yellow]⚠[/yellow] {label}  iLO restart timed out — continuing")

            except Exception as exc:
                progress.update(task,
                    description=f"[red]✗[/red] {label}  FAILED: {exc}")
                console.print(f"[red]ERROR staging {fname}: {exc}[/red]")

    # ── Step 5: Reboot (optional) ───────────────────────────────────────────
    if reboot:
        console.print(f"\n[bold yellow]Rebooting {host['name']}...[/bold yellow]")
        try:
            with ilo_session(host) as client:
                reset_server(client, reset_type="GracefulRestart")
        except Exception as exc:
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
                firmware.wait_for_online(host, offline_grace=30, timeout=600)
                progress.update(task, description="[green]✓ Server back online[/green]")
            except TimeoutError as exc:
                progress.update(task, description=f"[red]✗ {exc}[/red]")
                sys.exit(1)

        # Verify updated versions
        console.print("\n[bold]Verifying firmware versions after reboot...[/bold]")
        with ilo_session(host) as client:
            new_fw = dict(inventory.fetch_all_firmware(client))

        result_table = Table(box=box.SIMPLE)
        result_table.add_column("Component")
        result_table.add_column("Before")
        result_table.add_column("After")
        result_table.add_column("Result")
        all_ok = True
        for c in updates:
            after = new_fw.get(c.name, "?")
            ok = sdr.parse_inventory_version(after) >= c.sdr.version
            if not ok:
                all_ok = False
            result_table.add_row(
                c.name, c.current, after,
                "[green]✓ Updated[/green]" if ok else "[yellow]⚠ Check manually[/yellow]"
            )
        console.print(result_table)

        # Clear stale queue entries — iLO 7 leaves Pending tasks after UEFI applies them
        with ilo_session(host) as client:
            queue = firmware.get_task_queue(client)
            stale = [t for t in queue if t.get("State") in ("Pending", "Complete")]
            if stale:
                firmware.clear_task_queue(client)
                console.print(f"  [dim]Cleared {len(stale)} stale task(s) from queue.[/dim]")
    else:
        console.print(
            f"\n[bold yellow]Updates queued.[/bold yellow] "
            f"Reboot [bold]{host['name']}[/bold] to apply.\n"
            f"  • Use [bold]--reboot[/bold] flag to reboot automatically\n"
            f"  • Run [bold]pcli ilo upgrade queue --host {host['name']}[/bold] to check queue status"
        )


def _print_fw_components(host_name: str, components: list[dict]) -> None:
    """Print staged components table."""
    print(f"\n--- Staged Components: {host_name} ---")
    if not components:
        print("  (no components staged)")
        return
    name_w, ver_w, size_w = 50, 15, 12
    print(f"{'Name':<{name_w}}   {'Version':<{ver_w}}   {'Size (MB)':<{size_w}}")
    print("-" * (name_w + ver_w + size_w + 6))
    for c in components:
        name = c.get("Name", c.get("Filename", "unknown"))
        ver  = c.get("Version", "—")
        size = c.get("SizeBytes", 0)
        size_mb = f"{size / 1_048_576:.1f}" if size else "—"
        print(f"{name[:name_w]:<{name_w}}   {ver:<{ver_w}}   {size_mb}")


def _print_fw_queue(host_name: str, queue: list[dict]) -> None:
    """Print firmware update task queue table."""
    print(f"\n--- Update Task Queue: {host_name} ---")
    if not queue:
        print("  (task queue is empty)")
        return
    name_w, state_w, result_w = 50, 15, 20
    print(f"{'Name/Filename':<{name_w}}   {'State':<{state_w}}   {'Result':<{result_w}}")
    print("-" * (name_w + state_w + result_w + 6))
    for t in queue:
        name   = t.get("Name", t.get("Filename", "unknown"))
        state  = t.get("State", "—")
        result = t.get("Result", {})
        result_str = result.get("MessageId", "—") if isinstance(result, dict) else str(result)
        print(f"{name[:name_w]:<{name_w}}   {state:<{state_w}}   {result_str[:result_w]}")


if __name__ == "__main__":
    main()
