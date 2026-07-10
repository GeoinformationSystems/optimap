# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""OpenAlex-as-source harvester.

Treats OpenAlex itself as the harvest origin (rather than as an enrichment
layer over an OAI-PMH / Crossref payload). One ``Source`` row corresponds
to one OpenAlex ``sources`` record (e.g. ``S4210203054`` for AGILE
GIScience Series), and the harvester paginates
``/works?filter=primary_location.source.id:<S-id>``.

OpenAlex does not expose spatial / temporal coverage, and a smoke run
against AGILE-GISS confirmed the publisher landing pages don't carry
``DC.SpatialCoverage`` / ``DC.box`` / schema.org ``spatialCoverage`` /
``geo+json`` link / ``DC.temporal`` either. The harvester therefore does
not fetch landing pages — it stays fast and pulls everything it can from
the OpenAlex payload directly. The geometry / temporal extraction
pipeline still lives in ``works/harvesting/metadata_html.py`` for the
other harvesters.
"""

import logging
import re
from datetime import datetime

import requests
from django.contrib.gis.geos import GeometryCollection
from django.utils import timezone

from works.models import Source
from works.openalex_matcher import author_records_from_authorships

from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _save_or_update_work,
    complete_harvest,
    ensure_collection_for_source,
    fail_harvest,
    get_or_create_admin_command_user,
    log_work_failed,
    log_work_retrieved,
    render_harvest_email,
    resolve_user,
    send_harvest_email,
    start_harvesting_event,
)
from .openalex import link_openalex_authors
from .openalex_locations import build_locations
from .sessions import (
    OPENALEX_API_URL,
    OPENALEX_HTTP_TIMEOUT,
    OPENALEX_PAGE_SIZE,
    _openalex_session,
)

logger = logging.getLogger(__name__)

_OPENALEX_SOURCE_ID_RE = re.compile(r"S\d+", re.IGNORECASE)


def _resolve_openalex_source_id(source: Source) -> str | None:
    """Return the OpenAlex source identifier (e.g. ``S4210203054``) for a Source.

    Tries ``Source.openalex_id`` first (canonical), then ``Source.url_field``
    as a fallback. Neither is required at the model level, so the harvester
    surfaces a clear error if the operator hasn't set one.
    """
    for candidate in (source.openalex_id, source.url_field):
        if not candidate:
            continue
        match = _OPENALEX_SOURCE_ID_RE.search(candidate)
        if match:
            return match.group(0).upper().replace("S", "S", 1)
    return None


def _reconstruct_abstract(inverted_index: dict | None) -> str | None:
    """Reconstruct plain text from OpenAlex's ``abstract_inverted_index``.

    OpenAlex stores abstracts as ``{word: [positions]}`` to dodge a copyright
    issue with verbatim storage. Walking the position list back into a string
    is straightforward.
    """
    if not inverted_index:
        return None
    positions = []
    for word, idxs in inverted_index.items():
        for idx in idxs or []:
            positions.append((idx, word))
    if not positions:
        return None
    positions.sort(key=lambda p: p[0])
    return " ".join(word for _, word in positions) or None


def _strip_doi_prefix(doi_url: str | None) -> str | None:
    """``https://doi.org/10.5194/agile-giss-1-1-2020`` → ``10.5194/agile-giss-1-1-2020``."""
    if not doi_url:
        return None
    doi = doi_url.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/"):
        if doi.lower().startswith(prefix):
            return doi[len(prefix) :]
    return doi


def _parse_publication_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _authors_from_authorships(authorships) -> list[str]:
    if not authorships:
        return []
    out = []
    for a in authorships:
        author = (a or {}).get("author") or {}
        name = author.get("display_name") or a.get("raw_author_name")
        if name:
            out.append(name.strip())
    return out


def _keywords_from_payload(payload) -> list[str]:
    raw = payload.get("keywords") or []
    out = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("display_name") or item.get("keyword")
            if name:
                out.append(name.strip())
        elif isinstance(item, str):
            out.append(item.strip())
    return out


def _topics_from_payload(payload) -> list[str]:
    raw = payload.get("topics") or []
    out = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("display_name")
            if name:
                out.append(name.strip())
    return out


def _landing_page_for(payload) -> str | None:
    primary = payload.get("primary_location") or {}
    landing = primary.get("landing_page_url")
    if landing:
        return landing
    doi = payload.get("doi")
    if doi:
        return doi  # OpenAlex returns the full https://doi.org/... URL
    for loc in payload.get("locations") or []:
        if loc and loc.get("landing_page_url"):
            return loc["landing_page_url"]
    return None


def _openalex_item_to_work_kwargs(item, source, event):
    """Convert one OpenAlex `works` payload item to ``Work.objects.create`` kwargs.

    Returns ``None`` when the item lacks both a DOI and a landing page URL —
    OPTIMAP needs at least one of those to deduplicate against re-runs.
    """
    doi = _strip_doi_prefix(item.get("doi"))
    title = item.get("title") or item.get("display_name") or doi
    landing_url = _landing_page_for(item) or (f"https://doi.org/{doi}" if doi else None)
    if not doi and not landing_url:
        return None
    log_work_retrieved(event, title)

    pub_date = _parse_publication_date(item.get("publication_date"))
    abstract = _reconstruct_abstract(item.get("abstract_inverted_index"))
    authors = _authors_from_authorships(item.get("authorships"))
    keywords = _keywords_from_payload(item)
    topics = _topics_from_payload(item)

    biblio = item.get("biblio") or {}
    volume = biblio.get("volume") or None
    issue = biblio.get("issue") or None
    first_page = biblio.get("first_page") or None
    last_page = biblio.get("last_page") or None

    open_access = item.get("open_access") or {}
    fulltext_origin = item.get("fulltext_origin") or None

    metadata_provenance = {"openalex": "primary"}
    if authors:
        metadata_provenance["authors"] = "openalex"
    if keywords:
        metadata_provenance["keywords"] = "openalex"
    if topics:
        metadata_provenance["topics"] = "openalex"

    work_type = item.get("type") or (source.default_work_type if source else "article")

    return {
        "title": title,
        "abstract": abstract,
        "doi": doi,
        "url": landing_url,
        "publicationDate": pub_date,
        "source": source,
        "job": event,
        # OpenAlex carries no spatial / temporal coverage — leave geometry as
        # an empty collection (the model default) and timeperiod arrays empty.
        "geometry": GeometryCollection(),
        "timeperiod_startdate": [],
        "timeperiod_enddate": [],
        "authors": authors or None,
        "keywords": keywords or None,
        "topics": topics or None,
        "type": work_type,
        "volume": volume,
        "issue": issue,
        "first_page": first_page,
        "last_page": last_page,
        "openalex_id": item.get("id"),
        "openalex_ids": item.get("ids") or {},
        "openalex_fulltext_origin": fulltext_origin,
        "openalex_is_retracted": bool(item.get("is_retracted", False)),
        "openalex_open_access_status": open_access.get("oa_status"),
        "locations": build_locations(item),
        "author_records": author_records_from_authorships(item.get("authorships")),
        "provenance": {
            "harvest": {
                "harvester": "harvest_openalex_source",
                "source_url": OPENALEX_API_URL,
                "source_type": source.source_type if source else "openalex",
                "source_name": source.name if source else None,
                "openalex_source_id": _resolve_openalex_source_id(source) if source else None,
                "harvested_at": timezone.now().isoformat(),
                "harvesting_event_id": event.id if event else None,
                "doi": doi,
            },
            "metadata_sources": metadata_provenance,
        },
        "status": "h",
    }


def parse_openalex_response_and_save_works(
    source,
    event,
    openalex_source_id,
    max_records=None,
    sort=None,
    update_existing=False,
    stats=None,
):
    """Page through ``/works?filter=primary_location.source.id:<S-id>`` and persist.

    ``sort`` is passed through verbatim (e.g. ``publication_date:desc``);
    OpenAlex defaults to relevance for filtered queries.
    """
    session = _openalex_session()
    cursor = "*"
    saved = 0
    seen = 0
    if stats is None:
        stats = HarvestStats()
    log_interval = 20 if (max_records or 0) <= 100 else 50

    filter_value = f"primary_location.source.id:{openalex_source_id}"
    select_value = (
        "id,doi,title,display_name,publication_date,type,authorships,"
        "abstract_inverted_index,keywords,topics,biblio,primary_location,"
        "locations,ids,fulltext_origin,open_access,is_retracted"
    )

    while True:
        params = {
            "filter": filter_value,
            "per-page": str(OPENALEX_PAGE_SIZE),
            "cursor": cursor,
            "select": select_value,
        }
        if sort:
            params["sort"] = sort
        try:
            resp = session.get(OPENALEX_API_URL, params=params, timeout=OPENALEX_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"OpenAlex request failed: {e}") from e
        if not resp.ok:
            raise RuntimeError(
                f"OpenAlex returned HTTP {resp.status_code} for filter {filter_value!r}: {resp.text[:300]}"
            )

        data = resp.json()
        results = data.get("results", [])
        if not results:
            break

        for item in results:
            seen += 1
            if seen % log_interval == 0:
                suffix = f"/{max_records}" if max_records else ""
                logger.info("Processed %d%s records", seen, suffix)
                if event is not None:
                    event.append_log(f"Processed {seen}{suffix} records")
            kwargs = _openalex_item_to_work_kwargs(item, source, event)
            if not kwargs:
                continue
            author_records = kwargs.pop("author_records", [])
            try:
                work, action = _save_or_update_work(
                    kwargs,
                    source,
                    event,
                    update_existing=update_existing,
                )
                stats.record(action)
                if action in ("created", "updated"):
                    link_openalex_authors(work, {"author_records": author_records})
                    if source and source.collection_id:
                        work.collections.add(source.collection_id)
                if action == "created":
                    saved += 1
            except Exception as e:
                logger.warning(
                    "Failed to persist OpenAlex work %s: %s",
                    kwargs.get("doi") or kwargs.get("url"),
                    e,
                )
                ident = kwargs.get("title") or kwargs.get("doi") or kwargs.get("url")
                log_work_failed(event, ident, e)
            if max_records and seen >= max_records:
                return saved, seen

        next_cursor = (data.get("meta") or {}).get("next_cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return saved, seen


def harvest_openalex_source(
    source_id,
    user=None,
    max_records=None,
    sort=None,
    update_existing=False,
    event_id=None,
    trigger_source=None,
):
    """Harvest publications from a configured OpenAlex source.

    The Source row must have its OpenAlex source identifier set on
    ``openalex_id`` (preferred) or ``url_field`` (fallback) — anything
    containing the ``S<digits>`` token is accepted.
    """
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    # Issue #192 generalised: every harvester auto-creates a Collection on first
    # run if the source has none, mirroring the OAI-PMH path. Idempotent — re-runs
    # are no-ops once the source has a collection assigned.
    ensure_collection_for_source(source)
    event = start_harvesting_event(source, event_id, trigger_source, max_records)
    if event is None:
        return
    event.append_log(f"Starting OpenAlex harvest for {source.name}")

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    try:
        openalex_source_id = _resolve_openalex_source_id(source)
        if not openalex_source_id:
            raise RuntimeError(
                f"Source id={source.id} has no OpenAlex source identifier "
                f"on openalex_id (preferred) or url_field (fallback)."
            )

        # Touch admin user up-front so the row exists when the harvester needs it.
        get_or_create_admin_command_user()

        logger.info(
            "Starting OpenAlex harvest: openalex_source=%s sort=%s max_records=%s",
            openalex_source_id,
            sort,
            max_records,
        )

        stats = HarvestStats()
        saved, seen = parse_openalex_response_and_save_works(
            source,
            event,
            openalex_source_id=openalex_source_id,
            max_records=max_records,
            sort=sort,
            update_existing=update_existing,
            stats=stats,
        )

        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)

        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "OpenAlex ",
                "source_label": source.name,
                "detail_header": "OpenAlex harvest details:",
                "source_name": source.name,
                "source_url": None,
                "url_label": None,
                "collection_label": None,
                "records_added_label": "New works saved",
                "records_added": stats.created,
                "records_updated_label": "Updated works",
                "records_updated": stats.updated,
                "spatial_label": "Articles with spatial metadata",
                "spatial_count": spatial_count,
                "temporal_label": "Articles with temporal metadata",
                "temporal_count": temporal_count,
                "event_started": f"{event.started_at:%Y-%m-%d %H:%M:%S}",
                "event_completed": f"{event.completed_at:%Y-%m-%d %H:%M:%S}",
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": None,
                "container_title_filters": None,
                "openalex_source_id": openalex_source_id,
                "records_seen": seen,
                "records_processed": None,
            },
            event=event,
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error(
            "OpenAlex harvesting failed for source %s: %s",
            source.url_field,
            str(e),
        )
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "OpenAlex ",
                "source_label": source.name,
                "source_type_label": "OpenAlex",
                "source_name": source.name,
                "source_url": None,
                "collection_label": None,
                "resolved_prefix": None,
                "event_started": None,
                "event_failed": None,
                "error": str(e),
                "warning_summary": "",
            },
        )
        send_harvest_email(user, subject, body, fail_silently=True)
        raise
    finally:
        logger.removeHandler(warning_collector)
