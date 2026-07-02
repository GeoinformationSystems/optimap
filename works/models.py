# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser, Group, Permission
from django.contrib.gis.db import models
from django.contrib.gis.db.models.functions import Centroid, Envelope
from django.contrib.gis.geos import Point
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import connection
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.timezone import now
from django_currentuser.db.models import CurrentUserField
from django_q.models import Schedule

logger = logging.getLogger(__name__)

# ISO code of the reserved sentinel Country used to mark a work as "will not be
# matched to a country" (manual curation, issue #261). It carries an empty
# geometry so the point-in-polygon join never auto-assigns it, and it is hidden
# from every public country/iso enumeration. See Country.real().
SENTINEL_COUNTRY_ISO = "ZZ"

STATUS_CHOICES = (
    ("d", "Draft"),
    ("p", "Published"),
    ("t", "Testing"),
    ("w", "Withdrawn"),
    ("h", "Harvested"),
    ("c", "Contributed"),
    # Redirected: a duplicate that was merged into a canonical work (same
    # OpenAlex id). The row is kept only so its identifiers still resolve and
    # 302-redirect to the canonical work — see works/dedup.py. Excluded from
    # all listings, feeds, map, and the API list.
    ("r", "Redirected"),
)

EMAIL_STATUS_CHOICES = [
    ("success", "Success"),
    ("failed", "Failed"),
]

# Work types based on Crossref/OpenAlex controlled vocabulary
# Source: https://api.crossref.org/types and https://docs.openalex.org/api-entities/works/work-object#type
WORK_TYPE_CHOICES = [
    ("article", "Article"),  # OpenAlex primary type (journal articles, proceedings, posted content)
    ("book", "Book"),
    ("book-chapter", "Book Chapter"),
    ("book-part", "Book Part"),
    ("book-section", "Book Section"),
    ("book-series", "Book Series"),
    ("book-set", "Book Set"),
    ("book-track", "Book Track"),
    ("component", "Component"),
    ("database", "Database"),
    ("dataset", "Dataset"),
    ("dissertation", "Dissertation"),
    ("edited-book", "Edited Book"),
    ("editorial", "Editorial"),
    ("erratum", "Erratum"),
    ("grant", "Grant"),
    ("journal", "Journal"),
    ("journal-article", "Journal Article"),  # Crossref legacy type
    ("journal-issue", "Journal Issue"),
    ("journal-volume", "Journal Volume"),
    ("letter", "Letter"),
    ("libguides", "Library Guides"),
    ("monograph", "Monograph"),
    ("other", "Other"),
    ("paratext", "Paratext"),  # OpenAlex type (covers, TOCs, etc.)
    ("peer-review", "Peer Review"),
    ("posted-content", "Posted Content"),  # Crossref legacy type
    ("preprint", "Preprint"),  # OpenAlex primary type
    ("proceedings", "Proceedings"),
    ("proceedings-article", "Proceedings Article"),  # Crossref legacy type
    ("proceedings-series", "Proceedings Series"),
    ("reference-book", "Reference Book"),
    ("reference-entry", "Reference Entry"),
    ("report", "Report"),
    ("report-component", "Report Component"),
    ("report-series", "Report Series"),
    ("review", "Review"),
    ("standard", "Standard"),
    ("supplementary-materials", "Supplementary Materials"),
]


class CustomUser(AbstractUser):
    groups = models.ManyToManyField(Group, related_name="works_users", blank=True)
    user_permissions = models.ManyToManyField(Permission, related_name="works_users_permissions", blank=True)

    @classmethod
    def deleted_user(cls):
        """Return the sentinel account that holds contributions of deleted users."""
        return cls.objects.get(username="deleted")


class Work(models.Model):
    title = models.TextField()
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default="d")
    type = models.CharField(
        max_length=50,
        choices=WORK_TYPE_CHOICES,
        default="article",
        db_index=True,
        help_text="Work type following Crossref/OpenAlex vocabulary. Set from source or OpenAlex metadata.",
    )
    created_by = CurrentUserField(
        verbose_name=("Created by"),
        related_name="%(app_label)s_%(class)s_creator",
    )
    creationDate = models.DateTimeField(auto_now_add=True)
    lastUpdate = models.DateTimeField(auto_now=True)
    updated_by = CurrentUserField(
        verbose_name=("Updated by"),
        related_name="%(app_label)s_%(class)s_updater",
        on_update=True,
    )

    doi = models.CharField(max_length=1024, unique=True, blank=True, null=True)
    source = models.ForeignKey("Source", on_delete=models.SET_NULL, null=True, related_name="works")
    collections = models.ManyToManyField(
        "Collection",
        blank=True,
        related_name="works",
        help_text="Curated collections this work belongs to (e.g. mountain-wetlands, agile-gi). A work can belong to multiple collections.",
    )
    provenance = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured provenance: harvest details, per-field metadata sources, OpenAlex match, contribution/publish events.",
    )
    publicationDate = models.DateField(null=True, blank=True)
    abstract = models.TextField(null=True, blank=True)
    # Journal-citation fields. Strings (not ints) because publishers use roman
    # numerals, electronic locators like "e12345", and ranges with non-digit
    # separators. Populated by harvesters (Crossref, GeoScienceWorld) and by the
    # OpenAlex matcher / OpenAIRE enrichment; many harvesters leave them blank.
    volume = models.CharField(max_length=64, null=True, blank=True)
    issue = models.CharField(max_length=64, null=True, blank=True)
    first_page = models.CharField(max_length=64, null=True, blank=True)
    last_page = models.CharField(max_length=64, null=True, blank=True)
    language = models.CharField(
        max_length=8, null=True, blank=True, help_text="ISO 639-2 alpha-3 language code (e.g. 'eng')"
    )
    publisher = models.CharField(max_length=255, null=True, blank=True)
    url = models.URLField(max_length=1024, null=True, blank=True, unique=True)
    geometry = models.GeometryCollectionField(verbose_name="Work geometry/ies", srid=4326, null=True, blank=True)
    timeperiod_startdate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    timeperiod_enddate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    job = models.ForeignKey("HarvestingEvent", on_delete=models.CASCADE, related_name="works", null=True, blank=True)

    # Metadata fields (can come from original source or OpenAlex)
    authors = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        null=True,
        help_text="Author names (from original source or OpenAlex)",
    )
    keywords = ArrayField(
        models.CharField(max_length=255),
        blank=True,
        null=True,
        help_text="Keywords/subjects (from original source or OpenAlex)",
    )
    topics = ArrayField(
        models.CharField(max_length=255), blank=True, null=True, help_text="Research topics (typically from OpenAlex)"
    )
    # EO4GEO Body of Knowledge concept codes (e.g. 'CV', 'AM10-3'). Resolved
    # to human-readable name + URI at render time via the cached BoK snapshot
    # (see works/bok/). Populated via user contribution on the work landing page.
    bok_concepts = ArrayField(
        models.CharField(max_length=32),
        blank=True,
        null=True,
        help_text="EO4GEO BoK concept codes contributed by users (resolved against the active BoK snapshot at render time).",
    )

    # Reverse-geocoded placename + region for the geometry centroid (issue
    # #222). Populated by ``works.signals.update_work_placename`` on geometry
    # change and by the ``backfill_placenames`` management command. Used by
    # ``works.seo.geo_meta_tags`` to emit ``geo.placename`` / ``geo.region``
    # HTML meta tags and by the JSON-LD ``Place`` payload.
    placename = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Reverse-geocoded placename for the geometry centroid (Nominatim).",
    )
    countries = models.ManyToManyField(
        "Country",
        related_name="works",
        blank=True,
        help_text="Countries whose outline intersects the work's geometry "
        "(offline point-in-polygon join; multi-valued for transboundary studies).",
    )
    regions = models.ManyToManyField(
        "GlobalRegion",
        related_name="works",
        blank=True,
        help_text="Global regions (continents and oceans) whose outline intersects the "
        "work's geometry (offline point-in-polygon join; multi-valued).",
    )
    author_links = models.ManyToManyField(
        "Author",
        through="WorkAuthor",
        related_name="works",
        blank=True,
        help_text="Identity-bearing authors (with ORCID and/or OpenAlex id) of this work, "
        "ordered via WorkAuthor.rank. Display names remain in Work.authors.",
    )

    # OpenAlex-specific fields (only from OpenAlex)
    openalex_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    openalex_match_info = models.JSONField(blank=True, null=True, help_text="Information about partial matches found")
    openalex_fulltext_origin = models.CharField(max_length=255, blank=True, null=True)
    openalex_is_retracted = models.BooleanField(default=False)
    openalex_ids = models.JSONField(blank=True, null=True, help_text="OpenAlex IDs object (doi, pmid, etc)")
    openalex_open_access_status = models.CharField(max_length=50, blank=True, null=True)

    # All hosting copies of this work (journal version, preprint, repository
    # copies), normalised from OpenAlex `primary_location` + `locations[]` by
    # works/harvesting/openalex_locations.py::build_locations. Each entry is
    # credited (`credit: "openalex"`). When duplicate Works (same openalex_id)
    # are merged, the non-primary versions' identifiers and locations are folded
    # in here on the canonical work. See works/dedup.py.
    locations = models.JSONField(
        default=list,
        blank=True,
        help_text="Hosting copies/versions of this work (from OpenAlex). Each entry carries a `credit` field.",
    )

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(fields=["doi", "url"], name="unique_work_entry"),
        ]
        # Note: GeometryCollectionField auto-creates a GIST index on `geometry`
        # via GeoDjango's default `spatial_index=True`, so no explicit index here.
        indexes = [
            models.Index(fields=["status"], name="work_status_idx"),
            models.Index(fields=["-creationDate", "-id"], name="work_creationdate_id_idx"),
            models.Index(fields=["publicationDate"], name="work_publicationdate_idx"),
            models.Index(
                fields=["-creationDate", "-id"],
                name="work_published_recent_idx",
                condition=Q(status="p"),
            ),
            # JSONB containment lookups for identifier->canonical resolution:
            # `openalex_ids__contains` (pmid/pmcid/mag) and `locations__contains`
            # (location landing URL / version DOI). See works/utils/identifiers.py.
            GinIndex(fields=["openalex_ids"], name="work_openalex_ids_gin"),
            GinIndex(fields=["locations"], name="work_locations_gin"),
        ]

    def __str__(self):
        return self.title

    def get_identifier(self) -> str:
        """
        Return the most suitable identifier for this work.
        Prefers DOI if available, otherwise returns internal ID as string.

        This identifier can be used in URLs, API responses, and anywhere
        a unique work identifier is needed.

        Returns:
            str: DOI (if available) or internal ID (as string)
        """
        return self.doi if self.doi else str(self.id)

    def permalink(self) -> str | None:
        """
        Return the absolute OPTIMAP permalink (/work/<identifier>).
        Uses DOI if available, otherwise falls back to internal ID.
        """
        base = settings.BASE_URL.rstrip("/")
        rel = reverse("optimap:work-landing", args=[self.get_identifier()])
        return f"{base}{rel}"

    permalink.short_description = "Permalink"

    @property
    def is_redirected(self) -> bool:
        """True when this row is a merged-away duplicate (status='r')."""
        return self.status == "r"

    @property
    def country_codes(self) -> list[str]:
        """Sorted ISO 3166-1 alpha-2 codes from the ``countries`` M2M (#261).

        Iterates the related manager (not ``values_list``) so a
        ``prefetch_related("countries")`` cache is reused rather than triggering
        a fresh query per work. Empty for unsaved works or works with no
        country association.
        """
        return sorted(c.iso_code for c in self.display_countries)

    @property
    def display_countries(self) -> list["Country"]:
        """Linked countries excluding the sentinel, for public display (#261).

        Reuses any ``prefetch_related("countries")`` cache. A work marked "will
        not be matched" carries only the sentinel, so this is empty for it.
        """
        if not self.pk:
            return []
        return [c for c in self.countries.all() if c.iso_code != SENTINEL_COUNTRY_ISO]

    @property
    def country_match_excluded(self) -> bool:
        """True when a curator marked this work "will not be matched" (#261)."""
        if not self.pk:
            return False
        return any(c.iso_code == SENTINEL_COUNTRY_ISO for c in self.countries.all())

    @property
    def region_names(self) -> list[str]:
        """Sorted global-region names from the ``regions`` M2M.

        Iterates the related manager (not ``values_list``) so a
        ``prefetch_related("regions")`` cache is reused rather than triggering a
        fresh query per work. Empty for unsaved works or works with no region
        association.
        """
        if not self.pk:
            return []
        return sorted(r.name for r in self.regions.all())

    def canonical_work(self) -> "Work":
        """Return the canonical work this row resolves to.

        For a redirected duplicate (``status='r'``) this follows
        ``provenance.redirect.canonical_work_id`` to the surviving work; for any
        other work it returns ``self``. Falls back to ``self`` if the pointer is
        missing or dangling. See works/dedup.py for how the pointer is written.
        """
        if not self.is_redirected:
            return self
        provenance = self.provenance if isinstance(self.provenance, dict) else {}
        canonical_id = (provenance.get("redirect") or {}).get("canonical_work_id")
        if canonical_id and canonical_id != self.id:
            canonical = Work.objects.filter(id=canonical_id).first()
            if canonical is not None:
                return canonical
        return self

    @property
    def openaire_url(self) -> str | None:
        """Public OpenAIRE Explore URL for a matched record, from provenance.

        Mirrors how ``openalex_id`` exposes the OpenAlex page. Reads the URL
        recorded by OpenAIRE enrichment (``provenance.openaire_match.url``) and,
        for matches recorded before that URL was stored, derives it from the
        OpenAIRE id. Returns ``None`` when the work was not matched in OpenAIRE.
        """
        provenance = self.provenance if isinstance(self.provenance, dict) else {}
        match = provenance.get("openaire_match") or {}
        if match.get("status") != "matched":
            return None
        url = match.get("url")
        if url:
            return url
        openaire_id = match.get("openaire_id")
        if openaire_id:
            return f"https://explore.openaire.eu/search/result?id={openaire_id}"
        return None

    def get_center_coordinate(self):
        """
        Calculate and return the center coordinate of the work's geometry using PostGIS.

        For publications with geometry, this method:
        1. Uses PostGIS ST_Envelope to get the bounding box
        2. Uses PostGIS ST_Centroid to calculate the center of the bounding box
        3. Returns a tuple (longitude, latitude) or None if no geometry

        This uses database-level geometry operations for accuracy and performance.

        Returns:
            tuple: (longitude, latitude) as floats, or None if no geometry

        Examples:
            >>> work.geometry = Point(10, 20)
            >>> work.get_center_coordinate()
            (10.0, 20.0)

            >>> work.geometry = GeometryCollection([Point(0, 0), Point(10, 10)])
            >>> work.get_center_coordinate()
            (5.0, 5.0)
        """
        if not self.geometry:
            return None

        try:
            # Use database query to calculate centroid of bounding box
            # ST_Centroid(ST_Envelope(geometry)) gives us the center of the bounding box
            result = (
                Work.objects.filter(pk=self.pk)
                .annotate(bbox_center=Centroid(Envelope("geometry")))
                .values_list("bbox_center", flat=True)
                .first()
            )

            if result and isinstance(result, Point):
                # Return as (longitude, latitude)
                return (result.x, result.y)

            return None

        except Exception:
            # If there's any error calculating center, return None
            return None

    def get_extreme_points(self):
        """
        Calculate and return the extreme points (northernmost, southernmost, easternmost, westernmost)
        of the work's geometry using PostGIS.

        Uses ST_DumpPoints to extract all vertices and orders them by coordinates.

        Returns:
            dict: Dictionary with keys 'north', 'south', 'east', 'west', each containing
                  a tuple (longitude, latitude), or None if no geometry

        Examples:
            >>> work.geometry = Polygon([(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)])
            >>> extremes = work.get_extreme_points()
            >>> extremes['north']  # (5.0, 10.0) - any point at max latitude
            >>> extremes['south']  # (5.0, 0.0) - any point at min latitude
            >>> extremes['east']   # (10.0, 5.0) - any point at max longitude
            >>> extremes['west']   # (0.0, 5.0) - any point at min longitude
        """
        if not self.geometry:
            return None

        try:
            # Raw SQL query to get all extreme points
            # For each direction, we dump all points, order by coordinate, and take the first
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH points AS (
                        SELECT (ST_DumpPoints(geometry)).geom AS pt
                        FROM works_work
                        WHERE id = %s
                    )
                    SELECT
                        -- Northernmost point (highest Y/latitude)
                        (SELECT ST_X(pt) FROM points ORDER BY ST_Y(pt) DESC LIMIT 1) AS north_lon,
                        (SELECT ST_Y(pt) FROM points ORDER BY ST_Y(pt) DESC LIMIT 1) AS north_lat,
                        -- Southernmost point (lowest Y/latitude)
                        (SELECT ST_X(pt) FROM points ORDER BY ST_Y(pt) ASC LIMIT 1) AS south_lon,
                        (SELECT ST_Y(pt) FROM points ORDER BY ST_Y(pt) ASC LIMIT 1) AS south_lat,
                        -- Easternmost point (highest X/longitude)
                        (SELECT ST_X(pt) FROM points ORDER BY ST_X(pt) DESC LIMIT 1) AS east_lon,
                        (SELECT ST_Y(pt) FROM points ORDER BY ST_X(pt) DESC LIMIT 1) AS east_lat,
                        -- Westernmost point (lowest X/longitude)
                        (SELECT ST_X(pt) FROM points ORDER BY ST_X(pt) ASC LIMIT 1) AS west_lon,
                        (SELECT ST_Y(pt) FROM points ORDER BY ST_X(pt) ASC LIMIT 1) AS west_lat
                """,
                    [self.pk],
                )

                row = cursor.fetchone()

                if row:
                    north_lon, north_lat, south_lon, south_lat, east_lon, east_lat, west_lon, west_lat = row

                    return {
                        "north": (north_lon, north_lat) if north_lon is not None else None,
                        "south": (south_lon, south_lat) if south_lon is not None else None,
                        "east": (east_lon, east_lat) if east_lon is not None else None,
                        "west": (west_lon, west_lat) if west_lon is not None else None,
                    }

            return None

        except Exception:
            # If there's any error calculating extremes, return None
            return None

    def save(self, *args, **kwargs):
        # Repair a topologically invalid geometry (e.g. a self-intersecting
        # polygon from harvested HTML) before it is written, so it can't later
        # crash a GEOS predicate during save, a spatial query, or an export.
        # Covers harvesting too, since every harvester ultimately calls save().
        from works.utils.geometry import repair_geometry
        from works.utils.provenance import append_event

        update_fields = kwargs.get("update_fields")
        geometry_written = update_fields is None or "geometry" in update_fields
        if geometry_written and self.geometry is not None:
            repaired = repair_geometry(self.geometry)
            if repaired is not self.geometry:
                # The geometry was topologically invalid and has been repaired;
                # record it so the change of stored coordinates is auditable.
                self.geometry = repaired
                append_event(self, "geometry_repair", method="make_valid")
                if update_fields is not None:
                    kwargs["update_fields"] = set(update_fields) | {"provenance"}
        super().save(*args, **kwargs)


class WorkRelation(models.Model):
    """
    A directed, typed relationship from one Work to another resource.

    Generic across all work types (publication↔sample, sample↔dataset,
    work↔work) — nothing here is sample-specific. The target may be another
    ``Work`` already in OPTIMAP (``to_work``) or, when the related resource has
    not been harvested yet, a bare identifier (``to_identifier`` +
    ``to_identifier_type``). The ``resolve_incoming_work_relations`` signal
    (works/signals.py) upgrades an identifier-only relation to a ``to_work`` FK
    once the target is harvested. ``to_identifier`` is always kept for
    provenance, even after ``to_work`` is set.

    Populated from DataCite ``relatedIdentifiers`` during IGSN-sample harvesting
    (issue #187); relation types normalize a subset of the DataCite
    relationType vocabulary via ``normalize_relation_type``.
    """

    RELATION_TYPE_CHOICES = [
        ("references", "References"),
        ("is_referenced_by", "Is referenced by"),
        ("cites", "Cites"),
        ("is_cited_by", "Is cited by"),
        ("is_supplement_to", "Is supplement to"),
        ("is_supplemented_by", "Is supplemented by"),
        ("is_part_of", "Is part of"),
        ("has_part", "Has part"),
        ("is_derived_from", "Is derived from"),
        ("is_source_of", "Is source of"),
        ("related_to", "Related to"),  # generic fallback
    ]

    # Map DataCite relationType values (case-insensitive) to our canonical set.
    # Anything unmapped falls back to "related_to".
    _DATACITE_RELATION_MAP = {
        "references": "references",
        "isreferencedby": "is_referenced_by",
        "cites": "cites",
        "iscitedby": "is_cited_by",
        "issupplementto": "is_supplement_to",
        "issupplementedby": "is_supplemented_by",
        "ispartof": "is_part_of",
        "haspart": "has_part",
        "isderivedfrom": "is_derived_from",
        "issourceof": "is_source_of",
    }

    from_work = models.ForeignKey(Work, on_delete=models.CASCADE, related_name="relations_from")
    to_work = models.ForeignKey(
        Work,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="relations_to",
        help_text="The related Work, once it exists in OPTIMAP. Null while the relation is still 'dangling'.",
    )
    to_identifier = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        db_index=True,
        help_text="Identifier of the related resource (bare DOI, URL, IGSN, …). Kept for provenance even after to_work is set.",
    )
    to_identifier_type = models.CharField(
        max_length=32, blank=True, default="", help_text="Type of to_identifier: DOI, URL, IGSN, Handle, …"
    )
    relation_type = models.CharField(max_length=32, choices=RELATION_TYPE_CHOICES, default="related_to")
    source = models.CharField(
        max_length=32, default="datacite", help_text="Provenance of this relation (datacite, manual, …)."
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Two partial constraints instead of one 4-field constraint, because
            # a single constraint over the nullable to_work treats NULLs as
            # distinct on Postgres < 15 (and nulls_distinct=False is unsupported
            # there — prod runs PG17, local dev PG14). Splitting by whether the
            # relation is dangling avoids relying on NULL semantics entirely.
            # to_identifier stays in the resolved key so two dangling rows with
            # different identifiers never collide after they resolve to the same
            # to_work.
            models.UniqueConstraint(
                fields=["from_work", "to_identifier", "relation_type"],
                condition=Q(to_work__isnull=True),
                name="uniq_work_relation_dangling",
            ),
            models.UniqueConstraint(
                fields=["from_work", "to_work", "to_identifier", "relation_type"],
                condition=Q(to_work__isnull=False),
                name="uniq_work_relation_resolved",
            ),
        ]

    def __str__(self):
        target = self.to_work_id or self.to_identifier or "?"
        return f"{self.from_work_id} {self.relation_type} {target}"

    @classmethod
    def normalize_relation_type(cls, datacite_value):
        """Map a DataCite ``relationType`` string to our canonical value (fallback: related_to)."""
        if not datacite_value:
            return "related_to"
        return cls._DATACITE_RELATION_MAP.get(datacite_value.strip().lower(), "related_to")


class Subscription(models.Model):
    NOTIFICATION_INTERVAL_CHOICES = [
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
    ]

    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="subscriptions", null=True, blank=True)
    name = models.CharField(max_length=4096, default="default_subscription")
    search_term = models.CharField(max_length=4096, null=True, blank=True)
    timeperiod_startdate = models.DateField(null=True, blank=True)
    timeperiod_enddate = models.DateField(null=True, blank=True)
    region = models.GeometryCollectionField(null=True, blank=True)  # Deprecated, kept for backward compatibility
    regions = models.ManyToManyField(
        "GlobalRegion",
        related_name="subscriptions",
        blank=True,
        help_text="Predefined geographic regions (continents and oceans)",
    )
    sources = models.ManyToManyField(
        "Source",
        related_name="subscriptions",
        blank=True,
        help_text="Harvesting sources whose new published works trigger a notification",
    )
    collections = models.ManyToManyField(
        "Collection",
        related_name="subscriptions",
        blank=True,
        help_text="Published collections whose new works trigger a notification",
    )
    subscribed = models.BooleanField(default=True)
    notification_interval = models.CharField(
        max_length=10,
        choices=NOTIFICATION_INTERVAL_CHOICES,
        default="monthly",
    )
    last_notified = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "subscription"

    def __str__(self):
        return f"{self.user.username if self.user else 'Anonymous'} - {self.name}"


class EmailLog(models.Model):
    TRIGGER_CHOICES = [
        ("admin", "Admin Panel"),
        ("scheduled", "Scheduled Task"),
        ("manual", "Manually Triggered"),
    ]
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=255)
    sent_at = models.DateTimeField(auto_now_add=True)
    email_content = models.TextField(blank=True, null=True)
    sent_by = models.ForeignKey(CustomUser, null=True, blank=True, on_delete=models.SET_NULL)
    trigger_source = models.CharField(max_length=50, choices=TRIGGER_CHOICES, default="manual")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="success")
    error_message = models.TextField(null=True, blank=True)

    def __str__(self):
        sender = self.sent_by.email if self.sent_by else "System"
        return f"Email to {self.recipient_email} by {sender} ({self.get_trigger_source_display()})"

    @classmethod
    def log_email(
        cls, recipient, subject, content, sent_by=None, trigger_source="manual", status="success", error_message=None
    ):
        cls.objects.create(
            recipient_email=recipient,
            subject=subject,
            sent_at=now(),
            email_content=content,
            sent_by=sent_by,
            trigger_source=trigger_source,
            status=status,
            error_message=error_message,
        )


class HarvestingEvent(models.Model):
    source = models.ForeignKey("Source", on_delete=models.CASCADE, related_name="harvesting_events")
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=[
            ("pending", "Pending"),
            ("in_progress", "In Progress"),
            ("completed", "Completed"),
            ("failed", "Failed"),
        ],
        default="pending",
    )
    error_message = models.TextField(blank=True, default="")
    log_text = models.TextField(blank=True, default="")
    records_added = models.IntegerField(null=True, blank=True)
    records_updated = models.IntegerField(null=True, blank=True)
    records_skipped = models.IntegerField(null=True, blank=True)
    records_with_spatial = models.IntegerField(null=True, blank=True)
    records_with_temporal = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["source", "-started_at"]),
        ]

    def __str__(self):
        return f"Harvesting Event ({self.status}) for {self.source.url_field} at {self.started_at}"


class UserProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    notify_new_manuscripts = models.BooleanField(default=False)
    notify_work_events = models.BooleanField(
        default=True,
        help_text=(
            "Receive emails for work-state changes (contributions to review as "
            "an admin/curator, and 'your contribution was published' messages "
            "as a contributor). Opt-out — defaults to on so collaborators stay "
            "in the loop."
        ),
    )
    recognition_opt_in = models.BooleanField(default=False)
    recognition_username = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        help_text="Display name for the public contributor recognition board. Only shown when opt-in is enabled.",
    )
    consented_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp of the user's initial privacy-policy consent (set when they click 'I consent' on first login). Null for accounts created before this field was introduced.",
    )

    def __str__(self):
        return f"{self.user.username} - Notifications: {self.notify_new_manuscripts}"


class BlockedEmail(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    blocked_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="blocked_emails"
    )

    def __str__(self):
        return self.email


class BlockedDomain(models.Model):
    domain = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    blocked_by = models.ForeignKey(
        CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="blocked_domains"
    )

    def __str__(self):
        return self.domain


class BaseMapLayer(models.Model):
    """Admin-configurable background tile layers for all Leaflet maps.

    Each row corresponds to a provider key in the vendored leaflet-providers.js
    plugin (e.g. ``OpenStreetMap.Mapnik``, ``CartoDB.Voyager``).  Enabled layers
    are exposed to JavaScript via the ``#optimap-basemaps`` json_script; the
    ``createBaseMap`` factory reads them and builds an ``L.control.layers``
    switcher.

    Exactly one enabled row should have ``is_default=True`` (enforced by the
    admin).  ``enabled_layers()`` falls back to the first enabled row when none
    is marked as default.
    """

    provider_key = models.CharField(
        max_length=100,
        unique=True,
        help_text="leaflet-providers.js id, e.g. 'OpenStreetMap.Mapnik', 'CartoDB.Voyager'.",
    )
    label = models.CharField(
        max_length=100,
        help_text="Display name shown in the map layer switcher.",
    )
    enabled = models.BooleanField(
        default=False,
        help_text="Show this layer in the switcher for all visitors.",
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Layer shown on initial map load.  Exactly one enabled layer should be default.",
    )
    order = models.PositiveIntegerField(
        default=0,
        help_text="Position in the layer switcher (lower = earlier).",
    )
    options = models.JSONField(
        default=dict,
        blank=True,
        help_text="Extra options passed to L.tileLayer.provider(key, options), e.g. {'apiKey': '…'}.",
    )

    class Meta:
        ordering = ["order", "label"]

    def __str__(self):
        return f"{self.label} ({self.provider_key})"

    @classmethod
    def enabled_layers(cls):
        """Return enabled layers ordered by (order, label), guaranteeing one default.

        If no enabled layer has ``is_default=True``, the first enabled row is
        treated as the default in the serialized output.
        """
        layers = list(cls.objects.filter(enabled=True).order_by("order", "label"))
        if not layers:
            return []
        has_default = any(layer.is_default for layer in layers)
        result = [
            {
                "provider_key": layer.provider_key,
                "label": layer.label,
                "default": layer.is_default if has_default else (i == 0),
                "options": layer.options,
            }
            for i, layer in enumerate(layers)
        ]
        return result


class GlobalRegion(models.Model):
    CONTINENT = "C"
    OCEAN = "O"
    TYPE_CHOICES = [
        (CONTINENT, "Continent"),
        (OCEAN, "Ocean"),
    ]

    name = models.CharField(max_length=100, unique=True)
    region_type = models.CharField(max_length=1, choices=TYPE_CHOICES)
    source_url = models.URLField()
    license = models.CharField(max_length=200)
    geom = models.MultiPolygonField(srid=4326)
    last_loaded = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.get_region_type_display()})"

    def get_slug(self):
        """Generate URL slug from region name."""
        return self.name.lower().replace(" ", "-")

    def get_absolute_url(self):
        """Get the landing page URL for this region."""
        slug = self.get_slug()
        if self.region_type == self.CONTINENT:
            return reverse("optimap:feed-continent-page", kwargs={"continent_slug": slug})
        else:  # OCEAN
            return reverse("optimap:feed-ocean-page", kwargs={"ocean_slug": slug})


class CountryQuerySet(models.QuerySet):
    def real(self):
        """Exclude the reserved sentinel row (see ``SENTINEL_COUNTRY_ISO``)."""
        return self.exclude(iso_code=SENTINEL_COUNTRY_ISO)


class CountryManager(models.Manager.from_queryset(CountryQuerySet)):
    pass


class Country(models.Model):
    """A country with a simplified outline geometry, used for the /at/<country>
    permalink pages (issue #29) and a toggleable countries map layer.

    ``iso_code`` is ISO 3166-1 alpha-2. Works are associated with a country via
    the ``Work.countries`` M2M (``related_name="works"``), populated by an
    offline point-in-polygon join against ``geom`` (see
    ``works.services.countries.countries_for_geometry``). The geometry is a
    simplified Natural Earth outline, used for that join, the map layer, and the
    ``/at/<country>`` pages. Mirrors :class:`GlobalRegion`.
    """

    objects = CountryManager()

    name = models.CharField(max_length=100, unique=True)
    iso_code = models.CharField(max_length=2, unique=True, db_index=True)
    slug = models.SlugField(
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="URL slug for /at/<slug>/, derived from the name. Indexed so place_page can look it up without scanning every country.",
    )
    continent = models.CharField(
        max_length=50,
        blank=True,
        default="",
        db_index=True,
        help_text="Continent name (from Natural Earth), used to group the /countries/ overview.",
    )
    geom = models.MultiPolygonField(srid=4326)
    last_loaded = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Countries"

    def __str__(self):
        return f"{self.name} ({self.iso_code})"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_slug(self):
        """URL slug from the country name (mirrors GlobalRegion.get_slug)."""
        return self.slug or slugify(self.name)

    def get_absolute_url(self):
        return reverse("optimap:at-place", kwargs={"place_slug": self.get_slug()})


class Author(models.Model):
    """An identity-bearing author (has an ORCID and/or OpenAlex author id), used for
    the /by/<orcid> permalink pages (issue #29). Linked to works via the ordered
    Work.author_links M2M (related_name="works"). Display names continue to live in
    the denormalized Work.authors ArrayField. Mirrors :class:`Country`.

    Only authors with at least one identifier (orcid or openalex_id) get a row here;
    anonymous/unidentified names stay only in Work.authors.
    """

    orcid = models.CharField(
        max_length=19,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Bare ORCID iD, e.g. '0000-0002-1825-0097'. Null when only an OpenAlex id is known.",
    )
    openalex_id = models.CharField(
        max_length=255,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="OpenAlex author id, e.g. 'A5012345678'.",
    )
    name = models.CharField(max_length=255, help_text="Most recently seen display name.")
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.orcid or self.openalex_id})"

    def get_absolute_url(self):
        if self.orcid:
            return reverse("optimap:by-author", kwargs={"orcid": self.orcid})
        return reverse("optimap:by-index")


class WorkAuthor(models.Model):
    """Ordered through-table between Work and Author (preserves author rank)."""

    work = models.ForeignKey("Work", on_delete=models.CASCADE, related_name="work_authors")
    author = models.ForeignKey("Author", on_delete=models.CASCADE, related_name="work_authors")
    rank = models.PositiveIntegerField(default=0, help_text="0-based position in the author list.")

    class Meta:
        unique_together = ("work", "author")
        ordering = ["rank"]

    def __str__(self):
        return f"{self.author.name} on {self.work_id} (rank {self.rank})"


class Collection(models.Model):
    """
    A curated grouping of Works.

    Concrete examples: a source (`scientific-data`), a thematic dataset
    (`mountain-wetlands`), or a community-curated series (`agile-gi`).

    A Work can belong to multiple Collections (`Work.collections`, M2M).
    A Source can be associated with a default Collection so that newly
    harvested works are automatically tagged.
    """

    identifier = models.SlugField(
        max_length=100,
        unique=True,
        help_text='URL-safe identifier (e.g. "mountain-wetlands"). Used in /collections/<identifier>/.',
    )
    short_slug = models.SlugField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        help_text="Optional vanity URL slug. If set, /<short_slug>/ 301-redirects to /collections/<identifier>/.",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    homepage_url = models.URLField(max_length=512, blank=True, null=True)
    logo_url = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Optional external URL for a collection logo (no file upload). Displayed as a thumbnail on the collection landing page and index.",
    )
    is_published = models.BooleanField(
        default=False,
        help_text="Only published collections are visible to anonymous users and listed in sitemaps.",
    )
    curators = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="curated_collections",
        help_text="Users who can add/remove works to/from this collection from the work landing page.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("optimap:collection-page", kwargs={"collection_slug": self.identifier})


class Source(models.Model):
    SOURCE_TYPE_CHOICES = [
        ("oai-pmh", "OAI-PMH (generic)"),
        ("ojs", "OJS (Open Journal Systems)"),
        ("janeway", "Janeway"),
        ("rss", "RSS / Atom feed"),
        ("crossref-prefix", "Crossref (DOI prefix)"),
        ("mountain-wetlands", "Mountain Wetlands Repository"),
        ("openalex", "OpenAlex source"),
        ("geoscienceworld", "GeoScienceWorld"),
        ("datacite", "DataCite (IGSN samples)"),
        ("gfz-igsn", "GFZ IGSN (landing-page scrape)"),
    ]

    # Map source_type → Django-Q task path. Types not listed here cannot be auto-scheduled.
    SOURCE_TYPE_TASKS = {
        "oai-pmh": "works.tasks.harvest_oai_endpoint",
        "ojs": "works.tasks.harvest_oai_endpoint",
        "janeway": "works.tasks.harvest_oai_endpoint",
        "rss": "works.tasks.harvest_rss_endpoint",
        "crossref-prefix": "works.tasks.harvest_crossref_prefix",
        "mountain-wetlands": "works.tasks.harvest_mountain_wetlands",
        "openalex": "works.tasks.harvest_openalex_source",
        "geoscienceworld": "works.tasks.harvest_geoscienceworld",
        "datacite": "works.tasks.harvest_datacite",
        "gfz-igsn": "works.tasks.harvest_gfz_igsn",
    }

    url_field = models.URLField(
        max_length=999,
        help_text=(
            "Source endpoint URL. What goes here depends on source_type — see "
            'docs/manage.md → "Source field cheatsheet". OAI-PMH: full '
            "ListRecords URL incl. metadataPrefix. RSS: feed URL. "
            "Crossref-prefix / GeoScienceWorld: display-only URL (harvester "
            "reads doi_prefix for the actual Crossref filter). "
            "Mountain-wetlands: API base URL. OpenAlex: any placeholder URL "
            "works as long as openalex_id is set to the S<id>; otherwise put "
            "a URL containing the S<id> here."
        ),
    )
    source_type = models.CharField(
        max_length=32,
        choices=SOURCE_TYPE_CHOICES,
        default="oai-pmh",
        db_index=True,
        help_text="Platform / API style of this source. Selects which harvester runs.",
    )
    harvest_interval_minutes = models.IntegerField(
        default=0,
        help_text="Auto-harvest interval in minutes. 0 means manual-only (run via management command or admin action).",
    )
    last_harvest = models.DateTimeField(auto_now_add=True, null=True)
    collection = models.ForeignKey(
        "Collection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sources",
        help_text=(
            "Default collection added to every work harvested from this source. "
            "Optional — leaving this blank is not an error: harvesting still "
            "succeeds, the works are simply not added to any collection (curators "
            "can add them later from each work landing page)."
        ),
    )
    tags = models.CharField(
        max_length=1024,
        blank=True,
        null=True,
        help_text="Free-form comma-separated tags for admin filtering. Display only.",
    )
    is_preprint = models.BooleanField(
        default=False,
        help_text="Display flag — marks works from this source as preprints. Does not affect harvesting.",
    )
    name = models.CharField(
        max_length=255,
        help_text="Display name shown in the admin source list and on /pages, /sitemap.",
    )
    slug = models.SlugField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        help_text=(
            "URL-safe identifier for the public /in/<slug>/ landing page and "
            "per-source feeds. Auto-generated from name when left blank."
        ),
    )
    issn_l = models.CharField(
        max_length=9,
        blank=True,
        null=True,
        help_text=(
            "Linking ISSN. For crossref-prefix sources, also used as an issn: "
            "Crossref filter — the preferred alternative to source_titles when "
            "the journal title contains a comma (which Crossref treats as a "
            "filter-clause separator)."
        ),
    )
    openalex_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text=(
            'OpenAlex Source identifier, e.g. "S4210203054" (or the full URL '
            '"https://openalex.org/S4210203054"). REQUIRED when '
            "source_type=openalex — the harvester scans this field first, "
            "then url_field, for an S<digits> token. The display URL "
            "(`openalex_url` in the public Source API) is derived from this."
        ),
    )
    doi_prefix = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text=(
            'DOI prefix used for Crossref-based harvesters (e.g. "10.1190" '
            'for SEG journals, "10.5194" for Copernicus). Used by '
            "source_type=crossref-prefix and source_type=geoscienceworld. "
            "Overrides the hardcoded fallback in harvest_crossref_prefix."
        ),
    )
    doi_contains = models.CharField(
        max_length=50,
        blank=True,
        default="",
        help_text=(
            "Crossref-only: keep only works whose DOI contains this substring "
            "(case-insensitive). Use when a Crossref query returns more than one "
            "venue — e.g. ESS Open Archive shares DOI prefixes and the Wiley "
            "member id with Authorea; doi_contains='essoar' keeps only ESSOAr "
            "(.../essoar.*). Leave blank to keep all query matches."
        ),
    )
    crossref_filter = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text=(
            "Crossref-only: raw Crossref filter clauses used as the harvest query "
            "instead of prefix:<doi_prefix>. Comma-separated, e.g. "
            "'member:311,type:posted-content' for ESS Open Archive (whose two DOI "
            "eras — 10.1002/essoar.* and 10.22541/essoar.* — are only jointly "
            "captured by the Wiley member + posted-content slice, then narrowed "
            "with doi_contains). Leave blank to harvest by doi_prefix."
        ),
    )
    source_titles = models.JSONField(
        blank=True,
        null=True,
        help_text=(
            "Optional list of Crossref container-title filter values "
            '(e.g. ["Scientific Data"] or ["AGILE: GIScience Series"]). '
            "Required when doi_prefix covers multiple journals (e.g. 10.1038 = all Springer Nature) "
            "to restrict Crossref harvesting and stats to the target journal only. "
            "Leave blank for prefix-only sources (e.g. Copernicus 10.5194). "
            "Auto-populated from SOURCE_CONFIG; manual edits are preserved."
        ),
    )
    publisher_name = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Display only — shown on the admin source detail page.",
    )
    works_count = models.IntegerField(
        blank=True,
        null=True,
        help_text="Auto-populated statistic (display only).",
    )
    statistics = models.JSONField(
        blank=True,
        null=True,
        help_text=(
            "Auto-populated harvest statistics. Stored as JSON; holds: "
            "`openalex_works_count` / `openalex_fetched_at` (OpenAlex, when openalex_id is set), "
            "`oai_works_count` / `oai_fetched_at` (OAI-PMH completeListSize, OAI source types only), "
            "`crossref_works_count` / `crossref_fetched_at` (Crossref rows=0 count, crossref-prefix type only). "
            "All timestamps are ISO-8601. Populated automatically after each harvest."
        ),
    )
    homepage_url = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Public homepage of the source. Display only.",
    )
    abbreviated_title = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Abbreviated title (display only).",
    )

    is_oa = models.BooleanField(
        default=False,
        help_text="Display flag — marks the source as Open Access in admin lists. Does not affect harvesting.",
    )
    cited_by_count = models.IntegerField(
        blank=True,
        null=True,
        help_text="Auto-populated statistic (display only).",
    )

    # Default work type for harvested works from this source
    default_work_type = models.CharField(
        max_length=50,
        choices=WORK_TYPE_CHOICES,
        default="article",
        help_text="Default work type assigned to works harvested from this source (can be overridden by OpenAlex metadata)",
    )
    harvest_config = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            "Per-source harvester configuration (JSON). Currently used by the DataCite "
            "(IGSN) harvester for sampling filters — e.g. publisher, require_coordinates, "
            "only_with_related_doi, bbox, date_from/date_to, resource_type, query, "
            "max_records. Command-line flags override these for ad-hoc runs."
        ),
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self) -> str | None:
        """Public landing-page URL (``/in/<slug>/``). None until a slug exists."""
        if not self.slug:
            return None
        return reverse("optimap:in-source", kwargs={"source_slug": self.slug})

    def latest_coverage(self):
        """Most recent SourceCoverageSnapshot for this source, or None."""
        return self.coverage_snapshots.order_by("-computed_at").first()

    def _generate_unique_slug(self) -> str:
        """Slugify the name (truncated to the field length) and append a
        numeric suffix on collision, keeping the result within max_length."""
        from works.utils.slugs import unique_slug

        max_length = self._meta.get_field("slug").max_length
        qs = Source.objects.exclude(pk=self.pk)
        return unique_slug(
            self.name,
            max_length,
            exists=lambda candidate: qs.filter(slug=candidate).exists(),
            fallback=f"source-{self.pk or ''}".rstrip("-"),
        )

    @property
    def works_api_url(self) -> str | None:
        if not self.openalex_id:
            return None
        source_id = self.openalex_id.rstrip("/").split("/")[-1]
        return f"https://api.openalex.org/works?filter=primary_location.source.id:{source_id}"

    @property
    def openalex_url(self) -> str | None:
        """Public OpenAlex page URL derived from ``openalex_id``."""
        if not self.openalex_id:
            return None
        s_id = self.openalex_id.rstrip("/").split("/")[-1]
        if not s_id:
            return None
        return f"https://openalex.org/{s_id}"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_unique_slug()
        super().save(*args, **kwargs)
        schedule_name = f"Harvest Source {self.id}"
        existing = Schedule.objects.filter(name=schedule_name).first()
        task_func = self.SOURCE_TYPE_TASKS.get(self.source_type)
        # Manual-only: no task for this source_type, or interval set to 0/negative.
        if not task_func or self.harvest_interval_minutes <= 0:
            if existing:
                existing.delete()
            return
        # Existing schedule already matches what we want — leave it alone (preserves next_run).
        if existing and existing.minutes == self.harvest_interval_minutes and existing.func == task_func:
            return
        if existing:
            existing.delete()
        Schedule.objects.create(
            func=task_func,
            args=str(self.id),
            schedule_type=Schedule.MINUTES,
            minutes=self.harvest_interval_minutes,
            next_run=timezone.now() + timedelta(minutes=self.harvest_interval_minutes),
            name=schedule_name,
            intended_date_kwarg="scheduled_for",
        )


Journal = Source


class WikidataExportLog(models.Model):
    """
    Log of Wikidata exports for works.
    Tracks when works were exported, what action was taken,
    and links to the created/updated Wikidata items.
    """

    ACTION_CHOICES = [
        ("created", "Created"),
        ("updated", "Updated"),
        ("skipped", "Skipped"),
        ("error", "Error"),
    ]

    work = models.ForeignKey("Work", on_delete=models.CASCADE, related_name="wikidata_exports")
    export_date = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    wikidata_qid = models.CharField(max_length=50, blank=True, null=True, help_text="Wikidata Q-ID (e.g., Q12345)")
    wikidata_url = models.URLField(max_length=512, blank=True, null=True, help_text="Full URL to Wikidata item")
    exported_fields = models.JSONField(blank=True, null=True, help_text="List of fields that were exported")
    error_message = models.TextField(blank=True, null=True)
    export_summary = models.TextField(blank=True, null=True, help_text="Summary of what was exported")
    wikibase_endpoint = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Wikibase API endpoint used for this export (e.g., https://www.wikidata.org/w/api.php)",
    )

    class Meta:
        ordering = ["-export_date"]
        verbose_name = "Wikidata Export Log"
        verbose_name_plural = "Wikidata Export Logs"
        indexes = [
            models.Index(fields=["wikidata_qid"], name="works_wikidata_qid_idx"),
        ]

    def __str__(self):
        return f"{self.action.capitalize()} {self.work.title[:50]} on {self.export_date.strftime('%Y-%m-%d')}"


class Contribution(models.Model):
    """
    Per-event audit row for user contributions to a Work's spatial/temporal metadata.

    Always recorded when a user contributes via the contribution endpoint, regardless of
    Recognition Board opt-in. Drives the public /recognition-board/ page via aggregate queries.
    `user` is nullable so counts survive account deletion.
    """

    SPATIAL = "spatial"
    TEMPORAL = "temporal"
    # Generic "ontology" bucket covers any controlled-vocabulary tagging
    # (EO4GEO BoK today; e.g. GCMD, Wikidata QIDs in the future).
    ONTOLOGY = "ontology"
    # A new work brought into OPTIMAP by a user submitting its DOI on /contribute/.
    DOI = "doi"
    KIND_CHOICES = [
        (SPATIAL, "Spatial metadata"),
        (TEMPORAL, "Temporal metadata"),
        (ONTOLOGY, "Ontology contributions"),
        (DOI, "Submitted works (DOI)"),
    ]

    user = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contributions",
    )
    work = models.ForeignKey(
        Work,
        on_delete=models.CASCADE,
        related_name="contributions",
    )
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "kind"]),
        ]

    def __str__(self):
        who = self.user.username if self.user else "(deleted)"
        return f"{who} → {self.get_kind_display()} on {self.work_id}"


class StatisticsSnapshot(models.Model):
    """One nightly snapshot of aggregate publication statistics (for time-series tracking)."""

    computed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    next_update = models.DateTimeField(null=True, blank=True)

    total_works = models.IntegerField(default=0)
    published_works = models.IntegerField(default=0)
    harvested_works = models.IntegerField(default=0)
    contributed_works = models.IntegerField(default=0)
    # Cumulative count of works submitted by users via the contribute-by-DOI form.
    contributed_dois = models.IntegerField(default=0)
    with_geometry = models.IntegerField(default=0)
    with_temporal = models.IntegerField(default=0)
    with_complete_metadata = models.IntegerField(default=0)
    complete_percentage = models.FloatField(default=0)
    with_authors = models.IntegerField(default=0)
    with_doi = models.IntegerField(default=0)
    with_abstract = models.IntegerField(default=0)
    open_access = models.IntegerField(default=0)
    sources = models.IntegerField(default=0)
    collections = models.IntegerField(default=0)
    users = models.IntegerField(default=0)
    contributors = models.IntegerField(default=0)

    # Breakdowns: list of {"name": str, "count": int} sorted descending by count.
    # by_country uses ISO 3166-1 alpha-2 codes as the "name" key.
    by_continent = models.JSONField(default=list)
    by_ocean = models.JSONField(default=list)
    by_country = models.JSONField(default=list)
    by_publisher = models.JSONField(default=list)
    by_journal = models.JSONField(default=list)
    by_collection = models.JSONField(default=list)
    # by_topic: list of {"name": str, "count": int}; by_year: list of
    # {"year": int, "count": int} in ascending chronological order.
    by_topic = models.JSONField(default=list)
    by_year = models.JSONField(default=list)

    class Meta:
        ordering = ["-computed_at"]
        get_latest_by = "computed_at"

    def __str__(self):
        return f"StatisticsSnapshot {self.computed_at:%Y-%m-%d %H:%M}"


class SourceCoverageSnapshot(models.Model):
    """Weekly coverage snapshot: how many works from a Source are in OPTIMAP vs OpenAlex."""

    source = models.ForeignKey("Source", on_delete=models.CASCADE, related_name="coverage_snapshots")
    computed_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # null when the source has no OpenAlex ID (coverage is incalculable)
    openalex_total = models.IntegerField(null=True, blank=True, default=None)
    optimap_count = models.IntegerField(default=0)
    coverage_pct = models.FloatField(null=True, blank=True, default=None)
    # metadata quality rates (null when optimap_count == 0)
    spatial_rate = models.FloatField(null=True, blank=True, default=None)
    temporal_rate = models.FloatField(null=True, blank=True, default=None)
    open_access_ratio = models.FloatField(null=True, blank=True, default=None)
    contributors_count = models.IntegerField(default=0)
    # published works per year: [{"year": 2023, "count": 42}, ...]
    by_year = models.JSONField(default=list)

    class Meta:
        ordering = ["-computed_at"]
        get_latest_by = "computed_at"

    def __str__(self):
        pct = f"{self.coverage_pct:.1f}%" if self.coverage_pct is not None else "N/A"
        return f"Coverage {self.source_id} @ {self.computed_at:%Y-%m-%d}: {pct}"

    def as_summary(self):
        """Canonical dict of this snapshot's fields.

        Single source of truth shared by the public API
        (``SourceSerializer.get_latest_coverage``) and the source landing page
        (``works.views_sources._coverage_context``) so the two never diverge.
        Adding a field here surfaces it in both — keep the serializer's
        ``@extend_schema`` inline_serializer in sync.
        """
        return {
            "optimap_count": self.optimap_count,
            "openalex_total": self.openalex_total,
            "coverage_pct": self.coverage_pct,
            "spatial_rate": self.spatial_rate,
            "temporal_rate": self.temporal_rate,
            "open_access_ratio": self.open_access_ratio,
            "contributors_count": self.contributors_count,
            "by_year": self.by_year,
            "computed_at": self.computed_at,
        }


class ServiceToken(models.Model):
    """Stored credential for an external-service connector (e.g. OpenAIRE).

    Holds a manually-pasted **refresh token** — editable in the Django admin so
    staff can rotate it without SSH access to the server — plus a cached,
    short-lived **access token** exchanged from it. Generic by design: one row
    per service, keyed by ``service``. Per-service constants (lifetime, reminder
    lead-time, docs / token-page URLs) live in
    ``works.utils.service_tokens.get_service_token_specs``, so the renewal
    reminder works for any registered service without code changes here.
    """

    OPENAIRE = "openaire"
    OPENALEX = "openalex"
    SERVICE_CHOICES = [
        (OPENAIRE, "OpenAIRE Graph API"),
        (OPENALEX, "OpenAlex API"),
    ]

    # Refresh the cached access token slightly before it actually expires.
    ACCESS_TOKEN_SKEW_SECONDS = 60

    service = models.CharField(max_length=64, choices=SERVICE_CHOICES, unique=True)
    refresh_token = models.TextField(
        blank=True,
        default="",
        help_text="Long-lived refresh token pasted from the provider's token page. Rotating it resets the cached access token.",
    )
    refresh_token_set_at = models.DateTimeField(null=True, blank=True)
    # Short-lived access token cache, shared across the web and qcluster processes.
    access_token = models.TextField(blank=True, default="")
    access_token_expires_at = models.DateTimeField(null=True, blank=True)
    # Informational: when staff were last reminded to renew this token.
    last_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Service token"
        verbose_name_plural = "Service tokens"
        ordering = ["service"]

    def __str__(self):
        return f"{self.get_service_display()} token"

    @property
    def spec(self):
        from works.utils.service_tokens import get_spec

        return get_spec(self.service)

    def set_refresh_token(self, value):
        """Store a new refresh token and reset all derived state."""
        self.refresh_token = (value or "").strip()
        self.refresh_token_set_at = now() if self.refresh_token else None
        self.access_token = ""
        self.access_token_expires_at = None
        self.last_reminder_sent_at = None
        self.save()

    @property
    def refresh_token_expires_at(self):
        if not self.refresh_token_set_at:
            return None
        spec = self.spec
        if spec is None:
            return None  # Key with no registered spec is treated as non-expiring.
        return self.refresh_token_set_at + timedelta(days=spec.lifetime_days)

    def days_until_refresh_expiry(self):
        expires = self.refresh_token_expires_at
        if expires is None:
            return None
        return (expires - now()).days

    def access_token_valid(self):
        if not self.access_token or not self.access_token_expires_at:
            return False
        skew = timedelta(seconds=self.ACCESS_TOKEN_SKEW_SECONDS)
        return self.access_token_expires_at - skew > now()

    def store_access_token(self, token, ttl_seconds):
        """Cache a freshly exchanged access token with its lifetime."""
        self.access_token = token
        self.access_token_expires_at = now() + timedelta(seconds=int(ttl_seconds))
        self.save(update_fields=["access_token", "access_token_expires_at", "updated_at"])

    def due_for_reminder(self):
        """True when the refresh token expires within the renewal window.

        Purely a window check — the weekly task may therefore flag the same token
        on consecutive runs while it stays inside the window.
        """
        expires = self.refresh_token_expires_at
        if expires is None:
            return False
        spec = self.spec
        reminder_days = spec.reminder_days if spec else 9
        return now() >= expires - timedelta(days=reminder_days)
