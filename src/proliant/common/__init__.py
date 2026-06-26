"""proliant.common — shared utilities across all proliant modules."""
from __future__ import annotations

from pathlib import Path


def config_dir() -> Path:
    """User config root: ~/.config/proliant-cli/ (all platforms)."""
    return Path.home() / ".config" / "proliant-cli"


def cache_dir() -> Path:
    """User cache root: ~/.cache/proliant-cli/ (all platforms)."""
    return Path.home() / ".cache" / "proliant-cli"
