# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OAI-PMH repository discovery for the ``/manage/sources/new/`` two-step
create flow (issues #133/#44).

``discover_oai_endpoint(url)`` takes a journal/repository URL pasted by an
admin (a homepage, or an OAI base URL directly) and returns a ranked list of
validated OAI-PMH endpoint candidates, each carrying the repository metadata
from its ``Identify`` response — so the admin can inspect *what* they're
about to add before creating the ``Source`` row.

Candidate generation order (best guess first):
1. ``<head>`` ``<link>``/``<meta>`` pointers on the given page (structured,
   most reliable when present).
2. Platform-specific path patterns (OJS ``…/index.php/<journal>/oai``,
   generic ``…/oai``) inferred from the given URL's path.
3. The given URL itself, on the chance it already *is* an OAI base URL or
   accepts ``?verb=Identify`` appended directly.

Every candidate is validated with a real ``verb=Identify`` request before
being returned — only endpoints that produce a parseable OAI-PMH ``Identify``
response make it into the result list.
"""

import logging
from urllib.parse import urljoin, urlparse
from xml.dom import minidom

import requests
from bs4 import BeautifulSoup

from .sessions import OAI_HTTP_TIMEOUT, _looks_like_oai_xml, _oai_session, _try_solve_pow_challenge

logger = logging.getLogger(__name__)

_DISCOVERY_FETCH_TIMEOUT = 15


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = f"https://{url}"
    return url


def _fetch_with_redirects(session: requests.Session, url: str) -> tuple[str, requests.Response | None]:
    """GET ``url`` (following redirects — ``requests``' default for GET, made
    explicit here) and return ``(resolved_url, response)``, or ``(url, None)``
    on any failure — discovery must degrade gracefully, not raise, when the
    given URL can't be fetched at all.

    A bare domain like ``josis.org`` almost always 302s to the real journal
    path (e.g. ``josis.org/index.php/josis`` for an OJS install); every
    subsequent candidate-guessing step needs that *resolved* URL, not the one
    the admin typed, or the OJS ``/index.php/<journal>/oai`` pattern below
    never matches (the un-redirected URL's path is empty).
    """
    try:
        resp = session.get(url, timeout=_DISCOVERY_FETCH_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
        return resp.url, resp
    except Exception as exc:  # noqa: BLE001
        logger.info("discovery: could not fetch %s: %s", url, exc)
        return url, None


def _candidate_urls_from_head(resp: requests.Response, base_url: str) -> list[str]:
    """Parse an already-fetched page's ``<head>`` for OAI-PMH pointers
    (``<link>``/``<meta>``).

    Looks for a ``<link>`` whose ``type`` mentions ``oai`` or whose ``href``
    contains ``oai``/``verb=Identify``, resolved to an absolute URL against
    ``base_url`` (the *resolved*, post-redirect URL — see
    ``_fetch_with_redirects``).
    """
    candidates: list[str] = []
    soup = BeautifulSoup(resp.text, "html.parser")
    head = soup.head or soup
    for link in head.find_all("link", href=True):
        href = link["href"]
        link_type = (link.get("type") or "").lower()
        if "oai" in link_type or "oai" in href.lower() or "verb=identify" in href.lower():
            candidates.append(urljoin(base_url, href))
    return candidates


def _candidate_urls_from_platform_patterns(url: str) -> list[str]:
    """Guess OAI-PMH base URLs from common platform path conventions."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    candidates: list[str] = []

    # OJS: https://example.org/index.php/<journal>/... -> .../index.php/<journal>/oai
    if "/index.php/" in parsed.path:
        prefix, _, rest = parsed.path.partition("/index.php/")
        journal = rest.split("/")[0]
        if journal:
            candidates.append(f"{origin}{prefix}/index.php/{journal}/oai")

    # Generic conventions, tried against both the full path and the bare origin.
    stripped_path = parsed.path.rstrip("/")
    if stripped_path:
        candidates.append(f"{origin}{stripped_path}/oai")
    candidates.append(f"{origin}/oai")
    candidates.append(f"{origin}/oai/request")

    return candidates


def _validate_oai_candidate(session: requests.Session, base_url: str) -> dict | None:
    """Fetch ``<base_url>?verb=Identify`` and return its metadata, or ``None``."""
    identify_url = f"{base_url}{'&' if '?' in base_url else '?'}verb=Identify"
    try:
        resp = session.get(identify_url, timeout=OAI_HTTP_TIMEOUT)
        if resp.status_code == 403 and _try_solve_pow_challenge(session, resp):
            resp = session.get(identify_url, timeout=OAI_HTTP_TIMEOUT)
        if not resp.ok or not _looks_like_oai_xml(resp.content):
            return None
        dom = minidom.parseString(resp.content)
    except Exception as exc:  # noqa: BLE001
        logger.info("discovery: %s is not a valid OAI-PMH endpoint: %s", base_url, exc)
        return None

    identify_nodes = dom.documentElement.getElementsByTagName("Identify")
    if not identify_nodes:
        return None

    def _text(tag_name):
        nodes = identify_nodes[0].getElementsByTagName(tag_name)
        if nodes and nodes[0].firstChild:
            return nodes[0].firstChild.nodeValue.strip()
        return None

    return {
        "base_url": _text("baseURL") or base_url,
        "repository_name": _text("repositoryName"),
        "granularity": _text("granularity"),
        "earliest_datestamp": _text("earliestDatestamp"),
        "admin_email": _text("adminEmail"),
        "protocol_version": _text("protocolVersion"),
    }


def discover_oai_endpoint(url: str) -> list[dict]:
    """Return validated OAI-PMH endpoint candidates for the given URL.

    Accepts a bare domain (``josis.org``) as well as a full URL —
    ``_normalize_url`` prepends ``https://`` when no scheme is given. The
    given URL is then fetched *with redirects followed*
    (``_fetch_with_redirects``): a bare domain or homepage URL commonly 302s
    to the actual journal path (e.g. an OJS install redirecting
    ``josis.org`` → ``josis.org/index.php/josis``), and every candidate-
    guessing step below needs that resolved URL, not the one the admin
    typed, or the platform-specific path patterns never match.

    Each candidate dict has ``base_url``, ``repository_name``,
    ``granularity``, ``earliest_datestamp``, ``admin_email``,
    ``protocol_version``. Candidates are de-duplicated (first occurrence
    wins) and returned in best-guess-first order: head pointers, then
    platform path patterns, then the resolved URL itself, then (if
    different) the original URL as typed.
    """
    url = _normalize_url(url)
    if not url:
        return []

    session = _oai_session()
    resolved_url, resp = _fetch_with_redirects(session, url)

    ordered_urls = [
        *(_candidate_urls_from_head(resp, resolved_url) if resp is not None else []),
        *_candidate_urls_from_platform_patterns(resolved_url),
        resolved_url,
    ]
    if resolved_url != url:
        ordered_urls.append(url)

    seen: set[str] = set()
    results: list[dict] = []
    for candidate_url in ordered_urls:
        normalized = candidate_url.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        metadata = _validate_oai_candidate(session, candidate_url)
        if metadata is not None:
            results.append(metadata)
    return results
