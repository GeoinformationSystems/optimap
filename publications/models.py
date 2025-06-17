import logging

from django.contrib.auth.models import AbstractUser, Group, Permission
from django.contrib.gis.db import models
from django.contrib.postgres.fields import ArrayField
from django.conf import settings
from django_currentuser.db.models import CurrentUserField
from django_q.models import Schedule
from django.utils.timezone import now
from django.contrib.auth.models import AbstractUser, Group, Permission
from import_export import fields, resources
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
)

class CustomUser(AbstractUser):
    groups = models.ManyToManyField(Group, related_name="publications_users", blank=True)
    user_permissions = models.ManyToManyField(Permission, related_name="publications_users_permissions", blank=True)

class Publication(models.Model):
    title = models.TextField()
    status = models.CharField(max_length=1, choices=STATUS_CHOICES, default="d")
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
    source = models.ForeignKey('Source', on_delete=models.SET_NULL, null=True, related_name='publications')
    provenance = models.TextField(null=True, blank=True)
    publicationDate = models.DateField(null=True, blank=True)
    abstract = models.TextField(null=True, blank=True)
    url = models.URLField(max_length=1024, null=True, blank=True, unique=True)
    geometry = models.GeometryCollectionField(
        verbose_name='Publication geometry/ies', srid=4326, null=True, blank=True
    )
    timeperiod_startdate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    timeperiod_enddate = ArrayField(models.CharField(max_length=1024, null=True), null=True, blank=True)
    job = models.ForeignKey(
        'HarvestingEvent', on_delete=models.CASCADE, related_name='publications', null=True, blank=True
    )

    class Meta:
        ordering = ['-id']
        constraints = [
            models.UniqueConstraint(fields=['doi', 'url'], name='unique_publication_entry')
        ]

    def __str__(self):
        return self.title

class Subscription(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="subscriptions", null=True, blank=True)
    name = models.CharField(max_length=4096, default="default_subscription")
    search_term = models.CharField(max_length=4096, null=True)
    timeperiod_startdate = models.DateField(null=True)
    timeperiod_enddate = models.DateField(null=True)
    region = models.GeometryCollectionField(null=True, blank=True)
    subscribed = models.BooleanField(default=True)

    class Meta:
        ordering = ['name']
        verbose_name = "subscription"

    def __str__(self):
        return self.name

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

    def __str__(self):
        return f"Harvesting Event ({self.status}) for {self.source.url_field} at {self.started_at}"

class UserProfile(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    notify_new_manuscripts = models.BooleanField(default=False)

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

class Source(models.Model):
    url_field                = models.URLField(max_length=999)
    harvest_interval_minutes = models.IntegerField(default=60*24*3)
    last_harvest             = models.DateTimeField(auto_now_add=True, null=True)
    collection_name          = models.CharField(max_length=255, blank=True, null=True)
    tags                     = models.CharField(max_length=1024, blank=True, null=True)
    is_preprint              = models.BooleanField(default=False)
    name                     = models.CharField(max_length=255)
    issn_l                   = models.CharField(max_length=9, blank=True, null=True)
    openalex_id              = models.CharField(max_length=50, blank=True, null=True)
    openalex_url             = models.URLField(max_length=512, blank=True, null=True)
    publisher_name           = models.CharField(max_length=255, blank=True, null=True)
    works_count              = models.IntegerField(blank=True, null=True)
    homepage_url             = models.URLField(max_length=512, blank=True, null=True)
    abbreviated_title        = models.CharField(max_length=255, blank=True, null=True)

    is_oa                    = models.BooleanField(default=False)
    cited_by_count           = models.IntegerField(blank=True, null=True)

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

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        Schedule.objects.filter(name=f"Harvest Source {self.id}").delete()
        Schedule.objects.create(
            func='publications.tasks.harvest_oai_endpoint',
            args=str(self.id),
            schedule_type=Schedule.MINUTES,
            minutes=self.harvest_interval_minutes,
            name=f"Harvest Source {self.id}",
        )
