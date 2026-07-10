# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""DataCite IGSN physical-sample harvester (issue #187).

Harvests ``resourceTypeGeneral:PhysicalObject`` DOIs from the DataCite REST
API (``api.datacite.org``) as ``Work`` rows with ``type='physical-object'``.
Coordinates come from DataCite ``geoLocations`` (essentially all points), and
``relatedIdentifiers`` become generic ``WorkRelation`` rows (see #187).

The full corpus is ~13 M geolocated points, so a harvest is always a bounded
subset. Sampling is driven by ``Source.harvest_config`` (JSON), overridable per
run. Defaults: ``publisher='SESAR'``, ``require_coordinates=True`` (only samples
with a ``geoLocationPoint``), stable ``sort='created'`` so that raising
``max_records`` re-encounters the same records first (skipped as duplicates)
before adding new ones. See docs/manage.md → "DataCite IGSN samples".
"""

import html
import logging

import requests
from django.contrib.gis.geos import GeometryCollection, LinearRing, Point, Polygon
from django.utils import timezone

from works.models import Source, WorkRelation
from works.utils.identifiers import normalize_doi

from .common import (
    HarvestStats,
    HarvestWarningCollector,
    _save_or_update_work,
    complete_harvest,
    ensure_collection_for_source,
    fail_harvest,
    log_work_failed,
    log_work_retrieved,
    render_harvest_email,
    resolve_user,
    send_harvest_email,
    start_harvesting_event,
)
from .sessions import (
    DATACITE_API_URL,
    DATACITE_HTTP_TIMEOUT,
    DATACITE_PAGE_SIZE,
    _datacite_session,
)

logger = logging.getLogger(__name__)

# How often (records seen) to emit an INFO progress line during the harvest loop.
_PROGRESS_EVERY = 100

# Default sampling config; merged under Source.harvest_config and run overrides.
DEFAULT_CONFIG = {
    "publisher": "SESAR",
    "require_coordinates": True,
    "only_with_related_doi": False,
    "resource_type": None,  # e.g. "Specimen"
    "bbox": None,  # [minLon, minLat, maxLon, maxLat]
    "date_from": None,  # year or ISO date (matched on `registered`)
    "date_to": None,
    "query": None,  # raw ES query-string fragment, ANDed on
    "sort": "created",  # stable order (see module docstring)
}


def _effective_config(source, overrides=None):
    """Merge DEFAULT_CONFIG < Source.harvest_config < per-run overrides."""
    config = dict(DEFAULT_CONFIG)
    if isinstance(getattr(source, "harvest_config", None), dict):
        config.update({k: v for k, v in source.harvest_config.items() if v is not None})
    if overrides:
        config.update({k: v for k, v in overrides.items() if v is not None})
    return config


def _build_datacite_query(config):
    """Build the DataCite ``query`` (Elasticsearch query-string) from config."""
    clauses = ["types.resourceTypeGeneral:PhysicalObject"]
    if config.get("publisher"):
        clauses.append(f'publisher:"{config["publisher"]}"')
    if config.get("require_coordinates"):
        clauses.append("_exists_:geoLocations.geoLocationPoint")
    if config.get("only_with_related_doi"):
        clauses.append("relatedIdentifiers.relatedIdentifierType:DOI")
        clauses.append("_exists_:relatedIdentifiers.relatedIdentifier")
    if config.get("resource_type"):
        clauses.append(f'types.resourceType:"{config["resource_type"]}"')
    bbox = config.get("bbox")
    if bbox and len(bbox) == 4:
        min_lon, min_lat, max_lon, max_lat = bbox
        clauses.append(f"geoLocations.geoLocationPoint.pointLatitude:[{min_lat} TO {max_lat}]")
        clauses.append(f"geoLocations.geoLocationPoint.pointLongitude:[{min_lon} TO {max_lon}]")
    if config.get("date_from") or config.get("date_to"):
        lo = config.get("date_from") or "*"
        hi = config.get("date_to") or "*"
        clauses.append(f"registered:[{lo} TO {hi}]")
    if config.get("query"):
        clauses.append(f"({config['query']})")
    return " AND ".join(clauses)


def _geometry_from_geolocations(geolocations):
    """Build a GeometryCollection (SRID 4326) from DataCite ``geoLocations``.

    Handles ``geoLocationPoint`` (the overwhelming majority), ``geoLocationBox``
    and ``geoLocationPolygon``. Returns ``None`` when no usable geometry exists.
    """
    if not geolocations:
        return None
    geoms = []
    for loc in geolocations:
        if not isinstance(loc, dict):
            continue
        point = loc.get("geoLocationPoint")
        if isinstance(point, dict):
            lat, lon = point.get("pointLatitude"), point.get("pointLongitude")
            if lat is not None and lon is not None:
                geoms.append(Point(float(lon), float(lat), srid=4326))
        box = loc.get("geoLocationBox")
        if isinstance(box, dict):
            try:
                w = float(box["westBoundLongitude"])
                e = float(box["eastBoundLongitude"])
                s = float(box["southBoundLatitude"])
                n = float(box["northBoundLatitude"])
                geoms.append(Polygon(LinearRing((w, s), (w, n), (e, n), (e, s), (w, s)), srid=4326))
            except (KeyError, TypeError, ValueError):
                pass
        polygon = loc.get("geoLocationPolygon")
        if isinstance(polygon, list) and len(polygon) >= 3:
            try:
                pts = [
                    (float(p["polygonPoint"]["pointLongitude"]), float(p["polygonPoint"]["pointLatitude"]))
                    for p in polygon
                ]
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                geoms.append(Polygon(LinearRing(pts), srid=4326))
            except (KeyError, TypeError, ValueError):
                pass
    if not geoms:
        return None
    return GeometryCollection(geoms, srid=4326)


def _temporal_from_dates(dates):
    """Extract sampling ('Collected') dates into (startdate[], enddate[]) string arrays."""
    starts, ends = [], []
    for entry in dates or []:
        if not isinstance(entry, dict) or entry.get("dateType") != "Collected":
            continue
        value = (entry.get("date") or "").strip()
        if not value:
            continue
        # DataCite allows "start/end" ranges; a single value means start == end.
        if "/" in value:
            lo, _, hi = value.partition("/")
            starts.append(lo.strip())
            ends.append((hi or lo).strip())
        else:
            starts.append(value)
            ends.append(value)
    return starts, ends


def _clean_subjects(subjects):
    """Return de-duplicated material/classification strings from DataCite ``subjects``.

    Unescapes HTML entities (SESAR encodes ``>`` as ``&gt;`` in e.g.
    ``Macrobiology&gt;Biology``) and drops blanks.
    """
    out = []
    for entry in subjects or []:
        value = entry.get("subject") if isinstance(entry, dict) else None
        if not value:
            continue
        value = html.unescape(value).strip()
        if value and value not in out:
            out.append(value)
    return out


def _extract_repositories(contributors):
    """Return names of hosting/curating institutions from DataCite ``contributors``."""
    out = []
    for c in contributors or []:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def _first_place(geolocations):
    """Return the first non-empty ``geoLocationPlace`` from DataCite ``geoLocations``."""
    for loc in geolocations or []:
        if isinstance(loc, dict):
            place = (loc.get("geoLocationPlace") or "").strip()
            if place:
                return place
    return None


def _extract_descriptions(descriptions):
    """Return the non-empty free-text description strings from DataCite ``descriptions``."""
    out = []
    for d in descriptions or []:
        if isinstance(d, dict):
            text = (d.get("description") or "").strip()
            if text:
                out.append(text)
    return out


def _count_related(related_identifiers):
    """Count relatedIdentifiers that actually carry a target value (GFZ often has none)."""
    return sum(1 for r in related_identifiers or [] if isinstance(r, dict) and r.get("relatedIdentifier"))


def synthesise_sample_description(resource_type, materials, collected, place, publisher, repositories, related_count):
    """Compose a one-paragraph human description of a physical sample from its metadata.

    IGSN ``descriptions`` are almost never populated by SESAR/GFZ, so samples would
    otherwise show an empty body. This builds a readable sentence from the structured
    fields we already harvest. When a real description also exists, the caller appends
    it as further paragraphs.
    """
    kind = resource_type or "physical sample"
    # Drop materials that merely restate the resource type (e.g. subject "Specimen").
    materials_display = [m for m in materials if m.lower() != kind.lower()]

    sentence = f"Physical sample ({kind})"
    if materials_display:
        sentence += " of " + ", ".join(materials_display[:3])
    if collected:
        sentence += f", collected {collected}"
    if place:
        sentence += f" at {place}"
    sentence += "."

    tail = []
    if publisher:
        tail.append(f"Registered by {publisher}")
    if repositories:
        tail.append("archived at " + "; ".join(repositories[:2]))
    if tail:
        sentence += " " + "; ".join(tail) + "."
    if related_count:
        sentence += f" Linked to {related_count} related resource{'s' if related_count != 1 else ''}."
    return sentence


def build_sample_metadata(attrs, geometry_place=None, related_count=0, geometry_source="datacite.geoLocations"):
    """Derive display metadata (abstract, keywords, sample block) for a sample record.

    Shared by the SESAR (DataCite) and GFZ harvesters. ``geometry_place`` lets GFZ
    pass a place scraped from its landing page; ``geometry_source`` is the origin
    recorded for the geometry (``datacite.geoLocations`` / ``gfz.landing_page``).
    Returns a dict with ``abstract``, ``keywords``, ``placename``,
    ``metadata_sources`` (the complete per-field attribution — no null values) and a
    ``sample`` provenance block (structured resource_type / materials / repositories).
    """
    resource_type = ((attrs.get("types") or {}).get("resourceType") or "").strip() or None
    materials = _clean_subjects(attrs.get("subjects"))
    repositories = _extract_repositories(attrs.get("contributors"))
    place = _first_place(attrs.get("geoLocations")) or geometry_place
    starts, _ = _temporal_from_dates(attrs.get("dates"))
    real_descriptions = _extract_descriptions(attrs.get("descriptions"))

    synthesised = synthesise_sample_description(
        resource_type,
        materials,
        starts[0] if starts else None,
        place,
        attrs.get("publisher"),
        repositories,
        related_count,
    )
    # Always synthesise; append any real descriptions as further paragraphs.
    abstract = synthesised
    if real_descriptions:
        abstract += "\n\n" + "\n\n".join(real_descriptions)

    place_from_datacite = _first_place(attrs.get("geoLocations"))

    # Record exactly which fields fed the synthesised sentence, so the abstract
    # is auditable and regenerable and is never mistaken for author-supplied text.
    synthesised_from = []
    for name, present in (
        ("resource_type", resource_type),
        ("subjects", materials),
        ("collected_date", starts),
        ("geoLocationPlace" if place_from_datacite else "landing_page_place", place),
        ("publisher", attrs.get("publisher")),
        ("contributors", repositories),
        ("related_identifiers", related_count),
    ):
        if present:
            synthesised_from.append(name)

    # Per-field origin (Work.provenance.metadata_sources) — the complete map for a
    # sample, with no null values. Title/authors/publisher come from DataCite
    # (local sample number / collector / registrant).
    metadata_sources = {
        "title": "datacite.title",
        "publisher": "datacite.publisher",
        "geometry": geometry_source,
        "abstract": "synthesised+datacite.descriptions" if real_descriptions else "synthesised",
    }
    if starts:
        metadata_sources["timeperiod"] = "datacite.dates[Collected]"
    if attrs.get("creators"):
        metadata_sources["authors"] = "datacite.creators"
    if materials:
        metadata_sources["keywords"] = "datacite.subjects"
    if place:
        metadata_sources["placename"] = "datacite.geoLocationPlace" if place_from_datacite else "gfz.landing_page"

    return {
        "abstract": abstract,
        "keywords": materials or None,
        "placename": place[:255] if place else None,  # Work.placename is max_length=255
        "metadata_sources": metadata_sources,
        "sample": {
            "resource_type": resource_type,
            "materials": materials,
            "repositories": repositories,
            "place": place,
            "description": {
                "synthesised": True,
                "synthesised_from": synthesised_from,
                "source_description_appended": bool(real_descriptions),
            },
        },
    }


def _datacite_item_to_work_kwargs(item, source, event):
    """Map one DataCite record to Work kwargs, or None to skip (no usable geometry)."""
    attrs = item.get("attributes") or {}
    doi = attrs.get("doi")
    if not doi:
        return None

    geometry = _geometry_from_geolocations(attrs.get("geoLocations"))
    if geometry is None:
        # The map needs coordinates; skip place-name-only records.
        return None

    titles = attrs.get("titles") or []
    title = (titles[0].get("title") if titles and isinstance(titles[0], dict) else None) or doi
    log_work_retrieved(event, title)
    landing_url = attrs.get("url") or f"https://doi.org/{doi}"
    authors = [c.get("name") for c in (attrs.get("creators") or []) if isinstance(c, dict) and c.get("name")]
    starts, ends = _temporal_from_dates(attrs.get("dates"))
    related_identifiers = attrs.get("relatedIdentifiers") or []
    sample_meta = build_sample_metadata(attrs, related_count=_count_related(related_identifiers))

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
                "harvester": "harvest_datacite",
                "source_url": DATACITE_API_URL,
                "source_type": source.source_type if source else "datacite",
                "source_name": source.name if source else None,
                "harvested_at": timezone.now().isoformat(),
                "harvesting_event_id": event.id if event else None,
                "doi": doi,
            },
            "metadata_sources": sample_meta["metadata_sources"],
            "sample": sample_meta["sample"],
        },
        # Carried out of band (not a Work field) — consumed by the caller.
        "_related_identifiers": related_identifiers,
    }


def _create_relations_from_datacite(work, related_identifiers):
    """Create generic WorkRelation rows from DataCite ``relatedIdentifiers``.

    Stored as dangling links (``to_work`` null); the ``resolve_incoming_work_relations``
    signal upgrades them to a ``to_work`` FK if/when the target is harvested. DOIs are
    normalized to bare form so they match ``Work.doi``.
    """
    for rel in related_identifiers or []:
        if not isinstance(rel, dict):
            continue
        raw = rel.get("relatedIdentifier")
        if not raw:
            continue
        id_type = (rel.get("relatedIdentifierType") or "").strip()
        identifier = normalize_doi(raw) if id_type.upper() == "DOI" else raw.strip()
        if not identifier:
            continue
        WorkRelation.objects.get_or_create(
            from_work=work,
            to_identifier=identifier,
            relation_type=WorkRelation.normalize_relation_type(rel.get("relationType")),
            defaults={"to_identifier_type": id_type or "URL", "source": "datacite"},
        )


def iter_datacite_records(query, sort="created", max_records=None, session=None):
    """Yield DataCite records for ``query`` via cursor pagination (stable ``sort``).

    Shared by the SESAR harvester and the GFZ landing-page scraper (which uses it
    only to enumerate GFZ IGSNs, then scrapes coordinates separately).
    """
    session = session or _datacite_session()
    cursor = "1"  # DataCite cursor pagination starts at page[cursor]=1
    yielded = 0
    while True:
        params = {
            "query": query,
            "sort": sort,
            "page[cursor]": cursor,
            "page[size]": str(DATACITE_PAGE_SIZE),
        }
        try:
            resp = session.get(DATACITE_API_URL, params=params, timeout=DATACITE_HTTP_TIMEOUT)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"DataCite request failed: {e}") from e
        if not resp.ok:
            raise RuntimeError(f"DataCite returned HTTP {resp.status_code} for query {query!r}: {resp.text[:300]}")

        data = resp.json()
        results = data.get("data", [])
        if not results:
            return
        for item in results:
            yield item
            yielded += 1
            if max_records and yielded >= max_records:
                return

        next_cursor = _next_cursor_from_link((data.get("links") or {}).get("next"))
        if not next_cursor or next_cursor == cursor:
            return
        cursor = next_cursor


def parse_datacite_response_and_save_works(
    source,
    event,
    config,
    max_records=None,
    update_existing=False,
    stats=None,
):
    """Page through the DataCite REST API (cursor) and persist sample Works."""
    query = _build_datacite_query(config)
    sort = config.get("sort") or "created"
    if stats is None:
        stats = HarvestStats()
    saved = 0
    seen = 0

    logger.info("Starting DataCite harvest: query=%r sort=%s max_records=%s", query, sort, max_records)

    for item in iter_datacite_records(query, sort=sort, max_records=max_records):
        seen += 1
        if seen % _PROGRESS_EVERY == 0:
            suffix = f"/{max_records}" if max_records else ""
            logger.info(
                "DataCite: processed %d%s records (created=%d, updated=%d, skipped=%d)",
                seen,
                suffix,
                stats.created,
                stats.updated,
                stats.skipped,
            )
            if event is not None:
                event.append_log(
                    f"Processed {seen}{suffix} records ({stats.created} created, "
                    f"{stats.updated} updated, {stats.skipped} skipped)"
                )
        kwargs = _datacite_item_to_work_kwargs(item, source, event)
        if not kwargs:
            stats.record("skipped_no_geometry")  # no coordinates → not ingested
            continue
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
            logger.warning("Failed to persist DataCite sample %s: %s", kwargs.get("doi"), e)
            if event is not None:
                log_work_failed(event, kwargs.get("title") or kwargs.get("doi"), e)

    logger.info(
        "DataCite harvest finished: %d seen, %d created, %d updated, %d skipped (%d no-geometry, %d duplicate)",
        seen,
        stats.created,
        stats.updated,
        stats.skipped,
        stats.skipped_no_geometry,
        stats.skipped_same_source + stats.skipped_cross_source,
    )
    return saved, seen


def _next_cursor_from_link(next_link):
    """Extract the ``page[cursor]`` value from a DataCite ``links.next`` URL."""
    if not next_link:
        return None
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(next_link).query)
    values = qs.get("page[cursor]")
    return values[0] if values else None


def harvest_datacite(
    source_id,
    user=None,
    max_records=None,
    update_existing=False,
    event_id=None,
    trigger_source=None,
    **overrides,
):
    """Harvest IGSN physical samples from DataCite for a configured Source.

    Sampling comes from ``Source.harvest_config``, overridable via ``overrides``
    (e.g. ``publisher=``, ``bbox=``, ``only_with_related_doi=``). ``max_records``
    caps the run; a stable sort makes raising it additive.
    """
    user = resolve_user(user)
    source = Source.objects.get(id=source_id)
    ensure_collection_for_source(source)
    # Resolve the effective max_records (Source.harvest_config's default,
    # unless the caller passed an explicit override) before creating the
    # event, so the *actual* cap used for this run is what gets recorded.
    config = _effective_config(source, overrides)
    if max_records is None:
        max_records = config.get("max_records")
    event = start_harvesting_event(source, event_id, trigger_source, max_records)
    if event is None:
        return
    event.append_log(f"Starting DataCite (IGSN) harvest for {source.name}")

    warning_collector = HarvestWarningCollector()
    warning_collector.setLevel(logging.INFO)
    logger.addHandler(warning_collector)

    try:
        stats = HarvestStats()
        saved, seen = parse_datacite_response_and_save_works(
            source,
            event,
            config,
            max_records=max_records,
            update_existing=update_existing,
            stats=stats,
        )
        spatial_count, temporal_count = complete_harvest(event, stats, warning_collector)

        subject, body = render_harvest_email(
            "email/harvest_success.en.txt",
            {
                "subject_prefix": "DataCite IGSN ",
                "source_label": source.name,
                "detail_header": "DataCite IGSN harvest details:",
                "source_name": source.name,
                "source_url": DATACITE_API_URL,
                "url_label": "DataCite API",
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
            event=event,
        )
        send_harvest_email(user, subject, body)

    except Exception as e:
        logger.error("DataCite harvesting failed for source %s: %s", source_id, str(e))
        fail_harvest(event, e, warning_collector)
        subject, body = render_harvest_email(
            "email/harvest_failure.en.txt",
            {
                "subject_prefix": "DataCite IGSN ",
                "source_label": source.name,
                "source_type_label": "DataCite IGSN",
                "source_name": source.name,
                "source_url": DATACITE_API_URL,
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
