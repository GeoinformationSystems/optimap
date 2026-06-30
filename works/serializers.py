# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

"""publications serializers."""

import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from drf_spectacular.utils import extend_schema_field, inline_serializer
from rest_framework import serializers
from rest_framework import serializers as drf_serializers
from rest_framework_gis.fields import GeoJsonDict
from rest_framework_gis.fields import GeometryField as _BaseGeometryField
from rest_framework_gis.serializers import GeoFeatureModelSerializer

from .models import Author, Collection, Country, GlobalRegion, Source, Subscription, Work
from .utils.geometry import COORDINATE_PRECISION, round_geojson_coordinates

User = get_user_model()


class _GeometryWithPrecomputedJSON:
    """Wraps a real GEOSGeometry together with a DB-precomputed, already
    rounded GeoJSON string, so a custom field can hand back the precomputed
    text while ``GeoFeatureModelSerializer``'s ``auto_bbox`` logic (which
    needs ``.extent`` and truthiness on whatever ``get_attribute`` returns)
    keeps working unmodified."""

    __slots__ = ("geometry", "rounded_geojson")

    def __init__(self, geometry, rounded_geojson):
        self.geometry = geometry
        self.rounded_geojson = rounded_geojson

    def __bool__(self):
        return bool(self.geometry)

    @property
    def extent(self):
        return self.geometry.extent


class PrecomputedGeometryField(_BaseGeometryField):
    """GeometryField that prefers a DB-precomputed, already-rounded GeoJSON
    string (``instance._rounded_geojson``, produced by
    ``works.utils.geometry.annotate_rounded_geometry``) over re-serializing
    the GEOSGeometry in Python — PostGIS does the rounding during
    serialization instead of Python re-parsing/rounding/re-dumping the
    full-precision GeoJSON (~30x faster for complex geometries).

    Falls back to the original GEOS-based rounding for any instance not
    sourced from an annotated queryset (e.g. ad hoc instances in tests)."""

    def get_attribute(self, instance):
        return _GeometryWithPrecomputedJSON(instance.geometry, getattr(instance, "_rounded_geojson", None))

    def to_representation(self, value):
        geometry = value.geometry
        if geometry is None:
            return None
        if value.rounded_geojson is not None:
            return GeoJsonDict(value.rounded_geojson)
        if geometry.geojson:
            return GeoJsonDict(round_geojson_coordinates(json.loads(geometry.geojson)))
        return GeoJsonDict({"type": geometry.geom_type, "coordinates": []})


class SourceSerializer(serializers.ModelSerializer):
    openalex_url = serializers.ReadOnlyField()
    source_type_display = serializers.CharField(source="get_source_type_display", read_only=True)
    source_url = serializers.SerializerMethodField(
        help_text="Absolute URL to this source's entry in the OPTIMAP API.",
    )
    collection = serializers.SerializerMethodField(
        help_text="Default collection for works harvested from this source: identifier, name, and absolute API URL. Null if no collection is set.",
    )
    latest_coverage = serializers.SerializerMethodField(
        help_text="Most recent SourceCoverageSnapshot: optimap_count, openalex_total, coverage_pct, computed_at. Null if no snapshot exists.",
    )
    landing_page_url = serializers.SerializerMethodField(
        help_text="Absolute URL of the public source landing page (/in/<slug>/). Null if the source has no slug.",
    )
    feed_georss_url = serializers.SerializerMethodField(
        help_text="Absolute URL of this source's GeoRSS feed. Null if the source has no slug.",
    )
    feed_atom_url = serializers.SerializerMethodField(
        help_text="Absolute URL of this source's GeoAtom feed. Null if the source has no slug.",
    )

    class Meta:
        model = Source
        fields = (
            "id",
            "name",
            "slug",
            "issn_l",
            "openalex_id",
            "openalex_url",
            "publisher_name",
            "works_count",
            "works_api_url",
            "default_work_type",
            "source_type",
            "source_type_display",
            "homepage_url",
            "abbreviated_title",
            "is_oa",
            "is_preprint",
            "source_url",
            "landing_page_url",
            "feed_georss_url",
            "feed_atom_url",
            "collection",
            "latest_coverage",
        )

    def _abs(self, path):
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_landing_page_url(self, obj):
        return self._abs(obj.get_absolute_url()) if obj.slug else None

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_feed_georss_url(self, obj):
        if not obj.slug:
            return None
        return self._abs(reverse("optimap:api-source-georss", kwargs={"source_slug": obj.slug}))

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_feed_atom_url(self, obj):
        if not obj.slug:
            return None
        return self._abs(reverse("optimap:api-source-atom", kwargs={"source_slug": obj.slug}))

    @extend_schema_field(serializers.URLField())
    def get_source_url(self, obj):
        request = self.context.get("request")
        path = f"/api/v1/sources/{obj.pk}/"
        return request.build_absolute_uri(path) if request else path

    @extend_schema_field(
        inline_serializer(
            name="SourceCollectionRef",
            fields={
                "identifier": serializers.CharField(),
                "name": serializers.CharField(),
                "collection_url": serializers.URLField(),
            },
            allow_null=True,
        )
    )
    def get_collection(self, obj):
        if not obj.collection_id:
            return None
        coll = obj.collection
        request = self.context.get("request")
        path = f"/api/v1/collections/{coll.identifier}/"
        coll_url = request.build_absolute_uri(path) if request else path
        return {
            "identifier": coll.identifier,
            "name": coll.name,
            "collection_url": coll_url,
        }

    @extend_schema_field(
        inline_serializer(
            name="SourceCoverageRef",
            fields={
                "optimap_count": serializers.IntegerField(),
                "openalex_total": serializers.IntegerField(allow_null=True),
                "coverage_pct": serializers.FloatField(allow_null=True),
                "spatial_rate": serializers.FloatField(allow_null=True),
                "temporal_rate": serializers.FloatField(allow_null=True),
                "open_access_ratio": serializers.FloatField(allow_null=True),
                "contributors_count": serializers.IntegerField(),
                "by_year": serializers.ListField(child=serializers.DictField()),
                "computed_at": serializers.DateTimeField(),
            },
            allow_null=True,
        )
    )
    def get_latest_coverage(self, obj):
        snap = obj.latest_coverage()
        return snap.as_summary() if snap is not None else None


class AuthorSerializer(serializers.ModelSerializer):
    """Serializer for :class:`works.models.Author` — ORCID-keyed author identity."""

    url = serializers.SerializerMethodField(
        help_text="OPTIMAP /by/<orcid>/ permalink for this author (null when orcid is missing)."
    )
    work_count = serializers.SerializerMethodField(
        help_text="Number of published works associated with this author on OPTIMAP."
    )

    class Meta:
        model = Author
        fields = ["id", "orcid", "openalex_id", "name", "url", "work_count"]

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_url(self, obj):
        if obj.orcid:
            from django.urls import reverse

            return reverse("optimap:by-author", kwargs={"orcid": obj.orcid})
        return None

    @extend_schema_field(serializers.IntegerField())
    def get_work_count(self, obj):
        # Annotated by the viewset queryset; fall back to a live count.
        if hasattr(obj, "_work_count"):
            return obj._work_count
        return obj.works.filter(status="p").count()


class WorkSerializer(GeoFeatureModelSerializer):
    geometry = PrecomputedGeometryField()
    source_details = serializers.SerializerMethodField(
        help_text="Embedded source row (same shape as `/api/v1/sources/<id>/`)."
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    bok_concepts_resolved = serializers.SerializerMethodField(
        help_text="Each BoK code resolved against the active EO4GEO BoK snapshot: code, name, uri, parent_code, breadcrumb, orphan flag."
    )
    country_codes = serializers.SerializerMethodField(
        help_text="ISO 3166-1 alpha-2 codes of the countries the geometry intersects "
        "(offline point-in-polygon join; multi-valued for transboundary works)."
    )
    region_names = serializers.SerializerMethodField(
        help_text="Names of the global regions (continents and oceans) the geometry "
        "intersects (offline point-in-polygon join; multi-valued)."
    )
    author_orcids = serializers.SerializerMethodField(
        help_text="Identity-bearing authors of this work with their ORCID iDs and OPTIMAP "
        "permalink URLs. Authors without an ORCID are omitted here (their display names are "
        "still in the `authors` field). Ordered by authorship rank."
    )

    class Meta:
        model = Work
        geo_field = "geometry"
        auto_bbox = True
        fields = [
            "id",
            "title",
            "type",
            "abstract",
            "publicationDate",
            "doi",
            "url",
            "timeperiod_startdate",
            "timeperiod_enddate",
            "placename",
            "country_codes",
            "region_names",
            "source_details",
            "status",
            "status_display",
            "authors",
            "author_orcids",
            "keywords",
            "topics",
            "bok_concepts",
            "bok_concepts_resolved",
            "openalex_id",
            "openalex_match_info",
            "openalex_fulltext_origin",
            "openalex_is_retracted",
            "openalex_ids",
            "openalex_open_access_status",
            "locations",
        ]

    locations = serializers.JSONField(
        read_only=True,
        help_text=(
            "Hosting copies/versions of this work (journal version, preprint, repository "
            "copies). Each entry carries a `credit` field crediting the data source "
            "(currently always `openalex`; OpenAlex data is CC0)."
        ),
    )

    def to_representation(self, instance):
        feature = super().to_representation(instance)
        # auto_bbox computes the bbox from the real (full-precision) geometry's
        # .extent — round it to match the geometry coordinates, which the
        # PrecomputedGeometryField already serves pre-rounded from the DB.
        bbox = feature.get("bbox")
        if bbox is not None:
            feature["bbox"] = tuple(round(v, COORDINATE_PRECISION) for v in bbox)
        return feature

    @extend_schema_field(SourceSerializer)
    def get_source_details(self, obj):
        source = obj.source
        if not source:
            return {}
        return SourceSerializer(source, context=self.context).data

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_country_codes(self, obj):
        return obj.country_codes

    @extend_schema_field(serializers.ListField(child=serializers.CharField()))
    def get_region_names(self, obj):
        return obj.region_names

    @extend_schema_field(
        serializers.ListField(
            child=inline_serializer(
                "AuthorOrcidEntry",
                fields={
                    "name": drf_serializers.CharField(),
                    "orcid": drf_serializers.CharField(),
                    "url": drf_serializers.URLField(),
                },
            )
        )
    )
    def get_author_orcids(self, obj):
        from django.urls import reverse

        try:
            wa_qs = obj.work_authors.select_related("author").order_by("rank")
            return [
                {
                    "name": wa.author.name,
                    "orcid": wa.author.orcid,
                    "url": reverse("optimap:by-author", kwargs={"orcid": wa.author.orcid})
                    if wa.author.orcid
                    else None,
                }
                for wa in wa_qs
                if wa.author.orcid
            ]
        except Exception:
            return []

    @extend_schema_field(serializers.ListField(child=serializers.DictField()))
    def get_bok_concepts_resolved(self, obj):
        codes = obj.bok_concepts or []
        if not codes:
            return []
        # Late import to avoid circular dependency at module load.
        from works.bok import client as bok_client

        try:
            return bok_client.resolve(codes)
        except Exception:
            # If the BoK snapshot is unreachable, fall back to bare codes
            # so the API stays responsive.
            return [
                {"code": c, "name": c, "uri": "", "parent_code": "", "breadcrumb": [], "orphan": True} for c in codes
            ]


class WorkMinimalSerializer(GeoFeatureModelSerializer):
    """Slim serializer for chunked map loading — geometry + identifiers only."""

    geometry = PrecomputedGeometryField()
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Work
        geo_field = "geometry"
        auto_bbox = False
        fields = ["id", "title", "doi", "status", "status_display"]


class SubscriptionSerializer(GeoFeatureModelSerializer):
    class Meta:
        model = Subscription
        fields = (
            "id",
            "user",
            "name",
            "search_term",
            "timeperiod_startdate",
            "timeperiod_enddate",
            "region",
            "subscribed",
        )

        geo_field = "region"
        auto_bbox = True


class EmailChangeSerializer(serializers.ModelSerializer):
    """Handles email change requests."""

    class Meta:
        model = User
        fields = ["email"]

    def validate_email(self, value):
        """Ensure the new email is not already in use."""
        value = value.lower().strip()
        if User.objects.filter(email=value).exists():
            raise drf_serializers.ValidationError("This email is already registered.")
        return value


class UserSerializer(drf_serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]


# Geoextent API Serializers

# Shared field definitions
RESPONSE_FORMAT_CHOICES = ["geojson", "wkt", "wkb"]
RESPONSE_FORMAT_DEFAULT = "geojson"
RESPONSE_FORMAT_HELP = "Response format: 'geojson' (default - GeoJSON FeatureCollection), 'wkt' (WKT string with metadata), 'wkb' (WKB hex string with metadata)"

# All gazetteers the library supports.
GAZETTEER_CHOICES = ["nominatim", "geonames", "photon"]
GAZETTEER_DEFAULT = "nominatim"


def get_available_gazetteers():
    """Return gazetteers that are usable with the current configuration.

    GeoNames requires OPTIMAP_GEOEXTENT_GEONAMES_USERNAME to be set; if it is
    absent or empty the library raises ValueError at call time, so we exclude
    it here to give users a clean 400 / empty dropdown instead of a 500.
    """
    available = ["nominatim", "photon"]
    if getattr(settings, "GEOEXTENT_GEONAMES_USERNAME", ""):
        available.append("geonames")
    return available


class GeoextentBaseSerializer(serializers.Serializer):
    """Base serializer with common geoextent parameters."""

    bbox = serializers.BooleanField(default=True)
    tbox = serializers.BooleanField(default=True)
    convex_hull = serializers.BooleanField(default=False)
    response_format = serializers.ChoiceField(
        choices=RESPONSE_FORMAT_CHOICES, default=RESPONSE_FORMAT_DEFAULT, help_text=RESPONSE_FORMAT_HELP
    )
    placename = serializers.BooleanField(default=False)
    gazetteer = serializers.ChoiceField(choices=GAZETTEER_CHOICES, default=GAZETTEER_DEFAULT)
    external_metadata = serializers.BooleanField(
        default=True,
        help_text="Retrieve external metadata from CrossRef/DataCite for DOIs (only applies to remote resources)",
    )
    external_metadata_method = serializers.ChoiceField(
        choices=["auto", "all", "crossref", "datacite"],
        default="auto",
        help_text="Method for retrieving metadata: 'auto' (default), 'all', 'crossref', or 'datacite'",
    )

    def validate_gazetteer(self, value):
        """Reject unconfigured gazetteers before they reach the library."""
        available = get_available_gazetteers()
        if value not in available:
            raise serializers.ValidationError(
                f"Gazetteer '{value}' is not available. "
                f"Available options: {', '.join(available)}. "
                f"GeoNames requires OPTIMAP_GEOEXTENT_GEONAMES_USERNAME to be configured."
            )
        return value


class GeoextentExtractSerializer(GeoextentBaseSerializer):
    """Serializer for extracting extent from uploaded file."""

    file = serializers.FileField(required=True)


class GeoextentRemoteSerializer(GeoextentBaseSerializer):
    """Serializer for extracting extent from remote repository."""

    identifiers = serializers.ListField(
        child=serializers.CharField(), required=True, min_length=1, help_text="List of DOIs or repository URLs"
    )
    file_limit = serializers.IntegerField(default=10, min_value=1, max_value=100)
    size_limit_mb = serializers.IntegerField(default=100, min_value=1)

    def validate_size_limit_mb(self, value):
        """Ensure requested size doesn't exceed server maximum."""
        max_allowed = getattr(settings, "GEOEXTENT_MAX_DOWNLOAD_SIZE_MB", 1000)
        if value > max_allowed:
            raise serializers.ValidationError(
                f"Requested size limit ({value}MB) exceeds server maximum ({max_allowed}MB)"
            )
        return value


class GeoextentRemoteGetSerializer(GeoextentBaseSerializer):
    """Serializer for GET endpoint with URL parameters."""

    identifiers = serializers.CharField(required=True, help_text="Comma-separated DOIs or repository URLs")
    file_limit = serializers.IntegerField(default=10, min_value=1, max_value=100)
    size_limit_mb = serializers.IntegerField(default=100, min_value=1)

    def validate_identifiers(self, value):
        """Parse comma-separated identifiers and validate."""
        identifiers = [i.strip() for i in value.split(",") if i.strip()]
        if not identifiers:
            raise serializers.ValidationError("At least one identifier must be provided")
        return identifiers

    def validate_size_limit_mb(self, value):
        """Ensure requested size doesn't exceed server maximum."""
        max_allowed = getattr(settings, "GEOEXTENT_MAX_DOWNLOAD_SIZE_MB", 1000)
        if value > max_allowed:
            raise serializers.ValidationError(
                f"Requested size limit ({value}MB) exceeds server maximum ({max_allowed}MB)"
            )
        return value


class GeoextentBatchSerializer(GeoextentBaseSerializer):
    """Serializer for extracting extent from multiple files."""

    # files handled separately in view
    size_limit_mb = serializers.IntegerField(default=100, min_value=1)

    def validate_size_limit_mb(self, value):
        """Ensure total batch size doesn't exceed server maximum."""
        max_allowed = getattr(settings, "GEOEXTENT_MAX_BATCH_SIZE_MB", 500)
        if value > max_allowed:
            raise serializers.ValidationError(
                f"Requested batch size ({value}MB) exceeds server maximum ({max_allowed}MB)"
            )
        return value


class GeoextentExtractTextSerializer(serializers.Serializer):
    """Serializer for NER-based location extraction from free text."""

    text = serializers.CharField(
        required=True,
        max_length=20000,
        help_text="Free text (title, abstract, etc.) to extract place names from.",
    )
    gazetteer = serializers.ChoiceField(
        choices=GAZETTEER_CHOICES,
        default=None,
        allow_null=True,
        help_text="Gazetteer service for geocoding found place names. Defaults to OPTIMAP_GEOEXTENT_NER_GAZETTEER.",
    )
    ner_ambiguity = serializers.ChoiceField(
        choices=["drop", "top"],
        default="drop",
        help_text=(
            "'drop' (default): skip place names that match multiple gazetteer candidates. "
            "'top': keep the highest-ranked candidate when multiple are returned."
        ),
    )
    tbox = serializers.BooleanField(
        default=True,
        help_text="Also extract temporal extent from date mentions in the text (enabled by default).",
    )
    convex_hull = serializers.BooleanField(
        default=False,
        help_text="Return convex hull of all matched places instead of bounding box.",
    )

    def validate_gazetteer(self, value):
        if value is None:
            value = getattr(settings, "GEOEXTENT_NER_GAZETTEER", "nominatim")
        available = get_available_gazetteers()
        if value not in available:
            raise serializers.ValidationError(
                f"Gazetteer '{value}' is not available. "
                f"Available options: {', '.join(available)}. "
                f"GeoNames requires OPTIMAP_GEOEXTENT_GEONAMES_USERNAME to be configured."
            )
        return value

    def validate_text(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Text must not be empty.")
        return value


class ContributeDoiSerializer(serializers.Serializer):
    """Validate a user-submitted DOI (bare or as a doi.org URL).

    ``validate_doi`` normalizes the input to a bare DOI via
    ``works.utils.identifiers.normalize_doi`` and rejects anything that is not a
    syntactically valid DOI, so the view only ever sees a clean identifier.
    """

    doi = serializers.CharField(
        required=True,
        max_length=500,
        help_text="A DOI (e.g. 10.5194/example) or DOI URL (e.g. https://doi.org/10.5194/example).",
    )

    def validate_doi(self, value):
        from .utils.identifiers import normalize_doi

        normalized = normalize_doi(value)
        if not normalized:
            raise serializers.ValidationError("Not a valid DOI.")
        return normalized


class GlobalRegionSerializer(GeoFeatureModelSerializer):
    """Serializer for GlobalRegion model with GeoJSON output."""

    region_type_display = serializers.CharField(source="get_region_type_display", read_only=True)
    slug = serializers.CharField(source="get_slug", read_only=True)
    absolute_url = serializers.CharField(source="get_absolute_url", read_only=True)

    class Meta:
        model = GlobalRegion
        geo_field = "geom"
        auto_bbox = True
        fields = [
            "id",
            "name",
            "region_type",
            "region_type_display",
            "slug",
            "absolute_url",
            "source_url",
            "license",
        ]


class CountrySerializer(GeoFeatureModelSerializer):
    """Country outline as a GeoJSON feature for the countries map layer."""

    slug = serializers.CharField(source="get_slug", read_only=True)
    absolute_url = serializers.CharField(source="get_absolute_url", read_only=True)

    class Meta:
        model = Country
        geo_field = "geom"
        auto_bbox = True
        fields = [
            "id",
            "name",
            "iso_code",
            "slug",
            "absolute_url",
        ]


class CollectionSerializer(serializers.ModelSerializer):
    works_count = serializers.IntegerField(
        read_only=True,
        help_text="Number of published works in this collection.",
    )
    collection_url = serializers.SerializerMethodField(
        help_text="Absolute URL of the collection landing page.",
    )
    feeds = serializers.SerializerMethodField(
        help_text="GeoRSS and GeoAtom feed URLs for this collection.",
    )
    downloads = serializers.SerializerMethodField(
        help_text="Download URLs (GeoJSON, GeoPackage, CSV) for this collection.",
    )

    class Meta:
        model = Collection
        fields = [
            "id",
            "identifier",
            "short_slug",
            "name",
            "description",
            "homepage_url",
            "logo_url",
            "is_published",
            "created_at",
            "updated_at",
            "works_count",
            "collection_url",
            "feeds",
            "downloads",
        ]

    def _abs(self, url_name, slug):
        request = self.context.get("request")
        path = reverse(url_name, kwargs={"collection_slug": slug})
        return request.build_absolute_uri(path) if request else path

    @extend_schema_field(serializers.URLField())
    def get_collection_url(self, obj):
        return self._abs("optimap:collection-page", obj.identifier)

    @extend_schema_field(
        {
            "type": "object",
            "properties": {
                "rss": {"type": "string", "format": "uri"},
                "atom": {"type": "string", "format": "uri"},
            },
        }
    )
    def get_feeds(self, obj):
        return {
            "rss": self._abs("optimap:api-collection-georss", obj.identifier),
            "atom": self._abs("optimap:api-collection-atom", obj.identifier),
        }

    @extend_schema_field(
        {
            "type": "object",
            "properties": {
                "geojson": {"type": "string", "format": "uri"},
                "gpkg": {"type": "string", "format": "uri"},
                "csv": {"type": "string", "format": "uri"},
            },
        }
    )
    def get_downloads(self, obj):
        return {
            "geojson": self._abs("optimap:download-collection-geojson", obj.identifier),
            "gpkg": self._abs("optimap:download-collection-gpkg", obj.identifier),
            "csv": self._abs("optimap:download-collection-csv", obj.identifier),
        }
