from django.contrib import admin, messages
from leaflet.admin import LeafletGeoAdmin
from publications.models import Publication
from import_export.admin import ImportExportModelAdmin
from django_q.tasks import schedule
from django.utils.timezone import now
from publications.models import SentEmailLog
from publications.tasks import send_monthly_email, schedule_monthly_email_task
from django_q.models import Schedule
from datetime import datetime, timedelta


@admin.action(description="Mark selected publications as published")
def make_public(modeladmin, request, queryset):
    queryset.update(status="p")

@admin.action(description="Mark selected publications as draft (unpublished)")
def make_draft(modeladmin, request, queryset):
    queryset.update(status="d")

@admin.action(description="Send Monthly Manuscript Email")
def trigger_monthly_email(modeladmin, request, queryset):
    """
    Admin action to trigger the email task manually.
    """
    try:
        send_monthly_email(sent_by=request.user) 
        messages.success(request, "Monthly manuscript email has been sent successfully.")
    except Exception as e:
        messages.error(request, f"Failed to send email: {e}")

@admin.action(description="Schedule Monthly Email Task")
def trigger_monthly_email_task(modeladmin, request, queryset):
    """
    Admin action to manually schedule the email task.
    """
    try:
        if not Schedule.objects.filter(func='publications.tasks.send_monthly_email').exists():
            next_run_date = datetime.now().replace(day=1) + timedelta(days=30) 
            
            schedule(
                'publications.tasks.send_monthly_email',  
                schedule_type='M',  
                repeats=-1,
                next_run=next_run_date  
            )
            messages.success(request, "Monthly email task has been scheduled successfully.")
        else:
            messages.warning(request, "The monthly email task is already scheduled.")
    except Exception as e:
        messages.error(request, f"Failed to schedule task: {e}")

@admin.register(Publication)
class PublicationAdmin(LeafletGeoAdmin, ImportExportModelAdmin):
    """Publication Admin."""

    list_display = ("doi", "creationDate", "lastUpdate", "created_by", "updated_by", "status", "provenance")

    actions = [make_public,make_draft]

class SentEmailLogAdmin(admin.ModelAdmin):
    list_display = ("recipient_email", "subject", "sent_at")
    actions = [trigger_monthly_email,trigger_monthly_email_task]  


admin.site.register(SentEmailLog, SentEmailLogAdmin)
