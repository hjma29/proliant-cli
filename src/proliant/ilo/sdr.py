"""
hpeilo.sdr
~~~~~~~~~~
HPE Software Delivery Repository (SDR) auto-discovery.

Queries the public HPE SDR to find the latest firmware pack for a given
server generation, lists available .fwpkg files, and matches them against
components reported by the iLO FirmwareInventory.

SDR URL pattern:
  https://downloads.linux.hpe.com/SDR/repo/fwpp-gen{N}/{YYYY.MM.00.00}/
  e.g. https://downloads.linux.hpe.com/SDR/repo/fwpp-gen12/2026.03.00.00/
"""

import json
import re
import urllib.request
from dataclasses import dataclass, field


SDR_BASE = "https://downloads.linux.hpe.com/SDR/repo"
_UA = {"User-Agent": "hpeilo/1.0"}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class FwComponent:
    """A firmware component available in the SDR."""
    filename: str
    url: str
    prefix: str        # e.g. "A66", "ilo7", "ilo6", "U54", "BCM957414A4142HC"
    version_str: str   # e.g. "1.40", "1.20.00", "174", "235.1.164.14"
    version: tuple     # comparable: (1, 40), (1, 20, 0), (1, 74)
    chip_model: str = ""          # NIC chip model e.g. "BCM957414A4142HC" (empty for non-NIC)
    software_ids: list = field(default_factory=list)  # PLDM Target GUIDs from SDR JSON sidecar


@dataclass
class UpgradeCandidate:
    """Represents a component that can be upgraded."""
    name: str                   # FirmwareInventory Name e.g. "System ROM"
    current: str                # current version string e.g. "A66 v1.20 (07/11/2025)"
    current_ver: tuple          # parsed e.g. (1, 20)
    sdr: FwComponent | None     # best match from SDR; None = no SDR package found
    needs_update: bool          # sdr.version > current_ver and updateable
    updateable: bool = True     # iLO Updateable flag


# ---------------------------------------------------------------------------
# Server generation detection
# ---------------------------------------------------------------------------

def detect_gen(model: str) -> int:
    """Detect HPE server generation (11, 12, …) from the model string.

    Examples
    --------
    >>> detect_gen("HPE ProLiant DL325 Gen12")
    12
    >>> detect_gen("ProLiant DL380 Gen11")
    11
    """
    m = re.search(r'gen\s*(\d+)', model, re.IGNORECASE)
    if m:
        return int(m.group(1))
    raise ValueError(f"Cannot detect generation from model string: {model!r}")


# ---------------------------------------------------------------------------
# SDR fetching
# ---------------------------------------------------------------------------

def latest_pack_url(gen: int) -> tuple[str, str]:
    """Return (pack_date, pack_url) for the newest firmware pack for ``gen``.

    Raises
    ------
    RuntimeError
        If no packs are found at the SDR URL.
    """
    base = f"{SDR_BASE}/fwpp-gen{gen}/"
    html = _get(base)
    dates = sorted(set(re.findall(r'href="(\d{4}\.\d{2}\.\d{2}\.\d{2})/', html)))
    if not dates:
        raise RuntimeError(f"No firmware packs found at {base}")
    latest = dates[-1]
    return latest, f"{base}{latest}/"


def list_pack(pack_url: str) -> list[FwComponent]:
    """Return all .fwpkg components available in a firmware pack."""
    html = _get(pack_url)
    # Deduplicate — Apache directory listings show each href twice
    seen: set[str] = set()
    components: list[FwComponent] = []
    for fname in re.findall(r'href="([^/"]+\.fwpkg)"', html):
        if fname in seen:
            continue
        seen.add(fname)
        comp = _parse_fwpkg(fname, pack_url + fname)
        if comp:
            components.append(comp)
    return components


def _parse_fwpkg(filename: str, url: str) -> "FwComponent | None":
    """Parse a .fwpkg filename into a FwComponent.

    Supported patterns:
      A66_1.40_01_09_2026.fwpkg              → prefix=A66,  version=(1,40)
      U54_2.80_01_29_2026.fwpkg              → prefix=U54,  version=(2,80)
      ilo7_1.20.00.fwpkg                     → prefix=ilo7, version=(1,20,0)
      ilo6_174.fwpkg                         → prefix=ilo6, version=(1,74)  [no-dot]
      BCM235.1.164.14_BCM957414A4142HC.fwpkg → chip_model=BCM957414A4142HC, version=(235,1,164,14)
    """
    stem = filename.removesuffix(".fwpkg")
    parts = stem.split("_")
    if len(parts) < 2:
        return None

    # BCM NIC pattern: first part is a version like "BCM235.1.164.14", second is chip model
    # Detect by: parts[0] contains dots AND starts with letters followed by digits
    if re.match(r'^[A-Z]+\d+\.\d+', parts[0]) and re.match(r'^[A-Z]+\d+', parts[-1]):
        ver_str = re.sub(r'^[A-Za-z]+', '', parts[0])   # strip "BCM" prefix → "235.1.164.14"
        chip_model = parts[-1]                            # e.g. "BCM957414A4142HC"
        ver_tuple = _parse_version(ver_str)
        software_ids = _fetch_software_ids(url)
        return FwComponent(filename=filename, url=url, prefix=chip_model,
                           version_str=ver_str, version=ver_tuple,
                           chip_model=chip_model, software_ids=software_ids)

    # HPE storage/controller pattern:
    #   HPE_MR416i-p_Gen11_52.36.3-6584_A.fwpkg
    #   HPE_NS204i_Gen11_1.2.14.1026_A.fwpkg
    #   HPE_UBM6_Gen12_1.06_A.fwpkg
    if parts[0] == "HPE" and len(parts) >= 4 and re.match(r'^Gen\d+$', parts[2], re.IGNORECASE):
        model = parts[1]
        ver_str = parts[3]
        ver_tuple = _parse_version(ver_str)
        software_ids = _fetch_software_ids(url)
        return FwComponent(
            filename=filename,
            url=url,
            prefix=f"HPE_{model}",
            version_str=ver_str,
            version=ver_tuple,
            software_ids=software_ids,
        )

    prefix = parts[0]
    ver_str = parts[1]
    ver_tuple = _parse_version(ver_str, prefix)
    return FwComponent(filename=filename, url=url, prefix=prefix,
                       version_str=ver_str, version=ver_tuple)


def _fetch_software_ids(fwpkg_url: str) -> list[str]:
    """Fetch Target GUIDs from the .json sidecar for a firmware package.

    The JSON sidecar contains Devices.Device[].Target entries — these are
    PLDM UUIDs that match the SoftwareId field in iLO's FirmwareInventory.
    Returns empty list on any fetch/parse error (non-fatal).
    """
    json_url = fwpkg_url.rsplit(".", 1)[0] + ".json"
    try:
        raw = _get(json_url)
        data = json.loads(raw)
        devices = data.get("Devices", {}).get("Device", [])
        return [d["Target"] for d in devices if "Target" in d]
    except Exception:
        return []


def _parse_version(ver_str: str, prefix: str = "") -> tuple:
    """Parse a version string into a comparable int tuple.

    Handles the iLO 6 no-dot format: "174" → (1, 74).
    """
    # iLO 6 special case: 3-digit no-dot build number like "174"
    if re.match(r'^\d{3}$', ver_str) and prefix.lower().startswith("ilo6"):
        return (int(ver_str[0]), int(ver_str[1:]))
    dotted = re.search(r'\d+\.\d+(?:\.\d+)*', ver_str)
    if dotted:
        return tuple(int(n) for n in dotted.group(0).split("."))
    nums = re.findall(r'\d+', ver_str)
    return tuple(int(n) for n in nums) if nums else (0,)


# ---------------------------------------------------------------------------
# Version extraction from FirmwareInventory strings
# ---------------------------------------------------------------------------

def parse_inventory_version(version_str: str) -> tuple:
    """Extract a comparable version tuple from an iLO FirmwareInventory version string.

    Examples
    --------
    "A66 v1.20 (07/11/2025)" → (1, 20)
    "1.21.00 Apr 07 2026"    → (1, 21, 0)
    "1.70 Aug 11 2025"       → (1, 70)
    "v2.50 (04/22/2025)"     → (2, 50)
    """
    m = re.search(r'v?(\d+\.\d+(?:\.\d+)*)', version_str)
    if m:
        return tuple(int(x) for x in m.group(1).split("."))
    return (0,)


def extract_bios_prefix(version_str: str) -> str | None:
    """Extract the BIOS component prefix (e.g. 'A66', 'U54') from a version string.

    "A66 v1.20 (07/11/2025)" → "A66"
    "U54 v2.50 (04/22/2025)" → "U54"
    Returns None if no prefix found.
    """
    m = re.match(r'^([A-Z]\d+)\b', version_str.strip())
    return m.group(1) if m else None


def extract_storage_model_id(name: str) -> str | None:
    """Extract the controller model token from a storage inventory name."""
    m = re.search(r'\b(MR|SR|NS|UBM|P)\d+[A-Za-z0-9-]*\b', name, re.IGNORECASE)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Matching logic: FirmwareInventory → SDR component
# ---------------------------------------------------------------------------

def find_upgrades(
    inventory_entries: list[dict] | list[tuple[str, str]],
    pack_components: list[FwComponent],
) -> list[UpgradeCandidate]:
    """Match FirmwareInventory entries against SDR pack components.

    Parameters
    ----------
    inventory_entries:
        Either list of full dicts from ``fetch_firmware_inventory_full()``
        (preferred — includes Updateable flag), or legacy list of (name, version) tuples.
        NIC entries may include a ``chip_model`` key (e.g. "BCM57414") for NIC matching.
    pack_components:
        list of FwComponent from ``list_pack()``.

    Returns
    -------
    list[UpgradeCandidate]
        One entry per component. ``needs_update=False`` if already current or
        no SDR match found.
    """
    # Normalise to list of dicts
    entries: list[dict] = []
    for e in inventory_entries:
        if isinstance(e, tuple):
            entries.append({"Name": e[0], "Version": e[1], "Updateable": True})
        else:
            entries.append(e)

    # Build lookups:
    #   sdr_by_prefix:      prefix.lower() → FwComponent  (BIOS, iLO, etc.)
    #   sdr_nic:            list of NIC FwComponents (those with chip_model set)
    #   sdr_by_software_id: PLDM Target GUID → FwComponent (most accurate NIC match)
    sdr_by_prefix: dict[str, FwComponent] = {}
    sdr_by_software_id: dict[str, FwComponent] = {}
    sdr_nic: list[FwComponent] = []
    for comp in pack_components:
        if comp.chip_model:
            sdr_nic.append(comp)
            for sw_id in comp.software_ids:
                sdr_by_software_id[sw_id.lower()] = comp
        else:
            key = comp.prefix.lower()
            if key not in sdr_by_prefix:
                sdr_by_prefix[key] = comp

    candidates: list[UpgradeCandidate] = []
    seen_names: set[str] = set()  # deduplicate by (name_lower, version)

    for entry in entries:
        name = entry.get("Name", "N/A")
        version = entry.get("Version", "N/A") or "N/A"
        updateable = entry.get("Updateable", True)
        nl = name.lower()

        # Deduplicate: FirmwareInventory and NetworkAdapters can both list the same NIC
        dedup_key = f"{nl}:{version}"
        if dedup_key in seen_names:
            continue
        seen_names.add(dedup_key)

        sdr_comp: FwComponent | None = None

        # ── SoftwareId (PLDM Target GUID): most accurate match — works for any NIC
        #    naming variant (e.g. P225p marketing name → BCM57414 package)
        software_id = entry.get("SoftwareId", "")
        if software_id:
            sdr_comp = sdr_by_software_id.get(software_id.lower())

        # ── NIC: matched by chip_model substring (e.g. "BCM57414" → "BCM957414*")
        chip_model = entry.get("chip_model", "")
        if sdr_comp is None and chip_model:
            # Strip non-digits-letters, then look for numeric part inside SDR chip models
            # e.g. "BCM57414" → search for "57414" in SDR chip_model strings
            num_key = re.sub(r'^[A-Za-z]+', '', chip_model)   # "57414"
            for nic_comp in sdr_nic:
                if num_key and num_key in nic_comp.chip_model:
                    # Prefer the best (highest) version match
                    if sdr_comp is None or nic_comp.version > sdr_comp.version:
                        sdr_comp = nic_comp

        # ── System ROM / BIOS: matched by version prefix (e.g. "A66", "U54")
        elif sdr_comp is None and ("system rom" in nl or "bios" in nl):
            bios_prefix = extract_bios_prefix(version)
            if bios_prefix:
                sdr_comp = sdr_by_prefix.get(bios_prefix.lower())

        # ── iLO: matched by "ilo6" / "ilo7"
        elif sdr_comp is None and nl.startswith("ilo"):
            m = re.match(r'ilo\s*(\d+)', nl)
            if m:
                sdr_comp = sdr_by_prefix.get(f"ilo{m.group(1)}")

        # ── Storage controller packages: matched by model token (MR416i-p, NS204i, UBM6, ...)
        elif sdr_comp is None:
            storage_model = extract_storage_model_id(name)
            if storage_model:
                sdr_comp = sdr_by_prefix.get(f"hpe_{storage_model.lower()}")

        if sdr_comp is None:
            # NIC from NetworkAdapters feed with no matching SDR package: still show in table
            if chip_model:
                candidates.append(UpgradeCandidate(
                    name=name,
                    current=version,
                    current_ver=(),
                    sdr=None,
                    needs_update=False,
                    updateable=updateable,
                ))
            continue

        current_ver = parse_inventory_version(version)
        needs = updateable and (sdr_comp.version > current_ver)

        candidates.append(UpgradeCandidate(
            name=name,
            current=version,
            current_ver=current_ver,
            sdr=sdr_comp,
            needs_update=needs,
            updateable=updateable,
        ))

    return candidates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode()
