from django.contrib.gis.db import models
from django.contrib.postgres.fields import ArrayField
from django_currentuser.db.models import CurrentUserField
from import_export import fields, resources
from import_export.widgets import ForeignKeyWidget
from django.conf import settings
from django.contrib.auth.models import User

# Choices for publication status
STATUS_CHOICES = (
    ("d", "Draft"),
    ("p", "Published"),
    ("t", "Testing"),
    ("w", "Withdrawn"),
    ("h", "Harvested"),
)


class Publication(models.Model):
    # Core fields
    doi = models.CharField(max_length=1024, unique=True, help_text="Digital Object Identifier (DOI)")
    title = models.TextField(null=True, blank=True, help_text="Title of the publication")
    status = models.CharField(
        max_length=1, choices=STATUS_CHOICES, default="d", help_text="Publication status"
    )

    # Metadata fields
    created_by = CurrentUserField(
        verbose_name=("Created by"),
        related_name="%(app_label)s_%(class)s_creator",
    )
    updated_by = CurrentUserField(
        verbose_name=("Updated by"),
        related_name="%(app_label)s_%(class)s_updater",
        on_update=True,
    )
    creationDate = models.DateTimeField(auto_now_add=True, help_text="Record creation timestamp")
    lastUpdate = models.DateTimeField(auto_now=True, help_text="Last updated timestamp")
    publicationDate = models.DateField(null=True, blank=True, help_text="Date of publication")

    # Journal/Source fields
    journal_name = models.CharField(
        max_length=255, null=True, blank=True, help_text="Name of the journal or source"
    )
    journal_identifier = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Unique identifier (ISSN or OpenAlex ID) for the journal",
    )
    journal_publisher = models.CharField(
        max_length=255, null=True, blank=True, help_text="Publisher of the journal"
    )
    journal_openalex_url = models.URLField(
        max_length=1024,
        null=True,
        blank=True,
        help_text="OpenAlex URL for the journal or source",
    )

    # Additional metadata
    abstract = models.TextField(null=True, blank=True, help_text="Abstract of the publication")
    url = models.URLField(
        max_length=1024, null=True, blank=True, help_text="URL to the publication"
    )
    source = models.CharField(
        max_length=4096,
        null=True,
        blank=True,
        help_text="Source (e.g., journal, conference, preprint repository)",
    )
    provenance = models.TextField(
        null=True, blank=True, help_text="Provenance or source of this record"
    )

    # Spatial and temporal fields
    geometry = models.GeometryCollectionField(
        verbose_name="Publication geometry/ies",
        srid=4326,
        null=True,
        blank=True,
        help_text="Spatial data associated with the publication",
    )
    timeperiod_startdate = ArrayField(
        models.CharField(max_length=1024, null=True),
        null=True,
        blank=True,
        help_text="Start date(s) for the publication's time period",
    )
    timeperiod_enddate = ArrayField(
        models.CharField(max_length=1024, null=True),
        null=True,
        blank=True,
        help_text="End date(s) for the publication's time period",
    )

    def get_absolute_url(self):
        """Return API URL for the publication."""
        return f"/api/v1/publications/{self.id}.json"

    class Meta:
        ordering = ["-id"]
        verbose_name = "publication"
        verbose_name_plural = "publications"

    def __str__(self):
        """String representation of the publication."""
        return self.title or self.doi or f"Publication {self.id}"


class Source(models.Model):
    # Metadata fields
    creationDate = models.DateTimeField(auto_now_add=True, help_text="Record creation timestamp")
    lastUpdate = models.DateTimeField(auto_now=True, help_text="Last updated timestamp")
    created_by = CurrentUserField(
        verbose_name=("Created by"),
        related_name="%(app_label)s_%(class)s_creator",
    )
    updated_by = CurrentUserField(
        verbose_name=("Updated by"),
        related_name="%(app_label)s_%(class)s_updater",
        on_update=True,
    )

    # Source-specific fields
    url_field = models.URLField(max_length=999, help_text="URL of the source")
    harvest_interval_minutes = models.IntegerField(
        default=60 * 24 * 3, help_text="Harvest interval in minutes"
    )
    last_harvest = models.DateTimeField(
        auto_now_add=True, null=True, help_text="Last time the source was harvested"
    )


class Subscription(models.Model):
    name = models.CharField(max_length=4096, help_text="Name of the subscription")
    search_term = models.CharField(max_length=4096, null=True, help_text="Search term")
    timeperiod_startdate = models.DateField(
        null=True, help_text="Start date for the subscription's time period"
    )
    timeperiod_enddate = models.DateField(
        null=True, help_text="End date for the subscription's time period"
    )
    search_area = models.GeometryCollectionField(
        null=True, blank=True, help_text="Geographical area for the subscription"
    )
    user_name = models.CharField(max_length=4096, help_text="User name associated with the subscription")

    def __str__(self):
        """String representation of the subscription."""
        return self.name

    class Meta:
        ordering = ["user_name"]
        verbose_name = "subscription"
        verbose_name_plural = "subscriptions"


class PublicationResource(resources.ModelResource):
    created_by = fields.Field(
        column_name="created_by",
        attribute="created_by",
        widget=ForeignKeyWidget(User, field="username"),
    )
    updated_by = fields.Field(
        column_name="updated_by",
        attribute="updated_by",
        widget=ForeignKeyWidget(settings.AUTH_USER_MODEL, field="username"),
    )

    class Meta:
        model = Publication
        fields = ("created_by", "updated_by")

