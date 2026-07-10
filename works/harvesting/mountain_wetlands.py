# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""Mountain Wetlands Repository (MaRESS) — bespoke harvester (issue #192).

The MaRESS API at /api/v1/items/ is a Zotero-shaped item dump: every record
carries a title, a free-text date (often year-only), an abstract, a list of
`creators` (lastName/firstName), and a list of `study_sites` with point
coordinates. The ``DOI`` field is populated for most records and is treated
as authoritative; records without one fall back to title + first-author
matching against OpenAlex.
"""

import logging
from datetime import date

import requests
from django.contrib.gis.geos import GeometryCollection, GEOSGeometry, Point
from django.utils import timezone

from works.models import Source, Work

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
    log_work_failed,
    log_work_retrieved,
    log_work_skip,
    render_harvest_email,
    resolve_user,
    send_harvest_email,
    start_harvesting_event,
)
from .openalex import build_openalex_fields
from .sessions import (
    MWR_HTTP_TIMEOUT,
    MWR_PAGE_SIZE,
    _mwr_session,
    _short_body,
)

logger = logging.getLogger(__name__)


def _mwr_item_url(source_url, item_id):
    """Stable per-item URL we use as ``Work.url`` for idempotency.

    Uses the API path so every harvest of the same record collapses onto the
    same row (Work.url is unique).
    """
    base = source_url.rstrip("/").split("?")[0]
    return f"{base.rstrip('/')}/{item_id}"


def _mwr_geometry_from_study_sites(study_sites):
    """Build a ``GeometryCollection`` of points from the API's ``study_sites``."""
    points = []
    for site in study_sites or []:
        loc = site.get("location") or {}
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat is None or lon is None:
            continue
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            continue
        if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
            continue
        points.append(Point(lon_f, lat_f, srid=4326))
    if not points:
        return GeometryCollection(srid=4326)
    return GeometryCollection(*points, srid=4326)


def _mwr_first_author_surname(creators):
    """Return the first non-trivial creator surname, or None."""
    for c in creators or []:
        last = (c.get("lastName") or "").strip()
        if not last:
            continue
        if last.lower() in ("et al.", "et al", "others"):
            continue
        return last
    return None


def _mwr_authors_list(creators):
    """Build a ``[<First Last>, ...]`` author list from the API record."""
    authors = []
    for c in creators or []:
        last = (c.get("lastName") or "").strip()
        first = (c.get("firstName") or "").strip()
        if last and first:
            authors.append(f"{first} {last}")
        elif last:
            authors.append(last)
    return authors


def _mwr_clean_doi(raw):
    """Normalise the API's free-text ``DOI`` into a bare ``10.x/y`` string."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    lower = s.lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if lower.startswith(prefix):
            s = s[len(prefix) :]
            break
    s = s.lstrip("/").strip()
    if not s.lower().startswith("10."):
        return None
    return s


def _mwr_publication_year(date_str):
    """Parse a year from the API's free-text ``date`` field. Many records have
    only ``"1993"`` etc., so we don't try to recover month/day. Returns a
    ``datetime.date`` (Jan 1 of the year) or ``None``."""
    if not date_str:
        return None
    s = str(date_str).strip()
    if not s:
        return None
    try:
        year = int(s[:4])
    except ValueError:
        return None
    if not (1500 <= year <= 2100):
        return None
    return date(year, 1, 1)


def parse_mountain_wetlands_response_and_save_works(
    payload,
    source,
    event,
    max_records=None,
    processed_so_far=0,
    warning_collector=None,
    update_existing=False,
    stats=None,
):
    """Save one page of MaRESS items. Returns ``(saved, processed)`` for this page.

    When ``stats`` (a HarvestStats) is provided, the parser also accumulates
    .created / .updated / .skipped_* on it across multiple pages — the
    harvester then reads .updated to populate ``HarvestingEvent.records_updated``.
    """
    items = payload.get("data") or []
    saved = 0
    processed = 0
    admin_user = get_or_create_admin_command_user()
    if stats is None:
        stats = HarvestStats()
    log_interval = 20 if (max_records or 0) <= 100 else 50

    for item in items:
        if max_records and (processed_so_far + processed) >= max_records:
            break
        processed += 1
        total_so_far = processed_so_far + processed
        if total_so_far % log_interval == 0:
            suffix = f"/{max_records}" if max_records else ""
            logger.info("Processed %d%s records", total_so_far, suffix)
            if event is not None:
                event.append_log(
                    f"Processed {total_so_far}{suffix} records ({stats.created} created, {stats.updated} updated)"
                )

        item_id = item.get("id")
        title = (item.get("title") or "").strip()
        if not title:
            logger.info("Skipping MaRESS item with no title (id=%s)", item_id)
            continue
        if not item_id:
            logger.info("Skipping MaRESS item with no id: %s", title[:60])
            continue

        item_url = _mwr_item_url(source.url_field, item_id)

        creators = item.get("creators") or []
        api_authors = _mwr_authors_list(creators)
        first_author_surname = _mwr_first_author_surname(creators)
        pub_date = _mwr_publication_year(item.get("date"))
        geom_obj = _mwr_geometry_from_study_sites(item.get("study_sites"))
        abstract = (item.get("abstractNote") or None) or None
        api_doi = _mwr_clean_doi(item.get("DOI"))

        # Early dedup: skip OpenAlex for records already in the database.
        _early_existing = _find_existing_work(doi=api_doi, url=item_url)
        if _early_existing is not None:
            if api_doi and not _early_existing.doi:
                _backfill_empty_doi(_early_existing, api_doi, event)
            _cross_source = _early_existing.source_id != source.id
            if _cross_source or not update_existing:
                action = "skipped_cross_source" if _cross_source else "skipped_same_source"
                stats.record(action)
                log_work_skip(event, title, _cross_source)
                continue

        log_work_retrieved(event, title)

        existing_metadata = {}
        if api_authors:
            existing_metadata["authors"] = api_authors

        # Skip OpenAlex when the API already supplies both a DOI and authors —
        # no extra metadata to recover, and the call is wasted rate-limit budget.
        if api_doi and api_authors:
            openalex_fields, metadata_provenance = {}, {}
            match_status = "skipped"
        else:
            openalex_fields, metadata_provenance = build_openalex_fields(
                title=title,
                doi=api_doi,
                author=first_author_surname,
                existing_metadata=existing_metadata,
            )
            if openalex_fields.get("openalex_id"):
                match_status = "verified"
            elif openalex_fields.get("openalex_match_info"):
                match_status = "candidate"
            else:
                match_status = "none"
        event.log_work(f"Enriched: {title[:80] if title else 'No title'} — OpenAlex {match_status}")

        doi_value = api_doi
        if not doi_value:
            ids_blob = openalex_fields.get("openalex_ids") or {}
            if match_status == "verified" and ids_blob.get("doi"):
                doi_value = _mwr_clean_doi(ids_blob["doi"])

        if not metadata_provenance.get("authors") and api_authors:
            metadata_provenance["authors"] = "original_source"
        metadata_provenance["geometry"] = "study_sites" if not geom_obj.empty else None
        metadata_provenance["date"] = "original_source (year-only)" if pub_date else None
        if api_doi:
            metadata_provenance["doi"] = "original_source"
        elif doi_value:
            metadata_provenance["doi"] = "openalex"

        provenance = {
            "harvest": {
                "harvester": "harvest_mountain_wetlands",
                "source_url": source.url_field,
                "source_type": source.source_type,
                "source_name": source.name,
                "harvested_at": timezone.now().isoformat(),
                "harvesting_event_id": event.id,
                "external_id": item_id,
                "original_record": item,
            },
            "metadata_sources": {k: v for k, v in metadata_provenance.items() if v is not None},
            "openalex_match": {
                "status": match_status,
                "matched_id": openalex_fields.get("openalex_id"),
                "first_author_surname_used": first_author_surname,
            },
        }

        if "type" not in openalex_fields:
            openalex_fields["type"] = source.default_work_type or "article"

        try:
            work_kwargs = dict(
                title=title,
                abstract=abstract,
                publicationDate=pub_date,
                url=item_url,
                doi=doi_value,
                source=source,
                status="h",
                geometry=geom_obj,
                timeperiod_startdate=None,
                timeperiod_enddate=None,
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
            if action in ("created", "updated") and source.collection_id:
                work.collections.add(source.collection_id)
            if action == "created":
                saved += 1
                logger.info(
                    "Saved MaRESS work id=%s (%s) status=%s",
                    work.id,
                    item_id,
                    match_status,
                )
            elif action == "updated":
                logger.info(
                    "Updated MaRESS work id=%s (%s) status=%s",
                    work.id,
                    item_id,
                    match_status,
                )
        except Exception as save_err:
            logger.warning("Failed to save MaRESS item %s: %s", item_id, save_err)
            log_work_failed(event, title or item_id, save_err)
            continue

    return saved, processed


def harvest_mountain_wetlands(
    source_id, user=None, max_records=None, update_existing=False, event_id=None, trigger_source=None
):
    """Bespoke harvester for the Mountain Wetlands Repository (MaRESS API).

    Manual-only — the issue #192 explicitly forbids auto-scheduling. Run via
    ``python manage.py harvest_sources --source mountain-wetlands`` or via
    the Django admin "Trigger harvesting" action.
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
    event.append_log(f"Starting Mountain Wetlands (MaRESS) harvest from {source.url_field}")

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    total_saved = 0
    total_processed = 0
    stats = HarvestStats()
    try:
        session = _mwr_session()
        skip = 0
        base_url = source.url_field.split("?")[0]
        while True:
            params = {"limit": MWR_PAGE_SIZE, "skip": skip, "scope": "all"}
            logger.info("Fetching MaRESS items: skip=%d limit=%d", skip, MWR_PAGE_SIZE)
            try:
                response = session.get(base_url, params=params, timeout=MWR_HTTP_TIMEOUT)
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"MaRESS request failed for {base_url}: {e}") from e
            if not response.ok:
                raise RuntimeError(
                    f"MaRESS endpoint returned HTTP {response.status_code} for {base_url} "
                    f"(skip={skip}). Body preview: {_short_body(response)}"
                )
            try:
                payload = response.json()
            except ValueError as e:
                raise RuntimeError(f"MaRESS response was not valid JSON: {e}") from e

            saved, processed = parse_mountain_wetlands_response_and_save_works(
                payload,
                source,
                event,
                max_records=max_records,
                processed_so_far=total_processed,
                warning_collector=warning_collector,
                update_existing=update_existing,
                stats=stats,
            )
            total_saved += saved
            total_processed += processed
            event.append_log(f"skip={skip}: {total_saved} saved so far ({total_processed} processed).")

            count = payload.get("count") or 0
            page_data = payload.get("data") or []
            if not page_data:
                break
            if max_records and total_processed >= max_records:
                break
            skip += MWR_PAGE_SIZE
            if skip >= count:
                break

        # MWR-specific spatial count: every work gets a geometry attribute, but it
        # may be an empty GeometryCollection — those don't count as having spatial.
        spatial_count = (
            Work.objects.filter(job=event)
            .exclude(geometry__isnull=True)
            .exclude(geometry__exact=GEOSGeometry("GEOMETRYCOLLECTION EMPTY"))
            .count()
        )
        spatial_count, temporal_count = complete_harvest(
            event,
            stats,
            warning_collector,
            spatial_count=spatial_count,
        )

        collection_label = source.collection.name if source.collection else source.name
        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "",
                "source_label": collection_label,
                "detail_header": "MaRESS harvest details:",
                "source_name": source.name,
                "source_url": source.url_field,
                "url_label": "URL",
                "collection_label": None,
                "records_added_label": "New works saved",
                "records_added": stats.created,
                "records_updated_label": "Updated works",
                "records_updated": stats.updated,
                "spatial_label": "With spatial extent",
                "spatial_count": spatial_count,
                "temporal_label": "With temporal extent",
                "temporal_count": temporal_count,
                "event_started": f"{event.started_at:%Y-%m-%d %H:%M:%S}",
                "event_completed": f"{event.completed_at:%Y-%m-%d %H:%M:%S}",
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": None,
                "container_title_filters": None,
                "openalex_source_id": None,
                "records_seen": None,
                "records_processed": total_processed,
            },
            event=event,
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error(
            "MaRESS harvesting failed for source %s: %s",
            source.url_field,
            str(e),
        )
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "",
                "source_label": source.name,
                "source_type_label": "MaRESS",
                "source_name": source.name,
                "source_url": source.url_field,
                "collection_label": None,
                "resolved_prefix": None,
                "event_started": None,
                "event_failed": None,
                "error": str(e),
                "warning_summary": warning_collector.get_summary(),
            },
        )
        send_harvest_email(user, subject, body, fail_silently=True)
        raise
    finally:
        logger.removeHandler(warning_collector)

    return total_saved, total_processed
