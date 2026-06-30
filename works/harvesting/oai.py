# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OAI-PMH harvester (also used for OJS and Janeway sources, which expose
the same protocol).

Public surface:
- ``parse_oai_xml_and_save_works`` — parse a ListRecords payload into Works.
- ``harvest_oai_endpoint`` — Django-Q task: fetch + parse + persist + notify.
"""

import logging
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from xml.dom import minidom

import requests
from bs4 import BeautifulSoup
from django.contrib.gis.geos import GeometryCollection
from django.db import transaction
from django.utils import timezone

from works.models import HarvestingEvent, Source

from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _backfill_empty_doi,
    _find_existing_work,
    _save_or_update_work,
    complete_harvest,
    ensure_collection_for_source,
    fail_harvest,
    get_or_create_admin_command_user,
    parse_publication_date,
    render_harvest_email,
    resolve_user,
    send_harvest_email,
    start_harvesting_event,
)
from .metadata_html import extract_geometry_from_html, extract_timeperiod_from_html
from .openalex import build_openalex_fields, link_openalex_authors
from .sessions import (
    OAI_HTTP_TIMEOUT,
    OAI_RETRY_TOTAL,
    _looks_like_oai_xml,
    _oai_session,
    _short_body,
    _try_solve_pow_challenge,
)

logger = logging.getLogger(__name__)
DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
# Matches plain ISSNs (NNNN-NNNN) and info:eu-repo URI variants used by Pensoft/ARPHA
# and other OAI-PMH endpoints: info:eu-repo/semantics/altIdentifier/[pe]issn/<ISSN>
_ISSN_PLAIN = re.compile(r"^\d{4}-\d{3}[\dX]$", re.IGNORECASE)
_ISSN_URI = re.compile(r"altIdentifier/[pe]issn/(\d{4}-\d{3}[\dX])", re.IGNORECASE)


def _extract_issn(candidate: str | None) -> str | None:
    """Return the ISSN string from a plain ISSN or an info:eu-repo URI, else None."""
    if not candidate:
        return None
    if _ISSN_PLAIN.match(candidate.strip()):
        return candidate.strip()
    m = _ISSN_URI.search(candidate)
    if m:
        return m.group(1)
    return None


def parse_oai_xml_and_save_works(
    content,
    event: HarvestingEvent,
    max_records=None,
    warning_collector=None,
    update_existing=False,
    stats=None,
    session=None,
):
    source = event.source
    logger.info("Starting OAI-PMH parsing for source: %s", source.name)
    if stats is None:
        stats = HarvestStats()

    if content and len(content.strip()) > 0:
        logger.debug("Parsing XML content from response")
        try:
            dom = minidom.parseString(content)
            records = dom.documentElement.getElementsByTagName("record")
            logger.info("Found %d records in XML response", len(records))
        except Exception as e:
            logger.error("Failed to parse XML content: %s", str(e))
            logger.warning("No articles found in OAI-PMH response!")
            return
    else:
        logger.warning("Empty or no content provided - cannot harvest")
        return

    if not records:
        logger.warning("No articles found in OAI-PMH response!")
        return

    if max_records and hasattr(records, "__len__"):
        records = records[:max_records]
        logger.info("Limited to first %d records", max_records)
    elif max_records:
        records = list(records)[:max_records]
        logger.info("Limited to first %d records", max_records)

    processed_count = 0

    total_records = len(records) if hasattr(records, "__len__") else None
    _size_hint = total_records or max_records or 0
    log_interval = 20 if _size_hint <= 100 else 50

    for rec in records:
        try:
            processed_count += 1
            if processed_count % log_interval == 0:
                if total_records:
                    logger.info("Processed %d of %d records", processed_count, total_records)
                else:
                    logger.info("Processed %d records", processed_count)

            if hasattr(rec, "metadata"):
                identifiers = rec.metadata.get("identifier", []) + rec.metadata.get("relation", [])

                def get_field(k):
                    return rec.metadata.get(k, [""])[0]
            else:
                id_nodes = rec.getElementsByTagName("dc:identifier")
                rel_nodes = rec.getElementsByTagName("dc:relation")
                identifiers = [
                    n.firstChild.nodeValue.strip()
                    for n in list(id_nodes) + list(rel_nodes)
                    if n.firstChild and n.firstChild.nodeValue
                ]

                def get_field(tag):
                    nodes = rec.getElementsByTagName(tag)
                    return nodes[0].firstChild.nodeValue.strip() if nodes and nodes[0].firstChild else None

            http_urls = [u for u in identifiers if u and u.lower().startswith("http")]
            view_urls = [u for u in http_urls if "/view/" in u]
            identifier_value = (view_urls or http_urls or [None])[0]

            title_value = get_field("title") or get_field("dc:title")
            abstract_text = get_field("description") or get_field("dc:description")
            publisher_value = get_field("publisher") or get_field("dc:publisher")
            raw_date_value = get_field("date") or get_field("dc:date")
            date_value = parse_publication_date(raw_date_value)

            doi_text = None
            issn_text = None
            for u in identifiers:
                if u and (m := DOI_REGEX.search(u)):
                    doi_text = m.group(0)
                    break

            issn_candidates = list(identifiers)
            issn_candidates.append(get_field("source") or get_field("dc:source"))

            for candidate in issn_candidates:
                issn_text = _extract_issn(candidate)
                if issn_text:
                    break

            if not identifier_value or not identifier_value.startswith("http"):
                logger.debug("Skipping invalid URL: %s", identifier_value)
                continue

            # Early dedup: avoid the expensive HTML fetch and OpenAlex API
            # calls for records already in the database. _save_or_update_work
            # runs the same check later, but doing it here short-circuits the
            # network work for the common incremental-harvest case where most
            # records are already known. When update_existing=True we still
            # need the full pipeline for same-source records that need updates;
            # cross-source duplicates are never updated so we can skip them
            # unconditionally.
            _early_existing = _find_existing_work(doi=doi_text, url=identifier_value)
            if _early_existing is not None:
                if doi_text and not _early_existing.doi:
                    _backfill_empty_doi(_early_existing, doi_text, event)
                _cross_source = _early_existing.source_id != source.id
                if _cross_source or not update_existing:
                    action = "skipped_cross_source" if _cross_source else "skipped_same_source"
                    if _cross_source:
                        logger.info(
                            "Skipping cross-source duplicate %s — already under source id=%s",
                            doi_text or identifier_value,
                            _early_existing.source_id,
                        )
                    else:
                        logger.debug("Skipping same-source duplicate %s", doi_text or identifier_value)
                    stats.record(action)
                    continue

            logger.debug("Processing work: %s", title_value[:50] if title_value else "No title")

            src_obj = source

            if issn_text:
                try:
                    src_obj = Source.objects.get(issn_l=issn_text)
                    logger.debug("Matched source by ISSN %s: %s", issn_text, src_obj.name)
                except Source.DoesNotExist:
                    if publisher_value:
                        src_obj, created = Source.objects.get_or_create(
                            issn_l=issn_text, defaults={"name": publisher_value}
                        )
                        if created:
                            logger.debug("Created new source with ISSN %s: %s", issn_text, publisher_value)
                    else:
                        src_obj, created = Source.objects.get_or_create(
                            issn_l=issn_text, defaults={"name": f"Unknown Source (ISSN: {issn_text})"}
                        )
                        if created:
                            logger.debug("Created new source with ISSN %s", issn_text)
            # Publisher-name-only auto-creation removed: bare publisher strings are
            # unreliable identifiers (platform names, not journal names) and override
            # the explicitly configured source. ISSN-based matching above is sufficient.

            geom_obj = GeometryCollection()
            period_start, period_end = [], []
            geometry_source_label = None
            try:
                logger.debug("Fetching HTML content for geometry extraction: %s", identifier_value)
                http = session if session is not None else requests
                resp = http.get(identifier_value, timeout=10)
                # Some landing pages redirect to the same bot-protected host on a
                # different scheme (HTTP vs HTTPS). Secure cookies aren't sent on
                # HTTP redirects, so solve any fresh PoW challenge here too.
                if resp.status_code == 403 and session is not None and _try_solve_pow_challenge(session, resp):
                    resp = http.get(identifier_value, timeout=10)
                resp.raise_for_status()
                soup = BeautifulSoup(resp.content, "html.parser")
                extracted, geometry_source_label = extract_geometry_from_html(
                    soup,
                    base_url=identifier_value,
                )
                if extracted is not None:
                    geom_obj = extracted
                    logger.debug(
                        "Extracted geometry from HTML for %s via %s",
                        identifier_value,
                        geometry_source_label,
                    )
                ts, te = extract_timeperiod_from_html(soup)
                if ts:
                    period_start = ts
                if te:
                    period_end = te
            except Exception as fetch_err:
                logger.debug("Error fetching HTML for %s: %s", identifier_value, fetch_err)

            author_field = get_field("creator") or get_field("dc:creator")
            authors_list = []
            if author_field:
                authors_list = [a.strip() for a in author_field.replace(";", ",").split(",") if a.strip()]

            subject_field = get_field("subject") or get_field("dc:subject")
            keywords_list = []
            if subject_field:
                keywords_list = [k.strip() for k in subject_field.replace(";", ",").split(",") if k.strip()]

            existing_metadata = {}
            if authors_list:
                existing_metadata["authors"] = authors_list
            if keywords_list:
                existing_metadata["keywords"] = keywords_list

            openalex_fields, metadata_provenance = build_openalex_fields(
                title=title_value, doi=doi_text, author=author_field, existing_metadata=existing_metadata
            )

            if geometry_source_label:
                metadata_provenance["geometry"] = geometry_source_label

            try:
                with transaction.atomic():
                    admin_user = get_or_create_admin_command_user()

                    provenance = {
                        "harvest": {
                            "harvester": "harvest_oai_endpoint",
                            "source_url": source.url_field,
                            "source_type": source.source_type,
                            "source_name": source.name,
                            "harvested_at": timezone.now().isoformat(),
                            "harvesting_event_id": event.id,
                        },
                        "metadata_sources": dict(metadata_provenance or {}),
                    }

                    if "type" not in openalex_fields:
                        openalex_fields["type"] = src_obj.default_work_type if src_obj else "article"

                    work_kwargs = dict(
                        title=title_value,
                        abstract=abstract_text,
                        publicationDate=date_value,
                        url=identifier_value,
                        doi=doi_text,
                        source=src_obj,
                        status="h",
                        geometry=geom_obj,
                        timeperiod_startdate=period_start,
                        timeperiod_enddate=period_end,
                        job=event,
                        provenance=provenance,
                        created_by=admin_user,
                        **openalex_fields,
                    )
                    work, action = _save_or_update_work(
                        work_kwargs,
                        source,
                        event,
                        update_existing=update_existing,
                    )
                    stats.record(action)
                    if action in ("created", "updated"):
                        # Propagate the harvest's source collection to the work
                        # (no-op when the source has no collection set). The
                        # *event's* source wins over the per-record ISSN-matched
                        # src_obj — the operator's intent for this harvest takes
                        # precedence over per-record source switching.
                        if source and source.collection_id:
                            work.collections.add(source.collection_id)
                    if action in ("created", "updated"):
                        link_openalex_authors(work, openalex_fields)
                    if action == "created":
                        logger.info("Saved work id=%s: %s", work.id, title_value[:80] if title_value else "No title")
                    elif action == "updated":
                        logger.info("Updated work id=%s: %s", work.id, title_value[:80] if title_value else "No title")
            except Exception as save_err:
                logger.error("Failed to save work '%s': %s", title_value[:80] if title_value else "No title", save_err)
                continue

        except Exception as e:
            logger.error("Error parsing record %d: %s", processed_count, e)
            continue

    logger.info(
        "OAI-PMH parsing completed for source %s: processed %d records, created %d, updated %d, skipped %d",
        source.name,
        processed_count,
        stats.created,
        stats.updated,
        stats.skipped_same_source + stats.skipped_cross_source,
    )


def _extract_resumption_url(content: bytes, base_url: str) -> str | None:
    """Return the URL for the next OAI-PMH page, or None if this is the last page.

    Parses the resumptionToken from the XML response and constructs the next
    request URL using the base endpoint (scheme + host + path, without query).
    """
    try:
        dom = minidom.parseString(content)
        tokens = dom.documentElement.getElementsByTagName("resumptionToken")
        if not tokens:
            return None
        token_node = tokens[0]
        token_value = token_node.firstChild.nodeValue.strip() if token_node.firstChild else ""
        if not token_value:
            return None
        # Build the resumption URL from the base OAI endpoint (drop existing query)
        parsed = urlparse(base_url)
        next_query = urlencode({"verb": "ListRecords", "resumptionToken": token_value})
        next_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", next_query, ""))
        logger.debug("OAI-PMH resumptionToken found, next page: %s", next_url)
        return next_url
    except Exception as e:
        logger.warning("Failed to extract resumptionToken: %s", e)
        return None


def _get_earliest_year(base_url: str, session: requests.Session) -> int:
    """Fetch OAI-PMH Identify to find the repository's earliest record year.
    Falls back to 1970 if Identify is unreachable or the field is absent."""
    try:
        resp = session.get(f"{base_url}?verb=Identify", timeout=OAI_HTTP_TIMEOUT)
        if resp.ok and _looks_like_oai_xml(resp.content):
            dom = minidom.parseString(resp.content)
            nodes = dom.documentElement.getElementsByTagName("earliestDatestamp")
            if nodes and nodes[0].firstChild:
                year = int(nodes[0].firstChild.nodeValue.strip()[:4])
                logger.debug("Identify: earliestDatestamp year = %d", year)
                return year
    except Exception as exc:
        logger.warning("Could not determine earliestDatestamp via Identify (%s); using 1970", exc)
    return 1970


def _is_no_records_match(content: bytes) -> bool:
    """Return True when the OAI-PMH response is a noRecordsMatch error."""
    try:
        dom = minidom.parseString(content)
        for err in dom.documentElement.getElementsByTagName("error"):
            if err.getAttribute("code") == "noRecordsMatch":
                return True
    except Exception:
        pass
    return False


def _year_chunk_items(base_url: str, list_params: dict, start_year: int, end_year: int):
    """Yield ``(year, url)`` pairs for OAI-PMH ListRecords requests, latest first.

    Each URL covers exactly one year via ``from``/``until`` so the server
    never has to generate a full-history response in a single request.
    ``list_params`` carries ``metadataPrefix`` and any other params (e.g.
    ``set``) that were present in the stored source URL.
    """
    for year in range(end_year, start_year - 1, -1):
        yield (
            year,
            (
                base_url
                + "?"
                + urlencode({"verb": "ListRecords", **list_params, "from": f"{year}-01-01", "until": f"{year}-12-31"})
            ),
        )


def harvest_oai_endpoint(source_id, user=None, max_records=None, update_existing=False, event_id=None):
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    # Issue #192: the generic OAI-PMH harvester creates a Collection for each
    # endpoint on first run if the admin hasn't pre-assigned one. The new
    # Collection starts unpublished so the admin can review the auto-derived
    # name/description before flipping it on. No-op when source.collection is
    # already set (e.g. via harvest_sources --insert-sources).
    ensure_collection_for_source(source)
    event = start_harvesting_event(source, event_id)

    new_count = None
    spatial_count = None
    temporal_count = None
    visited_years: list[int] = []
    partial_year: int | None = None

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    try:
        # Derive the bare endpoint URL (scheme + host + path, no query)
        # and extract any operator-supplied params (metadataPrefix, set, …).
        _parsed = urlparse(source.url_field)
        base_oai_url = urlunparse((_parsed.scheme, _parsed.netloc, _parsed.path, "", "", ""))
        existing_qs = {k: v[0] for k, v in parse_qs(_parsed.query).items()}

        # Params forwarded verbatim on every ListRecords request (strip OAI
        # protocol keys that we control ourselves).
        list_params = {k: v for k, v in existing_qs.items() if k not in ("verb", "resumptionToken", "from", "until")}
        if "metadataPrefix" not in list_params:
            list_params["metadataPrefix"] = "oai_dc"

        session = _oai_session()
        stats = HarvestStats()

        # If the stored URL already carries explicit date bounds, use it
        # as-is (single request, no year chunking).  Otherwise chunk by
        # calendar year starting from the most recent, so the server never
        # has to generate a full-history response in a single round-trip.
        has_date_filter = "from" in existing_qs or "until" in existing_qs
        if has_date_filter:
            logger.info("Source URL has explicit date filter — skipping year chunking: %s", source.url_field)
            chunk_items: list[tuple[int | None, str]] = [(None, source.url_field)]
        else:
            earliest_year = _get_earliest_year(base_oai_url, session)
            current_year = timezone.now().year
            chunk_items = list(_year_chunk_items(base_oai_url, list_params, earliest_year, current_year))
            logger.info(
                "Harvesting %s in %d year chunks (%d → %d, latest first)",
                source.name,
                len(chunk_items),
                current_year,
                earliest_year,
            )

        budget_exhausted = False

        for chunk_year, chunk_url in chunk_items:
            if budget_exhausted:
                break

            logger.info("Fetching from OAI-PMH URL: %s", chunk_url)
            current_url = chunk_url
            page = 0
            year_had_records = False

            while current_url and not budget_exhausted:
                page += 1
                logger.info("Fetching OAI-PMH page %d: %s", page, current_url)
                try:
                    response = session.get(current_url, timeout=OAI_HTTP_TIMEOUT)
                except requests.exceptions.Timeout as e:
                    raise RuntimeError(
                        f"OAI-PMH endpoint timed out after {OAI_HTTP_TIMEOUT}s (after {OAI_RETRY_TOTAL} retries): {current_url}"
                    ) from e
                except requests.exceptions.ConnectionError as e:
                    raise RuntimeError(
                        f"OAI-PMH endpoint unreachable (after {OAI_RETRY_TOTAL} retries): {current_url}: {e}"
                    ) from e

                if not response.ok:
                    # Some repositories (e.g. GEO-LEO e-docs) sit behind a
                    # HAProxy/BunkerWeb SHA-256 Proof-of-Work challenge. Solve it
                    # once on the first page; the resulting cookie covers the rest
                    # of the session (cookie expires 2029-12-31).
                    if response.status_code == 403 and _try_solve_pow_challenge(session, response):
                        logger.info("PoW challenge solved; retrying %s", current_url)
                        try:
                            response = session.get(current_url, timeout=OAI_HTTP_TIMEOUT)
                        except requests.exceptions.Timeout as e:
                            raise RuntimeError(
                                f"OAI-PMH endpoint timed out after {OAI_HTTP_TIMEOUT}s (after {OAI_RETRY_TOTAL} retries): {current_url}"
                            ) from e
                        except requests.exceptions.ConnectionError as e:
                            raise RuntimeError(
                                f"OAI-PMH endpoint unreachable (after {OAI_RETRY_TOTAL} retries): {current_url}: {e}"
                            ) from e

                    if not response.ok:
                        content_type = response.headers.get("Content-Type", "?")
                        raise RuntimeError(
                            f"OAI-PMH endpoint returned HTTP {response.status_code} "
                            f"({content_type}) for {current_url}. "
                            f"This usually means the URL is outdated or the upstream is "
                            f"down. Body preview: {_short_body(response)}"
                        )

                if not _looks_like_oai_xml(response.content):
                    content_type = response.headers.get("Content-Type", "?")
                    raise RuntimeError(
                        f"OAI-PMH endpoint returned non-XML content "
                        f"(HTTP {response.status_code}, Content-Type: {content_type}) "
                        f"for {current_url}. Body preview: {_short_body(response)}"
                    )

                # Empty year range — move to the next chunk without failing.
                if _is_no_records_match(response.content):
                    logger.debug("No records in chunk %s", chunk_url)
                    break

                # First non-empty response for this year: mark it visited.
                if not year_had_records:
                    year_had_records = True
                    if chunk_year is not None:
                        visited_years.append(chunk_year)

                # Calculate remaining records budget for this page
                if max_records is not None:
                    records_so_far = (
                        stats.created + stats.updated + stats.skipped_same_source + stats.skipped_cross_source
                    )
                    remaining = max_records - records_so_far
                    if remaining <= 0:
                        # Budget consumed by the previous page; current_url points to
                        # unparsed pages, so this year is only partially covered.
                        logger.info("Reached max_records limit (%d), stopping pagination", max_records)
                        budget_exhausted = True
                        if chunk_year is not None:
                            partial_year = chunk_year
                        break
                    page_max = remaining
                else:
                    page_max = None

                parse_oai_xml_and_save_works(
                    response.content,
                    event,
                    max_records=page_max,
                    warning_collector=warning_collector,
                    update_existing=update_existing,
                    stats=stats,
                    session=session,
                )

                # Follow resumptionToken for next page within this year chunk
                current_url = _extract_resumption_url(response.content, base_oai_url)

        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)
        new_count = stats.created
        updated_count = stats.updated

        collection_label = source.collection.name if source.collection else source.name
        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "",
                "source_label": collection_label,
                "detail_header": "Harvesting job details:",
                "source_name": source.name,
                "source_url": source.url_field,
                "url_label": "Journal",
                "collection_label": collection_label,
                "records_added_label": "Number of added articles",
                "records_added": new_count,
                "records_updated_label": "Number of updated articles",
                "records_updated": updated_count,
                "spatial_label": "Number of articles with spatial metadata",
                "spatial_count": spatial_count,
                "temporal_label": "Number of articles with temporal metadata",
                "temporal_count": temporal_count,
                "event_started": event.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "event_completed": event.completed_at.strftime("%Y-%m-%d %H:%M:%S"),
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": None,
                "container_title_filters": None,
                "openalex_source_id": None,
                "records_seen": None,
                "records_processed": None,
            },
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error("Harvesting failed for source %s: %s", source.url_field, str(e))
        fail_harvest(event, e, warning_collector)
        collection_label = source.collection.name if source.collection else source.name
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "",
                "source_label": collection_label,
                "source_type_label": "OAI-PMH",
                "source_name": source.name,
                "source_url": source.url_field,
                "collection_label": collection_label,
                "resolved_prefix": None,
                "event_started": event.started_at.strftime("%Y-%m-%d %H:%M:%S"),
                "event_failed": event.completed_at.strftime("%Y-%m-%d %H:%M:%S"),
                "error": str(e),
                "warning_summary": warning_collector.get_summary() if warning_collector.has_issues() else "",
            },
        )
        send_harvest_email(user, subject, body)
    finally:
        logger.removeHandler(warning_collector)

    return {"visited_years": visited_years, "partial_year": partial_year}
