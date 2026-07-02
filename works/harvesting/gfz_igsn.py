# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""GFZ IGSN physical-sample harvester via landing-page scraping (issue #187).

GFZ Data Services registers ~38 k IGSN samples with DataCite, but — unlike SESAR —
their DataCite records carry **no** ``geoLocations``. The coordinates live only in
the server-rendered HTML landing page
(``dataservices.gfz-potsdam.de/igsn/esg/index.php?igsn=<IGSN>``), in a
``<table class="location">`` (Latitude / Longitude / Coordinate System / Elevation).
There is no schema.org/JSON-LD, no OAI-PMH, and the DataCite XML is empty too.

This harvester therefore enumerates GFZ IGSNs from DataCite (for title, dates and
related identifiers) and scrapes each landing page for the point. ~65 % of samples
have real coordinates; the rest show "N/A" and are skipped. It is a throttled bulk
job. Relation extraction reuses the DataCite path (mostly empty for GFZ).
"""

import logging
import time

import requests
from bs4 import BeautifulSoup
from django.contrib.gis.geos import GeometryCollection, Point
from django.utils import timezone

from works.models import Source

from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _save_or_update_work,
    complete_harvest,
    ensure_collection_for_source,
    fail_harvest,
    render_harvest_email,
    resolve_user,
    send_harvest_email,
    start_harvesting_event,
)
from .datacite import (
    _count_related,
    _create_relations_from_datacite,
    _temporal_from_dates,
    build_sample_metadata,
    iter_datacite_records,
)
from .sessions import GFZ_HTTP_TIMEOUT, GFZ_LANDING_URL, _gfz_session

logger = logging.getLogger(__name__)

GFZ_ENUM_QUERY = 'types.resourceTypeGeneral:PhysicalObject AND publisher:"GFZ Data Services"'
DEFAULT_THROTTLE_SECONDS = 0.5

# How often (records seen) to emit an INFO progress line. Smaller than the
# DataCite harvester's interval because each GFZ record is a throttled HTTP fetch.
_PROGRESS_EVERY = 50


def _to_float(value):
    """Parse a coordinate cell to float; None for empty / 'N/A' / non-numeric."""
    if not value:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def _parse_gfz_location(html):
    """Return a Point (SRID 4326) from a GFZ landing page, or None if no coordinates.

    Reads the ``<table class="location">`` rows (Latitude / Longitude / …). Rows
    with "N/A" or non-numeric values yield None.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="location")
    if table is None:
        return None
    values = {}
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 2:
            label = cells[0].get_text(strip=True).rstrip(":").strip().lower()
            values[label] = cells[1].get_text(strip=True)
    lat = _to_float(values.get("latitude"))
    lon = _to_float(values.get("longitude"))
    if lat is None or lon is None:
        return None
    return Point(lon, lat, srid=4326)


def _scrape_gfz_geometry(session, igsn):
    """Fetch a GFZ landing page and return a GeometryCollection([Point]) or None."""
    try:
        resp = session.get(GFZ_LANDING_URL, params={"igsn": igsn}, timeout=GFZ_HTTP_TIMEOUT)
    except requests.exceptions.RequestException as e:
        logger.warning("GFZ landing-page fetch failed for %s: %s", igsn, e)
        return None
    if not resp.ok:
        logger.warning("GFZ landing page HTTP %s for %s", resp.status_code, igsn)
        return None
    point = _parse_gfz_location(resp.text)
    return GeometryCollection([point], srid=4326) if point is not None else None


def _gfz_item_to_work_kwargs(item, geometry, source, event):
    """Build Work kwargs from a DataCite record + a scraped geometry."""
    attrs = item.get("attributes") or {}
    doi = attrs.get("doi")
    titles = attrs.get("titles") or []
    title = (titles[0].get("title") if titles and isinstance(titles[0], dict) else None) or doi
    landing_url = attrs.get("url") or f"https://doi.org/{doi}"
    authors = [c.get("name") for c in (attrs.get("creators") or []) if isinstance(c, dict) and c.get("name")]
    starts, ends = _temporal_from_dates(attrs.get("dates"))
    related_identifiers = attrs.get("relatedIdentifiers") or []
    sample_meta = build_sample_metadata(
        attrs, related_count=_count_related(related_identifiers), geometry_source="gfz.landing_page"
    )

    return {
        "title": title,
        "doi": doi,
        "url": landing_url,
        "source": source,
        "job": event,
        "geometry": geometry,
        "timeperiod_startdate": starts,
        "timeperiod_enddate": ends,
        "authors": authors or None,
        "publisher": attrs.get("publisher"),
        "abstract": sample_meta["abstract"],
        "keywords": sample_meta["keywords"],
        "placename": sample_meta["placename"],
        "type": "physical-object",
        "status": "p",
        "provenance": {
            "harvest": {
                "harvester": "harvest_gfz_igsn",
                "source_url": GFZ_LANDING_URL,
                "source_type": source.source_type if source else "gfz-igsn",
                "source_name": source.name if source else None,
                "harvested_at": timezone.now().isoformat(),
                "harvesting_event_id": event.id if event else None,
                "doi": doi,
            },
            "metadata_sources": sample_meta["metadata_sources"],
            "sample": sample_meta["sample"],
        },
        "_related_identifiers": related_identifiers,
    }


def scrape_gfz_and_save_works(source, event, max_records=None, update_existing=False, stats=None, throttle=None):
    """Enumerate GFZ IGSNs from DataCite, scrape coordinates, and persist sample Works."""
    if stats is None:
        stats = HarvestStats()
    if throttle is None:
        throttle = DEFAULT_THROTTLE_SECONDS
    gfz_session = _gfz_session()
    saved = 0
    seen = 0

    logger.info("Starting GFZ IGSN scrape (max_records=%s, throttle=%ss)", max_records, throttle)

    for item in iter_datacite_records(GFZ_ENUM_QUERY, sort="created", max_records=max_records):
        seen += 1
        if seen % _PROGRESS_EVERY == 0:
            logger.info(
                "GFZ IGSN: processed %d%s records (created=%d, updated=%d, skipped=%d)",
                seen,
                f"/{max_records}" if max_records else "",
                stats.created,
                stats.updated,
                stats.skipped,
            )
        doi = (item.get("attributes") or {}).get("doi") or ""
        igsn = doi.rsplit("/", 1)[-1].upper() if doi else None
        if not igsn:
            stats.record("skipped_no_geometry")
            continue

        geometry = _scrape_gfz_geometry(gfz_session, igsn)
        if throttle:
            time.sleep(throttle)
        if geometry is None:
            # Landing page had no usable coordinates ("N/A", ~35% of GFZ samples).
            stats.record("skipped_no_geometry")
            continue

        kwargs = _gfz_item_to_work_kwargs(item, geometry, source, event)
        related_identifiers = kwargs.pop("_related_identifiers", [])
        try:
            work, action = _save_or_update_work(kwargs, source, event, update_existing=update_existing)
            stats.record(action)
            if action in ("created", "updated"):
                if source and source.collection_id:
                    work.collections.add(source.collection_id)
                _create_relations_from_datacite(work, related_identifiers)
            if action == "created":
                saved += 1
        except Exception as e:
            logger.warning("Failed to persist GFZ sample %s: %s", doi, e)

    logger.info(
        "GFZ IGSN scrape finished: %d seen, %d created, %d skipped (%d no coordinates on landing page)",
        seen,
        stats.created,
        stats.skipped,
        stats.skipped_no_geometry,
    )
    return saved, seen


def harvest_gfz_igsn(
    source_id,
    user=None,
    max_records=None,
    update_existing=False,
    event_id=None,
    throttle=None,
):
    """Harvest GFZ IGSN physical samples by scraping landing-page coordinates."""
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    ensure_collection_for_source(source)
    event = start_harvesting_event(source, event_id)

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    try:
        if max_records is None and isinstance(source.harvest_config, dict):
            max_records = source.harvest_config.get("max_records")
        if throttle is None and isinstance(source.harvest_config, dict):
            throttle = source.harvest_config.get("throttle")
        stats = HarvestStats()
        saved, seen = scrape_gfz_and_save_works(
            source,
            event,
            max_records=max_records,
            update_existing=update_existing,
            stats=stats,
            throttle=throttle,
        )
        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)

        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "GFZ IGSN ",
                "source_label": source.name,
                "detail_header": "GFZ IGSN harvest details:",
                "source_name": source.name,
                "source_url": GFZ_LANDING_URL,
                "url_label": "GFZ IGSN landing pages",
                "collection_label": source.collection.name if source.collection_id else None,
                "records_added_label": "New samples saved",
                "records_added": stats.created,
                "records_updated_label": "Updated samples",
                "records_updated": stats.updated,
                "spatial_label": "Samples with spatial metadata",
                "spatial_count": spatial_count,
                "temporal_label": "Samples with temporal metadata",
                "temporal_count": temporal_count,
                "event_started": f"{event.started_at:%Y-%m-%d %H:%M:%S}",
                "event_completed": f"{event.completed_at:%Y-%m-%d %H:%M:%S}",
                "warning_summary": warning_collector.get_summary(),
                "resolved_prefix": None,
                "container_title_filters": None,
                "records_seen": seen,
                "records_processed": None,
            },
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error("GFZ IGSN harvesting failed for source %s: %s", source_id, str(e))
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "GFZ IGSN ",
                "source_label": source.name,
                "source_type_label": "GFZ IGSN",
                "source_name": source.name,
                "source_url": GFZ_LANDING_URL,
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
