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
_COLLATERAL_URL = "https://www.hpe.com/us/en/collaterals/collateral.{docid}.html"

# Cached token — fetched once per process lifetime
_coveo_token_cache: Optional[str] = None


# ── Data types ─────────────────────────────────────────────────────────────────

@dataclass
class QSEntry:
    doc_id: str
    title: str
    version: str
    last_modified: str  # raw date string from Coveo e.g. "05/13/2026 00:00:00.000"


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
    # Normalise: dl380gen12 → DL380 Gen12, dl380-gen12 → DL380 Gen12
    q = re.sub(r"(?i)(gen)(\d+)", r" Gen\2", model.replace("-", " "))
    q = q.upper().replace("GEN", "Gen").strip()
    # Append "QuickSpecs" so the search stays focused
    query = f"{q} QuickSpecs"

    token = fetch_coveo_token()
    payload = json.dumps({
        "q": query,
        "numberOfResults": count * 3,  # fetch more to account for filtering
        "sortCriteria": "@kmdoclastmod descending",
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
    for item in data.get("results", []):
        raw = item.get("raw", {})
        doc_id = raw.get("kmdocid", "")
        if not doc_id:
            continue
        # Skip doc IDs that reference sub-sections (contain ||)
        if "||" in doc_id:
            continue
        title = item.get("title", raw.get("kmdocfulltitle", "")).strip()
        # Only keep actual QuickSpec documents
        if "quickspec" not in title.lower():
            continue
        entries.append(QSEntry(
            doc_id=doc_id,
            title=title,
            version=str(raw.get("kmdocversion", "")),
            last_modified=raw.get("kmdoclastmod", ""),
        ))
    return entries[:count]


# ── Content fetch ──────────────────────────────────────────────────────────────

def fetch_quickspec_markdown(doc_id: str) -> tuple[str, list[str]]:
    """
    Fetch the HPE collateral HTML for *doc_id* and return:
      (markdown_text, list_of_section_names)

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

    url = _COLLATERAL_URL.format(docid=doc_id)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (pcli-qs/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")

    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    if not main:
        raise RuntimeError(f"Could not find <main> in page for doc {doc_id!r}")

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
