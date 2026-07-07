"""
proliant.setting.cli — setting subcommands.

Usage:
    proliant setting list inventory
    proliant setting list cli-tree
    proliant setting telemetry on|off
    proliant setting uninstall
"""
from __future__ import annotations

import argparse
import configparser
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich import box

# Reuse the same config-file search logic from ilo/config
from proliant.ilo.config import HOSTS_FILE

console = Console()


# ── Commands ───────────────────────────────────────────────────────────────────

def _cmd_list_inventory() -> None:
    if not HOSTS_FILE.exists():
        console.print(
            f"[red]Config file not found:[/red] {HOSTS_FILE}\n"
            "Run [bold]proliant setup[/bold] to add your iLO/OneView servers."
        )
        sys.exit(1)

    # interpolation=None: passwords may legitimately contain a literal '%'.
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(HOSTS_FILE)

    default_user = cfg.get("defaults", "username", fallback="Administrator")

    ilo_hosts: list[dict] = []
    ov_host: dict | None = None

    for section in cfg.sections():
        if section.lower() == "defaults":
            continue
        if section.lower() == "oneview":
            ov_host = {
                "host": cfg.get(section, "host", fallback=""),
                "username": cfg.get(section, "username", fallback=default_user),
            }
        else:
            ilo_hosts.append({
                "name": section,
                "host": cfg.get(section, "host", fallback=""),
                "username": cfg.get(section, "username", fallback=default_user),
            })

    console.print(f"[dim]Config: {HOSTS_FILE}[/dim]\n")

    # ── iLO table ──────────────────────────────────────────────────────────────
    if ilo_hosts:
        t = Table(
            title="[bold]iLO Hosts[/bold]",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
            title_justify="left",
        )
        t.add_column("#", justify="right", style="dim", no_wrap=True)
        t.add_column("Name", style="green", no_wrap=True)
        t.add_column("Address", no_wrap=True)
        t.add_column("Username")

        for i, h in enumerate(ilo_hosts, 1):
            t.add_row(str(i), h["name"], h["host"], h["username"])
        console.print(t)
    else:
        console.print("[yellow]No iLO hosts configured.[/yellow]")

    # ── OneView ────────────────────────────────────────────────────────────────
    if ov_host:
        t2 = Table(
            title="[bold]OneView[/bold]",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold cyan",
            padding=(0, 1),
            title_justify="left",
        )
        t2.add_column("Address", no_wrap=True)
        t2.add_column("Username")
        t2.add_row(ov_host["host"], ov_host["username"])
        console.print(t2)
    else:
        console.print("[dim]No [oneview] section configured.[/dim]")


def _get_subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    """Return the subparser choices dict from a parser, or {} if none."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices or {}
    return {}


def _node_priority(name: str) -> int:
    if name == "list":     return 0
    if name == "describe": return 1
    if name == "report":   return 2
    return 3


def _add_tree_nodes(branch: Tree, parser: argparse.ArgumentParser, depth: int = 0) -> None:
    """Recursively add subcommands as tree nodes (max 4 levels deep)."""
    if depth > 3:
        return
    items = sorted(_get_subparsers(parser).items(), key=lambda x: (_node_priority(x[0]), x[0]))
    for name, sub in items:
        node = branch.add(f"[cyan]{name}[/cyan]")
        _add_tree_nodes(node, sub, depth + 1)


def _cmd_list_cli_tree() -> None:
    """Print the full proliant command hierarchy as a tree."""
    from rich.table import Table
    from rich import box as rich_box

    try:
        from proliant.ilo.cli import _build_parser as ilo_parser
    except ImportError:
        ilo_parser = None

    try:
        from proliant.com.cli import _build_parser as com_parser
    except ImportError:
        com_parser = None

    try:
        from proliant.spp.cli import _build_parser as spp_parser
    except ImportError:
        spp_parser = None

    try:
        from proliant.oneview.cli import _build_parser as ov_parser
    except ImportError:
        ov_parser = None

    try:
        from proliant.qs.cli import _build_parser as qs_parser
    except ImportError:
        qs_parser = None

    namespaces = [
        ("ilo",     ilo_parser,    "Direct iLO Redfish"),
        ("com",     com_parser,    "GreenLake / COM"),
        ("spp",     spp_parser,    "Service Pack"),
        ("oneview", ov_parser,     "OneView"),
        ("qs",      qs_parser,     "QuickSpecs"),
        ("config",  _build_parser, "Configuration"),
        ("update",  None,          "Upgrade proliant"),
    ]

    # Build one Tree per namespace
    trees = []
    for ns, builder, desc in namespaces:
        if builder is None and ns != "update":
            continue
        t = Tree(f"[bold yellow]{ns}[/bold yellow]")
        if builder is not None:
            _add_tree_nodes(t, builder())
        trees.append(t)

    # Lay out as a single-row table — forces true side-by-side rendering
    tbl = Table(box=None, show_header=False, padding=(0, 1), expand=False)
    for _ in trees:
        tbl.add_column(no_wrap=False)
    tbl.add_row(*trees)

    console.print("[bold green]proliant[/bold green]\n")
    console.print(tbl)


# ── Argument parser ────────────────────────────────────────────────────────────

def _cmd_telemetry(state: str) -> None:
    """Enable or disable Sentry error telemetry."""
    from proliant.common import config_dir
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    enabled = cfg / "telemetry-enabled"
    disabled = cfg / "telemetry-disabled"

    if state == "on":
        disabled.unlink(missing_ok=True)
        enabled.touch()
        console.print("[green]✓[/green] Telemetry enabled.")
        console.print("[dim]Error reports will be sent anonymously to help improve proliant.[/dim]")
    else:
        enabled.unlink(missing_ok=True)
        disabled.touch()
        console.print("[yellow]✓[/yellow] Telemetry disabled. No data will be sent.")


def _cmd_uninstall() -> None:
    """Remove all proliant-cli config and cache directories."""
    from proliant.common import config_dir, cache_dir
    cfg = config_dir()
    cch = cache_dir()

    console.print("[bold]proliant config uninstall[/bold]\n")
    console.print("This will permanently remove:\n")
    for d in (cfg, cch):
        if d.exists():
            console.print(f"  [red]•[/red] {d}")
        else:
            console.print(f"  [dim]•[/dim] {d}  [dim](not found)[/dim]")

    console.print()
    try:
        answer = input("Continue? [y/N] ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        console.print("\nAborted.")
        return

    if answer != "y":
        console.print("Aborted.")
        return

    for d in (cfg, cch):
        if d.exists():
            shutil.rmtree(d)
            console.print(f"  [green]✓[/green] Removed {d}")
        else:
            console.print(f"  [dim]–[/dim] {d} not found, skipped")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proliant setting",
        description="Manage proliant configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  proliant setting list inventory    Show all iLO hosts and OneView appliance from inventory.ini
  proliant setting telemetry on      Enable Sentry error telemetry
  proliant setting telemetry off     Disable telemetry
  proliant setting uninstall         Remove all proliant-cli config and cache files
""",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    p_list = sub.add_parser("list", help="List configuration items")
    list_sub = p_list.add_subparsers(dest="item", metavar="ITEM")
    list_sub.add_parser("inventory", help="Show iLO hosts and OneView appliance")
    list_sub.add_parser("cli-tree",  help="Show full proliant command hierarchy as a tree")

    p_tel = sub.add_parser("telemetry", help="Enable or disable error telemetry")
    p_tel.add_argument("state", choices=["on", "off"], help="on or off")

    sub.add_parser("uninstall", help="Remove all proliant-cli config and cache directories")

    return p


def main(argv: list[str] | None = None) -> None:
    p = _build_parser()
    import argcomplete
    argcomplete.autocomplete(p)
    args = p.parse_args(argv)

    if args.cmd == "list":
        if args.item == "inventory":
            _cmd_list_inventory()
        elif args.item == "cli-tree":
            _cmd_list_cli_tree()
        else:
            p.parse_args(["list", "--help"])
    elif args.cmd == "telemetry":
        _cmd_telemetry(args.state)
    elif args.cmd == "uninstall":
        _cmd_uninstall()
    else:
        p.print_help()
