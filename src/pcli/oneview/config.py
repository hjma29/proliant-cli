"""
pcli.oneview.config
~~~~~~~~~~~~~~~~~~~~
Load OneView appliance connection details from hosts-ilo.ini.

Add an [oneview] section to your hosts-ilo.ini::

    [oneview]
    host     = 192.168.1.100
    username = Administrator
    password = yourpassword
"""

from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path


def _find_config_file() -> Path:
    """Same search order as ilo/config.py — first match wins."""
    if env := os.environ.get("PCLI_CONFIG"):
        return Path(env)

    candidates = [Path.cwd() / "hosts-ilo.ini"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "hosts-ilo.ini")
    else:
        candidates.append(Path(__file__).parent.parent.parent.parent / "hosts-ilo.ini")
    candidates.append(Path.home() / ".config" / "pcli" / "hosts-ilo.ini")

    for p in candidates:
        if p.exists():
            return p
    return Path.cwd() / "hosts-ilo.ini"


def load_oneview_config() -> dict[str, str]:
    """Return OneView connection details from the [oneview] section.

    Returns dict with keys: host, username, password, url.

    Raises
    ------
    FileNotFoundError
        If hosts-ilo.ini is not found.
    ValueError
        If [oneview] section or required 'host' key is missing.
    """
    config_file = _find_config_file()
    if not config_file.exists():
        raise FileNotFoundError(
            f"hosts-ilo.ini not found. Expected at: {config_file}\n"
            "Run 'pcli ilo init' to create a starter config."
        )

    cfg = configparser.ConfigParser()
    cfg.read(config_file)

    if not cfg.has_section("oneview"):
        raise ValueError(
            f"No [oneview] section found in {config_file}.\n"
            "Add one:\n\n"
            "  [oneview]\n"
            "  host     = <oneview-appliance-ip>\n"
            "  username = Administrator\n"
            "  password = yourpassword\n"
        )

    host = cfg.get("oneview", "host", fallback="").strip()
    if not host:
        raise ValueError(f"[oneview] section in {config_file} is missing the 'host' key")

    return {
        "host": host,
        "url": f"https://{host}",
        "username": cfg.get("oneview", "username", fallback="Administrator"),
        "password": cfg.get("oneview", "password", fallback=""),
    }
