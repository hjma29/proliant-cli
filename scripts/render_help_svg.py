#!/usr/bin/env python3
"""Render captured CLI output into a styled, fake-terminal SVG.

Matches the look of docs/assets/demo.svg (Dracula palette, macOS-style
traffic-light window chrome) so all docs screenshots share one visual
style without needing real OS-level screen capture or a GUI terminal.

Combine one or more real commands (each a fake "PS C:\\>" prompt line
followed by its captured output) into a single screenshot:

    proliant ilo servers list  > /tmp/ilo-servers.txt
    proliant ilo firmware list > /tmp/ilo-firmware.txt
    python scripts/render_help_svg.py docs/assets/ilo-screenshot.svg \\
        --cmd "proliant ilo servers list" /tmp/ilo-servers.txt \\
        --cmd "proliant ilo firmware list" /tmp/ilo-firmware.txt

Coloring is auto-detected from the captured text: box-drawing borders
and the header row of a bordered table are dimmed, the first content
line under a prompt (the table title) is highlighted, and "--- ... ---"
style section headers are highlighted like a heading.
"""
import argparse
import html
import re

FONT_FAMILY = 'Consolas,"Courier New",monospace'
FONT_SIZE = 12
CHAR_WIDTH = 7.21  # measured advance width for Consolas 12px
LINE_HEIGHT = 17
HEADER_HEIGHT = 34
PADDING_X = 18
PADDING_TOP = 53  # first text line y (matches demo.svg baseline)
PADDING_BOTTOM = 24

BG = "#282a36"
CHROME = "#1e1f29"
FG = "#f8f8f2"
PROMPT = "#50fa7b"
DIM = "#6272a4"
HEADING = "#f1fa8c"
TITLE = "#bd93f9"

HEADING_RE = re.compile(r"^(usage:|commands:|examples:|options:|positional arguments:)", re.I)
DASH_HEADING_RE = re.compile(r"^-{2,}\s.*\s-{2,}$")
BOX_ONLY_RE = re.compile(r"^[\s\-─│┌┐└┘├┤┬┴┼╭╮╰╯]+$")


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def render_prompt(prompt: str) -> str:
    return (
        f'<tspan fill="{PROMPT}">PS C:\\&gt; </tspan>'
        f'<tspan fill="{FG}">{esc(prompt)}</tspan>'
    )


def colorize_block(lines):
    """Return a list of (line, color) for one command's captured output."""
    out = []
    title_done = False
    force_header_next = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            out.append((line, FG))
            continue
        if BOX_ONLY_RE.match(line):
            out.append((line, DIM))
            force_header_next = "┌" in line
            continue
        if force_header_next:
            out.append((line, DIM))
            force_header_next = False
            continue
        if HEADING_RE.match(line) or DASH_HEADING_RE.match(line):
            out.append((line, HEADING))
            title_done = True
            continue
        if not title_done:
            out.append((line, TITLE))
            title_done = True
            continue
        out.append((line, FG))
    return out


def build_svg(blocks, title):
    """blocks: list of (prompt, [captured output lines])."""
    rendered_rows = []
    for prompt, lines in blocks:
        rendered_rows.append(render_prompt(prompt))
        for line, color in colorize_block(lines):
            rendered_rows.append(f'<tspan fill="{color}">{esc(line)}</tspan>')

    all_lines_for_width = []
    for prompt, lines in blocks:
        all_lines_for_width.append(f"PS C:> {prompt}")
        all_lines_for_width.extend(lines)
    max_len = max(len(l) for l in all_lines_for_width) if all_lines_for_width else 40
    width = int(max_len * CHAR_WIDTH) + PADDING_X * 2
    width = max(width, 620)
    height = PADDING_TOP + (len(rendered_rows) - 1) * LINE_HEIGHT + PADDING_BOTTOM

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">',
        f'<style>text{{font-family:{FONT_FAMILY};font-size:{FONT_SIZE}px;}}</style>',
        f'<rect width="{width}" height="{height}" rx="10" fill="{BG}"/>',
        f'<rect width="{width}" height="{HEADER_HEIGHT}" rx="10" fill="{CHROME}"/>',
        f'<rect y="24" width="{width}" height="10" fill="{CHROME}"/>',
        '<circle cx="20" cy="17" r="6" fill="#ff5555"/>',
        '<circle cx="40" cy="17" r="6" fill="#f1fa8c"/>',
        '<circle cx="60" cy="17" r="6" fill="#50fa7b"/>',
        f'<text x="{width // 2}" y="22" text-anchor="middle" fill="{DIM}" font-size="12">{esc(title)}</text>',
    ]

    y = PADDING_TOP
    svg_lines.append(f'<text x="{PADDING_X}" y="{y}" xml:space="preserve">{rendered_rows[0]}</text>')
    for row in rendered_rows[1:]:
        y += LINE_HEIGHT
        svg_lines.append(f'<text x="{PADDING_X}" y="{y}" xml:space="preserve">{row}</text>')
    svg_lines.append("</svg>")
    return "\n".join(svg_lines)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("output", help="Path to write the rendered .svg")
    ap.add_argument(
        "--cmd",
        nargs=2,
        action="append",
        metavar=("PROMPT", "FILE"),
        required=True,
        help="A fake prompt command and the file with its captured output. Repeatable.",
    )
    ap.add_argument("--title", default="Windows PowerShell", help="Fake terminal window title")
    args = ap.parse_args()

    blocks = []
    for prompt, path in args.cmd:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = text.rstrip("\n").split("\n")
        # Drop a single leading blank line some captures start with.
        if lines and lines[0].strip() == "":
            lines = lines[1:]
        blocks.append((prompt, lines))

    svg = build_svg(blocks, args.title)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(svg)
    total_lines = sum(len(lines) for _, lines in blocks)
    print(f"Wrote {args.output} ({len(blocks)} command(s), {total_lines} output lines)")


if __name__ == "__main__":
    main()
