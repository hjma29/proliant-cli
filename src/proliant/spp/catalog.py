"""
proliant.spp.catalog
~~~~~~~~~~~~~~~~~~~~
Fetch and parse HPE SPP (Service Pack for ProLiant) catalog metadata.

Catalogs live at:
  https://downloads.linux.hpe.com/SDR/repo/spp-gen{N}/{version}/manifest/metadata.json

Catalogs are cached locally under:
  <repo-root>/spp/gen{N}/{version}/metadata.json

The metadata.json file is ~5–8 MB per SPP version — no need to download the
full 10 GB ISO.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


SDR_BASE = "https://downloads.linux.hpe.com/SDR/repo"
_UA = {"User-Agent": "proliant/1.0"}

# Supported generations (canonical string form)
SUPPORTED_GENS = ("gen10", "gen11", "gen12")


def _norm_gen(gen: str | int) -> str:
    """Normalise gen argument to canonical form 'gen12', 'gen11', etc.

    Accepts: 'gen12', 'Gen12', '12', 12
    """
    s = str(gen).lower().strip()
    if s.startswith("gen"):
        return s
    return f"gen{s}"

# Component type filter keywords → category substrings to match
TYPE_FILTERS: dict[str, list[str]] = {
    "ilo":     ["lights-out management"],
    "bios":    ["bios - system rom"],
    "nic":     ["firmware - network"],
    "storage": ["firmware - storage controller"],
    "disk":    ["firmware - sas storage disk", "firmware - sata storage disk",
                "firmware - pcie nvme storage disk", "firmware - nvme"],
    "power":   ["firmware - power management"],
    "system":  ["firmware - system"],
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class SppComponent:
    """A single component entry from an SPP catalog."""
    name: str
    filename: str
    version: str
    category: str
    component_type: str         # Firmware, ComboFirmware, Software, Driver, etc.
    updatable_by: list[str]     # Bmc, Uefi, RuntimeAgent
    supported_models: list[str] # e.g. ["HPE ProLiant Compute DL360 Gen12", ...]
    release_date: str
    description: str
    sha256: str = ""
    product_id: str = ""
    size_bytes: int = 0        # from catalog Bytes field
    has_sidecar: bool = False  # True for FWPKG-v2 packages that ship a companion .json

    @property
    def update_method(self) -> str:
        """Human-readable update method string."""
        if not self.updatable_by:
            return "N/A"
        labels = {
            "Bmc": "Online (iLO)",
            "Uefi": "Offline (UEFI)",
            "RuntimeAgent": "Online (Agent)",
        }
        return " + ".join(labels.get(m, m) for m in self.updatable_by)

    @property
    def type_tag(self) -> str:
        """Short type label for display."""
        cat = self.category.lower()
        if "lights-out" in cat:
            return "iLO"
        if cat.startswith("bios"):
            return "BIOS"
        if "storage controller" in cat:
            return "Storage Ctrl"
        if "storage disk" in cat or "nvme" in cat:
            return "Disk FW"
        if "network" in cat:
            return "NIC"
        if "power management" in cat:
            return "Power Mgmt"
        if "firmware - system" in cat:
            return "System FW"
        t = self.component_type
        if t in ("Software",):
            return "Software"
        if t in ("Driver",):
            return "Driver"
        return self.component_type or "Other"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_dir() -> Path:
    """Return the spp/ directory used for caching catalogs and packages.

    - Frozen exe (PyInstaller/Nuitka): next to the executable so files survive restarts
    - Dev: repo root / spp/
    """
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "spp"
    return Path(__file__).parent.parent.parent.parent / "spp"


def _catalog_path(gen: str, version: str) -> Path:
    return _cache_dir() / gen / version / "metadata.json"


def _packages_dir(gen: str, version: str) -> Path:
    """Local directory where downloaded .fwpkg files are stored."""
    return _cache_dir() / gen / version / "packages"


def _package_url(gen: str, version: str, filename: str) -> str:
    """Public HTTP URL for a .fwpkg in the SPP repository."""
    return f"{SDR_BASE}/spp-{gen}/{version}/packages/{filename}"


def _complete_marker(gen: str, version: str) -> Path:
    return _packages_dir(gen, version) / ".complete"


def is_spp_downloaded(gen: str, version: str) -> bool:
    """True if all packages for this SPP version have been downloaded and verified."""
    return _complete_marker(gen, version).exists()


def _sha256_file(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def download_spp_packages(
    gen: str,
    version: str,
    components: "list[SppComponent]",
    *,
    force: bool = False,
    progress_cb: "Callable[[str, int, int, int, int], None] | None" = None,
) -> tuple[int, int]:
    """Download and verify all unique .fwpkg files for an SPP version.

    Parameters
    ----------
    gen, version:
        SPP generation and version (e.g. "gen12", "2026.03.00.00").
    components:
        Full list of SppComponents from fetch_catalog().
    force:
        Re-download and re-verify even if .complete marker exists.
    progress_cb:
        Optional callable(filename, file_index, file_total, bytes_done, bytes_total)
        called during each file download.

    Returns
    -------
    (downloaded_count, skipped_count) tuple.
    """
    dest_dir = _packages_dir(gen, version)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Build unique file → sha256 mapping
    file_map: dict[str, str] = {}
    for c in components:
        if c.filename and c.filename not in file_map:
            file_map[c.filename] = c.sha256

    marker = _complete_marker(gen, version)
    if marker.exists() and not force:
        return 0, len(file_map)

    downloaded = skipped = 0
    files = sorted(file_map.items())

    for idx, (filename, expected_sha256) in enumerate(files):
        dest = dest_dir / filename
        url = _package_url(gen, version, filename)

        # Skip if already present and checksum matches
        if dest.exists() and not force:
            if expected_sha256 and _sha256_file(dest) == expected_sha256.lower():
                skipped += 1
                continue
            dest.unlink()  # corrupted / partial — re-download

        req = urllib.request.Request(url, headers=_UA)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                done = 0
                with open(dest, "wb") as f:
                    while True:
                        buf = resp.read(65536)
                        if not buf:
                            break
                        f.write(buf)
                        done += len(buf)
                        if progress_cb:
                            progress_cb(filename, idx, len(files), done, total)
        except Exception as exc:
            if dest.exists():
                dest.unlink()
            raise RuntimeError(f"Failed to download {filename}: {exc}") from exc

        # Verify checksum
        if expected_sha256:
            actual = _sha256_file(dest)
            if actual != expected_sha256.lower():
                dest.unlink()
                raise RuntimeError(
                    f"Checksum mismatch for {filename}:\n"
                    f"  expected: {expected_sha256}\n"
                    f"  got:      {actual}"
                )

        downloaded += 1

        # Download sidecar .json (Gen12+) — best-effort, no checksum required
        stem = filename.rsplit(".", 1)[0]
        json_filename = stem + ".json"
        json_dest = dest_dir / json_filename
        if not json_dest.exists() or force:
            json_url = _package_url(gen, version, json_filename)
            try:
                json_req = urllib.request.Request(json_url, headers=_UA)
                with urllib.request.urlopen(json_req, timeout=30) as resp:
                    json_dest.write_bytes(resp.read())
            except Exception:
                pass  # sidecar may not exist for Gen11 packages — silently skip

    # Write completion marker
    import json as _json
    marker.write_text(_json.dumps({"gen": gen, "version": version, "files": len(file_map)}))

    return downloaded, skipped


def verify_package(path: Path, expected_sha256: str) -> bool:
    """Return True if file exists and its SHA256 matches expected_sha256."""
    if not path.exists():
        return False
    if not expected_sha256:
        return True  # no checksum in catalog — trust the file
    return _sha256_file(path) == expected_sha256.lower()


def get_verified_package(
    gen: str,
    version: str,
    filename: str,
    components: "list[SppComponent]",
) -> "Path | None":
    """Return the local path to a verified .fwpkg, or None if not present/valid.

    Verifies SHA256 against the catalog. If the file exists but checksum fails,
    the corrupted file is deleted so the caller can offer re-download.
    """
    dest = _packages_dir(gen, version) / filename
    expected = next((c.sha256 for c in components if c.filename == filename), "")

    if not dest.exists():
        return None

    if not verify_package(dest, expected):
        dest.unlink()
        return None

    return dest


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def _download_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Listing available versions
# ---------------------------------------------------------------------------

def list_versions(gen: str | int) -> list[str]:
    """Return sorted list of available SPP versions for ``gen`` from the SDR.

    Example return: ['2024.09.00.00', '2025.03.00.00', '2026.03.00.00']
    """
    gen = _norm_gen(gen)
    url = f"{SDR_BASE}/spp-{gen}/"
    html = _get(url)
    versions = sorted(set(re.findall(r'href="(\d{4}\.\d{2}\.\d{2}\.\d{2})/"', html)))
    return versions


def list_all_versions() -> dict[str, list[str]]:
    """Return {gen: [versions]} for all supported generations."""
    result: dict[str, list[str]] = {}
    for gen in SUPPORTED_GENS:
        try:
            result[gen] = list_versions(gen)
        except Exception:
            result[gen] = []
    return result


# ---------------------------------------------------------------------------
# Fetching and parsing the catalog
# ---------------------------------------------------------------------------

def fetch_catalog(
    gen: str | int,
    version: str,
    *,
    force: bool = False,
    progress_cb=None,
) -> list[SppComponent]:
    """Fetch and parse the SPP catalog for ``gen``/``version``.

    Results are cached in spp/gen{N}/{version}/metadata.json.
    Set ``force=True`` to re-download even if cached.

    Parameters
    ----------
    gen:
        Generation string: 'gen12', 'gen11', 'gen10' (or bare int 12, 11, 10).
    progress_cb:
        Optional callable(message: str) called during download.
    """
    gen = _norm_gen(gen)
    path = _catalog_path(gen, version)
    if path.exists() and not force:
        raw = path.read_bytes()
    else:
        url = f"{SDR_BASE}/spp-{gen}/{version}/manifest/metadata.json"
        if progress_cb:
            progress_cb(f"Downloading SPP {gen.upper()} {version} catalog…")
        raw = _download_bytes(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if progress_cb:
            progress_cb(f"Cached to {path}")

    data = json.loads(raw)
    return _parse_metadata(data)


def _parse_metadata(data: dict) -> list[SppComponent]:
    """Parse metadata.json dict into a list of SppComponent."""
    components: list[SppComponent] = []

    for prod in data.get("Components", []):
        prod_id = prod.get("ProductId", "")
        for ver in prod.get("Versions", []):
            pkg = ver.get("Package", {})
            comp_type = ver.get("Type", "")
            updatable_by = ver.get("UpdatableBy", [])

            name = _lang(pkg.get("Name", []))
            category = _lang(pkg.get("Category", []))
            description = _lang(pkg.get("Description", []))

            files = pkg.get("Files", [])
            if not files:
                continue
            f = files[0]
            filename = f.get("Name", "")
            if not filename:
                continue
            version_str = f.get("Version", "") or f.get("Revision", "")
            sha256 = f.get("SHA256Sum", "")
            size_bytes = int(f.get("Bytes", 0) or 0)
            has_sidecar = ver.get("PackageFormat", "") == "FWPKG-v2"
            release_date = pkg.get("ReleaseDate", "")
            if isinstance(release_date, dict):
                y = release_date.get("Year") or release_date.get("year", "")
                m = release_date.get("Month") or release_date.get("month", "")
                d = release_date.get("Day") or release_date.get("day", "")
                release_date = f"{y}-{int(m):02d}-{int(d):02d}" if y and m and d else ""

            supported_models = [
                sp.get("Model", "") for sp in pkg.get("SupportedProducts", [])
                if sp.get("Model")
            ]

            components.append(SppComponent(
                name=name,
                filename=filename,
                version=version_str,
                category=category,
                component_type=comp_type,
                updatable_by=updatable_by,
                supported_models=supported_models,
                release_date=release_date,
                description=description,
                sha256=sha256,
                product_id=prod_id,
                size_bytes=size_bytes,
                has_sidecar=has_sidecar,
            ))

    return components


def _lang(entries: list[dict], lang: str = "en") -> str:
    """Extract the English value from a [{Lang, Value}, ...] list."""
    for e in entries:
        if e.get("Lang") == lang:
            return e.get("Value", "")
    return entries[0].get("Value", "") if entries else ""


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_components(
    components: list[SppComponent],
    *,
    types: list[str] | None = None,
    model: str | None = None,
    gen: str | None = None,
    firmware_only: bool = False,
) -> list[SppComponent]:
    """Filter components by type tags and/or model substring.

    Parameters
    ----------
    types:
        List of type keys from TYPE_FILTERS, e.g. ['ilo', 'bios', 'nic'].
        If None/empty, no type filter is applied.
    model:
        Substring to match against supported_models, e.g. 'DL325'.
        When ``gen`` is also provided, only models matching both substrings
        are accepted — prevents cross-gen leakage (e.g. DL325 Gen11 entries
        appearing in a Gen12 SPP).
    gen:
        Normalised gen string e.g. 'gen12'. When set, model matching requires
        the supported model to also contain this generation string.
    firmware_only:
        If True, only return Firmware and ComboFirmware types.
    """
    result = components

    if firmware_only:
        result = [c for c in result if "firmware" in c.component_type.lower()]

    if types:
        type_keywords: list[str] = []
        for t in types:
            type_keywords.extend(TYPE_FILTERS.get(t.lower(), [t.lower()]))

        def _matches_type(c: SppComponent) -> bool:
            cat = c.category.lower()
            return any(kw in cat for kw in type_keywords)

        result = [c for c in result if _matches_type(c)]

    if model:
        model_lower = model.lower()
        gen_lower = gen.lower() if gen else None

        def _matches_model(c: SppComponent) -> bool:
            for m in c.supported_models:
                ml = m.lower()
                if model_lower not in ml:
                    continue
                # Require gen to also appear in the model string to prevent
                # cross-gen matches (e.g. 'DL325 Gen11' in a Gen12 SPP)
                if gen_lower and gen_lower not in ml:
                    continue
                return True
            return False

        result = [c for c in result if _matches_model(c)]

    return result


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------

@dataclass
class DiffEntry:
    filename: str
    name: str
    category: str
    old_version: str | None
    new_version: str | None
    status: str  # "added", "removed", "changed", "unchanged"
    updatable_by: list[str] = field(default_factory=list)


def diff_catalogs(
    old: list[SppComponent],
    new: list[SppComponent],
    *,
    types: list[str] | None = None,
    model: str | None = None,
    gen: str | None = None,
) -> list[DiffEntry]:
    """Diff two SPP catalogs (old vs new).

    Components are keyed by filename *stem* (without version/date suffix)
    so that e.g. ``A66_1.20_...fwpkg`` and ``A66_1.40_...fwpkg`` are
    treated as the same logical component.
    """
    if types or model:
        old = filter_components(old, types=types, model=model, gen=gen)
        new = filter_components(new, types=types, model=model, gen=gen)

    def _key(c: SppComponent) -> str:
        """Stable component key: first underscore-delimited token of filename."""
        stem = c.filename.rsplit(".", 1)[0]  # strip extension
        # For patterns like "A66_1.40_01_09_2026" → key = "A66"
        # For "BCM235.1.164.14_BCM957414" → key = "BCM957414" (chip model)
        # For "ilo7_1.20.00" → key = "ilo7"
        # For "HPE_MR408i-p_Gen11_52..." → key = "HPE_MR408i-p_Gen11"
        parts = stem.split("_")
        # Detect BCM NIC pattern: first part has dots + starts BCM
        if re.match(r'^BCM\d+\.\d+', parts[0]) and len(parts) >= 2:
            return parts[-1]  # chip model is the stable key
        # iLO / BIOS / generic: first token
        return parts[0]

    old_map: dict[str, SppComponent] = {}
    for c in old:
        k = _key(c)
        # Keep highest version if duplicated
        if k not in old_map or c.version > old_map[k].version:
            old_map[k] = c

    new_map: dict[str, SppComponent] = {}
    for c in new:
        k = _key(c)
        if k not in new_map or c.version > new_map[k].version:
            new_map[k] = c

    all_keys = sorted(set(old_map) | set(new_map))
    entries: list[DiffEntry] = []

    for k in all_keys:
        o = old_map.get(k)
        n = new_map.get(k)

        if o and n:
            status = "changed" if o.version != n.version else "unchanged"
            entries.append(DiffEntry(
                filename=n.filename,
                name=n.name,
                category=n.category,
                old_version=o.version,
                new_version=n.version,
                status=status,
                updatable_by=n.updatable_by,
            ))
        elif n:
            entries.append(DiffEntry(
                filename=n.filename,
                name=n.name,
                category=n.category,
                old_version=None,
                new_version=n.version,
                status="added",
                updatable_by=n.updatable_by,
            ))
        else:
            assert o is not None
            entries.append(DiffEntry(
                filename=o.filename,
                name=o.name,
                category=o.category,
                old_version=o.version,
                new_version=None,
                status="removed",
                updatable_by=o.updatable_by,
            ))

    return entries
