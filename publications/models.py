from django.contrib.gis.db import models
from django.contrib.postgres.fields import ArrayField
from django_currentuser.db.models import CurrentUserField
from django.utils.timezone import now
from django.contrib.auth.models import AbstractUser, Group, Permission
import uuid

STATUS_CHOICES = (
    ("d", "Draft"),
    ("p", "Published"),
    ("t", "Testing"),
    ("w", "Withdrawn"),
    ("h", "Harvested"),
)

class CustomUser(AbstractUser):
    deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    groups = models.ManyToManyField(Group, related_name="publications_users", blank=True)
    user_permissions = models.ManyToManyField(Permission, related_name="publications_users_permissions", blank=True)

    def soft_delete(self):
        """Marks the user as deleted instead of removing from the database."""
        self.deleted = True
        self.deleted_at = now()
        self.save()
    
    def restore(self):
        """Restores a previously deleted user."""
        self.deleted = False
        self.deleted_at = None
        self.save()

class Publication(models.Model):
    # required fields      
    doi = models.CharField(max_length=1024, unique=True)
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default="d")
    created_by = CurrentUserField( # see useful hint at https://github.com/zsoldosp/django-currentuser/issues/69
        verbose_name=("Created by"),
        related_name="%(app_label)s_%(class)s_creator",
    )

    # automatic fields
    creationDate = models.DateTimeField(auto_now_add=True)
    lastUpdate = models.DateTimeField(auto_now=True)
    updated_by = CurrentUserField(
        verbose_name=("Updated by"),
        related_name="%(app_label)s_%(class)s_updater",
        on_update=True,
    )
    
    # optional fields
    source = models.CharField(max_length=4096, null=True, blank=True) # journal, conference, preprint repo, ..
    provenance = models.TextField(null=True, blank=True)
    publicationDate = models.DateField(null=True,blank=True)
    title = models.TextField(null=True, blank=True)
    abstract = models.TextField(null=True, blank=True)
    url = models.URLField(max_length=1024, null=True, blank=True)
    geometry = models.GeometryCollectionField(verbose_name='Publication geometry/ies', srid = 4326, null=True, blank=True)# https://docs.openalex.org/api-entities/sources
    timeperiod_startdate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    timeperiod_enddate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)

    def get_absolute_url(self):
        return "/api/v1/publications/%i.json" % self.id
        # http://localhost:8000/api/v1/publications/5.json

    class Meta:
        ordering = ['-id']

    def __str__(self):
        """Return string representation."""
        return self.doi

class Source(models.Model):
    # automatic fields
    creationDate = models.DateTimeField(auto_now_add=True)
    lastUpdate = models.DateTimeField(auto_now=True)
    created_by = CurrentUserField(
        verbose_name=("Created by"),
        related_name="%(app_label)s_%(class)s_creator",
    )
    updated_by = CurrentUserField(
        verbose_name=("Updated by"),
        related_name="%(app_label)s_%(class)s_updater",
        on_update=True,
    )

    url_field = models.URLField(max_length = 999)
    harvest_interval_minutes = models.IntegerField(default=60*24*3)
    last_harvest = models.DateTimeField(auto_now_add=True,null=True)
    
class Subscription(models.Model):
    name = models.CharField(max_length=4096)
    search_term = models.CharField(max_length=4096,null=True)
    timeperiod_startdate = models.DateField(null=True)
    timeperiod_enddate = models.DateField(null=True)
    search_area = models.GeometryCollectionField(null=True, blank=True)
    user_name = models.CharField(max_length=4096)

    def __str__(self):
        """Return string representation."""
        return self.name

    class Meta:
        ordering = ['user_name']
        verbose_name = "subscription"

# handle import/export relations, see https://django-import-export.readthedocs.io/en/stable/advanced_usage.html#creating-non-existent-relations
from import_export import fields, resources
from import_export.widgets import ForeignKeyWidget
from django.conf import settings
from django.contrib.auth import get_user_model
User = get_user_model()
class PublicationResource(resources.ModelResource):
    #created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='username')
    #updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='username')
    created_by = fields.Field(
        column_name='created_by',
        attribute='created_by',
        widget=ForeignKeyWidget(User, field='username'))
    updated_by = fields.Field(
        column_name='updated_by',
        attribute='updated_by',
        widget=ForeignKeyWidget(settings.AUTH_USER_MODEL, field='username'))
    
    class Meta:
        model = Publication
        fields = ('created_by','updated_by',)
