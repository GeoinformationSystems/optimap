# SPDX-FileCopyrightText: 2026 OPTIMETA and KOMET projects
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Helpers for the structured ``Work.provenance`` JSON.

Schema:
    {
        "harvest": {
            "harvester": "harvest_oai_endpoint",
            "source_url": "https://...",
            "source_type": "oai-pmh",
            "harvested_at": "2026-04-30T12:00:00+00:00",
            "original_record": {...},   # source-specific raw record (optional)
        },
        "metadata_sources": {            # per-field origin
            "authors": "openalex" | "original_source" | "datacite.creators",
            "author_orcids": "openalex" | "openaire",   # source that wrote Work.author_links (Author M2M)
            "geometry": "DC.SpatialCoverage" | "datacite.geoLocations" | "gfz.landing_page",
            "abstract": "synthesised" | "synthesised+datacite.descriptions",  # IGSN samples (#187)
            "keywords": "datacite.subjects",             # IGSN sample materials
            "placename": "datacite.geoLocationPlace" | "gfz.landing_page",
            ...
        },
        "sample": {                       # IGSN physical sample metadata (#187)
            "resource_type": "Specimen",  # DataCite types.resourceType (specific kind)
            "materials": ["Rock"],        # DataCite subjects (→ keywords)
            "repositories": ["USGS ..."], # DataCite contributors (hosting institution)
            "place": "Oak Ridge NEON ...",# DataCite geoLocationPlace or scraped place
            "description": {              # provenance of the synthesised abstract
                "synthesised": True,
                "synthesised_from": ["resource_type", "subjects", "collected_date",
                                     "geoLocationPlace", "publisher", "contributors",
                                     "related_identifiers"],  # fields actually used
                "source_description_appended": False,  # True if a real DataCite
                                                       # description was appended
            },
        },
        "openalex_match": {
            "status": "verified" | "unverified" | "none",
            "score": 0.92,
            "matched_id": "https://openalex.org/W123",
            "top_candidate": {...}        # only when unverified
        },
        # present in metadata_sources when a versioned DOI (e.g. /v2) was not
        # found in OpenAlex and enrichment fell back to an earlier version:
        # "openalex_doi_version_fallback": {"queried_doi": "10.x/y/v2",
        #                                   "matched_doi": "10.x/y/v1"}
        "countries": {                    # offline point-in-polygon join (#261)
            "source": "natural_earth",
            "method": "intersects" | "buffer_snap",
            "snap_tolerance_degrees": 0.12,  # only when method == buffer_snap
            "iso_codes": ["CR"],
            "assigned_at": "2026-04-30T..."
        },
        "regions": {                      # offline point-in-polygon join (continents + oceans)
            "source": "global_regions",
            "method": "intersects",
            "regions": [{"name": "Asia", "region_type": "Continent"}],
            "assigned_at": "2026-04-30T..."
        },
        "events": [                       # chronological audit log
            {"type": "doi_contribution", "user_id": 42, "doi": "10.5194/...",
             "at": "2026-04-30T..."},     # user added this work by submitting its DOI
            {"type": "contribution", "user_id": 42, "kind": "spatial",
             "at": "2026-04-30T...", "changes": [...]},
            {"type": "publish", "user_id": 1, "at": "..."},
            {"type": "unpublish", "user_id": 1, "at": "..."},
            {"type": "author_link", "source": "openalex", "linked": 3,
             "orcids": ["0000-0002-1825-0097"], "at": "..."},
            {"type": "geometry_repair", "method": "make_valid",
             "at": "..."},          # invalid geometry repaired with GEOS make_valid on save
        ],
    }

All keys are optional; fresh Works start with ``{}``.

Public subset (returned to unauthenticated callers and non-curator users):
  - ``harvest.original_record`` is removed (raw upstream payload)
  - ``openalex_match.top_candidate`` is removed (verbose raw API response)
  - ``user_id`` is removed from every event (personal data)
"""

import copy

from django.utils import timezone


def public_subset(provenance) -> dict:
    """Return a privacy-safe copy of ``provenance`` suitable for anonymous API responses.

    Strips: ``harvest.original_record``, ``openalex_match.top_candidate``,
    and ``user_id`` from every event.
    """
    if not isinstance(provenance, dict):
        return {}
    result = copy.deepcopy(provenance)

    harvest = result.get("harvest")
    if isinstance(harvest, dict):
        harvest.pop("original_record", None)

    openalex = result.get("openalex_match")
    if isinstance(openalex, dict):
        openalex.pop("top_candidate", None)

    events = result.get("events")
    if isinstance(events, list):
        for ev in events:
            if isinstance(ev, dict):
                ev.pop("user_id", None)

    return result


def _ensure_dict(value):
    """Tolerate None or unexpected values in the provenance field."""
    if isinstance(value, dict):
        return value
    return {}


def set_block(work, key, value):
    """Persist ``work.provenance[key] = value`` via a queryset update.

    Uses ``.update()`` rather than ``work.save()`` so it neither re-fires the
    ``Work`` save signals (which would re-run reverse geocoding / country
    assignment) nor bumps ``lastUpdate``. Mutates the in-memory instance too so
    the caller sees the merged provenance.
    """
    merged = {**_ensure_dict(work.provenance), key: value}
    type(work).objects.filter(pk=work.pk).update(provenance=merged)
    work.provenance = merged


def append_event(work, event_type, **fields):
    """Append a structured event to ``work.provenance['events']`` and save the field.

    Caller is responsible for any other field changes; this helper only touches provenance.
    """
    provenance = _ensure_dict(work.provenance)
    event = {
        "type": event_type,
        "at": timezone.now().isoformat(),
    }
    event.update({k: v for k, v in fields.items() if v is not None})
    provenance.setdefault("events", []).append(event)
    work.provenance = provenance


def work_has_contribution_kind(work, kind) -> bool:
    """True if *any* user has contributed ``kind`` (e.g. ``"spatial"``) to ``work``.

    The user-agnostic counterpart of :func:`user_has_contributed_kind`. Used by
    the admin re-harvest to decide whether a source-derived field may be
    overridden: a field a curator/contributor has touched is never clobbered by
    a re-harvest, regardless of who touched it.
    """
    if kind is None:
        return False
    provenance = _ensure_dict(work.provenance)
    for evt in provenance.get("events", []) or []:
        if evt.get("type") != "contribution":
            continue
        if kind in (evt.get("kinds") or []):
            return True
    return False


def user_has_contributed_kind(work, user_id, kind) -> bool:
    """True if ``user_id`` has already contributed ``kind`` to ``work``.

    Source of truth is the provenance event log — survives both account
    deletion (Contribution.user goes NULL) and Recognition Board row
    deletion. Used by the contribution endpoints to dedupe Recognition
    Board counters: same user repeatedly editing the same property type
    on the same work counts once. Different users editing the same
    property each count once.

    Call this *before* ``append_event`` so the event being recorded now
    is not in the log yet.
    """
    if user_id is None or kind is None:
        return False
    provenance = _ensure_dict(work.provenance)
    for evt in provenance.get("events", []) or []:
        if evt.get("type") != "contribution":
            continue
        if evt.get("user_id") != user_id:
            continue
        if kind in (evt.get("kinds") or []):
            return True
    return False
