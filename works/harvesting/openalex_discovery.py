# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OpenAlex Source lookup helpers for the ``/manage/sources/new/`` "suggest
values" button (OpenAlex source ID / ISSN-L / DOI prefix).

Read-only preview, like ``works.harvesting.discovery``'s OAI-PMH endpoint
discovery — these never create or modify a ``Source``; the admin picks a
candidate (or types a value manually) and the create form still does the
actual write.
"""

import logging
from collections import Counter

import requests

logger = logging.getLogger(__name__)

OPENALEX_SOURCES_URL = "https://api.openalex.org/sources"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"
_HTTP_TIMEOUT = 15


def search_openalex_sources(query: str, limit: int = 5) -> list[dict]:
    """Search OpenAlex's Sources API by name. Up to ``limit`` candidates:
    ``openalex_id`` (bare ``"S..."``), ``name``, ``issn_l``, ``homepage_url``,
    ``works_count``. Empty list on no match or a request failure — this is a
    convenience suggestion, never a hard requirement for creating a Source.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        resp = requests.get(
            OPENALEX_SOURCES_URL,
            params={"search": query, "per-page": limit},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        logger.debug("OpenAlex source search failed for %r: %s", query, exc)
        return []

    candidates = []
    for item in results[:limit]:
        full_id = item.get("id") or ""
        openalex_id = full_id.rsplit("/", 1)[-1] if full_id else None
        if not openalex_id:
            continue
        candidates.append(
            {
                "openalex_id": openalex_id,
                "name": item.get("display_name") or "",
                "issn_l": item.get("issn_l") or "",
                "homepage_url": item.get("homepage_url") or "",
                "works_count": item.get("works_count"),
            }
        )
    return candidates


def guess_doi_prefix(openalex_id: str, sample_size: int = 25) -> str | None:
    """Best-effort DOI-prefix guess for an OpenAlex source id: the most
    common prefix among a small sample of its works. OpenAlex doesn't expose
    a source-level DOI-prefix field directly, so this is the closest cheap
    proxy. Returns ``None`` if no sampled work has a DOI or the request fails.
    """
    if not openalex_id:
        return None
    try:
        resp = requests.get(
            OPENALEX_WORKS_URL,
            params={
                "filter": f"primary_location.source.id:{openalex_id}",
                "select": "doi",
                "per-page": sample_size,
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        logger.debug("OpenAlex DOI-prefix guess failed for %s: %s", openalex_id, exc)
        return None

    prefixes = []
    for item in results:
        doi = (item.get("doi") or "").strip()
        # OpenAlex DOIs are full https://doi.org/10.xxxx/yyyy URLs.
        if "10." not in doi:
            continue
        bare = doi.split("10.", 1)[1]
        prefixes.append("10." + bare.split("/", 1)[0])
    if not prefixes:
        return None
    return Counter(prefixes).most_common(1)[0][0]
