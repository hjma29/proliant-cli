"""
hpeilo.config
~~~~~~~~~~~~~
Central location for all tunable constants and hosts.yml loading.

Putting constants here means a single edit propagates to every module
that imports them — no hunting through multiple files.
"""

from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Table display widths (characters).  Adjust here; every print function
# in cli.py imports these — no magic numbers scattered across the codebase.
# ---------------------------------------------------------------------------
COL_SERVER_WIDTH: int = 20
COL_ILO_WIDTH: int = 10
COL_NIC_WIDTH: int = 40
COL_NAME_WIDTH: int = 65

# Maximum parallel iLO sessions.  Increase for larger fleets, but iLOs will
# reject connections if you saturate their session limit (~10 concurrent).
MAX_WORKERS: int = 10

# ---------------------------------------------------------------------------
# Hosts file — credentials live outside source control in inventory/hosts.yml
# Path(__file__).parent.parent resolves to the repo root regardless of where
# the script is invoked from.
# ---------------------------------------------------------------------------
import os
import sys

def _find_hosts_file() -> Path:
    """Resolve hosts.yml from several well-known locations.

    Search order:
      1. HPEILO_HOSTS env var (explicit override)
      2. ~/.config/pcli/ilo/hosts.yml  (installed/distributed users)
      3. ./hosts.yml                 (current directory)
      4. ./inventory/hosts.yml       (repo dev layout)
      5. <binary dir>/inventory/hosts.yml  (PyInstaller bundle)
    """
    if env := os.environ.get("PCLI_ILO_HOSTS"):
        return Path(env)
    candidates = [
        Path.home() / ".config" / "pcli" / "ilo" / "hosts.yml",
        Path.cwd() / "hosts.yml",
        Path.cwd() / "inventory" / "hosts.yml",
    ]
    # When frozen by PyInstaller, sys.executable is the binary path
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "inventory" / "hosts.yml")
    else:
        # Dev: relative to this source file  (src/hpeilo/ → repo root)
        candidates.append(Path(__file__).parent.parent.parent / "inventory" / "hosts.yml")

    for p in candidates:
        if p.exists():
            return p
    # Return the preferred user path so the FileNotFoundError message is helpful
    return Path.home() / ".config" / "pcli" / "ilo" / "hosts.yml"

HOSTS_FILE: Path = _find_hosts_file()


def load_hosts(name: str | None = None) -> list[dict]:
    """Load and return the list of iLO host dicts from hosts.yml.

    Each entry must have: name, url, username, password.

    Parameters
    ----------
    name:
        If provided, return only the host whose ``name`` field matches.
        Raises ``ValueError`` if no match is found.

    Raises
    ------
    FileNotFoundError
        If hosts.yml does not exist at HOSTS_FILE.
    KeyError
        If hosts.yml is missing the top-level 'ilos' key.
    ValueError
        If ``name`` is specified but no matching host is found.
    """
    with HOSTS_FILE.open() as fh:
        # safe_load prevents arbitrary Python object instantiation from
        # untrusted YAML — always prefer it over yaml.load().
        hosts: list[dict] = yaml.safe_load(fh)["ilos"]

    if name is not None:
        matched = [h for h in hosts if h["name"] == name]
        if not matched:
            known = ", ".join(h["name"] for h in hosts)
            raise ValueError(f"Host '{name}' not found in hosts.yml. Known hosts: {known}")
        return matched

    return hosts
