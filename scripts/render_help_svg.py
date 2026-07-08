#!/usr/bin/env python3
"""Render a CLI --help text capture into a styled, fake-terminal SVG.

Matches the look of docs/assets/demo.svg (Dracula palette, macOS-style
traffic-light window chrome) so all docs screenshots share one visual
style without needing real OS-level screen capture or a GUI terminal.

Usage:
    proliant ilo --help > /tmp/help-ilo.txt
    python scripts/render_help_svg.py /tmp/help-ilo.txt docs/assets/help-ilo.svg \
        --prompt "proliant ilo --help"
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

HEADING_RE = re.compile(r"^(usage:|commands:|examples:|options:|positional arguments:)", re.I)


def esc(s: str) -> str:
    return html.escape(s, quote=False)


def render_line(line: str, prompt):
    if prompt is not None:
        return (
            f'<tspan fill="{PROMPT}">PS C:\\&gt; </tspan>'
            f'<tspan fill="{FG}">{esc(prompt)}</tspan>'
        )
    if HEADING_RE.match(line):
        return f'<tspan fill="{HEADING}">{esc(line)}</tspan>'
    if line.strip().startswith("#"):
        return f'<tspan fill="{DIM}">{esc(line)}</tspan>'
    return f'<tspan fill="{FG}">{esc(line)}</tspan>'


def build_svg(lines, title, prompt):
    all_render_lines = [""] + lines
    max_len = max([len(prompt) + 4] + [len(l) for l in lines]) if lines else len(prompt)
    width = int(max_len * CHAR_WIDTH) + PADDING_X * 2
    width = max(width, 620)
    height = PADDING_TOP + (len(all_render_lines) - 1) * LINE_HEIGHT + PADDING_BOTTOM

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
    svg_lines.append(f'<text x="{PADDING_X}" y="{y}" xml:space="preserve">{render_line("", prompt)}</text>')
    for line in lines:
        y += LINE_HEIGHT
        svg_lines.append(f'<text x="{PADDING_X}" y="{y}" xml:space="preserve">{render_line(line, None)}</text>')
    svg_lines.append("</svg>")
    return "\n".join(svg_lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="Path to a captured --help text file")
    ap.add_argument("output", help="Path to write the rendered .svg")
    ap.add_argument("--title", default="Windows PowerShell", help="Fake terminal window title")
    ap.add_argument("--prompt", required=True, help="Command shown after the fake PS C:\\> prompt")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        text = f.read()
    lines = text.rstrip("\n").split("\n")

    svg = build_svg(lines, args.title, args.prompt)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"Wrote {args.output} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
