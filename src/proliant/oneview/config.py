"""
proliant.oneview.config
~~~~~~~~~~~~~~~~~~~~
Load OneView appliance connection details from inventory.ini.

Add an [oneview] section to your inventory.ini::

    [oneview]
    host     = 192.168.1.100
    username = Administrator
    password = yourpassword

Multiple appliances are supported: give each its own section name and add
``type = oneview`` (``proliant setup`` does this for you)::

    [datacenter-a]
    host     = 192.168.1.100
    username = Administrator
    password = yourpassword
    type     = oneview

    [datacenter-b]
    host     = 192.168.2.100
    username = Administrator
    password = yourpassword
    type     = oneview

With more than one configured, ``proliant oneview`` commands always target
the *active* appliance -- see ``proliant oneview appliances list`` /
``proliant oneview appliances use <name>`` to view/switch it.
"""

from __future__ import annotations

import configparser
import json
import os
import sys
from pathlib import Path

from proliant.common import config_dir
from proliant.common.platform import is_frozen


def _find_config_file() -> Path:
    """Same search order as ilo/config.py — first match wins.

    When no candidate exists on disk, fall back to the canonical
    ``~/.config/proliant-cli/inventory.ini`` location — the same one
    ``ilo/config.py`` uses and ``proliant setup`` writes to — so error
    messages always point somewhere consistent and actionable, instead of
    whatever the current working directory happens to be.
    """
    if env := os.environ.get("PCLI_CONFIG"):
        return Path(env)

    candidates = [Path.cwd() / "inventory.ini"]
    if is_frozen():
        candidates.append(Path(sys.executable).parent / "inventory.ini")
    else:
        candidates.append(Path(__file__).parent.parent.parent.parent / "inventory.ini")
    candidates.append(config_dir() / "inventory.ini")

    for p in candidates:
        if p.exists():
            return p
    return config_dir() / "inventory.ini"


def _state_file() -> Path:
    """Where the active-appliance selection is persisted (local, not synced)."""
    return config_dir() / "oneview_state.json"


def _read_state() -> dict:
    path = _state_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def list_oneview_appliances() -> list[dict[str, str]]:
    """Return every configured OneView appliance as a dict of connection details.

    A section counts as a OneView appliance if it's either literally named
    ``[oneview]`` or has ``type = oneview`` (the pattern ``proliant setup``
    writes and documents in inventory.ini). Order matches inventory.ini.
    Returns an empty list if inventory.ini doesn't exist or has no OneView
    sections -- callers decide whether that's an error.
    """
    config_file = _find_config_file()
    if not config_file.exists():
        return []

    # interpolation=None: passwords may legitimately contain a literal '%'.
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(config_file)

    appliances: list[dict[str, str]] = []
    for section in cfg.sections():
        if section.lower() == "defaults":
            continue
        is_oneview = (
            section.lower() == "oneview"
            or cfg.get(section, "type", fallback="").strip().lower() == "oneview"
        )
        if not is_oneview:
            continue

        host = cfg.get(section, "host", fallback="").strip()
        if not host:
            raise ValueError(f"[{section}] section in {config_file} is missing the 'host' key")

        appliances.append({
            "name": section,
            "host": host,
            "url": f"https://{host}",
            "username": cfg.get(section, "username", fallback="Administrator"),
            "password": cfg.get(section, "password", fallback=""),
        })
    return appliances


def get_active_appliance_name(appliances: list[dict[str, str]] | None = None) -> str | None:
    """Return the name of the currently-active OneView appliance, or None if none configured.

    Falls back to the first configured appliance (inventory.ini order) when
    nothing has been explicitly selected yet, or the previously-selected one
    was since removed from inventory.ini -- so a fresh multi-appliance setup
    still has a sane default instead of erroring. Use
    ``proliant oneview appliances use <name>`` to pin a different one.
    """
    if appliances is None:
        appliances = list_oneview_appliances()
    if not appliances:
        return None

    active = _read_state().get("active_appliance")
    names = {a["name"] for a in appliances}
    if active in names:
        return active
    return appliances[0]["name"]


def set_active_appliance(name: str) -> str:
    """Persist ``name`` as the active OneView appliance. Returns the resolved name.

    Matching is case-insensitive against configured section names.

    Raises
    ------
    ValueError
        If no appliance with that name is configured.
    """
    appliances = list_oneview_appliances()
    for a in appliances:
        if a["name"].lower() == name.lower():
            state = _read_state()
            state["active_appliance"] = a["name"]
            _write_state(state)
            return a["name"]

    known = ", ".join(a["name"] for a in appliances) or "(none configured — run 'proliant setup')"
    raise ValueError(f"OneView appliance '{name}' not found. Known appliances: {known}")


def load_oneview_config(name: str | None = None) -> dict[str, str]:
    """Return OneView connection details for one configured appliance.

    Returns dict with keys: name, host, username, password, url.

    Parameters
    ----------
    name:
        If provided, return the appliance whose section name matches
        (case-insensitive), raising ``ValueError`` if not found. If omitted,
        returns the active appliance -- the one set via
        ``proliant oneview appliances use <name>``, or the only configured
        appliance, or (with 2+ configured and none selected yet) the first
        one in inventory.ini order.

    Raises
    ------
    FileNotFoundError
        If inventory.ini is not found.
    ValueError
        If no OneView section exists, or ``name`` doesn't match any.
    """
    config_file = _find_config_file()
    if not config_file.exists():
        raise FileNotFoundError(
            f"inventory.ini not found. Expected at: {config_file}\n"
            "Run 'proliant setup' to add one."
        )

    appliances = list_oneview_appliances()
    if not appliances:
        raise ValueError(
            f"No OneView section found in {config_file}.\n"
            "Run 'proliant setup' to add one, or add it by hand:\n\n"
            "  [my-oneview]\n"
            "  host     = <oneview-appliance-ip>\n"
            "  username = Administrator\n"
            "  password = yourpassword\n"
            "  type     = oneview\n"
        )

    if name is not None:
        for a in appliances:
            if a["name"].lower() == name.lower():
                return a
        known = ", ".join(a["name"] for a in appliances)
        raise ValueError(f"OneView appliance '{name}' not found. Known appliances: {known}")

    active_name = get_active_appliance_name(appliances)
    for a in appliances:
        if a["name"] == active_name:
            return a
    return appliances[0]  # pragma: no cover — defensive fallback, unreachable in practice
