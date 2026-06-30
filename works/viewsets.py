# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""publications API views."""

import logging
import os
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

# Import geoextent at module level
import geoextent.lib.extent as geoextent
from django.conf import settings
from django.contrib.gis.geos import Point, Polygon
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django_q.humanhash import humanize
from django_q.tasks import async_task
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated, IsAuthenticatedOrReadOnly
from rest_framework.renderers import BrowsableAPIRenderer, JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework_gis import filters

from .models import Author, Collection, Country, GlobalRegion, Source, Subscription, Work
from .serializers import (
    AuthorSerializer,
    CollectionSerializer,
    ContributeDoiSerializer,
    CountrySerializer,
    GeoextentBatchSerializer,
    GeoextentExtractSerializer,
    GeoextentExtractTextSerializer,
    GeoextentRemoteGetSerializer,
    GeoextentRemoteSerializer,
    GlobalRegionSerializer,
    SourceSerializer,
    SubscriptionSerializer,
    WorkMinimalSerializer,
    WorkSerializer,
)
from .utils.geometry import annotate_rounded_geometry
from .utils.provenance import append_event, public_subset

logger = logging.getLogger(__name__)


# Reusable error-response schema. Most error paths in this module return
# `{"error": "<message>"}` (and sometimes also `{"details": "<…>"}`); a
# couple of validation paths fall back to DRF's serializer-error envelope.
# Schema-wise both fit `additionalProperties: true`, so a single shape covers them.
_ERROR_RESPONSE = inline_serializer(
    name="ErrorResponse",
    fields={
        "error": drf_serializers.CharField(),
        "details": drf_serializers.CharField(required=False),
    },
)


class ContributeDoiThrottle(UserRateThrottle):
    """Per-user rate limit for the contribute-by-DOI endpoint.

    Each new DOI triggers external API calls (Crossref + OpenAlex + OpenAIRE),
    so cap how often an authenticated user can submit. Rate is configured under
    the ``contribute_doi`` scope in ``REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']``.
    """

    scope = "contribute_doi"


class _GeoJSONRenderer(JSONRenderer):
    """Sets Content-Type: application/geo+json per W3C SDW-BP 5."""

    media_type = "application/geo+json"
    format = "geo+json"


@extend_schema_view(
    list=extend_schema(
        summary="List harvested data sources",
        tags=["Sources"],
        responses={200: SourceSerializer},
    ),
    retrieve=extend_schema(
        summary="Retrieve a source by ID",
        tags=["Sources"],
        responses={
            200: SourceSerializer,
            404: OpenApiResponse(_ERROR_RESPONSE, description="No source with this ID."),
        },
    ),
)
class SourceViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Source.objects.all()
    serializer_class = SourceSerializer
    permission_classes = [AllowAny]


@extend_schema_view(
    list=extend_schema(
        summary="List published works (paginated GeoJSON)",
        description=(
            "Returns published works as a GeoJSON `FeatureCollection`. Admins additionally "
            "see drafts and harvested-but-unpublished works. Filter the spatial slice with "
            "`?in_bbox=west,south,east,north`.\n\n"
            "Pass `?minimal=true` to receive only `id`, `title`, `doi`, `status`, "
            "`status_display`, and `geometry` — the reduced payload is used by the map "
            "for chunked loading; full details are fetched lazily per work.\n\n"
            "Pass `?author_orcid=<orcid>` to filter by a specific author's bare ORCID iD "
            "(e.g. `0000-0002-1825-0097`). Only works linked to that author are returned."
        ),
        tags=["Works"],
        parameters=[
            OpenApiParameter(
                name="author_orcid",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Filter to works linked to a specific author by their bare ORCID iD "
                    "(e.g. `0000-0002-1825-0097`). Invalid or unknown ORCIDs return an empty result set."
                ),
                examples=[
                    OpenApiExample(
                        name="filter by ORCID",
                        value="0000-0002-1825-0097",
                    )
                ],
            ),
        ],
    ),
    retrieve=extend_schema(
        summary="Retrieve a work by numeric ID",
        description="See the work landing page (`/work/<id>/`) for the human-readable view.",
        tags=["Works"],
        responses={
            200: WorkSerializer,
            404: OpenApiResponse(
                _ERROR_RESPONSE,
                description="No work with this ID, or the work is not yet published and the request is anonymous.",
            ),
        },
    ),
)
class WorkViewSet(viewsets.ReadOnlyModelViewSet):
    bbox_filter_field = "geometry"
    filter_backends = (filters.InBBoxFilter,)
    serializer_class = WorkSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]
    renderer_classes = [_GeoJSONRenderer, JSONRenderer, BrowsableAPIRenderer]

    def get_serializer_class(self):
        if self.request.query_params.get("minimal") == "true":
            return WorkMinimalSerializer
        return WorkSerializer

    def retrieve(self, request, *args, **kwargs):
        """302-redirect a merged-away duplicate's detail to the canonical work.

        Redirected works (``status='r'``) are excluded from ``get_queryset``, so a
        normal lookup would 404. Resolve the pk directly and, unless
        ``?include=redirected`` is set, send clients to the canonical detail URL.
        """
        qp = getattr(request, "query_params", request.GET)
        if qp.get("include") != "redirected":
            pk = kwargs.get(self.lookup_field or "pk")
            work = Work.objects.filter(pk=pk).first()
            if work is not None and work.status == "r":
                canonical = work.canonical_work()
                if canonical.id != work.id:
                    from django.shortcuts import redirect
                    from django.urls import reverse

                    return redirect(reverse("optimap:works:work-detail", args=[canonical.id]))
        return super().retrieve(request, *args, **kwargs)

    def get_queryset(self):
        """
        Return all publications for admin users, only published ones for others.
        Sorted by creation date (newest first) to match the works list page.

        For the ``provenance`` action: curators can access works in their
        collections at any publication status (not just published ones), so
        they can view provenance for harvested/contributed/draft works too.
        """
        # query_params is a DRF-only attribute; fall back to .GET for plain WSGIRequests.
        qp = getattr(self.request, "query_params", self.request.GET)
        # Merged-away duplicates (status='r') are tombstones for redirect only —
        # never list them, unless explicitly requested for inspection.
        include_redirected = qp.get("include") == "redirected"

        if self.request.user.is_authenticated and self.request.user.is_staff:
            qs = Work.objects.all().distinct()
            if not include_redirected:
                qs = qs.exclude(status="r")
            if qp.get("minimal") == "true":
                # For map loading, put published works (which have geometry) first so
                # markers appear in the first chunk instead of at the end of 4k harvested ones.
                qs = qs.annotate(
                    _status_priority=Case(
                        When(status="p", then=Value(0)),
                        default=Value(1),
                        output_field=IntegerField(),
                    )
                ).order_by("_status_priority", "-creationDate", "-id")
            else:
                qs = qs.order_by("-creationDate", "-id")
            qs = annotate_rounded_geometry(qs).prefetch_related("countries", "regions")
            author_orcid_staff = qp.get("author_orcid")
            if author_orcid_staff:
                from works.utils.orcid import normalize_orcid

                normalized = normalize_orcid(author_orcid_staff)
                if normalized:
                    qs = qs.filter(author_links__orcid=normalized)
            return qs
        if getattr(self, "action", None) == "provenance" and self.request.user.is_authenticated:
            curated = Work.objects.filter(collections__curators=self.request.user)
            public = Work.objects.filter(status="p")
            qs = (curated | public).distinct()
            if not include_redirected:
                qs = qs.exclude(status="r")
            return annotate_rounded_geometry(qs).prefetch_related("countries", "regions")
        public = Work.objects.filter(status="p").order_by("-creationDate", "-id").distinct()
        qs = annotate_rounded_geometry(public).prefetch_related("countries", "regions")
        author_orcid = qp.get("author_orcid")
        if author_orcid:
            from works.utils.orcid import normalize_orcid

            normalized = normalize_orcid(author_orcid)
            if normalized:
                qs = qs.filter(author_links__orcid=normalized)
        return qs

    @extend_schema(
        summary="Retrieve provenance for a work",
        tags=["Works"],
        description=(
            "Returns the structured provenance record for a work — where it was harvested from, "
            "per-field metadata attribution, OpenAlex enrichment result, reverse-geocoding details, "
            "and a chronological audit log of re-harvest/contribution/publish events.\n\n"
            "**Access tiers:**\n"
            "- **Staff** and **curators of any collection this work belongs to** receive the full "
            "provenance, including `harvest.original_record`, `openalex_match.top_candidate`, "
            "and `user_id` / `user_email` fields in events.\n"
            "- **All other callers** (including anonymous users) receive the public subset with "
            "those keys removed.\n\n"
            "**`harvest` keys:**\n"
            "| Key | Type | Description |\n"
            "|-----|------|-------------|\n"
            "| `harvester` | string | Task function name: `harvest_oai_endpoint`, `harvest_rss_endpoint`, `harvest_crossref_prefix`, `harvest_mountain_wetlands`, `harvest_openalex_source` |\n"
            "| `source_name` | string | Display name of the source |\n"
            "| `source_type` | string | One of: `oai-pmh`, `ojs`, `janeway`, `rss`, `crossref-prefix`, `mountain-wetlands`, `openalex` |\n"
            "| `source_url` | string | Harvest endpoint URL |\n"
            "| `harvested_at` | string | ISO 8601 timestamp of the harvest |\n"
            "| `harvesting_event_id` | integer | FK to the `HarvestingEvent` record |\n"
            "| `doi` | string | DOI as recorded at harvest time |\n"
            "| `original_record` | object | Raw upstream record (staff/curators only) |\n\n"
            "**`metadata_sources` keys and values:**\n"
            "Each key names a Work field; the value names where that field's data came from.\n"
            "| Key | Possible values |\n"
            "|-----|-----------------|\n"
            "| `abstract` | `crossref`, `openaire` |\n"
            "| `authors` | `original_source`, `openalex`, `crossref`, `openaire` |\n"
            "| `keywords` | `original_source`, `openalex`, `openaire` |\n"
            "| `topics` | `openalex` |\n"
            "| `type` | `openalex` |\n"
            "| `geometry` | `DC.SpatialCoverage`, `DC.box`, `link rel=alternate geo+json`, `study_sites`, `reharvest_html` (re-extracted from the landing page by the admin re-harvest) |\n"
            "| `timeperiod` | `reharvest_html` (re-extracted from the landing page by the admin re-harvest) |\n"
            "| `doi` | `original_source`, `openalex` |\n"
            "| `date` | `original_source (year-only)` |\n"
            "| `volume` / `issue` / `first_page` / `last_page` | `openalex`, `openaire` |\n"
            "| `language` | `openaire` |\n"
            "| `publisher` | `openaire` |\n"
            "| `biblio` | `crossref` (volume/issue/pages from Crossref in one batch) |\n"
            "| `openalex_metadata` | `openalex` (any OpenAlex enrichment was applied) |\n"
            "| `openalex_doi_version_fallback` | `{queried_doi, matched_doi}` dict — present when the work's versioned DOI (e.g. `/v2`) was not found in OpenAlex and enrichment succeeded via the bare DOI or an earlier version (e.g. `/v1`) |\n"
            "| `openalex` | `primary` (work was harvested directly from OpenAlex as the primary source) |\n"
            "| `author_orcids` | `openalex`, `openaire` (source that supplied the ORCID links written to the Author M2M) |\n\n"
            "**`openalex_match` keys:**\n"
            "| Key | Type | Description |\n"
            "|-----|------|-------------|\n"
            "| `status` | string | `verified`, `unverified`, `none`, or `skipped` (skipped means the primary source already supplied DOI + authors) |\n"
            "| `score` | number | Confidence score 0.0–1.0 (absent when status is `none` or `skipped`) |\n"
            "| `matched_id` | string | OpenAlex work URL, e.g. `https://openalex.org/W2741809807` |\n"
            "| `top_candidate` | object | Raw OpenAlex API response for the best candidate (staff/curators only; only present when status is `unverified`) |\n\n"
            "**`openaire_match` keys:**\n"
            "| Key | Type | Description |\n"
            "|-----|------|-------------|\n"
            "| `status` | string | `matched` (an OpenAIRE record was found for the DOI) or `none`. Recorded for every DOI-bearing work checked by the post-harvest sweep, even when nothing was filled |\n"
            "| `openaire_id` | string | OpenAIRE internal id, e.g. `doi_dedup___::…` (present when matched) |\n"
            "| `url` | string | Public OpenAIRE Explore page for the matched record, e.g. `https://explore.openaire.eu/search/result?id=doi_dedup___::…` (present when matched) |\n"
            "| `num_found` | integer | Number of OpenAIRE records found for the DOI |\n\n"
            "**`geocoding` keys:**\n"
            "| Key | Type | Description |\n"
            "|-----|------|-------------|\n"
            "| `gazetteer` | string | Always `nominatim` |\n"
            "| `placename` | string | Human-readable location hierarchy, e.g. `Sulawesi, Indonesia` |\n"
            "| `n_geocoded` | integer | Number of geometry points successfully reverse-geocoded |\n"
            "| `geocoded_at` | string | ISO 8601 timestamp |\n"
            "| `matches` | array | Per-point Nominatim results (display name, OSM type/id/url, lat, lon) |\n\n"
            "**`countries` keys** (offline point-in-polygon join behind the `Work.countries` M2M):\n"
            "| Key | Type | Description |\n"
            "|-----|------|-------------|\n"
            "| `source` | string | Outline dataset (`natural_earth`), or `manual` for a staff curation decision |\n"
            "| `method` | string | `intersects` (geometry directly intersects an outline) or `buffer_snap` (matched only after buffering — coastal/island works just outside the simplified outline); `curator_assigned` or `curator_excluded` when `source` is `manual` |\n"
            "| `snap_tolerance_degrees` | number | Buffer applied for the snap, in degrees (e.g. `0.12` ≈ 12 nautical miles); present only when `method` is `buffer_snap` |\n"
            "| `iso_codes` | array | ISO 3166-1 alpha-2 codes of the matched countries (empty for `curator_excluded`) |\n"
            "| `assigned_at` | string | ISO 8601 timestamp (automated joins) |\n"
            "| `decided_by` | integer | Staff user id; present only when `source` is `manual` |\n"
            "| `decided_at` | string | ISO 8601 timestamp; present only when `source` is `manual` |\n\n"
            "**`regions` keys** (offline point-in-polygon join behind the `Work.regions` M2M):\n"
            "| Key | Type | Description |\n"
            "|-----|------|-------------|\n"
            "| `source` | string | Outline dataset (`global_regions`), or `manual` for a curator decision |\n"
            "| `method` | string | `intersects` (geometry directly intersects a continent/ocean outline; no buffer-snap); `curator_assigned` or `curator_excluded` when `source` is `manual` |\n"
            "| `regions` | array | Matched regions as `{name, region_type}` objects (`region_type` is `Continent` or `Ocean`; empty for `curator_excluded`) |\n"
            "| `assigned_at` | string | ISO 8601 timestamp (automated join only) |\n"
            "| `decided_by` / `decided_at` | int / string | Staff user id and ISO 8601 timestamp of a manual decision |\n\n"
            "**`events` — event types:**\n"
            "| `type` | Extra fields | Description |\n"
            "|--------|-------------|-------------|\n"
            "| `harvest_update` | `harvesting_event_id` | Recorded each time an existing work is re-harvested |\n"
            "| `reharvest_source_extents` | `geometry`, `temporal` (each `updated` when present) | Admin re-harvest re-extracted geometry / temporal extent from the landing page and overrode the stored value (only for values not user-contributed) |\n"
            "| `doi_backfill` | `doi`, `harvesting_event_id` | DOI was added to a previously DOI-less work |\n"
            "| `doi_contribution` | `doi`, `user_id`\\*, `user_email`\\* | A user added this work to OPTIMAP by submitting its DOI on /contribute/ (harvested from Crossref + enriched) |\n"
            "| `contribution` | `kinds` (array: `spatial`, `temporal`, `bok`), `user_id`\\*, `user_email`\\*, `game` (bool, optional) | User added spatial/temporal/BoK metadata; `game: true` when submitted via the georeferencing game |\n"
            "| `publish` | `status_from`, `status_to`, `user_id`\\*, `user_email`\\* | Work was published |\n"
            "| `unpublish` | `status_from`, `user_id`\\*, `user_email`\\* | Work was unpublished |\n"
            "| `source_migration` | `from_source`, `to_source` | Work was reassigned to a different `Source` by the `migrate_source_works` management command |\n"
            "| `openaire_enrich` | `openaire_id`, `doi`, `source_url`, `fields_filled` (array), `fields_offered_not_applied` (array) | OpenAIRE enrichment ran; `fields_filled` were empty and were populated, `fields_offered_not_applied` had an OpenAIRE value but a value already existed (kept under the fill-if-empty policy) |\n"
            '| `country_curation` | `decision` (`assigned`/`excluded`), `iso_codes` (array, for `assigned`), `user_id`\\*, `user_email`\\* | Staff manually assigned one or more countries, or marked the work "will not be matched", on the /countries curation section |\n'
            '| `region_curation` | `decision` (`assigned`/`excluded`), `regions` (array, for `assigned`), `region` (single-region path only), `user_id`\\*, `user_email`\\* | Staff manually assigned one or more global regions, or marked the work "will not be matched", on the /regions curation section |\n'
            "| `author_link` | `source` (`openalex`/`openaire`), `linked` (integer — number of Author rows linked), `orcids` (array of bare ORCIDs) | Author ORCID iDs were linked to the work via the Author M2M |\n"
            "| `geometry_repair` | `method` (`make_valid`) | A topologically invalid stored geometry was repaired with GEOS `make_valid` (PostGIS `ST_MakeValid`) on save |\n\n"
            "\\* `user_id` and `user_email` are present for staff and curators only.\n\n"
            "**Other top-level keys:**\n"
            "| Key | Type | Description |\n"
            "|-----|------|-------------|\n"
            "| `publication_notified_at` | string | ISO 8601 timestamp of when the publication notification email was sent to the contributor (suppresses duplicate sends on republish) |"
        ),
        responses={
            200: inline_serializer(
                name="ProvenanceResponse",
                fields={
                    "harvest": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "How and when this work was harvested. "
                            "Keys: harvester, source_name, source_type, source_url, "
                            "harvested_at, harvesting_event_id, doi. "
                            "Staff/curators also see original_record (raw upstream payload)."
                        ),
                    ),
                    "metadata_sources": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "Per-field attribution map. Keys name Work fields; values name their source. "
                            "Known keys: abstract, authors, keywords, topics, type, geometry, doi, date, "
                            "volume, issue, first_page, last_page, language, publisher, biblio, openalex_metadata, openalex, author_orcids. "
                            "Known values: original_source, openalex, openaire, crossref, DC.SpatialCoverage, "
                            "DC.box, link rel=alternate geo+json, study_sites, "
                            "original_source (year-only), primary."
                        ),
                    ),
                    "openalex_match": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "OpenAlex enrichment result. "
                            "Keys: status (verified/unverified/none/skipped), score (0.0–1.0), matched_id. "
                            "Staff/curators also see top_candidate."
                        ),
                    ),
                    "openaire_match": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "OpenAIRE enrichment result. "
                            "Keys: status (matched/none), openaire_id, url, num_found. "
                            "See the openaire_enrich event for the fields filled/offered."
                        ),
                    ),
                    "geocoding": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "Reverse-geocoding via Nominatim. "
                            "Keys: gazetteer, placename, n_geocoded, geocoded_at, matches."
                        ),
                    ),
                    "countries": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "Offline point-in-polygon country join (Work.countries M2M), or a staff "
                            "curation decision. Keys: source (natural_earth/manual), method "
                            "(intersects/buffer_snap/curator_assigned/curator_excluded), "
                            "snap_tolerance_degrees (only for buffer_snap), iso_codes, assigned_at, "
                            "decided_by/decided_at (manual only)."
                        ),
                    ),
                    "regions": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "Offline point-in-polygon global-region join (Work.regions M2M), "
                            "or a manual staff curation decision. "
                            "Keys: source (global_regions/manual), method "
                            "(intersects/curator_assigned/curator_excluded), "
                            "regions (array of {name, region_type}), assigned_at, decided_by, decided_at."
                        ),
                    ),
                    "dedup": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "Present on a canonical work that absorbed duplicates — either siblings sharing its "
                            "OpenAlex id, or ESSOAr/Authorea per-version DOIs sharing its versionless DOI base. "
                            "Keys: openalex_id (null for version dedup), merged_work_ids, merged_identifiers, "
                            "method (openalex_id/doi_version), "
                            "primary_basis (openalex_primary_location/version_rank/existing/doi_version), at. "
                            "An optional dedup_conflict list records non-primary geometry/temporal extents "
                            "that differed from the canonical's (kept for audit)."
                        ),
                    ),
                    "redirect": drf_serializers.DictField(
                        required=False,
                        help_text=(
                            "Present on a merged-away duplicate (work status='r'). "
                            "Keys: canonical_work_id, canonical_identifier, openalex_id, at. "
                            "Requests for this work's identifiers 302-redirect to the canonical work."
                        ),
                    ),
                    "events": drf_serializers.ListField(
                        child=drf_serializers.DictField(),
                        required=False,
                        help_text=(
                            "Chronological audit log. Each event has type (string) and at (ISO timestamp). "
                            "Event types: harvest_update, reharvest_source_extents, doi_backfill, doi_contribution, "
                            "contribution, publish, unpublish, source_migration, openaire_enrich, dedup_merge, dedup_unmerge, author_link, geometry_repair. "
                            "user_id and user_email are present for staff/curators only."
                        ),
                    ),
                    "publication_notified_at": drf_serializers.CharField(
                        required=False,
                        help_text="ISO 8601 timestamp of when the publication notification email was sent to the contributor.",
                    ),
                },
            ),
            404: OpenApiResponse(
                _ERROR_RESPONSE,
                description="No work with this ID, or not yet published and the caller is anonymous.",
            ),
        },
        examples=[
            OpenApiExample(
                name="OAI-PMH work with OpenAlex enrichment (public response)",
                summary="Typical response for anonymous/regular-user callers",
                description=(
                    "A work harvested from an OAI-PMH journal, enriched by OpenAlex, "
                    "with a user-contributed geometry. Private keys (original_record, "
                    "top_candidate, user_id, user_email) are absent."
                ),
                value={
                    "harvest": {
                        "harvester": "harvest_oai_endpoint",
                        "source_name": "Earth System Science Data",
                        "source_type": "oai-pmh",
                        "source_url": "https://essd.copernicus.org/oai/",
                        "harvested_at": "2026-04-30T12:00:00+00:00",
                        "harvesting_event_id": 42,
                        "doi": "10.5194/essd-16-1234-2024",
                    },
                    "metadata_sources": {
                        "authors": "openalex",
                        "author_orcids": "openalex",
                        "keywords": "original_source",
                        "topics": "openalex",
                        "geometry": "DC.SpatialCoverage",
                        "volume": "openalex",
                        "issue": "openalex",
                    },
                    "openalex_match": {
                        "status": "verified",
                        "score": 0.97,
                        "matched_id": "https://openalex.org/W2741809807",
                    },
                    "geocoding": {
                        "gazetteer": "nominatim",
                        "placename": "Sulawesi, Indonesia",
                        "n_geocoded": 2,
                        "geocoded_at": "2026-04-30T12:00:05+00:00",
                    },
                    "countries": {
                        "source": "natural_earth",
                        "method": "intersects",
                        "iso_codes": ["ID"],
                        "assigned_at": "2026-04-30T12:00:06+00:00",
                    },
                    "regions": {
                        "source": "global_regions",
                        "method": "intersects",
                        "regions": [{"name": "Asia", "region_type": "Continent"}],
                        "assigned_at": "2026-04-30T12:00:07+00:00",
                    },
                    "events": [
                        {
                            "type": "harvest_update",
                            "at": "2026-05-15T08:00:00+00:00",
                            "harvesting_event_id": 51,
                        },
                        {
                            "type": "contribution",
                            "at": "2026-05-01T09:15:00+00:00",
                            "kinds": ["spatial"],
                        },
                        {
                            "type": "publish",
                            "at": "2026-05-02T14:30:00+00:00",
                            "status_from": "c",
                            "status_to": "p",
                        },
                    ],
                    "publication_notified_at": "2026-05-02T14:30:01+00:00",
                },
                response_only=True,
                status_codes=["200"],
            ),
            OpenApiExample(
                name="MaRESS work (full response, staff/curator)",
                summary="Full provenance returned to staff or curators — includes private keys",
                description=(
                    "A work harvested from the Mountain Wetlands Repository (MaRESS API). "
                    "The geometry comes from study-site coordinates in the API record. "
                    "Includes original_record and user_id which are stripped for public callers."
                ),
                value={
                    "harvest": {
                        "harvester": "harvest_mountain_wetlands",
                        "source_name": "Mountain Wetlands Repository",
                        "source_type": "mountain-wetlands",
                        "source_url": "https://andes.mountain-wetlands-repository.info/api/v1/items/",
                        "harvested_at": "2026-03-10T06:30:00+00:00",
                        "harvesting_event_id": 17,
                        "doi": "10.5281/zenodo.7654321",
                        "original_record": {
                            "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                            "title": "Wetland extent Himalayan foothills 1990–2020",
                            "date": "2023",
                            "authors": [{"name": "Smith, J."}, {"name": "Patel, R."}],
                        },
                    },
                    "metadata_sources": {
                        "authors": "original_source",
                        "geometry": "study_sites",
                        "date": "original_source (year-only)",
                        "doi": "original_source",
                        "topics": "openalex",
                    },
                    "openalex_match": {
                        "status": "verified",
                        "score": 0.91,
                        "matched_id": "https://openalex.org/W3128445612",
                    },
                    "geocoding": {
                        "gazetteer": "nominatim",
                        "placename": "Uttarakhand, India",
                        "n_geocoded": 5,
                        "geocoded_at": "2026-03-10T06:30:10+00:00",
                    },
                    "countries": {
                        "source": "natural_earth",
                        "method": "buffer_snap",
                        "snap_tolerance_degrees": 0.12,
                        "iso_codes": ["IN"],
                        "assigned_at": "2026-03-10T06:30:11+00:00",
                    },
                    "regions": {
                        "source": "global_regions",
                        "method": "intersects",
                        "regions": [{"name": "Asia", "region_type": "Continent"}],
                        "assigned_at": "2026-03-10T06:30:12+00:00",
                    },
                    "events": [
                        {
                            "type": "contribution",
                            "at": "2026-03-12T11:00:00+00:00",
                            "kinds": ["temporal"],
                            "user_id": 7,
                            "user_email": "curator@example.org",
                        },
                        {
                            "type": "publish",
                            "at": "2026-03-13T09:00:00+00:00",
                            "status_from": "c",
                            "status_to": "p",
                            "user_id": 1,
                            "user_email": "admin@optimap.science",
                        },
                    ],
                },
                response_only=True,
                status_codes=["200"],
            ),
        ],
    )
    @action(detail=True, url_path="provenance", methods=["get"], permission_classes=[AllowAny])
    def provenance(self, request, pk=None):
        work = self.get_object()
        is_privileged = request.user.is_authenticated and (
            request.user.is_staff or work.collections.filter(curators=request.user).exists()
        )
        data = work.provenance if is_privileged else public_subset(work.provenance or {})
        response = Response(data)
        if request.user.is_authenticated:
            response["Cache-Control"] = "private, no-store"
        else:
            response["Cache-Control"] = "public, max-age=3600"
        return response

    @extend_schema(
        summary="Contribute a new work by DOI",
        tags=["Contribute"],
        description=(
            "Add a publication to OPTIMAP by its DOI. Requires authentication.\n\n"
            "The DOI may be bare (`10.5194/example`) or a resolver URL "
            "(`https://doi.org/10.5194/example`); it is normalized and validated server-side.\n\n"
            "- **If the DOI already exists**, returns `200` with `exists: true` and the existing "
            "work's `work_url` so the client can redirect to it.\n"
            "- **If the DOI is new**, it is harvested from Crossref and enriched (OpenAlex + OpenAIRE) "
            "synchronously, attached to the dedicated *User contributions* source, recorded in the "
            "work's provenance (`doi_contribution` event) and on the recognition board, then returned "
            "with `201` and `created: true`.\n\n"
            "Rate-limited per user (`contribute_doi` scope) because each new DOI triggers external "
            "API calls."
        ),
        request=ContributeDoiSerializer,
        responses={
            201: OpenApiResponse(
                inline_serializer(
                    name="ContributeDoiCreatedResponse",
                    fields={
                        "exists": drf_serializers.BooleanField(),
                        "created": drf_serializers.BooleanField(),
                        "work_id": drf_serializers.IntegerField(),
                        "doi": drf_serializers.CharField(),
                        "work_url": drf_serializers.CharField(),
                    },
                ),
                description="A new work was harvested and created.",
            ),
            200: OpenApiResponse(
                inline_serializer(
                    name="ContributeDoiExistsResponse",
                    fields={
                        "exists": drf_serializers.BooleanField(),
                        "work_id": drf_serializers.IntegerField(),
                        "doi": drf_serializers.CharField(),
                        "work_url": drf_serializers.CharField(),
                    },
                ),
                description="A work with this DOI already exists; redirect the user to it.",
            ),
            400: OpenApiResponse(_ERROR_RESPONSE, description="The DOI is missing or not syntactically valid."),
            403: OpenApiResponse(_ERROR_RESPONSE, description="Authentication credentials were not provided."),
            404: OpenApiResponse(_ERROR_RESPONSE, description="Crossref has no record for this DOI."),
            429: OpenApiResponse(_ERROR_RESPONSE, description="Rate limit exceeded for DOI contributions."),
        },
    )
    @action(
        detail=False,
        methods=["post"],
        url_path="contribute-doi",
        permission_classes=[IsAuthenticated],
        throttle_classes=[ContributeDoiThrottle],
    )
    def contribute_doi(self, request):
        from django.urls import reverse

        from .harvesting.crossref import harvest_crossref_doi
        from .models import Contribution

        def landing_url(work):
            return reverse("optimap:work-landing", args=[work.get_identifier()])

        serializer = ContributeDoiSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doi = serializer.validated_data["doi"]

        existing = Work.objects.filter(doi__iexact=doi).first()
        if existing is not None:
            return Response(
                {
                    "exists": True,
                    "work_id": existing.id,
                    "doi": existing.doi,
                    "work_url": landing_url(existing),
                },
                status=status.HTTP_200_OK,
            )

        work, action_taken = harvest_crossref_doi(doi, user=request.user)
        if action_taken == "not_found" or work is None:
            return Response(
                {"error": f"Crossref has no record for DOI '{doi}'."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if action_taken == "exists":
            # Raced with a concurrent submission, or the DOI differs only in case.
            return Response(
                {
                    "exists": True,
                    "work_id": work.id,
                    "doi": work.doi,
                    "work_url": landing_url(work),
                },
                status=status.HTTP_200_OK,
            )

        # The harvest may have auto-merged this DOI with an existing version of
        # the same work (shared OpenAlex id) — e.g. the user added a preprint DOI
        # for an article we already hold. Re-fetch and follow to the canonical row
        # so the contribution and response attach to the surviving work.
        work = Work.objects.get(pk=work.pk).canonical_work()

        # Record the contribution: provenance event + recognition-board row.
        append_event(
            work,
            "doi_contribution",
            user_id=request.user.id,
            user_email=request.user.email,
            doi=work.doi,
        )
        work.save(update_fields=["provenance", "lastUpdate"])
        Contribution.objects.create(user=request.user, work=work, kind=Contribution.DOI)
        logger.info("User %s contributed new work %s via DOI %s", request.user, work.id, work.doi)

        return Response(
            {
                "exists": False,
                "created": True,
                "work_id": work.id,
                "doi": work.doi,
                "work_url": landing_url(work),
            },
            status=status.HTTP_201_CREATED,
        )


_SUBSCRIPTION_AUTH_RESPONSES = {
    401: OpenApiResponse(_ERROR_RESPONSE, description="Authentication credentials were not provided."),
    403: OpenApiResponse(
        _ERROR_RESPONSE, description="Authenticated user is not allowed to access this subscription."
    ),
}


@extend_schema_view(
    list=extend_schema(
        summary="List the current user's subscriptions",
        tags=["Subscriptions"],
        responses={200: SubscriptionSerializer(many=True), **_SUBSCRIPTION_AUTH_RESPONSES},
    ),
    create=extend_schema(
        summary="Create a new subscription",
        tags=["Subscriptions"],
        responses={
            201: SubscriptionSerializer,
            400: OpenApiResponse(_ERROR_RESPONSE, description="Invalid payload (validation error)."),
            **_SUBSCRIPTION_AUTH_RESPONSES,
        },
    ),
    retrieve=extend_schema(
        summary="Retrieve a subscription by ID",
        tags=["Subscriptions"],
        responses={
            200: SubscriptionSerializer,
            404: OpenApiResponse(
                _ERROR_RESPONSE, description="No subscription with this ID owned by the current user."
            ),
            **_SUBSCRIPTION_AUTH_RESPONSES,
        },
    ),
    update=extend_schema(
        summary="Replace a subscription",
        tags=["Subscriptions"],
        responses={
            200: SubscriptionSerializer,
            400: OpenApiResponse(_ERROR_RESPONSE, description="Invalid payload (validation error)."),
            404: OpenApiResponse(
                _ERROR_RESPONSE, description="No subscription with this ID owned by the current user."
            ),
            **_SUBSCRIPTION_AUTH_RESPONSES,
        },
    ),
    partial_update=extend_schema(
        summary="Patch a subscription",
        tags=["Subscriptions"],
        responses={
            200: SubscriptionSerializer,
            400: OpenApiResponse(_ERROR_RESPONSE, description="Invalid payload (validation error)."),
            404: OpenApiResponse(
                _ERROR_RESPONSE, description="No subscription with this ID owned by the current user."
            ),
            **_SUBSCRIPTION_AUTH_RESPONSES,
        },
    ),
    destroy=extend_schema(
        summary="Delete a subscription",
        tags=["Subscriptions"],
        responses={
            204: OpenApiResponse(description="Subscription deleted."),
            404: OpenApiResponse(
                _ERROR_RESPONSE, description="No subscription with this ID owned by the current user."
            ),
            **_SUBSCRIPTION_AUTH_RESPONSES,
        },
    ),
)
class SubscriptionViewSet(viewsets.ModelViewSet):
    """
    Subscription view set.
    Each user can list, create, update, or delete their own Subscriptions.
    """

    bbox_filter_field = "region"
    filter_backends = (filters.InBBoxFilter,)
    serializer_class = SubscriptionSerializer
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get_queryset(self):
        user = self.request.user
        return Subscription.objects.filter(user=user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


@extend_schema_view(
    list=extend_schema(
        summary="List global regions (continents + oceans)",
        description=(
            "Continent and ocean polygons used by region-filtered feeds and subscriptions. "
            "Region slugs in this response are the ones to use in `/api/v1/feeds/optimap-<slug>.rss`."
        ),
        tags=["Global regions"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a global region by ID",
        tags=["Global regions"],
        responses={
            200: GlobalRegionSerializer,
            404: OpenApiResponse(_ERROR_RESPONSE, description="No global region with this ID."),
        },
    ),
)
class GlobalRegionViewSet(viewsets.ReadOnlyModelViewSet):
    """
    GlobalRegion view set for continent and ocean geometries.
    Returns GeoJSON FeatureCollection for use in map layers.
    Read-only - regions are loaded via management command.
    """

    queryset = GlobalRegion.objects.all().order_by("region_type", "name")
    serializer_class = GlobalRegionSerializer
    permission_classes = [AllowAny]


@extend_schema_view(
    list=extend_schema(
        summary="List countries (outline geometries)",
        description=(
            "Country outlines (simplified Natural Earth geometries) as a GeoJSON FeatureCollection, "
            "used by the toggleable countries map layer. `iso_code` is ISO 3166-1 alpha-2 and links to "
            "works via the `Work.countries` M2M; `absolute_url` links to the `/at/<country>/` landing page."
        ),
        tags=["Global regions"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a country by ID",
        tags=["Global regions"],
        responses={
            200: CountrySerializer,
            404: OpenApiResponse(_ERROR_RESPONSE, description="No country with this ID."),
        },
    ),
)
class CountryViewSet(viewsets.ReadOnlyModelViewSet):
    """Country geometries for map layers. Read-only — loaded via load_countries."""

    queryset = Country.objects.real().order_by("name")
    serializer_class = CountrySerializer
    permission_classes = [AllowAny]


@extend_schema_view(
    list=extend_schema(
        summary="List authors with published works",
        description=(
            "Returns all identity-bearing authors (with ORCID iD) who have at least one "
            "published work on OPTIMAP. Paginated. Use the `orcid` lookup field on the "
            "detail endpoint to retrieve a specific author."
        ),
        tags=["Authors"],
    ),
    retrieve=extend_schema(
        summary="Retrieve an author by ORCID iD",
        description=(
            "Look up a single author by their bare ORCID iD "
            "(e.g. `0000-0002-1825-0097`). Returns 404 if the author is not in OPTIMAP."
        ),
        tags=["Authors"],
        responses={
            200: AuthorSerializer,
            404: OpenApiResponse(_ERROR_RESPONSE, description="No author with this ORCID iD in OPTIMAP."),
        },
    ),
)
class AuthorViewSet(viewsets.ReadOnlyModelViewSet):
    """Authors with an ORCID iD who have at least one published work in OPTIMAP."""

    serializer_class = AuthorSerializer
    permission_classes = [AllowAny]
    lookup_field = "orcid"
    lookup_value_regex = r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]"

    def get_queryset(self):
        from django.db.models import Count

        return (
            Author.objects.filter(orcid__isnull=False)
            .annotate(_work_count=Count("works", filter=Q(works__status="p")))
            .filter(_work_count__gt=0)
            .order_by("name")
        )


@extend_schema_view(
    list=extend_schema(
        summary="List published collections",
        description=(
            "Returns all published collections with their work count and links to feeds and "
            "downloads. Staff additionally see unpublished collections."
        ),
        tags=["Collections"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a collection by identifier",
        description=(
            "Look up a single collection by its slug `identifier` "
            "(e.g. `mountain-wetlands`). Returns 404 for unpublished collections "
            "unless the caller is staff."
        ),
        tags=["Collections"],
        responses={
            200: CollectionSerializer,
            404: OpenApiResponse(_ERROR_RESPONSE, description="No published collection with this identifier."),
        },
    ),
)
class CollectionViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = CollectionSerializer
    permission_classes = [AllowAny]
    lookup_field = "identifier"

    def get_queryset(self):
        qs = Collection.objects.annotate(
            works_count=Count("works", filter=Q(works__status="p"), distinct=True)
        ).order_by("name")
        if self.request.user.is_staff:
            return qs
        return qs.filter(is_published=True)


@extend_schema(tags=["Geoextent"])
class GeoextentViewSet(viewsets.ViewSet):
    """
    ViewSet for extracting geospatial and temporal extents from files.

    Provides three endpoints:
    - extract: Extract from uploaded file
    - extract-remote: Extract from remote repository (Zenodo, PANGAEA, etc.)
    - extract-batch: Extract from multiple uploaded files

    Public API - no authentication required.
    """

    permission_classes = [AllowAny]
    # Each @action declares its own request= via @extend_schema; this default
    # silences drf-spectacular's "unable to guess serializer" warning on the
    # parent ViewSet class.
    serializer_class = GeoextentExtractSerializer

    def _cleanup_temp_file(self, filepath):
        """Delete temporary file safely."""
        try:
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
                logger.debug(f"Cleaned up temp file: {filepath}")
        except Exception as e:
            logger.warning(f"Failed to cleanup temp file {filepath}: {e}")

    def _save_uploaded_file(self, uploaded_file):
        """Save uploaded file to temporary location."""
        temp_dir = Path(settings.GEOEXTENT_TEMP_DIR)
        temp_dir.mkdir(exist_ok=True, parents=True)

        # Generate unique filename
        file_ext = Path(uploaded_file.name).suffix
        temp_filename = f"{uuid.uuid4()}{file_ext}"
        temp_path = temp_dir / temp_filename

        # Save file
        with open(temp_path, "wb+") as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        logger.info(f"Saved uploaded file to: {temp_path}")
        return str(temp_path)

    def _process_geoextent_result(self, result):
        """
        Process geoextent result and format for API response.
        Geoextent returns the extent information directly.
        """
        try:
            # Check if result is None or empty
            if result is None:
                logger.error("Geoextent returned None - no valid spatial data found")
                return None

            if not isinstance(result, dict):
                logger.error(f"Geoextent returned unexpected type: {type(result)}")
                return None

            response = {
                "success": True,
            }

            # Add spatial extent if present
            if "bbox" in result:
                response["spatial_extent"] = result["bbox"]

            # Add temporal extent if present
            if "tbox" in result:
                response["temporal_extent"] = result["tbox"]

            # Add placename if present (geoextent extracts this)
            if "placename" in result and result["placename"]:
                response["placename"] = result["placename"]

            # Add external metadata if present (from CrossRef/DataCite)
            if "external_metadata" in result and result["external_metadata"]:
                response["external_metadata"] = result["external_metadata"]

            # Add metadata
            response["metadata"] = {}
            if "format" in result:
                response["metadata"]["file_format"] = result["format"]
            if "crs" in result:
                response["metadata"]["crs"] = result["crs"]
            if "file_size_bytes" in result:
                response["metadata"]["file_size_bytes"] = result["file_size_bytes"]

            return response
        except Exception as e:
            logger.error(f"Error processing geoextent result: {e}")
            raise

    def _build_geoextent_extraction_metadata(self, geoextent_result, identifiers=None):
        """
        Build geoextent_extraction metadata object matching CLI output format.

        Directly copies geoextent output structure to avoid confusion between API and CLI.
        """
        import geoextent

        metadata = {
            "version": geoextent.__version__,
            "inputs": identifiers if identifiers else [],
        }

        # Directly copy statistics from extraction_metadata if available
        if "extraction_metadata" in geoextent_result:
            em = geoextent_result["extraction_metadata"]
            stats = {}
            # Copy exactly as geoextent CLI returns them
            if "total_resources" in em:
                stats["files_processed"] = em["total_resources"]
            if "successful_resources" in em:
                stats["files_with_extent"] = em["successful_resources"]
            if "total_size" in em:
                stats["total_size"] = em["total_size"]
            if stats:
                metadata["statistics"] = stats

        # Directly copy format and CRS from geoextent result
        if "format" in geoextent_result:
            metadata["format"] = geoextent_result["format"]
        if "crs" in geoextent_result:
            metadata["crs"] = geoextent_result["crs"]

        # Determine extent type
        if geoextent_result.get("convex_hull"):
            metadata["extent_type"] = "convex_hull"
        else:
            metadata["extent_type"] = "bounding_box"

        # NER provenance: pass place_names and related fields through unchanged
        for key in ("place_names", "ner_model", "ner_gazetteer", "extraction_method"):
            if geoextent_result.get(key) is not None:
                metadata[key] = geoextent_result[key]

        return metadata

    def _format_response(self, geoextent_result, structured_result, response_format, identifiers=None):
        """
        Format the response based on the requested format.

        Args:
            geoextent_result: Raw result from geoextent (dict with bbox, tbox, etc.)
            structured_result: Processed structured result from _process_geoextent_result
            response_format: One of 'geojson', 'wkt', 'wkb'
            identifiers: List of input identifiers (for metadata)

        Returns:
            Formatted response based on response_format
        """
        if response_format == "geojson":
            # Use geoextent's format_extent_output to create proper GeoJSON
            # This ensures we match CLI output exactly and don't need to manually
            # reconstruct GeoJSON from bbox
            import geoextent.lib.helpfunctions as hf

            # Build extraction metadata for geoextent's formatter
            extraction_metadata = self._build_geoextent_extraction_metadata(geoextent_result, identifiers=identifiers)

            # Use geoextent's official formatter to create GeoJSON FeatureCollection
            # This handles bbox, convex_hull, tbox, placename, external_metadata automatically
            formatted_output = hf.format_extent_output(
                geoextent_result, output_format="geojson", extraction_metadata=extraction_metadata
            )

            return formatted_output

        elif response_format in ["wkt", "wkb"]:
            # For WKT/WKB, we need to convert bbox to geometry
            if not structured_result.get("spatial_extent"):
                return {"success": False, "error": f"Cannot convert to {response_format}: no spatial extent available"}

            bbox = structured_result["spatial_extent"]

            # Handle convex hull format (list of points)
            if isinstance(bbox, list) and len(bbox) > 0 and isinstance(bbox[0], list):
                # Convex hull: list of [lon, lat] points
                if len(bbox) == 1:
                    # Single point
                    geom = Point(bbox[0][0], bbox[0][1], srid=4326)
                else:
                    # Multiple points - create polygon from points
                    points = [(point[0], point[1]) for point in bbox]
                    # Close the polygon if not already closed
                    if points[0] != points[-1]:
                        points.append(points[0])
                    geom = Polygon(points, srid=4326)

            # Handle standard bbox format [min_lon, min_lat, max_lon, max_lat]
            elif isinstance(bbox, list) and len(bbox) == 4:
                geom = Polygon.from_bbox(bbox)
                geom.srid = 4326
            else:
                return {"success": False, "error": f"Cannot convert bbox format {bbox} to {response_format}"}

            # Build geoextent_extraction metadata
            geoextent_extraction = self._build_geoextent_extraction_metadata(geoextent_result, identifiers=identifiers)

            # Create result with geometry in requested format
            if response_format == "wkt":
                result = {"wkt": geom.wkt}
            else:  # wkb
                result = {"wkb": geom.wkb.hex()}

            # Add common fields
            result["crs"] = "EPSG:4326"
            result["geoextent_extraction"] = geoextent_extraction

            # Add tbox if present (using same property name as CLI)
            if structured_result.get("temporal_extent"):
                result["tbox"] = structured_result["temporal_extent"]
            if structured_result.get("placename"):
                result["placename"] = structured_result["placename"]
            if structured_result.get("external_metadata"):
                result["external_metadata"] = structured_result["external_metadata"]

            return result

        # Default fallback
        return structured_result

    @extend_schema(
        summary="Extract spatial / temporal extent from an uploaded file",
        description=(
            "Wraps the [geoextent](https://nuest.github.io/geoextent/) Python library, "
            "which inspects supported geospatial / data file formats and returns a "
            "bounding box and (when present) a temporal extent."
        ),
        tags=["Geoextent"],
        request=GeoextentExtractSerializer,
        responses={
            200: OpenApiResponse(
                OpenApiTypes.OBJECT, description="GeoJSON / WKT / WKB extent + metadata (see `response_format`)."
            ),
            400: OpenApiResponse(
                _ERROR_RESPONSE, description="Invalid request body, unreadable file, or no spatial data extracted."
            ),
            413: OpenApiResponse(_ERROR_RESPONSE, description="File exceeds OPTIMAP_GEOEXTENT_MAX_FILE_SIZE_MB."),
            500: OpenApiResponse(_ERROR_RESPONSE, description="Processing error inside the geoextent library."),
        },
    )
    @action(detail=False, methods=["post"])
    def extract(self, request):
        """
        Extract geospatial and temporal extent from uploaded file.

        POST /api/v1/geoextent/extract/
        """
        serializer = GeoextentExtractSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        uploaded_file = serializer.validated_data["file"]
        bbox = serializer.validated_data["bbox"]
        tbox = serializer.validated_data["tbox"]
        convex_hull = serializer.validated_data["convex_hull"]
        response_format = serializer.validated_data["response_format"]
        placename = serializer.validated_data["placename"]
        gazetteer = serializer.validated_data["gazetteer"]

        temp_path = None

        try:
            # Check file size
            max_size_bytes = settings.GEOEXTENT_MAX_FILE_SIZE_MB * 1024 * 1024
            if uploaded_file.size > max_size_bytes:
                return Response(
                    {
                        "success": False,
                        "error": "File too large",
                        "details": f"File size ({uploaded_file.size} bytes) exceeds maximum ({max_size_bytes} bytes)",
                    },
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )

            # Save uploaded file
            temp_path = self._save_uploaded_file(uploaded_file)

            # Check if the file is a ZIP archive
            is_zip = zipfile.is_zipfile(temp_path)
            temp_dir = None

            if is_zip:
                # Extract ZIP to temporary directory and process with from_directory
                temp_dir = tempfile.mkdtemp(prefix="geoextent_zip_")
                logger.info(f"Extracting ZIP file to: {temp_dir}")

                with zipfile.ZipFile(temp_path, "r") as zip_ref:
                    zip_ref.extractall(temp_dir)

                # Call geoextent.from_directory on extracted contents
                geoextent_result = geoextent.from_directory(
                    temp_dir,
                    bbox=bbox,
                    tbox=tbox,
                    convex_hull=convex_hull,
                    placename=gazetteer if placename else None,
                    show_progress=False,  # Disable progress bar in API
                    recursive=True,  # Process subdirectories in ZIP
                )
            else:
                # Call geoextent once with all parameters
                # placename parameter: None, 'nominatim', 'geonames', or 'photon'
                geoextent_result = geoextent.from_file(
                    temp_path,
                    bbox=bbox,
                    tbox=tbox,
                    convex_hull=convex_hull,
                    placename=gazetteer if placename else None,
                    show_progress=False,  # Disable progress bar in API
                )

            # Process result to structured format
            structured_result = self._process_geoextent_result(geoextent_result)

            # Check if processing failed
            if structured_result is None:
                return Response(
                    {
                        "error": f'Could not extract spatial extent from "{uploaded_file.name}". The file may not contain valid spatial data or may be in an unsupported format.'
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            structured_result["filename"] = uploaded_file.name

            # Format response based on requested format
            result = self._format_response(
                geoextent_result, structured_result, response_format, identifiers=[uploaded_file.name]
            )

            return Response(result, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing file extraction: {e}", exc_info=True)
            return Response(
                {"success": False, "error": "Processing error", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        finally:
            # Cleanup temp file
            if temp_path:
                self._cleanup_temp_file(temp_path)
            # Cleanup temp directory if ZIP was extracted
            if "temp_dir" in locals() and temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Cleaned up temp directory: {temp_dir}")
                except Exception as e:
                    logger.warning(f"Failed to cleanup temp directory {temp_dir}: {e}")

    @extend_schema(
        summary="Extract spatial / temporal extent from a remote DOI or URL",
        description=(
            "Resolves the identifier(s) against supported repositories "
            "(Zenodo, PANGAEA, OSF, Figshare, Dryad, GFZ, Dataverse) and runs the "
            "[geoextent](https://nuest.github.io/geoextent/) Python library on the "
            "downloaded files. Supports a list of identifiers via JSON POST or a "
            "comma-separated `?identifiers=…` query parameter."
        ),
        tags=["Geoextent"],
        request=GeoextentRemoteSerializer,
        responses={
            200: OpenApiResponse(OpenApiTypes.OBJECT, description="Combined extent across all resolved identifiers."),
            400: OpenApiResponse(_ERROR_RESPONSE, description="Invalid identifier(s) or request body."),
            500: OpenApiResponse(_ERROR_RESPONSE, description="Resolver or extraction error."),
        },
    )
    @action(detail=False, methods=["get", "post"], url_path="extract-remote")
    def extract_remote(self, request):
        """
        Extract geospatial and temporal extent from one or more remote repositories.

        POST /api/v1/geoextent/extract-remote/ - JSON body with identifiers array
        GET /api/v1/geoextent/extract-remote/?identifiers=doi1,doi2 - URL parameters with comma-separated identifiers
        """
        # Use different serializers for GET vs POST
        if request.method == "GET":
            serializer = GeoextentRemoteGetSerializer(data=request.query_params)
        else:
            serializer = GeoextentRemoteSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        identifiers = serializer.validated_data["identifiers"]
        bbox = serializer.validated_data["bbox"]
        tbox = serializer.validated_data["tbox"]
        convex_hull = serializer.validated_data["convex_hull"]
        response_format = serializer.validated_data["response_format"]
        placename = serializer.validated_data["placename"]
        gazetteer = serializer.validated_data["gazetteer"]
        size_limit_mb = serializer.validated_data["size_limit_mb"]
        external_metadata = serializer.validated_data["external_metadata"]
        external_metadata_method = serializer.validated_data["external_metadata_method"]

        try:
            workers = settings.GEOEXTENT_DOWNLOAD_WORKERS

            # Pass identifiers as list or string to geoextent.from_remote
            # It will handle combining extents natively
            geoextent_input = identifiers[0] if len(identifiers) == 1 else identifiers

            # Call geoextent once with all identifiers
            geoextent_result = geoextent.from_remote(
                geoextent_input,
                bbox=bbox,
                tbox=tbox,
                convex_hull=convex_hull,
                details=True,  # Get individual results
                placename=gazetteer if placename else None,
                max_download_workers=workers,
                max_download_size=f"{size_limit_mb}MB" if size_limit_mb else None,
                show_progress=False,  # Disable progress bar in API
                download_skip_nogeo=True,  # Skip non-geospatial files
                ext_metadata=external_metadata,
                ext_metadata_method=external_metadata_method,
            )

            # For single identifier, geoextent returns simple format
            if len(identifiers) == 1:
                structured_result = self._process_geoextent_result(geoextent_result)

                # Check if processing failed
                if structured_result is None:
                    return Response(
                        {
                            "error": f'Could not extract spatial extent from "{identifiers[0]}". The resource may not contain valid spatial data or may be inaccessible.'
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )

                structured_result["identifier"] = identifiers[0]
                formatted_result = self._format_response(
                    geoextent_result, structured_result, response_format, identifiers=identifiers
                )
                return Response(formatted_result, status=status.HTTP_200_OK)

            # For multiple identifiers, geoextent returns remote_bulk format
            # Extract individual results from details
            individual_results = []
            if "details" in geoextent_result:
                for identifier, file_result in geoextent_result["details"].items():
                    # Check if this result has an error
                    if "error" in file_result:
                        individual_results.append(
                            {"identifier": identifier, "success": False, "error": file_result["error"]}
                        )
                        continue

                    structured_result = self._process_geoextent_result(file_result)
                    structured_result["identifier"] = identifier

                    # Format based on response_format
                    formatted_result = self._format_response(
                        file_result, structured_result, response_format, identifiers=[identifier]
                    )
                    if response_format not in ["geojson", "wkt", "wkb"]:
                        formatted_result["identifier"] = identifier

                    individual_results.append(formatted_result)

            # Build response with combined extent (geoextent always combines)
            combined_structured = self._process_geoextent_result(geoextent_result)
            combined_formatted = self._format_response(
                geoextent_result, combined_structured, response_format, identifiers=identifiers
            )

            # For multiple identifiers, return structured response with combined + individual
            # For GeoJSON format, return FeatureCollection with all features
            if response_format == "geojson":
                # Merge all features into single FeatureCollection
                all_features = []
                if isinstance(combined_formatted, dict) and "features" in combined_formatted:
                    all_features = combined_formatted["features"].copy()

                # Add individual features
                for result in individual_results:
                    if isinstance(result, dict) and "features" in result:
                        all_features.extend(result["features"])

                response_data = {
                    "type": "FeatureCollection",
                    "features": all_features,
                    "geoextent_extraction": combined_formatted.get("geoextent_extraction", {}),
                }
            else:
                # For WKT/WKB, return combined with metadata
                response_data = combined_formatted

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing remote extraction: {e}", exc_info=True)
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @extend_schema(
        summary="Extract a combined spatial / temporal extent from multiple uploaded files",
        description=(
            "Wraps the [geoextent](https://nuest.github.io/geoextent/) Python library, "
            "running it across every uploaded file and merging the extents."
        ),
        tags=["Geoextent"],
        request=GeoextentBatchSerializer,
        responses={
            200: OpenApiResponse(
                OpenApiTypes.OBJECT, description="Combined extent across all uploaded files plus per-file features."
            ),
            400: OpenApiResponse(
                _ERROR_RESPONSE, description="Invalid request body or no spatial data extracted from any file."
            ),
            413: OpenApiResponse(
                _ERROR_RESPONSE, description="Total upload size exceeds OPTIMAP_GEOEXTENT_MAX_BATCH_SIZE_MB."
            ),
            500: OpenApiResponse(_ERROR_RESPONSE, description="Processing error inside the geoextent library."),
        },
    )
    @action(detail=False, methods=["post"], url_path="extract-batch")
    def extract_batch(self, request):
        """
        Extract geospatial and temporal extent from multiple uploaded files.

        POST /api/v1/geoextent/extract-batch/
        """
        serializer = GeoextentBatchSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        bbox = serializer.validated_data["bbox"]
        tbox = serializer.validated_data["tbox"]
        convex_hull = serializer.validated_data["convex_hull"]
        response_format = serializer.validated_data["response_format"]
        placename = serializer.validated_data["placename"]
        gazetteer = serializer.validated_data["gazetteer"]
        size_limit_mb = serializer.validated_data["size_limit_mb"]

        # Get uploaded files from request
        files = request.FILES.getlist("files")
        if not files:
            return Response(
                {"success": False, "error": "No files provided", "details": "At least one file must be uploaded"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        temp_dir = None

        try:
            # Check total size
            total_size = sum(f.size for f in files)
            max_size_bytes = size_limit_mb * 1024 * 1024
            if total_size > max_size_bytes:
                return Response(
                    {
                        "success": False,
                        "error": "Total size exceeds limit",
                        "details": f"Total size ({total_size} bytes) exceeds limit ({max_size_bytes} bytes)",
                    },
                    status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                )

            # Create a temporary directory for all uploaded files
            temp_dir = tempfile.mkdtemp(prefix="geoextent_batch_")
            logger.info(f"Created temp directory for batch processing: {temp_dir}")

            # Save all files to the temporary directory
            for uploaded_file in files:
                temp_path = os.path.join(temp_dir, uploaded_file.name)
                with open(temp_path, "wb") as destination:
                    for chunk in uploaded_file.chunks():
                        destination.write(chunk)
                logger.debug(f"Saved {uploaded_file.name} to {temp_path}")

            # Use geoextent.from_directory to process all files at once
            # details=True provides individual file results
            geoextent_result = geoextent.from_directory(
                temp_dir,
                bbox=bbox,
                tbox=tbox,
                convex_hull=convex_hull,
                details=True,  # Get individual file details
                placename=gazetteer if placename else None,
                show_progress=False,  # Disable progress bar in API
                recursive=False,  # Don't traverse subdirectories
            )

            # Process combined result
            combined_structured = self._process_geoextent_result(geoextent_result)

            # Check if processing failed for combined result
            if combined_structured is None:
                filenames = ", ".join([f.name for f in files])
                return Response(
                    {
                        "success": False,
                        "error": f"Could not extract spatial extent from the uploaded files: {filenames}",
                        "details": "The files may not contain valid spatial data or may be in unsupported formats.",
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Process individual file results from details
            individual_results = []
            if "details" in geoextent_result:
                for filename, file_result in geoextent_result["details"].items():
                    structured_result = self._process_geoextent_result(file_result)

                    # Skip files that failed processing
                    if structured_result is None:
                        logger.warning(f"Could not extract extent from {filename}")
                        individual_results.append(
                            {
                                "filename": filename,
                                "error": "Could not extract spatial extent",
                                "details": "The file may not contain valid spatial data or may be in an unsupported format.",
                            }
                        )
                        continue

                    structured_result["filename"] = filename

                    # Format based on response_format
                    formatted_result = self._format_response(
                        file_result, structured_result, response_format, identifiers=[filename]
                    )
                    if response_format not in ["geojson", "wkt", "wkb"]:
                        formatted_result["filename"] = filename

                    individual_results.append(formatted_result)

            # Build response with combined extent (geoextent always combines)
            filenames = [f.name for f in files]
            combined_formatted = self._format_response(
                geoextent_result, combined_structured, response_format, identifiers=filenames
            )

            response_data = {
                "success": True,
                "files_processed": len(files),
                "combined_extent": combined_formatted,
                "individual_results": individual_results,
            }

            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error processing batch extraction: {e}", exc_info=True)
            return Response(
                {"success": False, "error": "Processing error", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        finally:
            # Cleanup temporary directory and all files
            if temp_dir and os.path.exists(temp_dir):
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"Cleaned up temp directory: {temp_dir}")
                except Exception as e:
                    logger.error(f"Error cleaning up temp directory {temp_dir}: {e}")

    @extend_schema(
        summary="Extract place names and spatial extent from free text via NER",
        description=(
            "Runs spaCy Named Entity Recognition on the provided text string, then "
            "forward-geocodes the detected place names via the configured gazetteer "
            "(Nominatim by default) to produce a bounding box and a `place_names` "
            "provenance list. Each entry in `place_names` carries the original text "
            "span, character offsets (`char_start`/`char_end`) for client-side "
            "highlighting, matched coordinates, and the gazetteer result URL.\n\n"
            "The spaCy model is downloaded automatically on first use (~12 MB). "
            "Subsequent calls reuse the cached model without network access."
        ),
        tags=["Geoextent"],
        request=GeoextentExtractTextSerializer,
        responses={
            200: OpenApiResponse(
                OpenApiTypes.OBJECT,
                description=(
                    "GeoJSON FeatureCollection for the combined spatial extent, with "
                    "`geoextent_extraction.place_names` listing all detected entities "
                    "(matched, ambiguous, and unresolved)."
                ),
            ),
            400: OpenApiResponse(_ERROR_RESPONSE, description="Empty text or invalid parameters."),
            500: OpenApiResponse(_ERROR_RESPONSE, description="NER or gazetteer error."),
        },
    )
    @action(detail=False, methods=["post"], url_path="extract-text")
    def extract_text(self, request):
        """Extract place names and spatial extent from a free text string via NER."""
        serializer = GeoextentExtractTextSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        text = serializer.validated_data["text"]
        gazetteer = serializer.validated_data["gazetteer"]
        ner_ambiguity = serializer.validated_data["ner_ambiguity"]
        tbox = serializer.validated_data["tbox"]
        convex_hull = serializer.validated_data["convex_hull"]

        ner_model = getattr(settings, "GEOEXTENT_NER_MODEL", None) or None

        try:
            geoextent_result = geoextent.from_text(
                text,
                bbox=True,
                tbox=tbox,
                convex_hull=convex_hull,
                ner_gazetteer=gazetteer,
                ner_ambiguity=ner_ambiguity,
                ner_auto_download=True,
                include_source_text=False,
                ner_model=ner_model,
            )

            if geoextent_result is None:
                # No entities found — return empty FeatureCollection with metadata
                import geoextent as _ge

                return Response(
                    {
                        "type": "FeatureCollection",
                        "features": [],
                        "geoextent_extraction": {
                            "version": _ge.__version__,
                            "format": "text",
                            "ner_gazetteer": gazetteer,
                            "place_names": [],
                        },
                    },
                    status=status.HTTP_200_OK,
                )

            structured_result = self._process_geoextent_result(geoextent_result)

            # Build the base metadata — always include NER provenance and tbox.
            import geoextent as _ge

            extraction_meta = {
                "version": _ge.__version__,
                "format": geoextent_result.get("format", "text"),
                "ner_model": geoextent_result.get("ner_model"),
                "ner_gazetteer": geoextent_result.get("ner_gazetteer", gazetteer),
                "place_names": geoextent_result.get("place_names", []),
            }
            if geoextent_result.get("tbox"):
                extraction_meta["tbox"] = geoextent_result["tbox"]

            if structured_result is None or not structured_result.get("spatial_extent"):
                # NER ran but no places were resolved to coordinates.
                return Response(
                    {
                        "type": "FeatureCollection",
                        "features": [],
                        "geoextent_extraction": extraction_meta,
                    },
                    status=status.HTTP_200_OK,
                )

            formatted = self._format_response(
                geoextent_result,
                structured_result,
                "geojson",
                identifiers=["text"],
            )
            # Ensure place_names and tbox are always present in geoextent_extraction
            # even when _format_response uses geoextent's own formatter.
            if "geoextent_extraction" in formatted:
                ge_meta = formatted["geoextent_extraction"]
                if "place_names" not in ge_meta:
                    ge_meta["place_names"] = geoextent_result.get("place_names", [])
                if "tbox" not in ge_meta and geoextent_result.get("tbox"):
                    ge_meta["tbox"] = geoextent_result["tbox"]
            return Response(formatted, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error in NER text extraction: {e}", exc_info=True)
            return Response(
                {"error": "NER extraction error", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


@extend_schema_view(
    list=extend_schema(
        summary="Site statistics (cached, 24 h TTL)",
        description=(
            "Returns aggregate counts for the OPTIMAP database. "
            "Results are cached for 24 hours.\n\n"
            "`total_works_for_user` equals `published_works` for anonymous and non-staff users, "
            "and `total_works` for staff — matching the total that `/api/v1/works/` returns for "
            "the caller. Use it to drive a loading progress indicator."
        ),
        tags=["Statistics"],
        responses={
            200: OpenApiResponse(
                description="Aggregate counts.",
                response=inline_serializer(
                    name="StatisticsResponse",
                    fields={
                        "total_works": drf_serializers.IntegerField(
                            help_text="Total works in the database across all statuses.",
                        ),
                        "total_works_for_user": drf_serializers.IntegerField(
                            help_text=(
                                "Works visible to the current caller: published_works for "
                                "anonymous/non-staff, total_works for staff."
                            ),
                        ),
                        "published_works": drf_serializers.IntegerField(),
                        "with_geometry": drf_serializers.IntegerField(),
                        "with_temporal": drf_serializers.IntegerField(),
                        "with_authors": drf_serializers.IntegerField(),
                        "with_doi": drf_serializers.IntegerField(),
                        "with_abstract": drf_serializers.IntegerField(),
                        "open_access": drf_serializers.IntegerField(),
                        "from_openalex": drf_serializers.IntegerField(),
                        "contributed_dois": drf_serializers.IntegerField(
                            help_text="Works submitted by users via the contribute-by-DOI form.",
                        ),
                        "with_complete_metadata": drf_serializers.IntegerField(),
                        "complete_percentage": drf_serializers.FloatField(),
                        "works_by_status": drf_serializers.DictField(
                            child=drf_serializers.IntegerField(),
                            help_text="Count per status code: p=published, h=harvested, c=contributed, d=draft, t=testing, w=withdrawn.",
                        ),
                        "sources": drf_serializers.IntegerField(),
                        "collections": drf_serializers.IntegerField(),
                        "users": drf_serializers.IntegerField(),
                    },
                ),
            ),
        },
    ),
)
class StatisticsViewSet(viewsets.ViewSet):
    """Read-only viewset exposing cached site-wide statistics."""

    permission_classes = [AllowAny]

    def list(self, request):
        from works.models import StatisticsSnapshot
        from works.utils.statistics import get_cached_statistics

        try:
            snapshot = StatisticsSnapshot.objects.latest()
        except StatisticsSnapshot.DoesNotExist:
            snapshot = None

        stats = get_cached_statistics()
        is_staff = request.user.is_authenticated and request.user.is_staff
        total_for_user = stats.get("total_works", 0) if is_staff else stats.get("published_works", 0)

        return Response(
            {
                **stats,
                "total_works_for_user": total_for_user,
                "computed_at": snapshot.computed_at if snapshot else None,
                "next_update": snapshot.next_update if snapshot else None,
            }
        )

    @extend_schema(
        summary="Schedule a one-time statistics recomputation (staff only)",
        description=(
            "Enqueues a background Django-Q job that recomputes the statistics snapshot "
            "and refreshes the cache, then returns immediately. The updated numbers become "
            "available from `GET /api/v1/statistics/` once the worker finishes — this "
            "endpoint does **not** compute synchronously. Requires staff authentication "
            "and the Django-Q cluster to be running."
        ),
        tags=["Statistics"],
        request=None,
        responses={
            202: OpenApiResponse(
                description="Recomputation scheduled.",
                response=inline_serializer(
                    name="StatisticsRecomputeResponse",
                    fields={
                        "scheduled": drf_serializers.BooleanField(),
                        "task_id": drf_serializers.CharField(help_text="Django-Q task id (hex digest)."),
                        "task_name": drf_serializers.CharField(
                            help_text="Human-readable Django-Q task name, e.g. 'ceiling-mississippi-gee-delta'."
                        ),
                    },
                ),
            ),
            403: OpenApiResponse(_ERROR_RESPONSE, description="Staff authentication required."),
        },
    )
    @action(detail=False, methods=["post"], url_path="recompute")
    def recompute(self, request):
        if not (request.user.is_authenticated and request.user.is_staff):
            return Response(
                {"detail": "Recomputation requires staff authentication."},
                status=status.HTTP_403_FORBIDDEN,
            )
        task_id = async_task("works.tasks.recompute_statistics_snapshot")
        task_name = humanize(task_id)
        logger.info("User %s scheduled statistics recomputation (task %s)", request.user, task_name)
        return Response(
            {"scheduled": True, "task_id": task_id, "task_name": task_name},
            status=status.HTTP_202_ACCEPTED,
        )
