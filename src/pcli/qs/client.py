"""
pcli.qs.client
~~~~~~~~~~~~~~
HPE QuickSpecs data access via the HPE Resource Library (Coveo search) and
the public collateral HTML endpoint.

No authentication required — all endpoints are public.
"""
from __future__ import annotations

import json
import re
import tempfile
import os
import urllib.request
from dataclasses import dataclass
from typing import Optional


# ── Constants ─────────────────────────────────────────────────────────────────

_RESOURCE_LIBRARY_URL = (
    "https://www.hpe.com/us/en/resource-library.html"
    "/restype/quickspecs/status/active/sort/date"
)
_COVEO_ENDPOINT = (
    "https://hewlettpackardproductioniwmg9b9w.org.coveo.com/rest/search/v2"
)
_VARIANTS_URL = "https://www.hpe.com/services/hpe/psnow/variants?assetId={asset_id}"
_COLLATERAL_URL = "https://www.hpe.com/us/en/collaterals/collateral.{docid}.html"
# Old-style PSNow pages serve QuickSpecs only as PDF; the download link is embedded in
# the page HTML as: https://www.hpe.com/psnow/downloadDoc/<title>-<docid>.pdf?id=<docid>.pdf
_PSNOW_WRAPPER_URL = "https://www.hpe.com/psnow/doc/{docid}.pdf"
_PSNOW_DOWNLOAD_RE = re.compile(
    r'href="(https://www\.hpe\.com/psnow/downloadDoc/[^"]+\.pdf\?id=[^"]+)"'
)

# Cached token — fetched once per process lifetime
_coveo_token_cache: Optional[str] = None

# Raw HTML cache keyed by doc_id — shared between fetch_quickspec_versions and
# fetch_quickspec_markdown so a list → describe workflow only downloads once
_html_cache: dict[str, str] = {}

_MONTH_ABBR = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class QSEntry:
    doc_id: str
    title: str
    version: str
    last_modified: str  # raw date string from Coveo e.g. "05/13/2026 00:00:00.000"


@dataclass
class QSVersion:
    """One internal revision of a QuickSpec document."""
    doc_id: str
    title: str
    version_num: str   # "16"
    date: str          # "2026-06-01"


# ── Token ─────────────────────────────────────────────────────────────────────

def fetch_coveo_token() -> str:
    """Extract the Coveo search token embedded in the HPE Resource Library page."""
    global _coveo_token_cache
    if _coveo_token_cache:
        return _coveo_token_cache

    req = urllib.request.Request(
        _RESOURCE_LIBRARY_URL,
        headers={"User-Agent": "Mozilla/5.0 (pcli-qs/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    m = re.search(r"(xx[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", html)
    if not m:
        raise RuntimeError(
            "Could not find Coveo token in HPE Resource Library page. "
            "The page structure may have changed."
        )
    _coveo_token_cache = m.group(1)
    return _coveo_token_cache


# ── Search ─────────────────────────────────────────────────────────────────────

def search_quickspecs(model: str, count: int = 10) -> list[QSEntry]:
    """
    Search for QuickSpecs matching *model* using the Coveo search API.

    *model* is a free-text query, e.g. 'DL380 Gen12', 'dl380gen12', 'DL360'.
    Returns a list of QSEntry sorted by last-modified descending.
    """
    # Normalise: dl380gen12 → DL380 Gen12, dl380a-gen12 → DL380A Gen12
    # Also handles dl380gen12a → DL380A Gen12 (HPE variant letter belongs on model)
    q = re.sub(r"(?i)(gen)(\d+)([a-z]?)", lambda m: m.group(3).upper() + " Gen" + m.group(2), model.replace("-", " "))
    q = q.upper().replace("GEN", "Gen").strip()
    # Prefix "HPE ProLiant" for better Coveo relevance ranking
    if not q.upper().startswith("HPE"):
        q = "HPE ProLiant " + q
    # Append "QuickSpecs" so the search stays focused
    query = f"{q} QuickSpecs"

    # Extract generation token (e.g. "Gen12") for strict title filtering
    gen_filter = re.search(r"Gen\d+", q)
    gen_token = gen_filter.group(0).lower() if gen_filter else None  # "gen12"

    token = fetch_coveo_token()
    payload = json.dumps({
        "q": query,
        "numberOfResults": count * 5,  # fetch more to account for filtering + multi-version
    }).encode()
    req = urllib.request.Request(
        _COVEO_ENDPOINT,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (pcli-qs/1.0)",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

    entries: list[QSEntry] = []
    seen_keys: set[tuple[str, str]] = set()  # (doc_id, version) — allow multiple versions
    for item in data.get("results", []):
        raw = item.get("raw", {})
        doc_id = raw.get("kmdocid", "")
        version = str(raw.get("kmdocversion", ""))
        if not doc_id or (doc_id, version) in seen_keys:
            continue
        # Skip doc IDs that reference sub-sections (contain ||)
        if "||" in doc_id:
            continue
        title = item.get("title", raw.get("kmdocfulltitle", "")).strip()
        # Only keep actual QuickSpec documents
        if "quickspec" not in title.lower():
            continue
        # Strict generation filter: skip if title mentions a different generation
        if gen_token and gen_token not in title.lower():
            continue
        seen_keys.add((doc_id, version))
        entries.append(QSEntry(
            doc_id=doc_id,
            title=title,
            version=version,
            last_modified=raw.get("kmdoclastmod", ""),
        ))

    # Sort by last_modified descending
    def _sort_key(e: QSEntry) -> str:
        # Date format: "MM/DD/YYYY ..." → reformat for lexicographic sort
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", e.last_modified)
        return f"{m.group(3)}{m.group(1)}{m.group(2)}" if m else ""

    entries.sort(key=_sort_key, reverse=True)
    return entries[:count]


# ── Content fetch ──────────────────────────────────────────────────────────────

def _fetch_collateral_html(doc_id: str) -> str:
    """Fetch and cache the raw HTML for *doc_id*."""
    if doc_id in _html_cache:
        return _html_cache[doc_id]
    url = _COLLATERAL_URL.format(docid=doc_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pcli-qs/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    _html_cache[doc_id] = html
    return html


def _parse_qs_date(date_str: str) -> str:
    """Convert 'DD MMM YYYY' or 'DD-Mon-YYYY' to 'YYYY-MM-DD'."""
    m = re.match(r"(\d{1,2})[\s\-]([A-Za-z]{3})[\s\-](\d{4})", date_str.strip())
    if m:
        day, mon, year = m.groups()
        mon_num = _MONTH_ABBR.get(mon.lower(), "??")
        return f"{year}-{mon_num}-{day.zfill(2)}"
    return date_str


def fetch_quickspec_versions(doc_id: str, title: str = "", n: int = 3) -> list[QSVersion]:
    """
    Fetch version history for *doc_id* from the HPE psnow variants API.
    Returns up to *n* most-recent versions (newest first).

    The variants endpoint is the same source that powers the version dropdown
    on the HPE collateral page — no authentication required.
    """
    # doc_id like "a00073551enw" → asset_id "a00073551:enw" (colon before last 3 chars)
    asset_id = doc_id[:-3] + ":" + doc_id[-3:]
    url = _VARIANTS_URL.format(asset_id=asset_id)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (pcli-qs/1.0)",
            "Referer": _COLLATERAL_URL.format(docid=doc_id),
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())

    versions: list[QSVersion] = []
    for v in data.get("versions", []):
        label = v.get("label", "")           # "Version 16 - June 1, 2026"
        ver_m = re.match(r"Version\s+(\d+)\s+-\s+(.+)", label, re.IGNORECASE)
        if not ver_m:
            continue
        versions.append(QSVersion(
            doc_id=doc_id,
            title=title,
            version_num=ver_m.group(1),
            date=_parse_long_date(ver_m.group(2).strip()),
        ))
        if len(versions) >= n:
            break

    return versions


_MONTH_LONG = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _parse_long_date(date_str: str) -> str:
    """Convert 'June 1, 2026' → '2026-06-01'."""
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", date_str.strip())
    if m:
        mon, day, year = m.groups()
        mon_num = _MONTH_LONG.get(mon.lower(), "??")
        return f"{year}-{mon_num}-{day.zfill(2)}"
    return date_str


def _fetch_from_psnow_pdf(doc_id: str) -> tuple[str, list[str]]:
    """
    Fallback for old-style PSNow pages: download the PDF and convert to markdown.
    Extracts sections by scanning for known QuickSpec section title strings.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: {exc}\n"
            "Install with: pip install markitdown[pdf]"
        ) from exc

    # Fetch the wrapper page to extract the real PDF download link
    wrapper_url = _PSNOW_WRAPPER_URL.format(docid=doc_id)
    req = urllib.request.Request(
        wrapper_url,
        headers={"User-Agent": "Mozilla/5.0 (pcli-qs/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        wrapper_html = resp.read().decode("utf-8", errors="replace")

    m = _PSNOW_DOWNLOAD_RE.search(wrapper_html)
    if not m:
        raise RuntimeError(f"Could not find PDF download link for doc {doc_id!r}")
    pdf_url = m.group(1)

    # Download PDF to a temp file
    pdf_req = urllib.request.Request(
        pdf_url,
        headers={"User-Agent": "Mozilla/5.0 (pcli-qs/1.0)"},
    )
    with urllib.request.urlopen(pdf_req, timeout=60) as resp:
        pdf_bytes = resp.read()

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmpfile = f.name

    try:
        md = MarkItDown()
        result = md.convert(tmpfile)
    finally:
        os.unlink(tmpfile)

    text = result.text_content

    # Extract section names: lines that are all-caps or title-case standalone headings
    # QuickSpec PDFs use bold section titles in consistent positions
    _KNOWN_SECTIONS = [
        "Summary of Changes",
        "Overview",
        "Standard Features",
        "Configuration Information",
        "Core Options",
        "Additional Options",
        "Service and Support",
    ]
    sections = [s for s in _KNOWN_SECTIONS if s.lower() in text.lower()]

    # Normalize PDF text: insert ### headings before each known section so that
    # filter_section() works the same way as with HTML-converted markdown
    for sec in sections:
        # Find the line that contains exactly (or primarily) this section title
        pattern = re.compile(
            r"^(" + re.escape(sec) + r")\s*$",
            re.IGNORECASE | re.MULTILINE,
        )
        text = pattern.sub(r"### \1", text)

    return text, sections


def fetch_quickspec_markdown(doc_id: str) -> tuple[str, list[str]]:
    """
    Fetch the HPE collateral HTML for *doc_id* and return:
      (markdown_text, list_of_section_names)

    For newer HPE collateral pages the content is parsed from HTML.
    For older PSNow-style pages the QuickSpec PDF is downloaded and converted.
    Only the QuickSpec body is returned (nav, footer, "Recommended for you"
    are stripped).
    """
    try:
        from bs4 import BeautifulSoup
        from markitdown import MarkItDown
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: {exc}\n"
            "Install with: pip install beautifulsoup4 markitdown"
        ) from exc

    html = _fetch_collateral_html(doc_id)
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    if not main:
        # Old-style PSNow download-wrapper page — fall back to PDF
        return _fetch_from_psnow_pdf(doc_id)

    # The content is inside <hpe-left-rail-container>
    container = main.find("hpe-left-rail-container")
    if not container:
        raise RuntimeError(f"Could not find content container in page for doc {doc_id!r}")

    # Extract section names from h3 tags
    sections = [h.get_text(strip=True) for h in container.find_all("h3")]

    # Convert to markdown via markitdown
    inner_html = f"<html><body>{container}</body></html>"
    md = MarkItDown()
    with tempfile.NamedTemporaryFile(
        suffix=".html", mode="w", encoding="utf-8", delete=False
    ) as f:
        f.write(inner_html)
        tmpfile = f.name
    try:
        result = md.convert(tmpfile)
    finally:
        os.unlink(tmpfile)

    return result.text_content, sections


def filter_section(markdown: str, section: str) -> str:
    """
    Extract a single section from the full markdown by heading name.
    Returns text from '### <section>' up to the next '### ' heading.
    """
    # Find the heading line (case-insensitive)
    pattern = re.compile(
        r"^(#{1,3}\s+" + re.escape(section) + r"\s*)$",
        re.IGNORECASE | re.MULTILINE,
    )
    m = pattern.search(markdown)
    if not m:
        return f"Section '{section}' not found."

    start = m.start()
    # Find next same-level or higher heading
    level = len(m.group(1)) - len(m.group(1).lstrip("#"))
    next_heading = re.compile(
        r"^#{1," + str(level) + r"}\s+\S",
        re.MULTILINE,
    )
    end_m = next_heading.search(markdown, m.end())
    end = end_m.start() if end_m else len(markdown)
    return markdown[start:end].strip()

