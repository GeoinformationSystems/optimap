# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OpenAlex enrichment helper used by every harvester to fill in missing fields."""

import logging

from works.openalex_matcher import get_openalex_matcher

logger = logging.getLogger(__name__)


def build_openalex_fields(title, doi=None, author=None, existing_metadata=None):
    """
    Match a work against OpenAlex and return the appropriate fields dictionary.

    This function prioritizes existing metadata from the original source and only fills
    in missing information from OpenAlex.

    Args:
        title: Work title (required)
        doi: Work DOI (optional)
        author: Work author (optional)
        existing_metadata: Dict of metadata already extracted from original source (optional)

    Returns:
        tuple: (openalex_fields dict, metadata_provenance dict)
    """
    if existing_metadata is None:
        existing_metadata = {}

    openalex_fields = {}
    metadata_provenance = {}

    try:
        matcher = get_openalex_matcher()
        openalex_data, partial_matches = matcher.match_publication(title=title, doi=doi, author=author)

        if openalex_data:
            logger.debug("OpenAlex match found for: %s", title[:50] if title else "No title")

            if existing_metadata.get("authors"):
                openalex_fields["authors"] = existing_metadata["authors"]
                metadata_provenance["authors"] = "original_source"
            elif openalex_data.get("authors"):
                openalex_fields["authors"] = openalex_data["authors"]
                metadata_provenance["authors"] = "openalex"

            if existing_metadata.get("keywords"):
                openalex_fields["keywords"] = existing_metadata["keywords"]
                metadata_provenance["keywords"] = "original_source"
            elif openalex_data.get("keywords"):
                openalex_fields["keywords"] = openalex_data["keywords"]
                metadata_provenance["keywords"] = "openalex"

            if openalex_data.get("topics"):
                openalex_fields["topics"] = openalex_data["topics"]
                metadata_provenance["topics"] = "openalex"

            if openalex_data.get("type"):
                openalex_fields["type"] = openalex_data["type"]
                metadata_provenance["type"] = "openalex"

            openalex_fields["openalex_id"] = openalex_data.get("openalex_id")
            openalex_fields["openalex_fulltext_origin"] = openalex_data.get("openalex_fulltext_origin")
            openalex_fields["openalex_is_retracted"] = openalex_data.get("openalex_is_retracted", False)
            openalex_fields["openalex_ids"] = openalex_data.get("openalex_ids", {})
            openalex_fields["openalex_open_access_status"] = openalex_data.get("openalex_open_access_status")

            # Hosting copies/versions (credited to OpenAlex). Fill-if-empty: only
            # set when OpenAlex actually returned locations so a re-enrich does
            # not blank an existing list.
            if openalex_data.get("locations"):
                openalex_fields["locations"] = openalex_data["locations"]
                metadata_provenance["locations"] = "openalex"

            for biblio_key in ("volume", "issue", "first_page", "last_page"):
                if openalex_data.get(biblio_key):
                    openalex_fields[biblio_key] = openalex_data[biblio_key]
                    metadata_provenance[biblio_key] = "openalex"

            # Record DOI version fallback so provenance reflects the mismatch.
            if partial_matches:
                first_match = partial_matches[0]
                if first_match.get("queried_doi"):
                    metadata_provenance["openalex_doi_version_fallback"] = {
                        "queried_doi": first_match["queried_doi"],
                        "matched_doi": first_match.get("matched_doi"),
                    }

            metadata_provenance["openalex_metadata"] = "openalex"

        elif partial_matches:
            openalex_fields["openalex_id"] = None
            openalex_fields["openalex_match_info"] = partial_matches
            logger.debug("OpenAlex partial matches found for: %s", title[:50] if title else "No title")

            if existing_metadata.get("authors"):
                openalex_fields["authors"] = existing_metadata["authors"]
                metadata_provenance["authors"] = "original_source"
            if existing_metadata.get("keywords"):
                openalex_fields["keywords"] = existing_metadata["keywords"]
                metadata_provenance["keywords"] = "original_source"

        else:
            openalex_fields["openalex_id"] = None
            if doi:
                logger.warning("No OpenAlex match for work with DOI %s: %s", doi, title[:50] if title else "No title")
            else:
                logger.debug("No OpenAlex match for: %s", title[:50] if title else "No title")

            if existing_metadata.get("authors"):
                openalex_fields["authors"] = existing_metadata["authors"]
                metadata_provenance["authors"] = "original_source"
            if existing_metadata.get("keywords"):
                openalex_fields["keywords"] = existing_metadata["keywords"]
                metadata_provenance["keywords"] = "original_source"

    except Exception as openalex_err:
        logger.warning("OpenAlex matching failed for '%s': %s", title[:50] if title else "No title", openalex_err)
        openalex_fields["openalex_id"] = None

        if existing_metadata.get("authors"):
            openalex_fields["authors"] = existing_metadata["authors"]
            metadata_provenance["authors"] = "original_source"
        if existing_metadata.get("keywords"):
            openalex_fields["keywords"] = existing_metadata["keywords"]
            metadata_provenance["keywords"] = "original_source"

    return openalex_fields, metadata_provenance


def link_openalex_authors(work, openalex_fields, *, force: bool = False) -> int:
    """Link author identifiers from ``openalex_fields`` (as returned by
    :func:`build_openalex_fields`) to ``work`` via the WorkAuthor through-table.

    Must be called **after** the Work has been saved (relational M2M).
    Returns the number of WorkAuthor rows created (0 if nothing to link or
    fill-if-empty policy skips).
    """
    records = openalex_fields.get("author_records") or []
    if not records:
        return 0
    from works.services.authors import link_work_authors

    return link_work_authors(work, records, source="openalex", force=force)
