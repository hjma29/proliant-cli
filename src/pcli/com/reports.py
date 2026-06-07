"""Fleet-wide inventory report commands for COM."""

from __future__ import annotations

from pcli.common.display import get_console, print_json, print_memory_report, OutputMode, get_output_mode
from pcli.com.client import COMClient
from pcli.com.auth import COMSession


async def run_report_gpu(session: COMSession) -> None:
    from pcli.com.inventory import get_fleet_gpus, aggregate_gpus_by_model
    from rich import box as rich_box
    from rich.table import Table

    async with COMClient(session) as client:
        with get_console().status("[dim]Fetching GPU inventory across fleet…[/dim]"):
            try:
                gpus = await get_fleet_gpus(client)
            except RuntimeError as e:
                get_console().print(f"[red]Error:[/red] {e}")
                return

    if not gpus:
        get_console().print("[yellow]No discrete GPUs found across fleet.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(gpus)
        return

    rows = aggregate_gpus_by_model(gpus)
    total = sum(r["count"] for r in rows)

    table = Table(
        title=f"GPU Inventory  ({total} GPUs across {len({g['server'] for g in gpus})} servers)",
        box=rich_box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("GPU Model", min_width=28)
    table.add_column("Count",     justify="right", no_wrap=True, style="bold")
    table.add_column("Servers",   min_width=20)

    for r in rows:
        table.add_row(r["gpu"], str(r["count"]), ", ".join(sorted(r["servers"])))

    get_console().print(table)


async def run_report_memory(session: COMSession) -> None:
    from pcli.com.inventory import get_fleet_memory, aggregate_by_part_number

    async with COMClient(session) as client:
        with get_console().status("[dim]Fetching memory inventory across fleet…[/dim]"):
            try:
                dimms = await get_fleet_memory(client)
            except RuntimeError as e:
                get_console().print(f"[red]Error:[/red] {e}")
                return

    if not dimms:
        get_console().print("[yellow]No memory inventory data returned.[/yellow]")
        return

    if get_output_mode() == OutputMode.JSON:
        print_json(dimms)
        return

    rows = aggregate_by_part_number(dimms)
    print_memory_report(rows, source="COM")
