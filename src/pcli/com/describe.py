"""Single-server describe command for COM."""

from __future__ import annotations

import sys

from pcli.common.display import get_console, print_json, OutputMode, get_output_mode
from pcli.com.client import COMClient
from pcli.com.auth import COMSession

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
    from rich.panel import Panel
    from rich.table import Table

    async with COMClient(session) as c:
        with get_console().status("[dim]Fetching server list…[/dim]"):
            r = await c.get(session.com_url("/servers"), params={"limit": 1000})

    server = _find_server(r.get("items", []), target)
    if not server:
        get_console().print(f"[red]Server '{target}' not found.[/red]")
        sys.exit(1)

    hw     = server.get("hardware", {})
    bmc    = hw.get("bmc") or {}
    state  = server.get("state", {})
    health = hw.get("health", {})

    if get_output_mode() == OutputMode.JSON:
        print_json(server)
        return

    get_console().print(Panel(
        f"[bold]{server.get('name')}[/bold]   [dim]{hw.get('model', '—')}[/dim]",
        expand=False,
    ))

    # ── Identity ──────────────────────────────────────────────────────────────
    id_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    id_t.add_column(style="dim", no_wrap=True)
    id_t.add_column()
    id_t.add_row("Serial",      hw.get("serialNumber", "—"))
    id_t.add_row("Product ID",  hw.get("productId", "—"))
    id_t.add_row("Generation",  server.get("serverGeneration", "—"))
    id_t.add_row("Power",       _h(hw.get("powerState", "—")))
    id_t.add_row("Connection",  _h("CONNECTED" if state.get("connected") else "DISCONNECTED"))
    id_t.add_row("Managed",     "Yes" if state.get("managed") else "No")
    get_console().print(id_t)

    # ── iLO ───────────────────────────────────────────────────────────────────
    get_console().print("[bold]iLO[/bold]")
    ilo_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    ilo_t.add_column(style="dim", no_wrap=True)
    ilo_t.add_column()
    ilo_t.add_row("Model",    bmc.get("model", "—"))
    ilo_t.add_row("Version",  bmc.get("version", "—"))
    ilo_t.add_row("IP",       bmc.get("ip", "—"))
    ilo_t.add_row("Hostname", bmc.get("hostname", "—"))
    ilo_t.add_row("MAC",      bmc.get("mac", "—"))
    get_console().print(ilo_t)

    # ── Health ────────────────────────────────────────────────────────────────
    get_console().print("[bold]Health[/bold]")
    h_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    h_t.add_column(style="dim", no_wrap=True)
    h_t.add_column()
    skip = {"summary", "healthLED", "airFilter", "smartStorage"}
    for k, v in (health or {}).items():
        if k not in skip:
            h_t.add_row(k.replace("_", " ").title(), _h(v))
    get_console().print(h_t)

    # ── Subscription ──────────────────────────────────────────────────────────
    get_console().print("[bold]Subscription[/bold]")
    sub_t = Table(box=rich_box.SIMPLE, show_header=False, padding=(0, 2))
    sub_t.add_column(style="dim", no_wrap=True)
    sub_t.add_column()
    sub_t.add_row("Tier",    state.get("subscriptionTier", "—"))
    sub_t.add_row("Key",     state.get("subscriptionKey", "—"))
    sub_t.add_row("Expires", (state.get("subscriptionExpiresAt") or "—")[:10])
    get_console().print(sub_t)

    # ── GPU ───────────────────────────────────────────────────────────────────
    _GPU_KEYWORDS = ("video controller", "gpu", "nvidia", "radeon", "gaudi",
                     "accelerator", "xe graphics")
    fw_items = server.get("firmwareInventory") or []
    gpu_items = [
        fw for fw in fw_items
        if any(kw in (fw.get("name") or "").lower() for kw in _GPU_KEYWORDS)
    ]
    if gpu_items:
        get_console().print("[bold]GPU[/bold]")
        gpu_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        gpu_t.add_column("Model", no_wrap=True)
        gpu_t.add_column("Driver/FW")
        gpu_t.add_column("Slot", style="dim")
        for fw in gpu_items:
            gpu_t.add_row(fw.get("name", ""), fw.get("version", ""), fw.get("deviceContext", ""))
        get_console().print(gpu_t)

    # ── Memory (via iLO Redfish if creds available, else COM total) ───────────
    await _render_memory(hw, bmc)

    # ── Firmware inventory ────────────────────────────────────────────────────
    if fw_items:
        get_console().print("[bold]Firmware[/bold]")
        fw_t = Table(box=rich_box.SIMPLE, show_header=True, header_style="bold cyan", padding=(0, 2))
        fw_t.add_column("Component", no_wrap=True)
        fw_t.add_column("Version")
        fw_t.add_column("Location", style="dim")
        for fw in fw_items:
            fw_t.add_row(fw.get("name", ""), fw.get("version", ""), fw.get("deviceContext", ""))
        get_console().print(fw_t)


async def _render_memory(hw: dict, bmc: dict) -> None:
    """Render memory section: DIMM detail via iLO if available, else COM total."""
    from rich import box as rich_box
    from rich.table import Table

    ilo_ip = bmc.get("ip")
    if ilo_ip:
        try:
            from pcli.ilo.config import load_hosts
            from pcli.ilo.client import ILOClient
            from pcli.ilo.inventory import fetch_memory_population

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
