"""
proliant.qs.cli — QuickSpecs subcommand: list, describe.

Usage:
    proliant qs list --model <model>
    proliant qs describe <docid>
    proliant qs describe --model <model>            (describes the latest revision)
    proliant qs describe <docid> --section <name>
    proliant qs describe <docid> --list-sections
"""
from __future__ import annotations

import argparse
import re
import sys

from proliant.common.display import get_console, get_output_mode, make_table, OutputMode, print_json, set_output_mode
from rich.markdown import Markdown
from rich.rule import Rule
from rich import box

from proliant.qs.client import QSEntry, search_quickspecs, fetch_quickspec_markdown, fetch_quickspec_versions, filter_section



# ── Helpers ────────────────────────────────────────────────────────────────────

_SEP_RE = re.compile(r"^\|[\s\-:|]+\|")

# Matches HPE option entries: "HPE <description> P12345-B21"
_OPTION_ENTRY_RE = re.compile(r"HPE\s+(.*?)\s+([A-Z]\d{5}-[A-Z]\d{2})", re.DOTALL)
_PAGE_NUM_RE = re.compile(r"\bPage\s+\d+\b", re.I)


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
    t = make_table(
        "",
        *[(h, {}) for h in header_row],
        box_style=box.SIMPLE_HEAD,
        show_header=bool(any(header_row)),
        header_style="bold",
        padding=(0, 1),
    )
    for row in data_rows:
        padded = (row + [""] * col_count)[:col_count]
        t.add_row(*padded)
    get_console().print(t)


def _render_option_list(text: str) -> None:
    """Render HPE option entries (e.g. Core Options) as structured table(s).

    Gaps between entries are parsed line-by-line:
    - Lines starting with Notes:/−/- are shown as dim context text
    - Other short non-HPE lines become bold sub-section headers
    """
    segments: list[tuple] = []
    prev_end = 0
    for m in _OPTION_ENTRY_RE.finditer(text):
        gap = _PAGE_NUM_RE.sub("", text[prev_end:m.start()]).strip()
        if gap:
            notes: list[str] = []
            headers: list[str] = []
            for line in gap.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(("Notes:", "Note:", "−", "-", "•")):
                    notes.append(line)
                elif len(line) < 100:
                    headers.append(line)
                else:
                    notes.append(line)
            if notes:
                segments.append(("notes", "\n".join(notes)))
            for h in headers:
                segments.append(("header", h))

        desc = re.sub(r"\s+", " ", m.group(1)).strip()
        segments.append(("entry", "HPE " + desc, m.group(2)))
        prev_end = m.end()

    if not segments:
        get_console().print(Markdown(text))
        return

    pending_rows: list[tuple[str, str]] = []

    def _flush() -> None:
        if not pending_rows:
            return
        t = make_table(
            "",
            ("Description", {"ratio": 5}),
            ("Part Number", {"style": "cyan", "no_wrap": True, "justify": "right"}),
            box_style=box.SIMPLE,
            show_header=False,
            padding=(0, 1),
        )
        for desc, pn in pending_rows:
            t.add_row(desc, pn)
        get_console().print(t)
        pending_rows.clear()

    for seg in segments:
        if seg[0] == "header":
            _flush()
            get_console().print(f"\n[bold]{seg[1]}[/bold]")
        elif seg[0] == "notes":
            _flush()
            get_console().print(f"[dim]{seg[1]}[/dim]")
        else:
            pending_rows.append((seg[1], seg[2]))
    _flush()


def _split_sections(markdown: str) -> list[tuple[str, str]]:
    """Split markdown into [(heading, body)] pairs on ### / ## / # headings."""
    pattern = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)
    result: list[tuple[str, str]] = []
    last_heading: str | None = None
    last_end = 0
    for m in pattern.finditer(markdown):
        if last_heading is not None:
            result.append((last_heading, markdown[last_end:m.start()].strip()))
        last_heading = m.group(2).strip()
        last_end = m.end()
    if last_heading is not None:
        result.append((last_heading, markdown[last_end:].strip()))
    if not result:
        result = [("", markdown)]
    return result


def _render_section_body(body: str) -> None:
    """Render section body: Rich tables for markdown tables, option-list tables
    for HPE drive/accessory entries, and Markdown for everything else."""
    lines = body.splitlines()
    pending: list[str] = []

    def _flush_pending() -> None:
        text_block = "\n".join(pending).strip()
        pending.clear()
        if not text_block:
            return
        # If the block contains HPE option entries, render as structured table
        if len(_OPTION_ENTRY_RE.findall(text_block)) >= 2:
            _render_option_list(text_block)
        else:
            get_console().print(Markdown(text_block))

    i = 0
    while i < len(lines):
        if lines[i].startswith("|"):
            _flush_pending()
            table_lines = []
            while i < len(lines) and lines[i].startswith("|"):
                table_lines.append(lines[i])
                i += 1
            _render_md_table(table_lines)
        else:
            pending.append(lines[i])
            i += 1
    _flush_pending()


# Matches a date-leading plain-text version header from PDF conversion:
# "01-Jun-2026  Version 17  Changed  Description..."
_PDF_DATE_HDR = re.compile(
    r"^(\d{2}-[A-Za-z]+-\d{4})\s+(Version\s+\d+)\s+(Added|Changed|Removed)\s+(.*)"
)
# Matches an action-only plain-text line from PDF: "Added  description..."
_PDF_ACTION_LINE = re.compile(r"^(Added|Changed|Removed)\s{2,}(.*)")
_FLAT_CHANGE_BLOCK_RE = re.compile(
    r"(?P<date>\d{2}-[A-Za-z]{3}-\d{4})\s+"
    r"Version\s+(?P<ver>\d+)\s+"
    r"(?P<action>Added|Changed|Removed|New)\s+"
    r"(?P<desc>.*?)"
    r"(?=(?:\d{2}-[A-Za-z]{3}-\d{4}\s+Version\s+\d+\s+(?:Added|Changed|Removed|New)\b)"
    r"|(?:QuickSpecs\s+HPE\s+ProLiant\b)"
    r"|(?:Page\s+\d+\b)"
    r"|$)",
    re.S,
)


def _parse_change_rows(body: str) -> list[list[str]]:
    """Parse Summary of Changes text into [date, version, action, description] rows.

    Handles two source formats:
    - HTML-converted markdown: proper pipe-table rows
      Full:         | date | version | action | desc |  (4 cells)
      Continuation: | action | desc |              (2 cells)
    - PDF-converted text: plain-text lines
      Header:       "01-Jun-2026  Version N  Changed  text..."
      Continuation: "Added  text..." or plain continuation text
    """
    lines = body.splitlines()

    # Detect format: if there are enough pipe-table rows, use markdown parser
    md_lines = [ln for ln in lines if ln.startswith("|") and not _SEP_RE.match(ln)]
    if len(md_lines) >= 2:
        rows = []
        for ln in md_lines[1:]:  # skip header row
            cells = _parse_md_row(ln)
            if len(cells) >= 4:
                rows.append(cells[:4])
            elif len(cells) == 2:
                rows.append(["", ""] + cells)
            elif len(cells) == 3:
                rows.append(["", "", cells[0], " ".join(cells[1:])])
        return rows

    # PDF plain-text format: state-machine parser
    rows: list[list[str]] = []
    _TRUE_SEP = re.compile(r"^\|[\s\-:|]+$")  # only dashes/spaces, no alpha
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if "Date" in line and "Version History" in line:
            continue

        # Version header line
        m = _PDF_DATE_HDR.match(line)
        if m:
            rows.append([m.group(1), m.group(2), m.group(3), m.group(4).strip()])
            continue

        # Pipe rows: either a true separator (skip) or an action row
        if line.startswith("|"):
            if _TRUE_SEP.match(line):
                continue
            cells = _parse_md_row(line)
            if len(cells) >= 4 and cells[2] in ("Added", "Changed", "Removed"):
                rows.append(["", "", cells[2], cells[3]])
            elif len(cells) == 2 and cells[0] in ("Added", "Changed", "Removed"):
                rows.append(["", "", cells[0], cells[1]])
            elif rows:
                # Continuation text in a pipe cell — append to last row
                text = " ".join(c for c in cells if c)
                if text:
                    prev = rows[-1][3]
                    sep = " " if prev and prev[-1] not in ("-", "–", " ") else ""
                    rows[-1][3] = prev + sep + text
            continue

        # Plain action line
        m = _PDF_ACTION_LINE.match(line)
        if m:
            rows.append(["", "", m.group(1), m.group(2).strip()])
            continue

        # Continuation — append to last row's description
        if rows:
            prev = rows[-1][3]
            sep = " " if prev and not prev[-1] in ("-", "–", " ") else ""
            rows[-1][3] = prev + sep + line

    return rows


def _take_n_versions(rows: list[list[str]], n: int) -> list[list[str]]:
    """Return only rows belonging to the first *n* version groups (date not empty)."""
    groups = 0
    result = []
    for row in rows:
        if row[0]:  # non-empty date marks the start of a new version group
            groups += 1
            if groups > n:
                break
        if groups > 0:
            result.append(row)
    return result


def _version_num(text: str) -> str:
    """Extract '17' from 'Version 17' or return '' when absent."""
    m = re.search(r"(\d+)", text or "")
    return m.group(1) if m else ""


def _display_date(text: str) -> str:
    """Convert ISO date to QuickSpecs-style dd-Mon-yyyy for table output."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})$", text or "")
    if not m:
        return text
    year, month, day = m.groups()
    mon = {
        "01": "Jan", "02": "Feb", "03": "Mar", "04": "Apr",
        "05": "May", "06": "Jun", "07": "Jul", "08": "Aug",
        "09": "Sep", "10": "Oct", "11": "Nov", "12": "Dec",
    }.get(month, month)
    return f"{day}-{mon}-{year}"


def _clean_change_description(text: str) -> str:
    """Remove markdown-table artifacts from flattened QuickSpecs text."""
    text = text.replace("|", " ")
    text = re.sub(r"(?:\s*-{3,}\s*)+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -–")


def _collect_change_details(markdown: str, sections: list[str]) -> dict[str, tuple[str, str]]:
    """Return {version_num: (action, description)} from Summary of Changes when present."""
    matched = next((s for s in sections if "summary of changes" in s.lower()), None)
    if not matched:
        return {}

    text = filter_section(markdown, matched)
    lines = text.splitlines()
    body = "\n".join(lines[1:]).lstrip("\n")
    parsed_rows = _parse_change_rows(body)

    changes: dict[str, list[tuple[str, str]]] = {}
    current_version = ""
    for date, version, action, description in parsed_rows:
        version_num = _version_num(version) or current_version
        if not version_num:
            continue
        current_version = version_num
        if not action and not description:
            continue
        changes.setdefault(version_num, []).append((action, description))

    result: dict[str, tuple[str, str]] = {}
    for version_num, entries in changes.items():
        actions = [a for a, _ in entries if a]
        descs = [d for _, d in entries if d]
        result[version_num] = (
            "; ".join(actions),
            " ".join(descs).strip(),
        )

    # Some QuickSpecs flatten multiple versions into one wrapped paragraph, so the
    # line-oriented parser above misses later descriptions. Recover those by scanning
    # the whole body for repeated "DD-Mon-YYYY Version N Action ..." blocks.
    normalized = re.sub(r"\s+", " ", body).strip()
    normalized = re.sub(
        r"^Date\s+Version\s+History\s+Action\s+Description\s+of\s+Change\s+",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    for m in _FLAT_CHANGE_BLOCK_RE.finditer(normalized):
        version_num = m.group("ver")
        action = m.group("action").strip()
        description = _clean_change_description(m.group("desc"))
        if not description and version_num in result:
            continue
        current_action, current_description = result.get(version_num, ("", ""))
        result[version_num] = (
            current_action or action,
            current_description or description,
        )
    return result


def _enrich_missing_change_details(
    doc_id: str,
    versions: list,
    change_details: dict[str, tuple[str, str]],
) -> dict[str, tuple[str, str]]:
    """Backfill missing descriptions from version-specific QuickSpec pages."""
    enriched = dict(change_details)
    for version in versions:
        current_action, current_description = enriched.get(version.version_num, ("", ""))
        if current_description:
            continue
        try:
            markdown, sections = fetch_quickspec_markdown(doc_id, ver=version.version_num)
        except Exception:
            continue
        specific_details = _collect_change_details(markdown, sections)
        action, description = specific_details.get(version.version_num, ("", ""))
        if action or description:
            enriched[version.version_num] = (action, description)
    return enriched


# ── Commands ───────────────────────────────────────────────────────────────────

def _cmd_list(args: argparse.Namespace) -> None:
    model = args.model
    get_console().print(f"[dim]Searching QuickSpecs for: {model}…[/dim]")

    try:
        entries = search_quickspecs(model, count=args.count)
    except Exception as exc:
        get_console().print(f"[red]Error:[/red] {exc}", highlight=False)
        sys.exit(1)

    if not entries:
        get_console().print("[yellow]No results found.[/yellow]")
        return

    # Deduplicate by doc_id — Coveo can index the same document multiple times
    seen: set[str] = set()
    unique: list[QSEntry] = []
    for e in entries:
        if e.doc_id not in seen:
            seen.add(e.doc_id)
            unique.append(e)

    top = unique[0]
    get_console().print(f"[dim]Fetching QuickSpec {top.doc_id}…[/dim]")
    try:
        versions = fetch_quickspec_versions(top.doc_id, title=top.title, n=args.count)
    except Exception as exc:
        get_console().print(f"[red]Error:[/red] {exc}", highlight=False)
        sys.exit(1)

    if not versions:
        get_console().print("[yellow]No version history found for this QuickSpec.[/yellow]")
        get_console().print(f"[dim]Use 'proliant qs describe {top.doc_id}' to read the full QuickSpec.[/dim]")
        return

    change_details: dict[str, tuple[str, str]] = {}
    try:
        markdown, sections = fetch_quickspec_markdown(top.doc_id)
        change_details = _collect_change_details(markdown, sections)
    except Exception:
        # Version history comes from the variants API; descriptions are best-effort only.
        pass
    change_details = _enrich_missing_change_details(top.doc_id, versions, change_details)

    rows = []
    for version in versions:
        action, description = change_details.get(version.version_num, ("", ""))
        rows.append([
            _display_date(version.date),
            f"Version {version.version_num}",
            action,
            description,
        ])

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        print_json({
            "doc_id": top.doc_id,
            "title": top.title,
            "revisions": [
                {
                    "date": version.date,
                    "version": f"Version {version.version_num}",
                    "action": row[2],
                    "description": row[3],
                }
                for version, row in zip(versions, rows)
            ],
        })
        return

    # ── Table output ──────────────────────────────────────────────────────────
    get_console().print()
    get_console().print(Rule(f"[bold]{top.title}[/bold]  [dim]{top.doc_id}[/dim]"))

    t = make_table(
        "",
        ("Date",                  {"no_wrap": True, "style": "cyan"}),
        ("Version",               {"no_wrap": True, "style": "green"}),
        ("Description of Change", {}),
        box_style=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )

    for row in rows:
        t.add_row(row[0], row[1], row[3])

    get_console().print(t)
    get_console().print(
        f"[dim]Use 'proliant qs describe {top.doc_id}' to read the full QuickSpec.[/dim]"
    )


def _cmd_describe(args: argparse.Namespace) -> None:
    # Resolve doc_id: either explicit or via --model (latest result)
    doc_id = args.doc_id if args.doc_id else None
    resolved_title: str | None = None
    if not doc_id:
        if not args.model:
            get_console().print(
                "[red]Error:[/red] provide a doc ID or --model <model>",
                highlight=False,
            )
            sys.exit(1)
        get_console().print(f"[dim]Looking up latest QuickSpec for: {args.model}…[/dim]")
        try:
            entries = search_quickspecs(args.model, count=1)
        except Exception as exc:
            get_console().print(f"[red]Error:[/red] {exc}", highlight=False)
            sys.exit(1)
        if not entries:
            get_console().print("[yellow]No QuickSpec found for that model.[/yellow]")
            sys.exit(1)
        doc_id = entries[0].doc_id
        resolved_title = entries[0].title
        get_console().print(
            f"[dim]Using latest: {doc_id} ({resolved_title})[/dim]"
        )

    get_console().print(f"[dim]Fetching QuickSpec {doc_id}…[/dim]")
    try:
        markdown, sections = fetch_quickspec_markdown(doc_id)
    except Exception as exc:
        get_console().print(f"[red]Error:[/red] {exc}", highlight=False)
        sys.exit(1)

    # ── JSON early return ─────────────────────────────────────────────────────
    if get_output_mode() == OutputMode.JSON:
        section_map = {s: filter_section(markdown, s) for s in sections}
        print_json({"doc_id": doc_id, "title": resolved_title or doc_id, "sections": section_map})
        return

    if args.list_sections:
        get_console().print("[bold]Available sections:[/bold]")
        for s in sections:
            get_console().print(f"  • {s}")
        return

    if args.section:
        # Resolve slug → real section name, then partial-match against doc sections
        resolved = _QS_SECTIONS.get(args.section, args.section)
        target = resolved.lower()
        matched = next((s for s in sections if target in s.lower()), None)
        if not matched:
            get_console().print(
                f"[yellow]Section '{resolved}' not found.[/yellow]\n"
                f"Available sections: {', '.join(sections)}"
            )
            sys.exit(1)
        text = filter_section(markdown, matched)
        lines = text.splitlines()
        heading = lines[0].lstrip("#").strip() if lines else matched
        body = "\n".join(lines[1:]).lstrip("\n")
        get_console().print(Rule(f"[bold]{heading}[/bold]"))
        _render_section_body(body)
        return

    # Full document — render section by section so option lists get table formatting
    for heading, body in _split_sections(markdown):
        if heading:
            get_console().print(Rule(f"[bold]{heading}[/bold]"))
        _render_section_body(body)


# ── diff ──────────────────────────────────────────────────────────────────────

_QS_SECTIONS: dict[str, str] = {
    "summary-of-changes":       "Summary of Changes",
    "overview":                  "Overview",
    "standard-features":         "Standard Features",
    "configuration-information": "Configuration Information",
    "core-options":              "Core Options",
    "additional-options":        "Additional Options",
    "service-and-support":       "Service and Support",
}


_PAGE_RE = re.compile(r"^\s*Page\s+\d+\s*$", re.IGNORECASE)


def _real_changes(diff_lines: list[str]) -> list[str]:
    """Return only +/- lines that are not page-number noise."""
    return [
        l for l in diff_lines
        if (l.startswith("+") or l.startswith("-"))
        and not l.startswith("+++") and not l.startswith("---")
        and not _PAGE_RE.match(l[1:])
    ]


def _section_map(markdown: str, sections: list[str]) -> dict[str, str]:
    """Return {section_name: body_text} for all sections."""
    return {s: filter_section(markdown, s) for s in sections}


    """Return {section_name: body_text} for all sections."""
    return {s: filter_section(markdown, s) for s in sections}


def _cmd_diff(args: argparse.Namespace) -> None:
    import difflib

    get_console().print(f"[dim]Looking up QuickSpec for: {args.model}…[/dim]")
    try:
        entries = search_quickspecs(args.model, count=1)
    except Exception as exc:
        get_console().print(f"[red]Error:[/red] {exc}", highlight=False)
        sys.exit(1)
    if not entries:
        get_console().print("[yellow]No QuickSpec found for that model.[/yellow]")
        sys.exit(1)

    doc_id = entries[0].doc_id
    title = entries[0].title

    try:
        versions = fetch_quickspec_versions(doc_id, title=title, n=20)
    except Exception as exc:
        get_console().print(f"[red]Error fetching version list:[/red] {exc}", highlight=False)
        sys.exit(1)

    if len(versions) < 2:
        get_console().print("[yellow]Only one version available — nothing to diff.[/yellow]")
        sys.exit(0)

    ver_map = {v.version_num: v for v in versions}
    if args.v1 and args.v2:
        if args.v1 not in ver_map or args.v2 not in ver_map:
            available = ", ".join(f"v{v.version_num} ({v.date})" for v in versions)
            get_console().print(f"[red]Version not found.[/red] Available: {available}")
            sys.exit(1)
        v_old = ver_map[args.v1]
        v_new = ver_map[args.v2]
        if int(v_old.version_num) > int(v_new.version_num):
            v_old, v_new = v_new, v_old
    else:
        v_new = versions[0]
        v_old = versions[1]

    get_console().print(
        f"[dim]Comparing v{v_old.version_num} ({v_old.date}) → "
        f"v{v_new.version_num} ({v_new.date})  [{title}][/dim]"
    )

    try:
        with get_console().status(f"[dim]Fetching v{v_new.version_num}…[/dim]"):
            md_new, secs_new = fetch_quickspec_markdown(doc_id, ver=v_new.version_num)
        with get_console().status(f"[dim]Fetching v{v_old.version_num}…[/dim]"):
            md_old, secs_old = fetch_quickspec_markdown(doc_id, ver=v_old.version_num)
    except Exception as exc:
        get_console().print(f"[red]Error fetching content:[/red] {exc}", highlight=False)
        sys.exit(1)

    sec_map_new = _section_map(md_new, secs_new)
    sec_map_old = _section_map(md_old, secs_old)
    all_sections = list(dict.fromkeys(secs_new + secs_old))

    # ── Detailed diff for a single section ────────────────────────────────────
    if args.section:
        resolved = _QS_SECTIONS.get(args.section, args.section)
        target = resolved.lower()
        matched = next((s for s in all_sections if target in s.lower()), None)
        if not matched:
            get_console().print(f"[yellow]Section '{resolved}' not found.[/yellow]")
            get_console().print(f"Available: {', '.join(all_sections)}")
            sys.exit(1)

        old_lines = sec_map_old.get(matched, "").splitlines()
        new_lines = sec_map_new.get(matched, "").splitlines()

        get_console().print(Rule(f"[bold]{matched}[/bold]  v{v_old.version_num} → v{v_new.version_num}"))
        diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=0))
        real = _real_changes(diff)
        if not real:
            get_console().print("[green]No changes in this section.[/green]")
            return
        for line in real:
            if line.startswith("+"):
                get_console().print(f"[green]{line[1:]}[/green]")
            else:
                get_console().print(f"[red]{line[1:]}[/red]")
        return

    # ── Full diff: all changed sections ──────────────────────────────────────
    any_change = False
    for sec in all_sections:
        old_text = sec_map_old.get(sec, "")
        new_text = sec_map_new.get(sec, "")

        diff = list(difflib.unified_diff(
            old_text.splitlines(), new_text.splitlines(), lineterm="", n=0
        ))
        real = _real_changes(diff)
        if not real and old_text and new_text:
            continue  # only noise (e.g. page numbers) — skip

        if old_text.strip() == new_text.strip():
            continue

        any_change = True
        if not old_text:
            get_console().print(Rule(f"[bold green]{sec}[/bold green]  [green](new section)[/green]"))
        elif not new_text:
            get_console().print(Rule(f"[bold red]{sec}[/bold red]  [red](removed)[/red]"))
        else:
            get_console().print(Rule(f"[bold]{sec}[/bold]"))

        for line in real:
            if line.startswith("+"):
                get_console().print(f"[green]{line[1:]}[/green]")
            else:
                get_console().print(f"[red]{line[1:]}[/red]")

    if not any_change:
        get_console().print("[green]No differences found between these two versions.[/green]")


# ── Argument parser ────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Browse HPE QuickSpecs documents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  proliant qs list --model dl380gen12               List QuickSpec revisions for DL380 Gen12
  proliant qs list --model "DL360 Gen11"            List revisions for DL360 Gen11
  proliant qs describe a00073551enw                 Show full QuickSpec (latest DL380 Gen12)
  proliant qs describe --model dl380gen12           Fetch latest DL380 Gen12 QuickSpec
  proliant qs describe a00073551enw --list-sections List sections in the document
  proliant qs describe a00073551enw --section "Standard Features"
""",
    )
    p.add_argument("--json", action="store_true", dest="json_output",
                   help="Output as JSON (for piping/scripting)")
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
        choices=list(_QS_SECTIONS),
        help="Show only this section, e.g. standard-features",
    )
    p_desc.add_argument(
        "--list-sections", "-l",
        action="store_true",
        help="List available section names, then exit",
    )

    # ── diff ──────────────────────────────────────────────────────────────────
    p_diff = sub.add_parser("diff", help="Compare two versions of a QuickSpec")
    p_diff.add_argument(
        "--model", "-m",
        required=True,
        metavar="MODEL",
        help="Server model, e.g. dl380gen12, 'DL360 Gen11'",
    )
    p_diff.add_argument(
        "--v1",
        metavar="N",
        help="Older version number (default: second-latest)",
    )
    p_diff.add_argument(
        "--v2",
        metavar="N",
        help="Newer version number (default: latest)",
    )
    p_diff.add_argument(
        "--section", "-s",
        metavar="SECTION",
        choices=list(_QS_SECTIONS),
        help="Show detailed line diff for this section only",
    )

    return p


# ── Entry point ────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass
    args = parser.parse_args(argv)

    if getattr(args, "json_output", False):
        set_output_mode(OutputMode.JSON)

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.cmd == "list":
        _cmd_list(args)
    elif args.cmd == "describe":
        _cmd_describe(args)
    elif args.cmd == "diff":
        _cmd_diff(args)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
