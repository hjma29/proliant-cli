"""
pcli.qs.client
~~~~~~~~~~~~~~
HPE QuickSpecs data access via the HPE Resource Library JSON API and
the public collateral HTML endpoint.

No authentication required — all endpoints are public.
Uses httpx with HTTP/2 to pass Akamai bot detection on www.hpe.com.
"""
from __future__ import annotations

import json
import re
import socket
import tempfile
import time
import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

# curl_cffi uses Chrome's TLS fingerprint (BoringSSL) to pass Akamai bot detection.
# Falls back to httpx with HTTP/2 if curl_cffi is unavailable (e.g. dev install).
try:
    from curl_cffi.requests import Session as _CurlSession
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    _CURL_CFFI_AVAILABLE = False

# HPE endpoints return both A and AAAA records, but IPv6 is not routed on
# most lab/corp machines — force IPv4 to avoid connection hangs.
_orig_getaddrinfo = socket.getaddrinfo

def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    results = _orig_getaddrinfo(host, port, family, type, proto, flags)
    return [r for r in results if r[0] == socket.AF_INET]

socket.getaddrinfo = _getaddrinfo_ipv4_only


# ── Constants ─────────────────────────────────────────────────────────────────

# JSON search API — no token required, returns clean JSON
_JSON_SEARCH_URL = (
    "https://www.hpe.com/us/en/resource-library"
    "/_jcr_content/polaris-body-zone/medialibrary.model.json"
)
_VARIANTS_URL = "https://www.hpe.com/services/hpe/psnow/variants?assetId={asset_id}"
_COLLATERAL_URL = "https://www.hpe.com/us/en/collaterals/collateral.{docid}.html"
# Old-style PSNow pages serve QuickSpecs only as PDF; the download link is embedded in
# the page HTML as: https://www.hpe.com/psnow/downloadDoc/<title>-<docid>.pdf?id=<docid>.pdf
_PSNOW_WRAPPER_URL = "https://www.hpe.com/psnow/doc/{docid}.pdf"
_PSNOW_DOWNLOAD_RE = re.compile(
    r'href="(https://www\.hpe\.com/psnow/downloadDoc/[^"]+\.pdf\?id=[^"]+)"'
)

# Browser-like headers — httpx with HTTP/2 + these headers pass Akamai bot detection
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Lazy HTTP client — curl_cffi (Chrome TLS) or httpx fallback, created on first use
_client = None

# Raw HTML cache keyed by doc_id — shared between fetch_quickspec_versions and
# fetch_quickspec_markdown so a list → describe workflow only downloads once
_html_cache: dict[str, str] = {}


def _get_client():
    global _client
    if _client is None:
        if _CURL_CFFI_AVAILABLE:
            # Chrome TLS fingerprint bypasses Akamai bot detection on www.hpe.com
            _client = _CurlSession(impersonate="chrome")
        else:
            # Dev fallback — httpx with HTTP/2 and browser-like headers
            _client = httpx.Client(
                http2=True,
                follow_redirects=True,
                headers=_BROWSER_HEADERS,
                timeout=30.0,
            )
    return _client

# ── Disk cache ────────────────────────────────────────────────────────────────

_CACHE_TTL_LATEST = 7 * 24 * 3600   # 7 days for "latest" content (may change)
# Versioned content never changes — cached indefinitely


def _cache_dir() -> Path:
    import sys
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    d = base / "pcli" / "qs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _qs_cache_read(doc_id: str, ver: str) -> "tuple[str, list[str]] | None":
    """Return (markdown, sections) from disk cache, or None on miss/expiry."""
    path = _cache_dir() / f"{doc_id}_{ver or 'latest'}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Latest content expires after TTL; versioned content never expires
        if not ver and (time.time() - data.get("cached_at", 0)) > _CACHE_TTL_LATEST:
            return None
        return data["markdown"], data["sections"]
    except Exception:
        return None


def _qs_cache_write(doc_id: str, ver: str, markdown: str, sections: list) -> None:
    """Persist (markdown, sections) to disk cache silently."""
    path = _cache_dir() / f"{doc_id}_{ver or 'latest'}.json"
    try:
        path.write_text(
            json.dumps({"markdown": markdown, "sections": sections, "cached_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass  # cache write failure is non-fatal


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
    link: str = ""     # full psnow URL with ?ver=N


# ── Search ─────────────────────────────────────────────────────────────────────

def search_quickspecs(model: str, count: int = 10) -> list[QSEntry]:
    """
    Search for QuickSpecs matching *model* using the HPE Resource Library JSON API.

    *model* is a free-text query, e.g. 'DL380 Gen12', 'dl380gen12', 'DL360'.
    Results are returned newest-first (API returns sorted by date).
    """
    # Normalise: dl380gen12 → DL380 Gen12, dl380a-gen12 → DL380A Gen12
    q = re.sub(r"(?i)(gen)(\d+)([a-z]?)", lambda m: m.group(3).upper() + " Gen" + m.group(2), model.replace("-", " "))
    q = q.upper().replace("GEN", "Gen").strip()

    # Extract generation token (e.g. "Gen12") for strict title filtering
    gen_filter = re.search(r"Gen\d+", q)
    gen_token = gen_filter.group(0).lower() if gen_filter else None

    # Extract model prefix for strict title filtering (e.g. "DL110", "DL380A")
    model_token_m = re.search(r"\b(DL|ML|SY|XL|BL|CL)\d+[A-Z]?\b", q)
    model_token = model_token_m.group(0).lower() if model_token_m else None

    params = {
        "restype": "quickspecs",
        "status": "active",
        "sort": "date",
        "search": q,
        "limit": "100",  # fetch broadly — client-side filtering narrows to exact model
    }

    try:
        r = _get_client().get(_JSON_SEARCH_URL, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise RuntimeError(
            f"Cannot reach HPE Resource Library ({e}).\n"
            "  Check your network — try connecting to HPE network (VPN/ZPA) and retry."
        ) from e

    entries: list[QSEntry] = []
    seen_doc_ids: set[str] = set()
    for item in data.get("items", []):
        title = item.get("title", "").strip()
        if not title or "quickspec" not in title.lower():
            continue
        if gen_token and gen_token not in title.lower():
            continue
        if model_token and model_token not in title.lower():
            continue

        link = item.get("cta", {}).get("link", "")
        doc_id_m = re.search(r"/psnow/doc/([a-z0-9]+)", link)  # ignore any ?query params
        if not doc_id_m:
            continue
        doc_id = doc_id_m.group(1)
        if doc_id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc_id)

        last_modified = item.get("lastUpdated", "")
        entries.append(QSEntry(
            doc_id=doc_id,
            title=title,
            version="",
            last_modified=last_modified,
        ))
        if len(entries) >= count:
            break

    return entries


# ── Content fetch ──────────────────────────────────────────────────────────────

def _fetch_collateral_html(doc_id: str, ver: str = "") -> str:
    """Fetch and cache the raw HTML for *doc_id*, optionally for a specific version."""
    cache_key = f"{doc_id}:{ver}" if ver else doc_id
    if cache_key in _html_cache:
        return _html_cache[cache_key]
    url = _COLLATERAL_URL.format(docid=doc_id)
    if ver:
        url = f"{url}?ver={ver}"
    r = _get_client().get(url)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} fetching {url}")
    html = r.text
    _html_cache[cache_key] = html
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
    r = _get_client().get(url, headers={"Referer": _COLLATERAL_URL.format(docid=doc_id)})
    r.raise_for_status()
    data = r.json()

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
            link=v.get("link", ""),
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


def _fetch_from_psnow_pdf(doc_id: str, ver: str = "") -> tuple[str, list[str]]:
    """
    Download the QuickSpec PDF and convert to markdown.
    If *ver* is given, scrapes the versioned psnow page to get the right download URL.
    Extracts sections by scanning for known QuickSpec section title strings.
    Results are cached to disk (~/.cache/pcli/qs/).
    """
    cached = _qs_cache_read(doc_id, ver)
    if cached is not None:
        return cached

    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: {exc}\n"
            "Install with: pip install markitdown[pdf]"
        ) from exc

    if ver:
        # Fetch the versioned psnow page to extract the versioned PDF download link
        wrapper_url = f"https://www.hpe.com/psnow/doc/{doc_id}?ver={ver}"
        r = _get_client().get(wrapper_url)
        r.raise_for_status()
        wrapper_html = r.text
        # Extract the downloadDoc URL for this specific version
        ver_pattern = re.compile(
            r'href="(https://www\.hpe\.com/psnow/downloadDoc/[^"]+?ver='
            + re.escape(ver) + r'[^"]+)"'
        )
        m = ver_pattern.search(wrapper_html)
    else:
        # Fetch the wrapper page to extract the real PDF download link
        wrapper_url = _PSNOW_WRAPPER_URL.format(docid=doc_id)
        r = _get_client().get(wrapper_url)
        r.raise_for_status()
        wrapper_html = r.text
        m = _PSNOW_DOWNLOAD_RE.search(wrapper_html)

    if not m:
        raise RuntimeError(f"Could not find PDF download link for doc {doc_id!r} ver={ver!r}")
    raw_url = m.group(1)
    parsed = urllib.parse.urlparse(raw_url)
    pdf_url = parsed._replace(path=urllib.parse.quote(parsed.path)).geturl()

    # Download PDF bytes
    pdf_r = _get_client().get(pdf_url, timeout=60.0)
    pdf_r.raise_for_status()
    pdf_bytes = pdf_r.content

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

    _qs_cache_write(doc_id, ver, text, sections)
    return text, sections


def fetch_quickspec_markdown(doc_id: str, ver: str = "") -> tuple[str, list[str]]:
    """
    Fetch the HPE collateral HTML for *doc_id* and return:
      (markdown_text, list_of_section_names)

    Pass *ver* (e.g. "43") to fetch a specific older version.
    For newer HPE collateral pages the content is parsed from HTML.
    For older PSNow-style pages the QuickSpec PDF is downloaded and converted.
    Only the QuickSpec body is returned (nav, footer, "Recommended for you"
    are stripped).
    Results are cached to disk (~/.cache/pcli/qs/).
    """
    # Check disk cache first — versioned PDFs are cached forever, latest for 7 days
    cached = _qs_cache_read(doc_id, ver)
    if cached is not None:
        return cached

    try:
        from bs4 import BeautifulSoup
        from markitdown import MarkItDown
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: {exc}\n"
            "Install with: pip install beautifulsoup4 markitdown"
        ) from exc

    try:
        html = _fetch_collateral_html(doc_id, ver=ver)
    except Exception as e:
        # No collateral HTML page (404 or other error) — try PDF directly
        if "404" in str(e) or "Not Found" in str(e):
            return _fetch_from_psnow_pdf(doc_id, ver=ver)
        raise
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    if not main:
        # Old-style PSNow download-wrapper page — fall back to PDF (has its own cache)
        return _fetch_from_psnow_pdf(doc_id, ver=ver)

    # The content is inside <hpe-left-rail-container>
    container = main.find("hpe-left-rail-container")
    if not container:
        raise RuntimeError(f"Could not find content container in page for doc {doc_id!r}")

    # For versioned requests the collateral HTML always returns the latest content;
    # fall back to the versioned PDF instead (has its own cache).
    if ver:
        return _fetch_from_psnow_pdf(doc_id, ver=ver)

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

    markdown = result.text_content
    _qs_cache_write(doc_id, ver, markdown, sections)
    return markdown, sections


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

