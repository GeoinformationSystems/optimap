# SPDX-FileCopyrightText: 2022 OPTIMETA and KOMET projects <https://projects.tib.eu/komet>
# SPDX-License-Identifier: GPL-3.0-or-later

import logging
from datetime import timedelta

from django.contrib.auth.models import AbstractUser, Group, Permission
from django.contrib.gis.db import models
from django.contrib.gis.db.models.functions import Centroid, Envelope
from django.contrib.gis.geos import Point
from django.contrib.postgres.fields import ArrayField
from django.db import connection
from django.db.models import Q
from django.conf import settings
from django.utils import timezone
from django_currentuser.db.models import CurrentUserField
from django_q.models import Schedule
from django.utils.timezone import now
from django.contrib.auth.models import AbstractUser, Group, Permission
from import_export import fields, resources
from django.urls import reverse
from import_export.widgets import ForeignKeyWidget
from django.core.exceptions import ValidationError
from stdnum.issn import is_valid as is_valid_issn
from django.contrib.gis.db import models as gis_models

logger = logging.getLogger(__name__)

STATUS_CHOICES = (
    ("d", "Draft"),
    ("p", "Published"),
    ("t", "Testing"),
    ("w", "Withdrawn"),
    ("h", "Harvested"),
    ("c", "Contributed"),
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

class Work(models.Model):
    title = models.TextField()
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default="d")
    type = models.CharField(
        max_length=50,
        choices=WORK_TYPE_CHOICES,
        default="article",
        db_index=True,
        help_text="Work type following Crossref/OpenAlex vocabulary. Set from source or OpenAlex metadata."
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
    source = models.ForeignKey('Source', on_delete=models.SET_NULL, null=True, related_name='works')
    collections = models.ManyToManyField(
        'Collection', blank=True, related_name='works',
        help_text='Curated collections this work belongs to (e.g. mountain-wetlands, agile-gi). A work can belong to multiple collections.',
    )
    provenance = models.JSONField(
        default=dict, blank=True,
        help_text='Structured provenance: harvest details, per-field metadata sources, OpenAlex match, contribution/publish events.'
    )
    publicationDate = models.DateField(null=True, blank=True)
    abstract = models.TextField(null=True, blank=True)
    # Journal-citation fields. Strings (not ints) because publishers use roman
    # numerals, electronic locators like "e12345", and ranges with non-digit
    # separators. Currently populated only via the OpenAlex matcher; other
    # harvesters leave them blank.
    volume = models.CharField(max_length=64, null=True, blank=True)
    issue = models.CharField(max_length=64, null=True, blank=True)
    first_page = models.CharField(max_length=64, null=True, blank=True)
    last_page = models.CharField(max_length=64, null=True, blank=True)
    url = models.URLField(max_length=1024, null=True, blank=True, unique=True)
    geometry = models.GeometryCollectionField(
        verbose_name='Work geometry/ies', srid=4326, null=True, blank=True
    )
    timeperiod_startdate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    timeperiod_enddate   = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    job = models.ForeignKey(
        'HarvestingEvent', on_delete=models.CASCADE, related_name='works', null=True, blank=True
    )

    # Metadata fields (can come from original source or OpenAlex)
    authors = ArrayField(models.CharField(max_length=255), blank=True, null=True, help_text="Author names (from original source or OpenAlex)")
    keywords = ArrayField(models.CharField(max_length=255), blank=True, null=True, help_text="Keywords/subjects (from original source or OpenAlex)")
    topics = ArrayField(models.CharField(max_length=255), blank=True, null=True, help_text="Research topics (typically from OpenAlex)")
    # EO4GEO Body of Knowledge concept codes (e.g. 'CV', 'AM10-3'). Resolved
    # to human-readable name + URI at render time via the cached BoK snapshot
    # (see works/bok/). Populated via user contribution on the work landing page.
    bok_concepts = ArrayField(models.CharField(max_length=32), blank=True, null=True,
        help_text="EO4GEO BoK concept codes contributed by users (resolved against the active BoK snapshot at render time).")

    # Reverse-geocoded placename + region for the geometry centroid (issue
    # #222). Populated by ``works.signals.update_work_placename`` on geometry
    # change and by the ``backfill_placenames`` management command. Used by
    # ``works.seo.geo_meta_tags`` to emit ``geo.placename`` / ``geo.region``
    # HTML meta tags and by the JSON-LD ``Place`` payload.
    placename = models.CharField(max_length=255, null=True, blank=True,
        help_text="Reverse-geocoded placename for the geometry centroid (Nominatim).")
    country_code = models.CharField(max_length=8, null=True, blank=True,
        help_text="ISO 3166-1 alpha-2 country code (or 3166-2 subdivision) for the geometry centroid.")

    # OpenAlex-specific fields (only from OpenAlex)
    openalex_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)
    openalex_match_info = models.JSONField(blank=True, null=True, help_text="Information about partial matches found")
    openalex_fulltext_origin = models.CharField(max_length=255, blank=True, null=True)
    openalex_is_retracted = models.BooleanField(default=False)
    openalex_ids = models.JSONField(blank=True, null=True, help_text="OpenAlex IDs object (doi, pmid, etc)")
    openalex_open_access_status = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=['doi', 'url'], name='unique_work_entry'),
        ]
        # Note: GeometryCollectionField auto-creates a GIST index on `geometry`
        # via GeoDjango's default `spatial_index=True`, so no explicit index here.
        indexes = [
            models.Index(fields=['status'], name='work_status_idx'),
            models.Index(fields=['-creationDate', '-id'], name='work_creationdate_id_idx'),
            models.Index(fields=['publicationDate'], name='work_publicationdate_idx'),
            models.Index(
                fields=['-creationDate', '-id'],
                name='work_published_recent_idx',
                condition=Q(status='p'),
            ),
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
            result = Work.objects.filter(pk=self.pk).annotate(
                bbox_center=Centroid(Envelope('geometry'))
            ).values_list('bbox_center', flat=True).first()

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
                cursor.execute("""
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
                """, [self.pk])

                row = cursor.fetchone()

                if row:
                    north_lon, north_lat, south_lon, south_lat, east_lon, east_lat, west_lon, west_lat = row

                    return {
                        'north': (north_lon, north_lat) if north_lon is not None else None,
                        'south': (south_lon, south_lat) if south_lon is not None else None,
                        'east': (east_lon, east_lat) if east_lon is not None else None,
                        'west': (west_lon, west_lat) if west_lon is not None else None,
                    }

            return None

        except Exception:
            # If there's any error calculating extremes, return None
            return None

class Subscription(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="subscriptions", null=True, blank=True)
    name = models.CharField(max_length=4096, default="default_subscription")
    search_term = models.CharField(max_length=4096, null=True, blank=True)
    timeperiod_startdate = models.DateField(null=True, blank=True)
    timeperiod_enddate = models.DateField(null=True, blank=True)
    region = models.GeometryCollectionField(null=True, blank=True)  # Deprecated, kept for backward compatibility
    regions = models.ManyToManyField('GlobalRegion', related_name='subscriptions', blank=True, help_text="Predefined geographic regions (continents and oceans)")
    subscribed = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
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
    def log_email(cls, recipient, subject, content, sent_by=None, trigger_source="manual", status="success", error_message=None):
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
    source = models.ForeignKey('Source', on_delete=models.CASCADE, related_name='harvesting_events')
    user = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16,
        choices=[
            ('pending', 'Pending'),
            ('in_progress', 'In Progress'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
        ],
        default='pending'
    )
    error_message = models.TextField(blank=True, default="")
    log_text = models.TextField(blank=True, default="")
    records_added = models.IntegerField(null=True, blank=True)
    records_updated = models.IntegerField(null=True, blank=True)
    records_with_spatial = models.IntegerField(null=True, blank=True)
    records_with_temporal = models.IntegerField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['source', '-started_at']),
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
        max_length=64, unique=True, null=True, blank=True,
        help_text="Display name for the public contributor recognition board. Only shown when opt-in is enabled.",
    )

    def __str__(self):
        return f"{self.user.username} - Notifications: {self.notify_new_manuscripts}"

class BlockedEmail(models.Model):
    email = models.EmailField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    blocked_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="blocked_emails")

    def __str__(self):
        return self.email

class BlockedDomain(models.Model):
    domain = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    blocked_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="blocked_domains")

    def __str__(self):
        return self.domain

class GlobalRegion(models.Model):
    CONTINENT = 'C'
    OCEAN     = 'O'
    TYPE_CHOICES = [
        (CONTINENT, 'Continent'),
        (OCEAN,     'Ocean'),
    ]

    name        = models.CharField(max_length=100, unique=True)
    region_type = models.CharField(max_length=1, choices=TYPE_CHOICES)
    source_url  = models.URLField()
    license     = models.CharField(max_length=200)
    geom        = models.MultiPolygonField(srid=4326)
    last_loaded = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.get_region_type_display()})"

    def get_slug(self):
        """Generate URL slug from region name."""
        return self.name.lower().replace(' ', '-')

    def get_absolute_url(self):
        """Get the landing page URL for this region."""
        slug = self.get_slug()
        if self.region_type == self.CONTINENT:
            return reverse('optimap:feed-continent-page', kwargs={'continent_slug': slug})
        else:  # OCEAN
            return reverse('optimap:feed-ocean-page', kwargs={'ocean_slug': slug})

class Collection(models.Model):
    """
    A curated grouping of Works.

    Concrete examples: a journal (`scientific-data`), a thematic dataset
    (`mountain-wetlands`), or a community-curated series (`agile-gi`).

    A Work can belong to multiple Collections (`Work.collections`, M2M).
    A Source can be associated with a default Collection so that newly
    harvested works are automatically tagged.
    """

    identifier = models.SlugField(
        max_length=100, unique=True,
        help_text='URL-safe identifier (e.g. "mountain-wetlands"). Used in /collections/<identifier>/.',
    )
    short_slug = models.SlugField(
        max_length=100, unique=True, null=True, blank=True,
        help_text='Optional vanity URL slug. If set, /<short_slug>/ 301-redirects to /collections/<identifier>/.',
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')
    homepage_url = models.URLField(max_length=512, blank=True, null=True)
    is_published = models.BooleanField(
        default=False,
        help_text='Only published collections are visible to anonymous users and listed in sitemaps.',
    )
    curators = models.ManyToManyField(
        settings.AUTH_USER_MODEL, blank=True, related_name='curated_collections',
        help_text='Users who can add/remove works to/from this collection from the work landing page.',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('optimap:collection-page', kwargs={'collection_slug': self.identifier})


class Source(models.Model):
    SOURCE_TYPE_CHOICES = [
        ('oai-pmh',           'OAI-PMH (generic)'),
        ('ojs',               'OJS (Open Journal Systems)'),
        ('janeway',           'Janeway'),
        ('rss',               'RSS / Atom feed'),
        ('crossref-prefix',   'Crossref (DOI prefix)'),
        ('mountain-wetlands', 'Mountain Wetlands Repository'),
        ('openalex',          'OpenAlex source'),
    ]

    # Map source_type → Django-Q task path. Types not listed here cannot be auto-scheduled.
    SOURCE_TYPE_TASKS = {
        'oai-pmh':           'works.tasks.harvest_oai_endpoint',
        'ojs':               'works.tasks.harvest_oai_endpoint',
        'janeway':           'works.tasks.harvest_oai_endpoint',
        'rss':               'works.tasks.harvest_rss_endpoint',
        'crossref-prefix':   'works.tasks.harvest_crossref_prefix',
        'mountain-wetlands': 'works.tasks.harvest_mountain_wetlands',
        'openalex':          'works.tasks.harvest_openalex_source',
    }

    url_field                = models.URLField(
        max_length=999,
        help_text=(
            'Source endpoint URL. What goes here depends on source_type — see '
            'docs/manage.md → "Source field cheatsheet". OAI-PMH: full '
            'ListRecords URL incl. metadataPrefix. RSS: feed URL. '
            'Crossref-prefix: display-only (the prefix is hard-coded to '
            '10.5194 today, change requires code edit). Mountain-wetlands: '
            'API base URL. OpenAlex: any placeholder URL works as long as '
            'openalex_id is set to the S<id>; otherwise put a URL containing '
            'the S<id> here.'
        ),
    )
    source_type              = models.CharField(
        max_length=32, choices=SOURCE_TYPE_CHOICES, default='oai-pmh', db_index=True,
        help_text='Platform / API style of this source. Selects which harvester runs.',
    )
    harvest_interval_minutes = models.IntegerField(
        default=0,
        help_text='Auto-harvest interval in minutes. 0 means manual-only (run via management command or admin action).',
    )
    last_harvest             = models.DateTimeField(auto_now_add=True, null=True)
    collection               = models.ForeignKey(
        'Collection', on_delete=models.SET_NULL, null=True, blank=True, related_name='sources',
        help_text=(
            'Default collection added to every work harvested from this source. '
            'Optional — leaving this blank is not an error: harvesting still '
            'succeeds, the works are simply not added to any collection (curators '
            'can add them later from each work landing page).'
        ),
    )
    tags                     = models.CharField(
        max_length=1024, blank=True, null=True,
        help_text='Free-form comma-separated tags for admin filtering. Display only.',
    )
    is_preprint              = models.BooleanField(
        default=False,
        help_text='Display flag — marks works from this source as preprints. Does not affect harvesting.',
    )
    name                     = models.CharField(
        max_length=255,
        help_text='Display name shown in the admin source list and on /pages, /sitemap.',
    )
    issn_l                   = models.CharField(
        max_length=9, blank=True, null=True,
        help_text='Linking ISSN (display only).',
    )
    openalex_id              = models.CharField(
        max_length=50, blank=True, null=True,
        help_text=(
            'OpenAlex Source identifier, e.g. "S4210203054" (or the full URL '
            '"https://openalex.org/S4210203054"). REQUIRED when '
            'source_type=openalex — the harvester scans this field first, '
            'then url_field, for an S<digits> token. The display URL '
            '(`openalex_url` in the public Source API) is derived from this.'
        ),
    )
    publisher_name           = models.CharField(
        max_length=255, blank=True, null=True,
        help_text='Display only — shown on the admin source detail page.',
    )
    works_count              = models.IntegerField(
        blank=True, null=True,
        help_text='Auto-populated statistic (display only).',
    )
    homepage_url             = models.URLField(
        max_length=512, blank=True, null=True,
        help_text='Public homepage of the journal/repository. Display only.',
    )
    abbreviated_title        = models.CharField(
        max_length=255, blank=True, null=True,
        help_text='Abbreviated journal title (display only).',
    )

    is_oa                    = models.BooleanField(
        default=False,
        help_text='Display flag — marks the source as Open Access in admin lists. Does not affect harvesting.',
    )
    cited_by_count           = models.IntegerField(
        blank=True, null=True,
        help_text='Auto-populated statistic (display only).',
    )

    # Default work type for harvested works from this source
    default_work_type        = models.CharField(
        max_length=50,
        choices=WORK_TYPE_CHOICES,
        default="article",
        help_text="Default work type assigned to works harvested from this source (can be overridden by OpenAlex metadata)"
    )

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name

    @property
    def works_api_url(self) -> str | None:
        if not self.openalex_id:
            return None
        source_id = self.openalex_id.rstrip('/').split('/')[-1]
        return f"https://api.openalex.org/works?filter=primary_location.source.id:{source_id}"

    @property
    def openalex_url(self) -> str | None:
        """Public OpenAlex page URL derived from ``openalex_id``."""
        if not self.openalex_id:
            return None
        s_id = self.openalex_id.rstrip('/').split('/')[-1]
        if not s_id:
            return None
        return f"https://openalex.org/{s_id}"

    def save(self, *args, **kwargs):
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
        )

Journal = Source


class WikidataExportLog(models.Model):
    """
    Log of Wikidata exports for works.
    Tracks when works were exported, what action was taken,
    and links to the created/updated Wikidata items.
    """
    ACTION_CHOICES = [
        ('created', 'Created'),
        ('updated', 'Updated'),
        ('skipped', 'Skipped'),
        ('error', 'Error'),
    ]

    work = models.ForeignKey(
        'Work',
        on_delete=models.CASCADE,
        related_name='wikidata_exports'
    )
    export_date = models.DateTimeField(auto_now_add=True, db_index=True)
    action = models.CharField(max_length=20, choices=ACTION_CHOICES, db_index=True)
    wikidata_qid = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text='Wikidata Q-ID (e.g., Q12345)'
    )
    wikidata_url = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text='Full URL to Wikidata item'
    )
    exported_fields = models.JSONField(
        blank=True,
        null=True,
        help_text='List of fields that were exported'
    )
    error_message = models.TextField(blank=True, null=True)
    export_summary = models.TextField(
        blank=True,
        null=True,
        help_text='Summary of what was exported'
    )
    wikibase_endpoint = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text='Wikibase API endpoint used for this export (e.g., https://www.wikidata.org/w/api.php)'
    )

    class Meta:
        ordering = ['-export_date']
        verbose_name = 'Wikidata Export Log'
        verbose_name_plural = 'Wikidata Export Logs'
        indexes = [
            models.Index(fields=['wikidata_qid'], name='works_wikidata_qid_idx'),
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
    KIND_CHOICES = [
        (SPATIAL, "Spatial metadata"),
        (TEMPORAL, "Temporal metadata"),
        (ONTOLOGY, "Ontology contributions"),
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

class ZenodoDepositionLog(models.Model):
    """
    Log of Zenodo depositions.
    Tracks when data was deposited to Zenodo, success/failure status,
    file uploads, metadata updates, and any errors encountered.
    """
    STATUS_CHOICES = [
        ('success', 'Success'),
        ('partial', 'Partial Success'),
        ('failed', 'Failed'),
    ]

    deposition_date = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, db_index=True)

    # Zenodo-specific identifiers
    deposition_id = models.CharField(
        max_length=50,
        db_index=True,
        help_text='Zenodo deposition ID'
    )
    doi = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text='DOI assigned by Zenodo (if published)'
    )
    zenodo_url = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text='URL to Zenodo record'
    )

    # API endpoint used
    api_base = models.URLField(
        max_length=512,
        help_text='Zenodo API base URL (sandbox or production)'
    )

    # What was deposited
    version = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text='Zenodo deposit version label (e.g. "v1", "v2"); next-version counter for this api_base.'
    )
    files_uploaded = models.JSONField(
        blank=True,
        null=True,
        help_text='List of files uploaded (names and sizes)'
    )
    metadata_merged = models.JSONField(
        blank=True,
        null=True,
        help_text='Metadata fields that were updated'
    )

    # Statistics
    works_count = models.IntegerField(
        default=0,
        help_text='Number of works included in this deposition'
    )
    total_size_bytes = models.BigIntegerField(
        default=0,
        help_text='Total size of uploaded files in bytes'
    )
    upload_duration_seconds = models.FloatField(
        blank=True,
        null=True,
        help_text='Time taken to upload all files'
    )

    # Error tracking
    error_message = models.TextField(
        blank=True,
        null=True,
        help_text='Error message if deposition failed'
    )
    error_details = models.JSONField(
        blank=True,
        null=True,
        help_text='Detailed error information (stack trace, API response, etc.)'
    )

    # Summary and notes
    deposition_summary = models.TextField(
        blank=True,
        null=True,
        help_text='Human-readable summary of the deposition'
    )
    notes = models.TextField(
        blank=True,
        null=True,
        help_text='Additional notes or comments'
    )

    class Meta:
        ordering = ['-deposition_date']
        verbose_name = 'Zenodo Deposition Log'
        verbose_name_plural = 'Zenodo Deposition Logs'
        indexes = [
            models.Index(fields=['deposition_id'], name='works_zenodo_dep_id_idx'),
            models.Index(fields=['doi'], name='works_zenodo_doi_idx'),
        ]

    def __str__(self):
        return f"{self.status.capitalize()} deposition {self.deposition_id} on {self.deposition_date.strftime('%Y-%m-%d %H:%M')}"
