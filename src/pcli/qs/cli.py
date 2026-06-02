"""
pcli.qs.cli — QuickSpecs subcommand: list, describe.

Usage:
    pcli qs list --model <model>
    pcli qs describe <docid>
    pcli qs describe --model <model>            (describes the latest revision)
    pcli qs describe <docid> --section <name>
    pcli qs describe <docid> --list-sections
"""
from __future__ import annotations

import argparse
import re
import sys

from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown
from rich.rule import Rule
from rich import box

from pcli.qs.client import QSEntry, search_quickspecs, fetch_quickspec_markdown, filter_section

console = Console()


# ── Helpers ────────────────────────────────────────────────────────────────────

_SEP_RE = re.compile(r"^\|[\s\-:|]+\|")


def _parse_md_row(line: str) -> list[str]:
    """Parse '| **A** | [B](url) | C |' → ['A', 'B', 'C']."""
    cells = line.strip().strip("|").split("|")
    result = []
    for cell in cells:
        cell = cell.strip()
        cell = re.sub(r"\*\*(.*?)\*\*", r"\1", cell)          # bold
        cell = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cell)  # links
        result.append(cell)
    return result


def _render_md_table(table_lines: list[str]) -> None:
    """Render markdown table lines directly as a Rich Table (no Markdown padding)."""
    if not table_lines:
        return
    header_row = _parse_md_row(table_lines[0])
    data_rows = [
        _parse_md_row(ln)
        for ln in table_lines[1:]
        if not _SEP_RE.match(ln)
    ]
    col_count = len(header_row)
    t = Table(
        box=box.SIMPLE_HEAD,
        show_header=bool(any(header_row)),
        header_style="bold",
        padding=(0, 1),
    )
    for h in header_row:
        t.add_column(h)
    for row in data_rows:
        padded = (row + [""] * col_count)[:col_count]
        t.add_row(*padded)
    console.print(t)


def _render_section_body(body: str) -> None:
    """Render section body, using Rich Table for markdown tables to avoid padding."""
    lines = body.splitlines()
    pending: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].startswith("|"):
            if pending:
                text_block = "\n".join(pending).strip()
                if text_block:
                    console.print(Markdown(text_block))
                pending = []
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            _render_md_table(table_lines)
        else:
            pending.append(lines[i])
            i += 1
    if pending:
        text_block = "\n".join(pending).strip()
        if text_block:
            console.print(Markdown(text_block))


def _fmt_date(raw: str) -> str:
    """Convert '05/13/2026 00:00:00.000' → '2026-05-13'."""
    if not raw:
        return ""
    parts = raw.split(" ")[0].split("/")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[0]}-{parts[1]}"
    return raw


# ── Commands ───────────────────────────────────────────────────────────────────

def _cmd_list(args: argparse.Namespace) -> None:
    model = args.model
    console.print(f"[dim]Searching QuickSpecs for: {model}…[/dim]")

    try:
        entries = search_quickspecs(model, count=args.count)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        sys.exit(1)

    if not entries:
        console.print("[yellow]No results found.[/yellow]")
        return

    t = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold cyan",
        padding=(0, 1),
    )
    t.add_column("Doc ID", style="green", no_wrap=True)
    t.add_column("Last Modified", no_wrap=True)
    t.add_column("Title")

    for e in entries:
        t.add_row(e.doc_id, _fmt_date(e.last_modified), e.title)

    console.print(t)
    console.print(
        "[dim]Use 'pcli qs describe <docid>' to read the full QuickSpec.[/dim]"
    )


def _cmd_describe(args: argparse.Namespace) -> None:
    # Resolve doc_id: either explicit or via --model (latest result)
    doc_id = args.doc_id if args.doc_id else None
    if not doc_id:
        if not args.model:
            console.print(
                "[red]Error:[/red] provide a doc ID or --model <model>",
                highlight=False,
            )
            sys.exit(1)
        console.print(f"[dim]Looking up latest QuickSpec for: {args.model}…[/dim]")
        try:
            entries = search_quickspecs(args.model, count=1)
        except Exception as exc:
            console.print(f"[red]Error:[/red] {exc}", highlight=False)
            sys.exit(1)
        if not entries:
            console.print("[yellow]No QuickSpec found for that model.[/yellow]")
            sys.exit(1)
        doc_id = entries[0].doc_id
        console.print(
            f"[dim]Using latest: {doc_id} ({entries[0].title})[/dim]"
        )

    console.print(f"[dim]Fetching QuickSpec {doc_id}…[/dim]")
    try:
        markdown, sections = fetch_quickspec_markdown(doc_id)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}", highlight=False)
        sys.exit(1)

    if args.list_sections:
        console.print("[bold]Available sections:[/bold]")
        for s in sections:
            console.print(f"  • {s}")
        return

    if args.section:
        # Find best match (case-insensitive substring)
        target = args.section.lower()
        matched = next((s for s in sections if target in s.lower()), None)
        if not matched:
            console.print(
                f"[yellow]Section '{args.section}' not found.[/yellow]\n"
                f"Available sections: {', '.join(sections)}"
            )
            sys.exit(1)
        text = filter_section(markdown, matched)
        lines = text.splitlines()
        heading = lines[0].lstrip("#").strip() if lines else matched
        body = "\n".join(lines[1:]).lstrip("\n")
        console.print(Rule(f"[bold]{heading}[/bold]"))
        _render_section_body(body)
        return

    # Full document
    console.print(Markdown(markdown))


# ── Argument parser ────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pcli qs",
        description="Browse HPE QuickSpecs documents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  pcli qs list --model dl380gen12               List QuickSpec revisions for DL380 Gen12
  pcli qs list --model "DL360 Gen11"            List revisions for DL360 Gen11
  pcli qs describe a00073551enw                 Show full QuickSpec (latest DL380 Gen12)
  pcli qs describe --model dl380gen12           Fetch latest DL380 Gen12 QuickSpec
  pcli qs describe a00073551enw --list-sections List sections in the document
  pcli qs describe a00073551enw --section "Standard Features"
""",
    )
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    # ── list ──────────────────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="List QuickSpec revisions for a model")
    p_list.add_argument(
        "--model", "-m",
        required=True,
        metavar="MODEL",
        help="Server model, e.g. dl380gen12, 'DL360 Gen11'",
    )
    p_list.add_argument(
        "--count", "-n",
        type=int,
        default=5,
        metavar="N",
        help="Maximum results to show (default: 5)",
    )

    # ── describe ──────────────────────────────────────────────────────────────
    p_desc = sub.add_parser("describe", help="Show a QuickSpec document as markdown")
    p_desc.add_argument(
        "doc_id",
        nargs="?",
        metavar="DOCID",
        help="Document ID, e.g. a00073551enw (optional if --model is given)",
    )
    p_desc.add_argument(
        "--model", "-m",
        metavar="MODEL",
        help="Resolve the latest doc ID for this model",
    )
    p_desc.add_argument(
        "--section", "-s",
        metavar="SECTION",
        help="Show only this section, e.g. 'Standard Features'",
    )
    p_desc.add_argument(
        "--list-sections", "-l",
        action="store_true",
        help="List available section names, then exit",
    )

    return p


# ── Entry point ────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.cmd == "list":
        _cmd_list(args)
    elif args.cmd == "describe":
        _cmd_describe(args)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
