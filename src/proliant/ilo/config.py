"""
hpeilo.config
~~~~~~~~~~~~~
Central location for all tunable constants and inventory loading.

Putting constants here means a single edit propagates to every module
that imports them — no hunting through multiple files.
"""

from pathlib import Path
import configparser
import os

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
# Config file — inventory.ini uses simple INI format, no YAML indentation required.
# Search order (first match wins):
#   1. PCLI_CONFIG env var                        (explicit override)
#   2. ./inventory.ini                            (local/project override)
#   3. ~/.config/proliant-cli/inventory.ini       (default — created by 'proliant ilo init')
# ---------------------------------------------------------------------------

def _find_config_file() -> Path:
    if env := os.environ.get("PCLI_CONFIG"):
        return Path(env)

    from proliant.common import config_dir
    candidates = [
        Path.cwd() / "inventory.ini",
        config_dir() / "inventory.ini",
    ]

    for p in candidates:
        if p.exists():
            return p
    # Return config dir path so error messages point to the right place
    return config_dir() / "inventory.ini"


HOSTS_FILE: Path = _find_config_file()


def load_hosts(name: str | None = None) -> list[dict]:
    """Load and return the list of iLO host dicts from inventory.ini.

    inventory.ini format::

        [defaults]
        username = Administrator
        password = yourpassword

        [my-server]
        host = 192.168.1.10

        [other-server]
        host = myilo.example.com
        username = localadmin
        password = differentpass

    Returns dicts with keys: name, url, username, password.

    Parameters
    ----------
    name:
        If provided, return only the host whose section name matches.
        Raises ``ValueError`` if no match is found.

    Raises
    ------
    FileNotFoundError
        If inventory.ini does not exist.
    ValueError
        If ``name`` is specified but no matching host is found, or a
        server section is missing the required ``host`` key.
    """
    if not HOSTS_FILE.exists():
        raise FileNotFoundError(HOSTS_FILE)

    cfg = configparser.ConfigParser()
    try:
        cfg.read(HOSTS_FILE)
    except configparser.Error as exc:
        # Extract line number from the exception message if present
        import re
        line_hint = ""
        m = re.search(r"\[line (\d+)\]", str(exc))
        if m:
            line_hint = f" (line {m.group(1)})"
        raise ValueError(
            f"inventory.ini has a syntax error{line_hint}: {exc}\n\n"
            f"  Common cause: a key=value line is outside any section header.\n"
            f"  Every entry must belong to a [section]. For example:\n\n"
            f"    [my-server]          # iLO host\n"
            f"    host = 192.0.2.10\n\n"
            f"    [my-oneview]         # OneView appliance (skipped by 'proliant ilo')\n"
            f"    host = 192.0.2.20\n"
            f"    type = oneview\n\n"
            f"  Check {HOSTS_FILE}"
        ) from exc

    default_user = cfg.get("defaults", "username", fallback="")
    default_pass = cfg.get("defaults", "password", fallback="")

    hosts: list[dict] = []
    for section in cfg.sections():
        if section.lower() == "defaults":
            continue
        # Skip non-iLO entries (e.g. type = oneview) so inventory.ini can
        # hold other appliance addresses without polluting iLO commands.
        host_type = cfg.get(section, "type", fallback="ilo").strip().lower()
        if host_type != "ilo" or "oneview" in section.lower():
            continue
        host_addr = cfg.get(section, "host", fallback="").strip()
        if not host_addr:
            raise ValueError(f"Section [{section}] in inventory.ini is missing the 'host' key")
        hosts.append({
            "name": section,
            "url": f"https://{host_addr}",
            "username": cfg.get(section, "username", fallback=default_user),
            "password": cfg.get(section, "password", fallback=default_pass),
        })

    if name is not None:
        matched = [h for h in hosts if h["name"] == name]
        if not matched:
            known = ", ".join(h["name"] for h in hosts)
            raise ValueError(f"Host '{name}' not found in inventory.ini. Known hosts: {known}")
        return matched

    return hosts
