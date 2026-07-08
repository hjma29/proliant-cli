"""Single-server describe command for COM."""

from __future__ import annotations

import asyncio
import sys

from proliant.common.display import get_console, print_json, OutputMode, get_output_mode
from proliant.com.client import COMClient
from proliant.com.auth import COMSession

_HEALTH_STYLE = {
    "OK": "green", "WARNING": "yellow", "CRITICAL": "red",
    "REDUNDANT": "green", "NON_REDUNDANT": "yellow",
    "NOT_PRESENT": "dim", "UNKNOWN": "dim",
}


def _h(val: str) -> str:
    """Wrap a health/state value in its colour."""
    style = _HEALTH_STYLE.get((val or "").upper(), "")
    return f"[{style}]{val}[/{style}]" if style else (val or "—")


def _find_server(items: list[dict], target: str) -> dict | None:
    """Find a server by serial number, name, or iLO hostname (exact then substring)."""
    t = target.upper()
    for s in items:
        hw = s.get("hardware", {})
        if t in (
            (hw.get("serialNumber") or "").upper(),
            (s.get("name") or "").upper(),
            ((hw.get("bmc") or {}).get("hostname") or "").upper(),
        ):
            return s
    # Substring fallback — handles iLO FQDNs
    for s in items:
        hw = s.get("hardware", {})
        sn   = (hw.get("serialNumber") or "").upper()
        name = (s.get("name") or "").upper()
        if (sn and sn in t) or (name and name in t):
            return s
    return None


async def run_describe(session: COMSession, target: str) -> None:
    """Show full details for a single server from COM."""
    from rich import box as rich_box
    from rich.table import Table
    from rich.console import Group
    from rich.text import Text
    from proliant.com.servers import _fetch_appliance_map

    async with COMClient(session) as c:
        with get_console().status("[dim]Fetching server list…[/dim]"):
            r, appliance_map = await asyncio.gather(
                c.get(session.com_url("/servers"), params={"limit": 1000}),
                _fetch_appliance_map(c, session),
            )

    server = _find_server(r.get("items", []), target)
    if not server:
        get_console().print(f"[red]Server '{target}' not found.[/red]")
        sys.exit(1)

    hw       = server.get("hardware", {})
    bmc      = hw.get("bmc") or {}
    state    = server.get("state", {})
    health   = hw.get("health", {})
    host     = server.get("host") or {}
    oneview  = server.get("oneview") or {}
    appliance = server.get("appliance") or {}

    if get_output_mode() == OutputMode.JSON:
        print_json(server)
        return

    os_name  = host.get("osName") or server.get("name") or "—"
    ilo_name = bmc.get("hostname") or "—"
    serial   = hw.get("serialNumber", "—")

    connected = bool(state.get("connected"))
    if connected:
        state_label = "CONNECTED"
    elif (state.get("subscriptionState") or "").upper() == "REQUIRED":
        state_label = "NOT ACTIVATED"
    else:
        state_label = "NOT CONNECTED"

    connection_type = server.get("connectionType") or ""
    connection_label = {"DIRECT": "Direct", "ONEVIEW": "OneView managed"}.get(
        connection_type, connection_type or "—"
    )

    # ── LEFT COLUMN — matches COM GUI "Device details" ─────────────────────
    left = []

    # Header: serial bold + iLO name
    left.append(Text.assemble(
        (serial, "bold green"),
        "\n",
        (ilo_name, "green"),
    ))

    left.append(Text("\nDevice Details", style="bold"))
    dev_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    dev_t.add_column(style="dim", no_wrap=True)
    dev_t.add_column()
    dev_t.add_row("iLO Name",    f"[green]{ilo_name}[/green]")
    dev_t.add_row("OS Name",     f"[cyan]{os_name}[/cyan]" if os_name not in ("—", serial) else "[dim]—[/dim]")
    dev_t.add_row("Model",       hw.get("model", "—"))
    dev_t.add_row("Part Number", hw.get("productId", "—"))
    dev_t.add_row("UUID",        hw.get("uuid", "—"))
    dev_t.add_row("Generation",  server.get("serverGeneration", "—"))
    dev_t.add_row("CPU",         server.get("processorVendor", "—"))
    dev_t.add_row("Power",       _h(hw.get("powerState", "—")))
    dev_t.add_row("State",       _h(state_label))
    dev_t.add_row("Managed",     "Yes" if state.get("managed") else "No")
    dev_t.add_row("Maintenance Mode", "Yes" if server.get("maintenanceMode") else "No")
    left.append(dev_t)

    # Connection type / OneView appliance bridge -- only shown for servers
    # actually connected through a OneView appliance (nothing to add for
    # plain DIRECT-connected servers beyond the "Connection Type" row).
    left.append(Text("Connection", style="bold"))
    conn_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    conn_t.add_column(style="dim", no_wrap=True)
    conn_t.add_column()
    conn_t.add_row("Connection Type", connection_label)
    if connection_type == "ONEVIEW":
        appliance_name = appliance_map.get(appliance.get("applianceId", ""), "—")
        conn_t.add_row("Appliance",         appliance_name)
        conn_t.add_row("OneView Name",      oneview.get("name") or "—")
        conn_t.add_row("OneView State",     oneview.get("state") or "—")
    left.append(conn_t)

    left.append(Text("Subscription", style="bold"))
    sub_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    sub_t.add_column(style="dim", no_wrap=True)
    sub_t.add_column()
    sub_t.add_row("Tier",    state.get("subscriptionTier", "—"))
    sub_t.add_row("Key",     state.get("subscriptionKey", "—"))
    sub_t.add_row("Expires", (state.get("subscriptionExpiresAt") or "—")[:10])
    left.append(sub_t)

    # ── RIGHT COLUMN — iLO hardware + health + GPU ─────────────────────────
    right = []

    right.append(Text("iLO", style="bold"))
    ilo_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    ilo_t.add_column(style="dim", no_wrap=True)
    ilo_t.add_column()
    ilo_t.add_row("Model",   bmc.get("model", "—"))
    ilo_t.add_row("Version", bmc.get("version", "—"))
    ilo_t.add_row("License", bmc.get("license", "—"))
    ilo_t.add_row("IP",      bmc.get("ip", "—"))
    ilo_t.add_row("MAC",     bmc.get("mac", "—"))
    right.append(ilo_t)

    right.append(Text("Health", style="bold"))
    h_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    h_t.add_column(style="dim", no_wrap=True)
    h_t.add_column()
    skip = {"summary", "healthLED", "airFilter", "smartStorage"}
    for k, v in (health or {}).items():
        if k not in skip:
            h_t.add_row(k.replace("_", " ").title(), _h(v))
    right.append(h_t)

    _GPU_KEYWORDS = ("video controller", "gpu", "nvidia", "radeon", "gaudi",
                     "accelerator", "xe graphics")
    fw_items = server.get("firmwareInventory") or []
    gpu_items = [
        fw for fw in fw_items
        if any(kw in (fw.get("name") or "").lower() for kw in _GPU_KEYWORDS)
    ]
    if gpu_items:
        right.append(Text("GPU", style="bold"))
        gpu_t = Table(box=rich_box.SIMPLE, show_header=True,
                      header_style="bold cyan", padding=(0, 2))
        gpu_t.add_column("Model", no_wrap=True)
        gpu_t.add_column("Driver/FW")
        gpu_t.add_column("Slot", style="dim")
        for fw in gpu_items:
            gpu_t.add_row(fw.get("name", ""), fw.get("version", ""),
                          fw.get("deviceContext", ""))
        right.append(gpu_t)

    # ── 2-column layout ────────────────────────────────────────────────────
    layout = Table(box=None, show_header=False, padding=(0, 2), expand=True)
    layout.add_column(ratio=1)
    layout.add_column(ratio=1)
    layout.add_row(Group(*left), Group(*right))
    get_console().print(layout)

    # ── Full width: Server Profile (OneView-managed servers only) + Memory + Firmware ──
    if connection_type == "ONEVIEW":
        await _render_server_profile(appliance_map, appliance, serial)

    await _render_memory(hw, bmc)

    if fw_items:
        get_console().print("[bold]Firmware[/bold]")
        fw_t = Table(box=rich_box.SIMPLE, show_header=True,
                     header_style="bold cyan", padding=(0, 2))
        fw_t.add_column("Component", no_wrap=True)
        fw_t.add_column("Version")
        fw_t.add_column("Location", style="dim")
        for fw in fw_items:
            fw_t.add_row(fw.get("name", ""), fw.get("version", ""),
                         fw.get("deviceContext", ""))
        get_console().print(fw_t)


async def _render_server_profile(appliance_map: dict, appliance: dict, serial: str) -> None:
    """Best-effort: fetch the server's OneView server profile (status, virtual
    identity, network/SAN connections) directly from the bridging OneView
    appliance and render it, matching the GUI's "Server profile" panel.

    Matched by hardware serial number, not by COM's ``oneview.name`` field --
    that field is actually the server *hardware's* OneView name/bay label
    (e.g. "MXQ713060B, bay 5"), which is independent of the profile's own
    name (e.g. "HyperV-04"), so matching a profile by that value would
    silently fail to find anything.

    COM itself doesn't expose profile connections, so this connects straight
    to the OneView appliance using inventory.ini credentials (see
    ``proliant oneview appliances``). Silently skipped if no OneView
    appliance is configured locally, it's unreachable, or no profile is
    assigned to this hardware.
    """
    from rich import box as rich_box
    from rich.table import Table

    if not serial or serial == "—":
        return

    try:
        from proliant.oneview.client import OneViewClient
        from proliant.oneview.config import list_oneview_appliances
        from proliant.oneview.profiles import describe_profile_by_serial

        appliances = list_oneview_appliances()
        if not appliances:
            return

        chosen = appliances[0]
        if len(appliances) > 1:
            hostname = appliance_map.get(appliance.get("applianceId", ""), "")
            for a in appliances:
                if hostname and (hostname.lower() in a["host"].lower()
                                  or a["host"].lower() in hostname.lower()):
                    chosen = a
                    break

        async with OneViewClient(chosen["host"], chosen["username"], chosen["password"]) as ov:
            profile = await describe_profile_by_serial(ov, serial)
    except Exception:  # intentional: OneView unreachable/no creds/no profile assigned — skip
        return

    get_console().print("[bold]Server Profile[/bold]")
    sp_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    sp_t.add_column(style="dim", no_wrap=True)
    sp_t.add_column()
    sp_t.add_row("Status",                 _h(profile.get("status", "—")))
    sp_t.add_row("Name",                   profile.get("name") or "—")
    sp_t.add_row("State",                  profile.get("state") or "—")
    sp_t.add_row("Serial Number (virtual)", profile.get("serial_number") or "—")
    sp_t.add_row("UUID (virtual)",         profile.get("uuid") or "—")
    get_console().print(sp_t)

    connections = profile.get("connections") or []
    if connections:
        get_console().print("[bold]Connections[/bold]")
        conn_t = Table(box=rich_box.SIMPLE, show_header=True,
                        header_style="bold cyan", padding=(0, 2))
        conn_t.add_column("ID", no_wrap=True)
        conn_t.add_column("Name")
        conn_t.add_column("Type")
        conn_t.add_column("MAC/WWPN")
        for c in connections:
            mac_wwpn = c.get("mac") or c.get("wwpn") or c.get("wwnn") or "—"
            conn_t.add_row(str(c.get("id", "—")), c.get("name") or "—",
                           c.get("function_type") or "—", mac_wwpn)
        get_console().print(conn_t)


async def _render_memory(hw: dict, bmc: dict) -> None:
    """Render memory section: DIMM detail via iLO if available, else COM total."""
    from rich import box as rich_box
    from rich.table import Table

    ilo_ip = bmc.get("ip")
    if ilo_ip:
        try:
            from proliant.ilo.config import load_hosts
            from proliant.ilo.client import ILOClient
            from proliant.ilo.inventory import fetch_memory_population

            ilo_creds = None
            try:
                for h in load_hosts():
                    if ilo_ip in h.get("url", "") or (bmc.get("hostname") or "") in h.get("url", ""):
                        ilo_creds = h
                        break
                if not ilo_creds:
                    sn = (hw.get("serialNumber") or "").lower()
                    for h in load_hosts():
                        if sn and sn in h.get("name", "").lower():
                            ilo_creds = h
                            break
            except Exception:  # intentional: iLO creds lookup is best-effort
                pass

            if ilo_creds:
                async with ILOClient(ilo_creds["url"], ilo_creds["username"], ilo_creds["password"]) as ilo:
                    dimms = await fetch_memory_population(ilo)
                if dimms:
                    populated  = [d for d in dimms if d["present"]]
                    empty_cnt  = sum(1 for d in dimms if not d["present"])
                    total_gb   = sum(d["cap_gb"] for d in populated)
                    get_console().print("[bold]Memory[/bold]")
                    mem_t = Table(box=rich_box.SIMPLE, show_header=True,
                                  header_style="bold cyan", padding=(0, 2))
                    mem_t.add_column("Slot", no_wrap=True)
                    mem_t.add_column("Capacity")
                    mem_t.add_column("Type")
                    mem_t.add_column("Speed")
                    mem_t.add_column("Part Number", style="dim")
                    for d in dimms:
                        if d["present"]:
                            mem_t.add_row(d["slot"], f"{d['cap_gb']} GB", d["type"] or "—",
                                          f"{d['speed']} MT/s" if d["speed"] else "—",
                                          d["part"] or "—")
                        else:
                            mem_t.add_row(f"[dim]{d['slot']}[/dim]", "[dim]empty[/dim]", "", "", "")
                    get_console().print(mem_t)
                    get_console().print(
                        f"  [dim]{len(populated)} DIMMs populated, {empty_cnt} empty"
                        f" — {total_gb} GB total[/dim]"
                    )
                    return
        except Exception:  # intentional: iLO unreachable or no creds — fall through to COM total
            pass

    mem_mb = hw.get("memoryMb")
    if mem_mb:
        get_console().print("[bold]Memory[/bold]")
        get_console().print(f"  {mem_mb // 1024} GB total  [dim](slot detail requires iLO access)[/dim]")
