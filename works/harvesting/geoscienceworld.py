# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""GeoScienceWorld (GSW) harvester (issue #251).

Enumerates articles via Crossref (filter by DOI prefix) then extracts
geographic coordinates from each article's GSW landing page using
geoextent's built-in GeoScienceWorld content provider, which handles
the Cloudflare bypass via curl_cffi.

Temporal/epoch extraction is deferred pending geoextent#122.
Tracked in OPTIMAP#257.
"""

import json
import logging
import time
from datetime import datetime

import geoextent.lib.extent as geoextent_lib
import requests
from django.conf import settings
from django.contrib.gis.geos import GeometryCollection, GEOSGeometry
from django.utils import timezone

from works.models import Source

from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _find_existing_work,
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
from .crossref import (
    _authors_from_crossref,
    _build_crossref_filter,
    _split_crossref_page,
    _strip_jats,
)
from .sessions import (
    CROSSREF_API_URL,
    CROSSREF_HTTP_TIMEOUT,
    CROSSREF_PAGE_ROWS,
    _crossref_session,
)

logger = logging.getLogger(__name__)

GSW_THROTTLE_DEFAULT = 2.0


def _geom_from_geoextent_result(result):
    """Convert a geoextent from_remote() result dict to a GeometryCollection.

    Uses the GeoJSON features returned by geoextent rather than the bbox
    summary, which has a known coordinate-order bug for remote extractions.
    """
    if not result:
        return GeometryCollection(srid=4326)
    geoms = []
    for feature in result.get("features") or []:
        raw = feature.get("geometry")
        if not raw:
            continue
        try:
            geoms.append(GEOSGeometry(json.dumps(raw), srid=4326))
        except Exception as e:
            logger.debug("Could not parse geoextent feature geometry: %s", e)
    if geoms:
        return GeometryCollection(*geoms, srid=4326)
    return GeometryCollection(srid=4326)


def parse_gsw_response_and_save_works(
    source,
    event,
    prefix,
    max_records=None,
    warning_collector=None,
    update_existing=False,
    stats=None,
    throttle=None,
):
    """Enumerate articles from Crossref by DOI prefix, fetch coordinates from GSW.

    For each article:
    1. Pulls bibliographic metadata (title, abstract, authors, date) from Crossref.
    2. Calls geoextent.from_remote(doi) to extract geographic coordinates from
       the article's GSW landing page (Cloudflare bypass handled by geoextent).
    3. Persists the Work record via _save_or_update_work().

    Returns (saved_count, seen_count).
    """
    if stats is None:
        stats = HarvestStats()
    if throttle is None:
        throttle = getattr(settings, "GEOSCIENCEWORLD_THROTTLE_SECONDS", GSW_THROTTLE_DEFAULT)

    admin_user = get_or_create_admin_command_user()
    session = _crossref_session()
    filter_value = _build_crossref_filter(prefix)
    cursor = "*"
    saved = 0
    seen = 0
    log_interval = 20 if (max_records or 0) <= 100 else 50

    while True:
        params = {
            "filter": filter_value,
            "rows": str(CROSSREF_PAGE_ROWS),
            "cursor": cursor,
            "select": (
                "DOI,title,abstract,published-print,published-online,published,issued,URL,author,volume,issue,page"
            ),
        }
        try:
            resp = session.get(CROSSREF_API_URL, params=params, timeout=CROSSREF_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"Crossref request failed for prefix {prefix}: {e}") from e
        if not resp.ok:
            raise RuntimeError(f"Crossref returned HTTP {resp.status_code} for prefix {prefix!r}: {resp.text[:300]}")

        data = resp.json().get("message", {})
        items = data.get("items", [])
        if not items:
            break

        for item in items:
            seen += 1
            if seen % log_interval == 0:
                suffix = f"/{max_records}" if max_records else ""
                logger.info("Processed %d%s records", seen, suffix)
                if event is not None:
                    event.append_log(f"Processed {seen}{suffix} records")
            doi = item.get("DOI")
            if not doi:
                continue

            url = item.get("URL") or f"https://doi.org/{doi}"
            title_list = item.get("title") or []
            title = title_list[0] if title_list else doi
            log_work_retrieved(event, title)

            published = (
                item.get("published-print")
                or item.get("published-online")
                or item.get("published")
                or item.get("issued")
                or {}
            )
            pub_date = None
            parts = (published.get("date-parts") or [[]])[0]
            if parts:
                try:
                    year = int(parts[0])
                    month = int(parts[1]) if len(parts) > 1 else 1
                    day = int(parts[2]) if len(parts) > 2 else 1
                    pub_date = datetime(year, month, day).date()
                except (TypeError, ValueError):
                    pass

            abstract = _strip_jats(item.get("abstract"))
            authors = _authors_from_crossref(item.get("author"))
            volume = item.get("volume") or None
            issue = item.get("issue") or None
            first_page, last_page = _split_crossref_page(item.get("page"))

            # Skip the geoextent call (and the throttle sleep) when the work is
            # already in the database and we're not updating existing records.
            if not update_existing and _find_existing_work(doi=doi, url=url):
                logger.debug("GSW: DOI %s already harvested, skipping", doi)
                stats.record("skipped_existing")
                if event is not None:
                    event.log_work(f"Skipped (duplicate): {title[:80]}")
                if max_records and seen >= max_records:
                    return saved, seen
                continue

            # Extract geographic coordinates from GSW landing page via geoextent.
            # geoextent's GeoScienceWorld provider uses curl_cffi with Chrome TLS
            # impersonation to bypass Cloudflare and parse WKT <coordinates> elements.
            geometry = GeometryCollection(srid=4326)
            geo_source = None
            try:
                result = geoextent_lib.from_remote(doi, bbox=True)
                if result:
                    geometry = _geom_from_geoextent_result(result)
                    if not geometry.empty:
                        geo_source = "geoextent_gsw"
                        logger.info("GSW coords extracted for DOI %s (%d geom(s))", doi, len(geometry))
                    else:
                        logger.debug("GSW: no coordinates found for DOI %s", doi)
            except Exception as ge:
                logger.warning("geoextent failed for GSW DOI %s: %s", doi, ge)

            if throttle > 0:
                time.sleep(throttle)

            metadata_sources = {"crossref": "doi"}
            if authors:
                metadata_sources["authors"] = "crossref"
            if volume or issue or first_page or last_page:
                metadata_sources["biblio"] = "crossref"
            if geo_source:
                metadata_sources["geometry"] = geo_source

            work_kwargs = {
                "title": title,
                "abstract": abstract,
                "doi": doi,
                "url": url,
                "publicationDate": pub_date,
                "source": source,
                "job": event,
                "authors": authors or None,
                "volume": volume,
                "issue": issue,
                "first_page": first_page,
                "last_page": last_page,
                "status": "h",
                "geometry": geometry,
                "timeperiod_startdate": [],
                "timeperiod_enddate": [],
                "type": source.default_work_type or "article",
                "provenance": {
                    "harvest": {
                        "harvester": "harvest_geoscienceworld",
                        "source_url": "https://api.crossref.org/works",
                        "source_type": source.source_type,
                        "source_name": source.name,
                        "harvested_at": timezone.now().isoformat(),
                        "harvesting_event_id": event.id,
                        "doi_prefix": prefix,
                        "doi": doi,
                    },
                    "metadata_sources": metadata_sources,
                },
                "created_by": admin_user,
            }

            try:
                work, action = _save_or_update_work(
                    work_kwargs,
                    source,
                    event,
                    update_existing=update_existing,
                )
                stats.record(action)
                if action in ("created", "updated") and source.collection_id:
                    work.collections.add(source.collection_id)
                if action in ("created", "updated"):
                    saved += 1
            except Exception as save_err:
                logger.warning("Failed to save GSW work DOI=%s: %s", doi, save_err)
                log_work_failed(event, title, save_err)

            if max_records and seen >= max_records:
                return saved, seen

        next_cursor = data.get("next-cursor")
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    return saved, seen


def harvest_geoscienceworld(
    source_id, user=None, max_records=None, update_existing=False, event_id=None, trigger_source=None
):
    """Harvest publications from GeoScienceWorld (issue #251).

    Enumerates articles via Crossref (source.doi_prefix) then extracts
    geographic coordinates from each article's GSW landing page via geoextent.
    """
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    ensure_collection_for_source(source)
    event = start_harvesting_event(source, event_id, trigger_source, max_records)
    if event is None:
        return
    event.append_log(f"Starting GeoScienceWorld harvest for {source.name} (prefix={source.doi_prefix})")

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    prefix = source.doi_prefix
    if not prefix:
        fail_harvest(
            event,
            ValueError(f"Source {source.name!r} has no doi_prefix configured"),
            warning_collector,
        )
        logger.removeHandler(warning_collector)
        return

    stats = HarvestStats()
    try:
        logger.info(
            "Starting GSW harvest: source=%s prefix=%s max_records=%s",
            source.name,
            prefix,
            max_records,
        )
        saved, seen = parse_gsw_response_and_save_works(
            source,
            event,
            prefix,
            max_records=max_records,
            warning_collector=warning_collector,
            update_existing=update_existing,
            stats=stats,
        )

        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)

        collection_label = source.collection.name if source.collection else source.name
        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "GeoScienceWorld ",
                "source_label": collection_label,
                "detail_header": "GeoScienceWorld harvest details:",
                "source_name": source.name,
                "source_url": source.url_field,
                "url_label": "URL",
                "collection_label": None,
                "records_added_label": "New works saved",
                "records_added": stats.created,
                "records_updated_label": "Updated works",
                "records_updated": stats.updated,
                "spatial_label": "With spatial extent (GeoRef coordinates)",
                "spatial_count": spatial_count,
                "temporal_label": "With temporal extent",
                "temporal_count": temporal_count,
                "event_started": f"{event.started_at:%Y-%m-%d %H:%M:%S}",
                "event_completed": f"{event.completed_at:%Y-%m-%d %H:%M:%S}",
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": prefix,
                "container_title_filters": None,
                "openalex_source_id": None,
                "records_seen": seen,
                "records_processed": seen,
            },
            event=event,
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error("GSW harvesting failed for source %s: %s", source.name, str(e))
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "GeoScienceWorld ",
                "source_label": source.name,
                "source_type_label": "GeoScienceWorld",
                "source_name": source.name,
                "source_url": source.url_field,
                "collection_label": None,
                "resolved_prefix": prefix,
                "event_started": None,
                "event_failed": None,
                "error_message": str(e),
                "warning_summary": warning_collector.get_summary(),
            },
        )
        send_harvest_email(user, subject, body)
        raise
    finally:
        logger.removeHandler(warning_collector)
