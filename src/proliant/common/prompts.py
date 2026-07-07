"""
proliant.common.prompts
~~~~~~~~~~~~~~~~~~~~~~~~
Shared interactive-prompt helpers used across proliant subcommands
(`com login`, `setup`, ...).
"""

from __future__ import annotations

import asyncio
import sys


def _read_password_masked_windows(prompt: str) -> str:
    """Read a password on Windows, echoing '*' per character (typed or pasted)."""
    import msvcrt

    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf: list[str] = []
    while True:
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            break
        if ch == "\x03":  # Ctrl+C
            raise KeyboardInterrupt
        if ch == "\x08":  # Backspace
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if ch in ("\x00", "\xe0"):  # arrow/function-key prefix — swallow the next code
            msvcrt.getwch()
            continue
        buf.append(ch)
        sys.stdout.write("*")
        sys.stdout.flush()
    return "".join(buf)


def _read_password_masked_posix(prompt: str) -> str:
    """Read a password on POSIX, echoing '*' per character (typed or pasted)."""
    import termios
    import tty

    sys.stdout.write(prompt)
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    buf: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                break
            if ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            if ch in ("\x7f", "\x08"):  # Backspace / Delete
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            buf.append(ch)
            sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\r\n")
        sys.stdout.flush()
    return "".join(buf)


def prompt_password(prompt: str = "Password: ") -> str:
    """Prompt for a password, echoing '*' per character with paste + backspace support.

    Uses a pure-stdlib console reader (msvcrt on Windows, termios on POSIX) so it works
    in the compiled standalone binary without depending on prompt_toolkit. Falls back to
    getpass (no echo) only when stdin isn't an interactive TTY.
    """
    if not getattr(sys, "stdin", None) or not sys.stdin.isatty():
        import getpass
        return getpass.getpass(prompt)
    try:
        if sys.platform == "win32":
            return _read_password_masked_windows(prompt)
        return _read_password_masked_posix(prompt)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        import getpass
        return getpass.getpass(prompt)


async def prompt_password_async(prompt: str = "Password: ") -> str:
    """Async wrapper around the masked password reader."""
    return await asyncio.to_thread(prompt_password, prompt)
