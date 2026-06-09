"""
pcli.ilo.printers
~~~~~~~~~~~~~~~~~
Table and text printers for iLO CLI commands.

Extracted from cli.py to keep the main CLI module focused on argument parsing
and command dispatch.
"""
from __future__ import annotations

import pcli.ilo.inventory as inventory
from pcli.ilo.config import (
    COL_ILO_WIDTH,
    COL_NAME_WIDTH,
    COL_NIC_WIDTH,
    COL_SERVER_WIDTH,
)
from pcli.common.display import print_json


# ---------------------------------------------------------------------------
# JSON output helper
# ---------------------------------------------------------------------------

def _print_json_results(what: str, results: list[tuple[str, str | None, list]]) -> None:
    """Emit structured JSON for --json output mode (pipeable to jq/ConvertFrom-Json)."""
    output = []
    for host_name, error, rows in results:
        if error:
            output.append({"Server": host_name, "error": error})
        elif what == "firmwares":
            entry = {"Server": host_name}
            entry.update(dict(rows))
            output.append(entry)
        elif what in ("serial", "update_method"):
            if what == "serial":
                entry = {"Server": host_name}
                entry.update(dict(rows))
                output.append(entry)
            else:
                for item in rows:
                    output.append({"Server": host_name, **item})
        else:
            for item in rows:
                if isinstance(item, dict):
                    output.append({"Server": host_name, **item})
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    output.append({"Server": host_name, "Name": item[0], "Value": item[1]})
                else:
                    output.append({"Server": host_name, "data": item})
    print_json(output)


# ---------------------------------------------------------------------------
# Plain-text table printers
# ---------------------------------------------------------------------------

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


def print_network_table(results: list[tuple[str, str | None, list]]) -> None:
    normalized: list[tuple[str, str | None, list[tuple[str, str, str, str, str, str, str]]]] = []
    server_w = len("Server")
    name_w = 44
    part_w = len("Part #")
    loc_w = len("Location")
    port_w = len("Port")
    mac_w = len("MAC")
    link_w = len("Link")
    ver_w = len("Version")

    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        server_w = max(server_w, len(host_name))
        if error:
            normalized.append((host_name, error, []))
            continue

        norm_rows: list[tuple[str, str, str, str, str, str, str]] = []
        for row in rows:
            if isinstance(row, dict):
                name = str(row.get("Name", "N/A"))
                part = str(row.get("PartNumber", "N/A"))
                location = str(row.get("Location", "N/A"))
                port = str(row.get("Port", "N/A"))
                mac = str(row.get("MACAddress", "N/A"))
                link = str(row.get("LinkStatus", "N/A"))
                version = str(row.get("Version", row.get("Value", "N/A")))
            elif isinstance(row, (list, tuple)) and len(row) == 2:
                name = str(row[0])
                version = str(row[1])
                part = "N/A"
                location = "N/A"
                port = "N/A"
                mac = "N/A"
                link = "N/A"
            else:
                name = str(row)
                part = "N/A"
                location = "N/A"
                port = "N/A"
                mac = "N/A"
                link = "N/A"
                version = "N/A"
            part_w = max(part_w, len(part))
            loc_w = max(loc_w, len(location))
            port_w = max(port_w, len(port))
            mac_w = max(mac_w, len(mac))
            link_w = max(link_w, len(link))
            ver_w = max(ver_w, len(version))
            norm_rows.append((name, part, location, port, mac, link, version))
        normalized.append((host_name, None, norm_rows))

    total_w = server_w + name_w + part_w + loc_w + port_w + mac_w + link_w + ver_w + 21
    print("--- NIC Firmware ---")
    print(
        f"{'Server':<{server_w}}   "
        f"{'Name':<{name_w}}   "
        f"{'Part #':<{part_w}}   "
        f"{'Location':<{loc_w}}   "
        f"{'Port':<{port_w}}   "
        f"{'MAC':<{mac_w}}   "
        f"{'Link':<{link_w}}   "
        f"{'Version':<{ver_w}}"
    )
    print("-" * total_w)
    for host_name, error, rows in normalized:
        if error:
            print(
                f"{host_name:<{server_w}}   "
                f"{'ERROR':<{name_w}}   "
                f"{'N/A':<{part_w}}   "
                f"{'N/A':<{loc_w}}   "
                f"{'N/A':<{port_w}}   "
                f"{'N/A':<{mac_w}}   "
                f"{'N/A':<{link_w}}   "
                f"{error}"
            )
            continue
        prev_card_key: tuple[str, str, str, str] | None = None
        for i, (name, part, location, port, mac, link, version) in enumerate(rows):
            label = host_name if i == 0 else ""
            card_key = (name, part, location, version)
            display_name = _truncate(name, name_w) if card_key != prev_card_key else ""
            print(
                f"{label:<{server_w}}   "
                f"{display_name:<{name_w}}   "
                f"{part:<{part_w}}   "
                f"{location:<{loc_w}}   "
                f"{port:<{port_w}}   "
                f"{mac:<{mac_w}}   "
                f"{link:<{link_w}}   "
                f"{version:<{ver_w}}"
            )
            prev_card_key = card_key


def _truncate(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def print_nic_ilo_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print iLO dedicated NIC summary: LLDP status, neighbor info, IPv4."""
    server_w = len("Server")
    lldp_w = len("LLDP")
    sys_w = len("Neighbor System")
    port_w = len("Neighbor Port")
    nb_ip_w = len("Neighbor IPv4")
    ip_w = len("iLO IPv4")
    mac_w = len("MAC")
    link_w = len("Link")

    rows_out: list[tuple[str, str | None, dict]] = []
    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        server_w = max(server_w, len(host_name))
        if error:
            rows_out.append((host_name, error, {}))
            continue
        row = rows[0] if rows else {}
        lldp_w   = max(lldp_w,   len(str(row.get("lldp_enabled",    "—"))))
        sys_w    = max(sys_w,    len(str(row.get("neighbor_system",  "—"))))
        port_w   = max(port_w,   len(str(row.get("neighbor_port",    "—"))))
        nb_ip_w  = max(nb_ip_w,  len(str(row.get("neighbor_ipv4",   "—"))))
        ip_w     = max(ip_w,     len(str(row.get("ipv4",             "—"))))
        mac_w    = max(mac_w,    len(str(row.get("mac",              "—"))))
        link_w   = max(link_w,   len(str(row.get("link_status",      "—"))))
        rows_out.append((host_name, None, row))

    total_w = server_w + lldp_w + sys_w + port_w + nb_ip_w + ip_w + mac_w + link_w + 21
    print("--- iLO NIC ---")
    print(
        f"{'Server':<{server_w}}   "
        f"{'LLDP':<{lldp_w}}   "
        f"{'Neighbor System':<{sys_w}}   "
        f"{'Neighbor Port':<{port_w}}   "
        f"{'Neighbor IPv4':<{nb_ip_w}}   "
        f"{'iLO IPv4':<{ip_w}}   "
        f"{'MAC':<{mac_w}}   "
        f"{'Link':<{link_w}}"
    )
    print("-" * total_w)
    for host_name, error, row in rows_out:
        if error:
            print(f"{host_name:<{server_w}}   ERROR: {error}")
            continue
        lldp      = str(row.get("lldp_enabled",    "—"))
        neighbor  = str(row.get("neighbor_system",  "—"))
        nb_port   = str(row.get("neighbor_port",    "—"))
        nb_ip     = str(row.get("neighbor_ipv4",    "—"))
        ipv4      = str(row.get("ipv4",             "—"))
        mac       = str(row.get("mac",              "—"))
        link      = str(row.get("link_status",      "—"))
        print(
            f"{host_name:<{server_w}}   "
            f"{lldp:<{lldp_w}}   "
            f"{neighbor:<{sys_w}}   "
            f"{nb_port:<{port_w}}   "
            f"{nb_ip:<{nb_ip_w}}   "
            f"{ipv4:<{ip_w}}   "
            f"{mac:<{mac_w}}   "
            f"{link:<{link_w}}"
        )


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
    _key_map = {k.lower(): k for k in all_keys}

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


def print_servers_table(results: list[tuple[str, str | None, list]]) -> None:
    """Print server list with Serial, OS Name, iLO Name, Model, IP — Rich styled."""
    from rich import box
    from rich.table import Table
    from pcli.common.display import get_console

    console = get_console()
    table = Table(
        box=box.SIMPLE_HEAD,
        show_lines=False,
        expand=True,
    )
    table.add_column("Serial",   style="grey70",  no_wrap=True, min_width=13)
    table.add_column("OS Name",  style="default", ratio=4, overflow="fold")
    table.add_column("iLO Name", style="green",   ratio=4, overflow="fold")
    table.add_column("Model",    style="grey70",  no_wrap=True, max_width=13)
    table.add_column("IP",       style="white",   no_wrap=True, min_width=15)

    for host_name, error, rows in sorted(results, key=lambda r: r[0]):
        if error:
            table.add_row(
                f"[grey70]{host_name}[/grey70]", "—", "—", "—",
                f"[red]{error}[/red]",
            )
            continue
        d = dict(rows)
        serial   = d.get("Serial", "—")
        os_name  = d.get("OS_Name", "")
        ilo_name = d.get("iLO_Name", "") or "—"
        model    = d.get("Model", "—")
        ip_addr  = d.get("IP", "—")

        os_cell = f"[cyan]{os_name}[/cyan]" if os_name else "[cyan]—[/cyan]"
        table.add_row(serial, os_cell, ilo_name, model, ip_addr)

    count = len(results)
    console.print(f"\n[bold]iLO Servers ({count} total)[/bold]")
    console.print(table)


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


def print_license_table(results: list[tuple[str, str | None, list]]) -> None:
    keys = ("License", "Type", "Key")
    server_data: dict[str, dict[str, str]] = {}
    for host_name, error, rows in results:
        if error:
            server_data[host_name] = {k: "ERROR" for k in keys}
        else:
            server_data[host_name] = dict(rows)

    srv_w = max(len("Server"), max(len(n) for n in server_data))
    col_w: dict[str, int] = {k: len(k) for k in keys}
    for vals in server_data.values():
        for key in keys:
            col_w[key] = max(col_w[key], len(vals.get(key, "N/A")))

    header = f"{'Server':<{srv_w}}" + "".join(f"   {key:<{col_w[key]}}" for key in keys)
    print("\n--- iLO License ---")
    print(header)
    print("-" * len(header))
    for host_name in sorted(server_data):
        vals = server_data[host_name]
        row = f"{host_name:<{srv_w}}" + "".join(f"   {vals.get(key, 'N/A'):<{col_w[key]}}" for key in keys)
        print(row)


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


# ---------------------------------------------------------------------------
# Firmware upgrade progress printers
# ---------------------------------------------------------------------------

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
