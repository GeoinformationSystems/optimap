from django.contrib.gis.db import models
from django.contrib.postgres.fields import ArrayField
from django_currentuser.db.models import CurrentUserField
from django.contrib.auth import get_user_model
from django.utils.timezone import now

User = get_user_model()

def get_default_user():
    """
    Fetch the first user in the database to use as a default for existing subscriptions.
    If no users exist, return None to prevent errors.
    """
    return User.objects.first() if User.objects.exists() else None

STATUS_CHOICES = (
    ("d", "Draft"),
    ("p", "Published"),
    ("t", "Testing"),
    ("w", "Withdrawn"),
    ("h", "Harvested"),
)

class Publication(models.Model):
    # Required fields      
    doi = models.CharField(max_length=1024, unique=True)
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default="d")
    created_by = CurrentUserField(
        verbose_name="Created by",
        related_name="%(app_label)s_%(class)s_creator",
    )

    # Automatic fields
    creationDate = models.DateTimeField(auto_now_add=True)
    lastUpdate = models.DateTimeField(auto_now=True)
    updated_by = CurrentUserField(
        verbose_name="Updated by",
        related_name="%(app_label)s_%(class)s_updater",
        on_update=True,
    )
    
    # Optional fields
    source = models.CharField(max_length=4096, null=True, blank=True)  # Journal, conference, preprint repo, etc.
    provenance = models.TextField(null=True, blank=True)
    publicationDate = models.DateField(null=True, blank=True)
    title = models.TextField(null=True, blank=True)
    abstract = models.TextField(null=True, blank=True)
    url = models.URLField(max_length=1024, null=True, blank=True)
    
    # Geometry fields (Geospatial data for publications)
    geometry = models.GeometryCollectionField(verbose_name="Publication geometry/ies", srid=4326, null=True, blank=True)
    
    # Time period fields
    timeperiod_startdate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    timeperiod_enddate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)

    def get_absolute_url(self):
        return f"/api/v1/publications/{self.id}.json"

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        """Return string representation."""
        return self.doi


class Source(models.Model):
    # Automatic fields
    creationDate = models.DateTimeField(auto_now_add=True)
    lastUpdate = models.DateTimeField(auto_now=True)
    created_by = CurrentUserField(
        verbose_name="Created by",
        related_name="%(app_label)s_%(class)s_creator",
    )
    updated_by = CurrentUserField(
        verbose_name="Updated by",
        related_name="%(app_label)s_%(class)s_updater",
        on_update=True,
    )

    url_field = models.URLField(max_length=999)
    harvest_interval_minutes = models.IntegerField(default=60 * 24 * 3)
    last_harvest = models.DateTimeField(auto_now_add=True, null=True)


class Subscription(models.Model):
    """
    Model for managing user subscriptions.
    - Users can subscribe to search terms and areas.
    - Stores geospatial search areas.
    - Allows tracking of subscription time periods.
    """
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="subscriptions", default=get_default_user)
    name = models.CharField(max_length=255)  # Subscription name
    search_term = models.CharField(max_length=4096, null=True, blank=True)  # Keywords for filtering
    timeperiod_startdate = models.DateField(null=True, blank=True)  # Start date for filtering
    timeperiod_enddate = models.DateField(null=True, blank=True)  # End date for filtering
    search_area = models.GeometryCollectionField(null=True, blank=True)  # User-defined search area (Polygon)
    created_at = models.DateTimeField(auto_now_add=True)  
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        """Return string representation."""
        return f"{self.name} (User: {self.user.username if self.user else 'No User'})"

    class Meta:
        ordering = ["user"]
        verbose_name = "Subscription"
        verbose_name_plural = "Subscriptions"


# Import-Export Relations
from import_export import fields, resources
from import_export.widgets import ForeignKeyWidget
from django.conf import settings

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
        fields = ("created_by", "updated_by",)
