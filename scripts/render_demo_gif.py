#!/usr/bin/env python3
"""Render captured CLI output into an animated, fake-terminal demo GIF.

Same Dracula-palette, macOS-style window chrome as render_help_svg.py, but
produces a scrolling, typed-out animation instead of a static SVG -- built
from real captured command output (no VHS/ttyd/headless-browser recording
involved, which is unreliable in this environment).

Usage:
    proliant ilo servers describe dl380-gen11 > c1.txt
    proliant com servers list                 > c2.txt
    python scripts/render_demo_gif.py docs/assets/demo.gif \\
        --cmd "proliant ilo servers describe dl380-gen11" c1.txt \\
        --cmd "proliant com servers list" c2.txt
"""
import argparse
import html
import re

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = r"C:\Windows\Fonts\consola.ttf"
FONT_SIZE = 16
LINE_HEIGHT = 21
HEADER_HEIGHT = 40
PADDING_X = 20
PADDING_TOP = 16
PADDING_BOTTOM = 16
MAX_VISIBLE_LINES = 30

BG = "#282a36"
CHROME = "#1e1f29"
FG = "#f8f8f2"
PROMPT = "#50fa7b"
DIM = "#6272a4"
HEADING = "#f1fa8c"
TITLE = "#bd93f9"

HEADING_RE = re.compile(r"^\s*(usage:|commands:|examples:|options:|positional arguments:)", re.I)
BOX_ONLY_RE = re.compile(r"^[\s\-─│┌┐└┘├┤┬┴┼╭╮╰╯]+$")

# Consolas is missing a few symbols used in real CLI output; swap in glyphs
# it does have so nothing renders as a "tofu" box.
GLYPH_SUBSTITUTIONS = {
    "\u25c0": "<-",  # ◀ BLACK LEFT-POINTING TRIANGLE
    "\u25b6": "->",  # ▶ BLACK RIGHT-POINTING TRIANGLE
}


def sanitize(line: str) -> str:
    for src, dst in GLYPH_SUBSTITUTIONS.items():
        line = line.replace(src, dst)
    return line

TYPING_STEP = 3          # characters revealed per typing frame
TYPING_FRAME_MS = 55
POST_ENTER_FRAME_MS = 550
HOLD_FRAME_MS = 2400
FINAL_HOLD_MS = 3200


def classify(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return FG
    if BOX_ONLY_RE.match(line):
        return DIM
    if HEADING_RE.match(line):
        return HEADING
    return FG


def load_scenes(cmds):
    """cmds: list of (prompt, path). Returns list of (prompt, [output lines])."""
    scenes = []
    for prompt, path in cmds:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        lines = [sanitize(l) for l in text.rstrip("\n").split("\n")]
        # Drop a single leading/trailing blank line some captures have.
        while lines and lines[0].strip() == "":
            lines = lines[1:]
        while lines and lines[-1].strip() == "":
            lines = lines[:-1]
        scenes.append((prompt, lines))
    return scenes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("output", help="Path to write the rendered .gif")
    ap.add_argument("--cmd", nargs=2, action="append", metavar=("PROMPT", "FILE"), required=True,
                     help="A real command and the file with its captured output. Repeatable, in order.")
    ap.add_argument("--title", default="Windows PowerShell", help="Fake terminal window title")
    args = ap.parse_args()

    scenes = load_scenes(args.cmd)

    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    bold_font = ImageFont.truetype(FONT_PATH.replace("consola.ttf", "consolab.ttf"), FONT_SIZE)
    char_w = font.getlength("M")

    # Compute canvas width from the widest line across every scene.
    all_lines = ["PS C:\\> " + p for p, _ in scenes]
    for _, lines in scenes:
        all_lines.extend(lines)
    max_len = max(len(l) for l in all_lines) if all_lines else 60
    width = int(max_len * char_w) + PADDING_X * 2
    width = max(width, 760)
    height = HEADER_HEIGHT + PADDING_TOP + MAX_VISIBLE_LINES * LINE_HEIGHT + PADDING_BOTTOM

    frames = []
    durations = []

    def new_canvas():
        img = Image.new("RGB", (width, height), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, width, HEADER_HEIGHT], fill=CHROME)
        for i, color in enumerate(["#ff5555", "#f1fa8c", "#50fa7b"]):
            d.ellipse([20 + i * 22 - 7, HEADER_HEIGHT // 2 - 7, 20 + i * 22 + 7, HEADER_HEIGHT // 2 + 7], fill=color)
        title_w = d.textlength(args.title, font=font)
        d.text(((width - title_w) / 2, HEADER_HEIGHT // 2 - FONT_SIZE / 2 - 1), args.title, font=font, fill=DIM)
        return img, d

    def render(visible_rows, cursor=False):
        """visible_rows: list of (text, color). Draw bottom-anchored within the viewport."""
        rows = visible_rows[-MAX_VISIBLE_LINES:]
        img, d = new_canvas()
        y = HEADER_HEIGHT + PADDING_TOP
        for text, color in rows:
            d.text((PADDING_X, y), text, font=font, fill=color)
            y += LINE_HEIGHT
        if cursor:
            cx = PADDING_X + d.textlength(rows[-1][0], font=font) if rows else PADDING_X
            cy = y - LINE_HEIGHT
            d.rectangle([cx + 2, cy + 2, cx + 2 + char_w * 0.55, cy + LINE_HEIGHT - 4], fill=PROMPT)
        return img

    scrollback = []  # list of (text, color) already "printed"

    for prompt, out_lines in scenes:
        full_cmd = "PS C:\\> " + prompt
        # Typing animation.
        for n in range(TYPING_STEP, len(full_cmd) + TYPING_STEP, TYPING_STEP):
            partial = full_cmd[:n]
            rows = scrollback + [(partial, FG)]
            img = render(rows, cursor=True)
            frames.append(img)
            durations.append(TYPING_FRAME_MS)
        # Commit the fully-typed command line to scrollback (recolor prompt prefix).
        scrollback.append((full_cmd, FG))
        # Brief pause simulating command execution.
        img = render(scrollback)
        frames.append(img)
        durations.append(POST_ENTER_FRAME_MS)
        # Print output all at once (real CLI output isn't typed).
        for line in out_lines:
            scrollback.append((line, classify(line)))
        scrollback.append(("", FG))  # blank separator line
        img = render(scrollback)
        frames.append(img)
        durations.append(HOLD_FRAME_MS)

    durations[-1] = FINAL_HOLD_MS

    frames[0].save(
        args.output,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(f"Wrote {args.output} ({len(frames)} frames, {width}x{height})")


if __name__ == "__main__":
    main()
