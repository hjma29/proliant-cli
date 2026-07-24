"""
proliant.setting.cli — setting subcommands.

Usage:
    proliant setting cli-tree
    proliant setting telemetry on|off
    proliant setting uninstall
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.tree import Tree

console = Console()


# ── Commands ───────────────────────────────────────────────────────────────────


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
        ("oneview", ov_parser,     "OneView"),
        ("spp",     spp_parser,    "Service Pack"),
        ("qs",      qs_parser,     "QuickSpecs"),
        ("setting", _build_parser, "Configuration"),
        ("setup",   None,          "Guided inventory setup wizard"),
        ("version", None,          "Show version / upgrade"),
    ]

    # Build one Tree per namespace
    trees = []
    for ns, builder, desc in namespaces:
        if builder is None and ns not in ("setup", "version"):
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

def _telemetry_effective_state(cfg: Path) -> str:
    """Return the effective Sentry telemetry state ('on' or 'off'), matching
    the exact check order used by cli._init_sentry()."""
    import os
    if (cfg / "telemetry-disabled").exists():
        return "off"
    if (cfg / "telemetry-enabled").exists() or os.environ.get("PROLIANT_TELEMETRY"):
        return "on"
    return "off"


def _telemetry_reason(cfg: Path) -> str:
    """Human-readable reason for the current effective state."""
    import os
    if (cfg / "telemetry-disabled").exists():
        return "explicitly disabled"
    if (cfg / "telemetry-enabled").exists():
        return "explicitly enabled"
    if os.environ.get("PROLIANT_TELEMETRY"):
        return "enabled via PROLIANT_TELEMETRY environment variable"
    return "default — never configured"


def _apply_telemetry_state(state: str, enabled: Path, disabled: Path) -> None:
    if state == "on":
        disabled.unlink(missing_ok=True)
        enabled.touch()
        console.print("[green]✓[/green] Telemetry enabled.")
        console.print("[dim]Error reports are sent anonymously to help fix this problem.[/dim]")
    else:
        enabled.unlink(missing_ok=True)
        disabled.touch()
        console.print("[yellow]✓[/yellow] Telemetry disabled. No data will be sent.")


def _cmd_telemetry(state: str | None) -> None:
    """Show telemetry status (default vs. current), or set it directly.

    With no argument: prints the default, the current effective state and why,
    then interactively confirms before toggling. Pass 'on'/'off' to set the
    state directly without a prompt (e.g. for scripting).
    """
    from proliant.common import config_dir
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    enabled = cfg / "telemetry-enabled"
    disabled = cfg / "telemetry-disabled"

    if state in ("on", "off"):
        _apply_telemetry_state(state, enabled, disabled)
        return

    current = _telemetry_effective_state(cfg)
    reason = _telemetry_reason(cfg)

    console.print("[bold]Telemetry status[/bold]\n")
    console.print("  Default:  [dim]off (opt-in — crash reports are never sent unless enabled)[/dim]")
    current_label = "[green]on[/green]" if current == "on" else "[yellow]off[/yellow]"
    console.print(f"  Current:  {current_label}  [dim]({reason})[/dim]\n")

    # Always ask the same question -- "Enable telemetry?" -- never "Disable",
    # so the prompt's meaning never flips depending on current state. The
    # bracketed default reflects the current state; pressing Enter keeps it.
    default_on = current == "on"
    prompt = "Enable telemetry? [Y/n] " if default_on else "Enable telemetry? [y/N] "
    try:
        answer = input(prompt).strip().lower()
    except (KeyboardInterrupt, EOFError):
        console.print("\nNo changes made.")
        return

    if answer == "":
        want_on = default_on
    elif answer in ("y", "yes"):
        want_on = True
    elif answer in ("n", "no"):
        want_on = False
    else:
        console.print("No changes made.")
        return

    new_state = "on" if want_on else "off"
    if new_state == current:
        console.print("No changes made.")
    else:
        _apply_telemetry_state(new_state, enabled, disabled)


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
  proliant setting cli-tree          Show full proliant command hierarchy as a tree
  proliant setting telemetry         Show telemetry default/current state; confirm to toggle
  proliant setting telemetry on      Enable Sentry error telemetry (no prompt)
  proliant setting telemetry off     Disable telemetry (no prompt)
  proliant setting uninstall         Remove all proliant-cli config and cache files
""",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    sub.add_parser("cli-tree", help="Show full proliant command hierarchy as a tree")

    p_tel = sub.add_parser("telemetry", help="Show or change Sentry error telemetry status")
    p_tel.add_argument(
        "state", nargs="?", choices=["on", "off"],
        help="on or off; omit to see status and confirm interactively",
    )

    sub.add_parser("uninstall", help="Remove all proliant-cli config and cache directories")

    return p


def main(argv: list[str] | None = None) -> None:
    p = _build_parser()
    import argcomplete
    # See oneview/cli.py's argcomplete.autocomplete call for why
    # always_complete_options=False.
    argcomplete.autocomplete(p, always_complete_options=False)
    args = p.parse_args(argv)

    if args.cmd == "cli-tree":
        _cmd_list_cli_tree()
    elif args.cmd == "telemetry":
        _cmd_telemetry(args.state)
    elif args.cmd == "uninstall":
        _cmd_uninstall()
    else:
        p.print_help()
