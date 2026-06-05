"""Single-server describe command for iLO."""

from __future__ import annotations

import sys

from pcli.common.display import get_console
from pcli.ilo import inventory
from pcli.ilo.client import ILOClient, ServerDownOrUnreachableError


async def run_describe(host: dict) -> None:
    """Show full details for a single server: identity, iLO, CPU, GPU, memory, firmware."""
    from rich import box as rich_box
    from rich.panel import Panel
    from rich.table import Table

    console = get_console()

    async with ILOClient(host["url"], host["username"], host["password"]) as c:
        try:
            with console.status("[dim]Fetching server details…[/dim]"):
                # Sequential requests — iLO 6 (Gen11) does not handle concurrent
                # requests per session reliably; gather caused ConnectError on Gen11.
                sys_uri = await c.get_system_uri()
                mgr_uri = await c.get_manager_uri()
                system  = await c.get(sys_uri)
                manager = await c.get(mgr_uri)
                fw_list = await inventory.fetch_firmware_inventory_full(c)
                cpus    = await inventory.fetch_cpu_report_data(c)
                gpus    = await inventory.fetch_gpu_report_data(c)
                dimms   = await inventory.fetch_memory_population(c)
        except Exception as exc:
            console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
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

    _HS = {"OK": "green", "Warning": "yellow", "Critical": "red"}

    def _h(v: str | None) -> str:
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
