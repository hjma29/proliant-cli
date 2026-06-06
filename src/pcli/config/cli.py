"""
pcli.config.cli — config subcommands: list inventory, list cli-tree.

Usage:
    pcli config list inventory
    pcli config list cli-tree
"""
from __future__ import annotations

import argparse
import configparser
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.tree import Tree
from rich import box

# Reuse the same config-file search logic from ilo/config
from pcli.ilo.config import HOSTS_FILE

console = Console()


# ── Commands ───────────────────────────────────────────────────────────────────

def _cmd_list_inventory() -> None:
    if not HOSTS_FILE.exists():
        console.print(
            f"[red]Config file not found:[/red] {HOSTS_FILE}\n"
            "Run [bold]pcli ilo init[/bold] to create a starter config."
        )
        sys.exit(1)

    cfg = configparser.ConfigParser()
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


def _add_tree_nodes(branch: Tree, parser: argparse.ArgumentParser, depth: int = 0) -> None:
    """Recursively add subcommands as tree nodes (max 4 levels deep)."""
    if depth > 3:
        return
    for name, sub in _get_subparsers(parser).items():
        help_text = ""
        # Get help from the parent's subparser choices
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                choice_action = action._name_parser_map.get(name)
                if choice_action:
                    # help is stored on the parent's _choices_actions
                    pass
        # Get help text from the subparser's description or prog
        desc = (sub.description or "").split("\n")[0].strip()
        # Try to get help from _defaults or _option_string_actions
        label = f"[bold cyan]{name}[/bold cyan]"
        if desc:
            label += f"  [dim]{desc[:60]}[/dim]"
        node = branch.add(label)
        _add_tree_nodes(node, sub, depth + 1)


def _cmd_list_cli_tree() -> None:
    """Print the full pcli command hierarchy as a tree."""
    # Lazy imports to avoid circular deps and slow startup
    from pcli.ilo.cli      import _build_parser as ilo_parser
    from pcli.com.cli      import _build_parser as com_parser
    from pcli.spp.cli      import _build_parser as spp_parser
    from pcli.oneview.cli  import _build_parser as ov_parser
    from pcli.qs.cli       import _build_parser as qs_parser

    namespaces = {
        "ilo":     (ilo_parser,  "Direct iLO Redfish management"),
        "com":     (com_parser,  "HPE GreenLake / Compute Ops Management"),
        "spp":     (spp_parser,  "HPE Service Pack for ProLiant analysis"),
        "oneview": (ov_parser,   "HPE OneView fleet management"),
        "qs":      (qs_parser,   "HPE QuickSpecs browser"),
        "config":  (_build_parser, "View and manage pcli configuration"),
        "update":  (None,        "Upgrade pcli to the latest release"),
    }

    root = Tree("[bold green]pcli[/bold green]")
    for ns, (builder, desc) in namespaces.items():
        branch = root.add(f"[bold yellow]{ns}[/bold yellow]  [dim]{desc}[/dim]")
        if builder is not None:
            _add_tree_nodes(branch, builder())

    console.print(root)


# ── Argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcli config",
        description="Manage pcli configuration.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  pcli config list inventory    Show all iLO hosts and OneView appliance from hosts-ilo.ini
""",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    p_list = sub.add_parser("list", help="List configuration items")
    list_sub = p_list.add_subparsers(dest="item", metavar="ITEM")
    list_sub.add_parser("inventory", help="Show iLO hosts and OneView appliance")
    list_sub.add_parser("cli-tree",  help="Show full pcli command hierarchy as a tree")

    return p


def main(argv: list[str] | None = None) -> None:
    p = _build_parser()
    args = p.parse_args(argv)

    if args.cmd == "list":
        if args.item == "inventory":
            _cmd_list_inventory()
        elif args.item == "cli-tree":
            _cmd_list_cli_tree()
        else:
            p.parse_args(["list", "--help"])
    else:
        p.print_help()
