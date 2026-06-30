# SPDX-FileCopyrightText: 2025 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""
OpenAlex matching service for publications.

This module provides centralized OpenAlex work matching functionality
that can be used across all harvesting workflows (OAI-PMH, RSS, etc).
"""

import logging
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from works.harvesting.sessions import _openalex_session
from works.utils.orcid import normalize_orcid

logger = logging.getLogger(__name__)

OPENALEX_API_BASE = "https://api.openalex.org"
REQUEST_DELAY = 0.1  # 100ms between requests
_VERSIONED_DOI_RE = re.compile(r"^(.+)/v(\d+)$", re.IGNORECASE)


def author_records_from_authorships(authorships) -> list:
    """Extract structured author records (name, ORCID, openalex_id, rank) from an
    OpenAlex ``authorships`` array. Returns only records with at least one identifier."""
    records = []
    for i, authorship in enumerate(authorships or []):
        author = authorship.get("author") or {}
        name = (author.get("display_name") or "").strip()
        orcid = normalize_orcid(author.get("orcid"))
        openalex_id = (author.get("id") or "").strip() or None
        # Strip full URL prefix from OpenAlex author id.
        if openalex_id and openalex_id.startswith("https://openalex.org/"):
            openalex_id = openalex_id[len("https://openalex.org/") :]
        if orcid or openalex_id:
            records.append({"name": name, "orcid": orcid, "openalex_id": openalex_id, "rank": i})
    return records


class OpenAlexMatcher:
    """
    Centralized service for matching publications to OpenAlex works.

    Implements multiple matching strategies with fallback:
    1. DOI-based exact match
    2. Title + first author match
    3. Title-only match (partial)
    """

    def __init__(self):
        self.session = _openalex_session()
        self.last_request_time = 0

    def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < REQUEST_DELAY:
            time.sleep(REQUEST_DELAY - elapsed)
        self.last_request_time = time.time()

    def _make_request(self, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """Make a rate-limited request to OpenAlex API."""
        self._rate_limit()
        try:
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning("OpenAlex API request failed: %s", str(e))
            return None

    @staticmethod
    def _versioned_doi_fallbacks(doi: str) -> List[Tuple[str, str]]:
        """Return (fallback_doi, label) pairs to try when an exact-DOI lookup fails.

        Handles DOIs with a version suffix like ``/v2``:
        - Tries the bare DOI (no version) first
        - Then tries each earlier version number down to v1

        Returns an empty list when the DOI has no version suffix.
        """
        m = _VERSIONED_DOI_RE.search(doi)
        if not m:
            return []
        base, version = m.group(1), int(m.group(2))
        return [(base, "no_version")] + [(f"{base}/v{v}", f"v{v}") for v in range(version - 1, 0, -1)]

    def match_by_doi(self, doi: str) -> Optional[Dict]:
        """
        Exact match by DOI, with automatic version-suffix fallback.

        When the exact DOI fails (e.g. ``10.x/y/v2`` → 404), tries the bare
        DOI (``10.x/y``) and earlier version numbers (``/v1``, …) before
        giving up.  A successful fallback match is annotated with a private
        ``_doi_version_fallback`` key so callers can record the mismatch in
        provenance (consumed and removed in :meth:`match_publication`).

        Args:
            doi: The DOI string (e.g., "10.1234/example" or "10.1234/example/v2")

        Returns:
            OpenAlex work data if found, None otherwise
        """
        if not doi:
            return None

        # Clean DOI
        doi = doi.strip().lower()
        if doi.startswith("http"):
            doi = doi.split("doi.org/")[-1]

        url = f"{OPENALEX_API_BASE}/works/doi:{quote(doi)}"
        logger.debug("Matching by DOI: %s", doi)

        data = self._make_request(url)
        if data and data.get("id"):
            logger.info("Found OpenAlex match by DOI: %s -> %s", doi, data["id"])
            return data

        # Try version-suffix fallbacks (bare DOI and earlier versions)
        for fallback_doi, label in self._versioned_doi_fallbacks(doi):
            fallback_url = f"{OPENALEX_API_BASE}/works/doi:{quote(fallback_doi)}"
            logger.debug("Trying DOI version fallback (%s): %s", label, fallback_doi)
            fallback_data = self._make_request(fallback_url)
            if fallback_data and fallback_data.get("id"):
                logger.info(
                    "Found OpenAlex match via DOI version fallback (%s): %s -> %s",
                    label,
                    doi,
                    fallback_data["id"],
                )
                # Annotate so match_publication can record this in provenance.
                fallback_data["_doi_version_fallback"] = {
                    "queried_doi": doi,
                    "matched_doi": fallback_doi,
                }
                return fallback_data

        return None

    def match_by_title_author(self, title: str, author: Optional[str] = None) -> Tuple[Optional[Dict], List[Dict]]:
        """
        Match by title and optionally first author.

        Args:
            title: Work title
            author: First author name (optional)

        Returns:
            Tuple of (exact_match, partial_matches)
            - exact_match: Single best match if confidence is high
            - partial_matches: List of potential matches with metadata
        """
        if not title:
            return None, []

        # Build filter with values URL-encoded. Embed in the URL directly
        # (not via params) so requests does not double-encode the already-%xx
        # sequences.
        #
        # OpenAlex treats ? and * as wildcard characters — strip them.
        # Commas must also be removed from both title and author: the HTTP
        # server URL-decodes the query string before OpenAlex parses it, so
        # %2C in a value becomes a literal comma that OpenAlex splits on as a
        # filter-condition separator, producing a 400.
        # raw_author_name.search is the correct field; author.search does not
        # exist in the Works filter API.
        title_clean = re.sub(r"[,?*]", " ", title)
        title_clean = re.sub(r"\s+", " ", title_clean).strip()
        filter_parts = [f"title.search:{quote(title_clean, safe='')}"]
        if author:
            author_clean = re.sub(r"\s+", " ", author.replace(",", " ")).strip()
            filter_parts.append(f"raw_author_name.search:{quote(author_clean, safe='')}")

        filter_str = ",".join(filter_parts)
        url = f"{OPENALEX_API_BASE}/works?filter={filter_str}&per-page=5"

        logger.debug("Matching by title%s: %s", " + author" if author else "", title[:50])

        data = self._make_request(url)
        if not data or not data.get("results"):
            return None, []

        results = data["results"]
        partial_matches = []

        for result in results:
            match_info = {
                "openalex_id": result.get("id"),
                "title": result.get("title"),
                "doi": result.get("doi"),
                "match_type": "title+author" if author else "title",
                "authors": [a.get("author", {}).get("display_name") for a in result.get("authorships", [])[:3]],
            }
            partial_matches.append(match_info)

        # If we have a very close title match with author, consider it exact
        if results and author:
            best_match = results[0]
            # Check if title is very similar (simple heuristic)
            if self._titles_similar(title, best_match.get("title", "")):
                logger.info("Found strong OpenAlex match by title+author: %s", best_match["id"])
                return best_match, partial_matches

        logger.info("Found %d partial OpenAlex matches for title: %s", len(partial_matches), title[:50])
        return None, partial_matches

    def _titles_similar(self, title1: str, title2: str, threshold: float = 0.9) -> bool:
        """
        Simple title similarity check.

        Uses character overlap ratio as a basic similarity metric.
        """
        t1 = title1.lower().strip()
        t2 = title2.lower().strip()

        if not t1 or not t2:
            return False

        # Exact match
        if t1 == t2:
            return True

        # Character-level overlap
        set1 = set(t1)
        set2 = set(t2)
        overlap = len(set1 & set2)
        total = max(len(set1), len(set2))

        similarity = overlap / total if total > 0 else 0
        return similarity >= threshold

    def extract_openalex_fields(self, work_data: Dict) -> Dict:
        """
        Extract relevant fields from OpenAlex work data.

        Args:
            work_data: Full OpenAlex work response

        Returns:
            Dictionary with extracted fields for Work model
        """
        # Safely extract fulltext origin
        fulltext_origin = None
        primary_location = work_data.get("primary_location")
        if primary_location and isinstance(primary_location, dict):
            source = primary_location.get("source")
            if source and isinstance(source, dict):
                fulltext_origin = source.get("type")

        # Bare DOI (no ``https://doi.org/`` prefix) so callers can write
        # straight into ``Work.doi``. We *only* emit this when OpenAlex
        # carries one — see ``backfill_openalex.py`` for the apply step
        # which uses ``setattr`` and would otherwise blank a populated
        # field with ``None``.
        raw_doi = work_data.get("doi")
        bare_doi = None
        if raw_doi:
            bare_doi = raw_doi.strip()
            for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
                if bare_doi.lower().startswith(prefix):
                    bare_doi = bare_doi[len(prefix) :]
                    break

        from works.harvesting.openalex_locations import build_locations

        extracted = {
            "openalex_id": work_data.get("id"),
            "openalex_fulltext_origin": fulltext_origin,
            "openalex_is_retracted": work_data.get("is_retracted", False),
            "openalex_ids": work_data.get("ids", {}),
            "type": work_data.get("type"),  # OpenAlex work type
            "keywords": [],
            "openalex_open_access_status": None,
            "topics": [],
            "authors": [],
            "volume": None,
            "issue": None,
            "first_page": None,
            "last_page": None,
            # Hosting copies/versions, normalised + credited to OpenAlex.
            "locations": build_locations(work_data),
        }
        if bare_doi:
            extracted["doi"] = bare_doi

        biblio = work_data.get("biblio") or {}
        if isinstance(biblio, dict):
            for k in ("volume", "issue", "first_page", "last_page"):
                v = biblio.get(k)
                if v not in (None, ""):
                    extracted[k] = str(v)[:64]

        # Extract authors (display_name from authorships) and structured records with ORCIDs.
        authorships = work_data.get("authorships", [])
        if authorships:
            authors = []
            for authorship in authorships:
                author = authorship.get("author", {})
                if author and author.get("display_name"):
                    authors.append(author.get("display_name"))
            extracted["authors"] = authors
        extracted["author_records"] = author_records_from_authorships(authorships)

        # Extract keywords (display_name only)
        keywords = work_data.get("keywords", [])
        if keywords:
            extracted["keywords"] = [kw.get("display_name") for kw in keywords if kw.get("display_name")]

        # Extract open access status
        open_access = work_data.get("open_access", {})
        if open_access.get("is_oa"):
            extracted["openalex_open_access_status"] = open_access.get("oa_status")
        else:
            extracted["openalex_open_access_status"] = None

        # Extract topics (display_name only)
        topics = work_data.get("topics", [])
        if topics:
            extracted["topics"] = [topic.get("display_name") for topic in topics if topic.get("display_name")]

        return extracted

    def match_publication(
        self, title: str, doi: Optional[str] = None, author: Optional[str] = None
    ) -> Tuple[Optional[Dict], Optional[List[Dict]]]:
        """
        Main entry point for matching a work.

        Tries multiple strategies in order:
        1. DOI match (if DOI provided)
        2. Title + author match (if author provided)
        3. Title-only match

        Args:
            title: Work title (required)
            doi: DOI if available
            author: First author name if available

        Returns:
            Tuple of (exact_match_fields, partial_matches)
            - exact_match_fields: Dict with OpenAlex fields if perfect match found, None otherwise
            - partial_matches: List of potential matches with metadata
        """
        # Strategy 1: DOI match
        if doi:
            work_data = self.match_by_doi(doi)
            if work_data:
                # Pop the private fallback annotation before passing to extract.
                doi_version_fallback = work_data.pop("_doi_version_fallback", None)
                fields = self.extract_openalex_fields(work_data)
                match_item = {
                    "openalex_id": work_data.get("id"),
                    "title": work_data.get("title"),
                    "doi": work_data.get("doi"),
                    "match_type": "doi",
                    "matched": True,
                }
                if doi_version_fallback:
                    match_item.update(doi_version_fallback)
                return fields, [match_item]

        # Strategy 2 & 3: Title-based matching
        exact_match, partial_matches = self.match_by_title_author(title, author)

        if exact_match:
            fields = self.extract_openalex_fields(exact_match)
            return fields, partial_matches

        # No exact match, return partial matches
        return None, partial_matches if partial_matches else None


# Singleton instance
_matcher = None


def get_openalex_matcher() -> OpenAlexMatcher:
    """Get or create the singleton OpenAlexMatcher instance."""
    global _matcher
    if _matcher is None:
        _matcher = OpenAlexMatcher()
    return _matcher
